"""Child server lifecycle management for the MCP server tester."""

from __future__ import annotations

import asyncio
import contextlib
import dataclasses
import io
import shlex
import threading
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

import mcp.types
from fastmcp import FastMCP
from fastmcp.server.proxy import ProxyClient
from fastmcp.utilities.logging import get_logger

from .managed_stdio import ManagedStdioTransport, ProcessMonitor

logger = get_logger(__name__)


class ProcessLogBuffer(io.TextIOBase):
    """A thread-safe text buffer that stores stderr output from the child process."""

    def __init__(self, max_lines: int = 2000) -> None:
        super().__init__()
        self._lines: deque[str] = deque(maxlen=max_lines)
        self._pending: str = ""
        self._lock = threading.Lock()

    def write(self, data: str) -> int:  # type: ignore[override]
        if not data:
            return 0
        if not isinstance(data, str):
            data = str(data)

        normalized = data.replace("\r\n", "\n").replace("\r", "\n")

        with self._lock:
            segments = (self._pending + normalized).split("\n")
            self._pending = segments.pop()
            for segment in segments:
                self._lines.append(segment)

        return len(data)

    def flush(self) -> None:  # type: ignore[override]
        return

    def tail(self, lines: int = 20) -> str:
        with self._lock:
            history = list(self._lines)
            if self._pending:
                history.append(self._pending)
        return "\n".join(history[-lines:])

    def dump(self) -> str:
        with self._lock:
            history = list(self._lines)
            if self._pending:
                history.append(self._pending)
        return "\n".join(history)

    def clear(self) -> None:
        with self._lock:
            self._lines.clear()
            self._pending = ""


@dataclass
class LogEntry:
    level: str
    message: str
    logger: str | None
    timestamp: str


@dataclass
class ChildServerState:
    command: str
    args: list[str]
    env: dict[str, str]
    transport: ManagedStdioTransport
    client: ProxyClient
    started_at: datetime
    tools_loaded: int = 0
    running: bool = False
    log_buffer: ProcessLogBuffer = field(default_factory=ProcessLogBuffer)
    log_entries: deque[LogEntry] = field(default_factory=lambda: deque(maxlen=200))
    last_error: str | None = None
    monitor: ProcessMonitor = field(default_factory=ProcessMonitor)
    monitor_task: asyncio.Task[None] | None = None
    stopped_at: datetime | None = None

    def as_status(self) -> dict[str, Any]:
        return {
            "running": self.running,
            "command": " ".join([self.command, *self.args]).strip(),
            "env": self.env if self.env else {},
            "tools_loaded": self.tools_loaded,
            "started_at": self.started_at.isoformat().replace("+00:00", "Z"),
            "stopped_at": self.stopped_at.isoformat().replace("+00:00", "Z")
            if self.stopped_at
            else None,
            "exit_code": self.monitor.exit_code,
            "last_error": self.last_error,
            "stderr_tail": self.log_buffer.tail(),
            "log_messages": [dataclasses.asdict(entry) for entry in self.log_entries],
        }


class ChildServerManager:
    """Manage lifecycle of a single child MCP server."""

    def __init__(self, harness: FastMCP) -> None:
        self._harness = harness
        self._lock = asyncio.Lock()
        self._state: ChildServerState | None = None

    async def start(self, command: str, env: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        async with self._lock:
            if self._state and self._state.running:
                return {
                    "action": "start",
                    "success": False,
                    "error": "A child server is already running. Stop or switch first.",
                    "status": self._state.as_status(),
                }

            return await self._start_locked(command, env or {})

    async def switch(self, command: str, env: Optional[Dict[str, Any]] = None) -> dict[str, Any]:
        async with self._lock:
            previous_status: dict[str, Any] | None = None
            if self._state and self._state.running:
                previous_status = await self._stop_locked(reason="switch")

            start_result = await self._start_locked(command, env or {})
            if previous_status is not None:
                start_result["previous"] = previous_status
            return start_result

    async def stop(self) -> dict[str, Any]:
        async with self._lock:
            if not self._state or not self._state.running:
                return {
                    "action": "stop",
                    "success": False,
                    "error": "No child server is currently running.",
                    "status": self._state.as_status() if self._state else None,
                }

            return await self._stop_locked(reason="stop")

    async def status(self) -> dict[str, Any]:
        async with self._lock:
            return {
                "running": self._state.running if self._state else False,
                "status": self._state.as_status() if self._state else None,
            }

    async def _start_locked(self, command: str, env: Dict[str, Any]) -> dict[str, Any]:
        tokens = shlex.split(command)
        if not tokens:
            return {
                "action": "start",
                "success": False,
                "error": "Command string is empty.",
            }

        base_command, *args = tokens
        env_map = {key: str(value) for key, value in env.items()}

        log_buffer = ProcessLogBuffer()
        monitor = ProcessMonitor()
        transport = ManagedStdioTransport(
            command=base_command,
            args=args,
            env=env_map or None,
            log_buffer=log_buffer,
            monitor=monitor,
        )

        log_handler = self._build_log_handler(log_buffer)
        client = ProxyClient(
            transport,
            log_handler=log_handler,
        )

        try:
            async with client:
                tools_result = await client.list_tools()
            tools_loaded = len(tools_result.tools)
        except Exception as exc:
            await transport.close()
            monitor.record_failure(str(exc))
            status = {
                "running": False,
                "exit_code": monitor.exit_code,
                "stderr": log_buffer.dump(),
                "error": str(exc),
            }
            return {
                "action": "start",
                "success": False,
                "error": str(exc),
                "status": status,
            }

        proxy = FastMCP.as_proxy(client)
        self._unmount()
        self._harness.mount(proxy, prefix="tested_server")

        state = ChildServerState(
            command=base_command,
            args=args,
            env=env_map,
            transport=transport,
            client=client,
            started_at=datetime.now(timezone.utc),
            tools_loaded=tools_loaded,
            running=True,
            log_buffer=log_buffer,
            monitor=monitor,
        )
        self._state = state

        state.monitor_task = asyncio.create_task(self._monitor_child(state))

        return {
            "action": "start",
            "success": True,
            "status": state.as_status(),
        }

    async def _stop_locked(self, reason: str) -> dict[str, Any]:
        assert self._state is not None
        state = self._state
        state.running = False
        state.stopped_at = datetime.now(timezone.utc)

        try:
            await state.client.close()
        except Exception as exc:
            state.last_error = str(exc)
            logger.exception("Error while closing child client")

        await state.monitor.wait()

        if state.monitor_task is not None:
            state.monitor_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await state.monitor_task
            state.monitor_task = None

        self._unmount()

        status = state.as_status()
        return {
            "action": reason,
            "success": True,
            "status": status,
        }

    async def _monitor_child(self, state: ChildServerState) -> None:
        await state.monitor.wait()
        async with self._lock:
            if self._state is not state:
                return
            if state.running:
                state.running = False
                state.stopped_at = datetime.now(timezone.utc)
            self._unmount()
            state.monitor_task = None

    def _unmount(self) -> None:
        """Remove the tested server proxy from the harness."""
        mounted = getattr(self._harness, "_mounted_servers", None)
        if not mounted:
            return
        self._harness._mounted_servers = [
            item for item in mounted if item.prefix != "tested_server"
        ]

    def _build_log_handler(
        self, log_buffer: ProcessLogBuffer
    ) -> Callable[[mcp.types.LoggingMessageNotificationParams], asyncio.Future[Any]]:
        loop = asyncio.get_running_loop()

        async def handler(params: mcp.types.LoggingMessageNotificationParams) -> None:
            timestamp = datetime.now(timezone.utc).isoformat().replace("+00:00", "Z")
            level = getattr(params, "level", "info")
            logger_name = getattr(params, "logger", None)
            data = getattr(params, "data", {}) or {}
            msg = data.get("msg") or getattr(params, "message", None) or ""
            line = f"[{timestamp}] {level.upper()}: {msg}"
            log_buffer.write(line + "\n")
            if self._state and self._state.log_buffer is log_buffer:
                self._state.log_entries.append(
                    LogEntry(
                        level=str(level),
                        message=str(msg),
                        logger=str(logger_name) if logger_name else None,
                        timestamp=timestamp,
                    )
                )

        def schedule_handler(
            params: mcp.types.LoggingMessageNotificationParams,
        ) -> asyncio.Future[Any]:
            return asyncio.run_coroutine_threadsafe(handler(params), loop)

        return schedule_handler

