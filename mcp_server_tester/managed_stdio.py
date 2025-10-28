"""Custom stdio transport with process monitoring for the MCP server tester."""

from __future__ import annotations

import asyncio
import contextlib
import sys
import time
from dataclasses import dataclass, field
from typing import Any, AsyncIterator, Tuple

import anyio
from anyio.streams.memory import (
    MemoryObjectReceiveStream,
    MemoryObjectSendStream,
)
from anyio.streams.text import TextReceiveStream
from fastmcp.client.transports import SessionKwargs, StdioTransport
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import (
    PROCESS_TERMINATION_TIMEOUT,
    _create_platform_compatible_process,
    _get_executable_command,
    _terminate_process_tree,
    get_default_environment,
)
import mcp.types as types
from mcp.shared.message import SessionMessage


@dataclass
class ProcessMonitor:
    """Track lifecycle information about the child process."""

    started_at: float | None = None
    exited_at: float | None = None
    exit_code: int | None = None
    error: str | None = None
    pid: int | None = None
    _event: asyncio.Event = field(default_factory=asyncio.Event)

    def mark_process(self, process: Any) -> None:
        self.pid = getattr(process, "pid", None)
        self.started_at = time.time()

    def set_exit(self, exit_code: int | None, error: str | None = None) -> None:
        if exit_code is not None:
            self.exit_code = exit_code
        if error is not None:
            self.error = error
        if self.exited_at is None:
            self.exited_at = time.time()
        if not self._event.is_set():
            self._event.set()

    def record_failure(self, error: str, exit_code: int | None = -1) -> None:
        self.error = error
        if self.exit_code is None:
            self.exit_code = exit_code
        if self.exited_at is None:
            self.exited_at = time.time()
        if not self._event.is_set():
            self._event.set()

    async def wait(self) -> None:
        await self._event.wait()


class ManagedStdioTransport(StdioTransport):
    """A stdio transport that records process metadata for status reporting."""

    def __init__(
        self,
        command: str,
        args: list[str],
        *,
        env: dict[str, str] | None = None,
        cwd: str | None = None,
        keep_alive: bool | None = None,
        log_buffer: Any | None = None,
        monitor: ProcessMonitor | None = None,
    ) -> None:
        self.monitor = monitor or ProcessMonitor()
        self._log_buffer = log_buffer
        super().__init__(
            command=command,
            args=args,
            env=env,
            cwd=cwd,
            keep_alive=keep_alive,
            log_file=log_buffer,
        )

    async def connect(self, **session_kwargs: SessionKwargs) -> ClientSession | None:  # type: ignore[override]
        if self._connect_task is not None:
            return

        session_future: asyncio.Future[ClientSession] = asyncio.Future()
        self._connect_task = asyncio.create_task(
            _managed_stdio_transport_connect_task(
                command=self.command,
                args=self.args,
                env=self.env,
                cwd=self.cwd,
                log_file=self._log_buffer,
                monitor=self.monitor,
                session_kwargs=session_kwargs,
                ready_event=self._ready_event,
                stop_event=self._stop_event,
                session_future=session_future,
            )
        )

        await self._ready_event.wait()

        if self._connect_task.done():
            exc = self._connect_task.exception()
            if exc is not None:
                raise exc

        self._session = await session_future
        return self._session


async def _managed_stdio_transport_connect_task(
    *,
    command: str,
    args: list[str],
    env: dict[str, str] | None,
    cwd: str | None,
    log_file: Any | None,
    monitor: ProcessMonitor,
    session_kwargs: SessionKwargs,
    ready_event: anyio.Event,
    stop_event: anyio.Event,
    session_future: asyncio.Future[ClientSession],
) -> None:
    try:
        async with contextlib.AsyncExitStack() as stack:
            server_params = StdioServerParameters(
                command=command,
                args=args,
                env=env,
                cwd=cwd,
            )

            errlog = log_file if log_file is not None else None
            client_context = managed_stdio_client(server_params, monitor=monitor, errlog=errlog)
            transport = await stack.enter_async_context(client_context)
            read_stream, write_stream = transport

            session = await stack.enter_async_context(
                ClientSession(read_stream, write_stream, **session_kwargs)
            )
            session_future.set_result(session)
            ready_event.set()

            await stop_event.wait()
    except Exception as exc:
        monitor.record_failure(str(exc))
        ready_event.set()
        raise
    finally:
        if not monitor._event.is_set():
            monitor.set_exit(monitor.exit_code, monitor.error)


@contextlib.asynccontextmanager
async def managed_stdio_client(
    server: StdioServerParameters,
    *,
    monitor: ProcessMonitor,
    errlog: Any | None,
) -> AsyncIterator[Tuple[
    MemoryObjectReceiveStream[SessionMessage | Exception],
    MemoryObjectSendStream[SessionMessage],
]]:
    read_stream_writer: MemoryObjectSendStream[SessionMessage | Exception]
    read_stream: MemoryObjectReceiveStream[SessionMessage | Exception]
    write_stream: MemoryObjectSendStream[SessionMessage]
    write_stream_reader: MemoryObjectReceiveStream[SessionMessage]

    read_stream_writer, read_stream = anyio.create_memory_object_stream(0)
    write_stream, write_stream_reader = anyio.create_memory_object_stream(0)

    try:
        command = _get_executable_command(server.command)
        base_env = get_default_environment()
        env = base_env if server.env is None else {**base_env, **server.env}
        process = await _create_platform_compatible_process(
            command=command,
            args=server.args,
            env=env,
            errlog=errlog if errlog is not None else sys.stderr,
            cwd=server.cwd,
        )
        monitor.mark_process(process)
    except Exception as exc:  # pragma: no cover - initialization failure
        await read_stream.aclose()
        await write_stream.aclose()
        await read_stream_writer.aclose()
        await write_stream_reader.aclose()
        monitor.record_failure(str(exc))
        raise

    async def stdout_reader() -> None:
        assert process.stdout is not None
        try:
            async with read_stream_writer:
                buffer = ""
                async for chunk in TextReceiveStream(
                    process.stdout,
                    encoding=server.encoding,
                    errors=server.encoding_error_handler,
                ):
                    lines = (buffer + chunk).split("\n")
                    buffer = lines.pop()
                    for line in lines:
                        try:
                            message = types.JSONRPCMessage.model_validate_json(line)
                        except Exception as exc:  # pragma: no cover - parse error
                            await read_stream_writer.send(exc)
                            continue
                        session_message = SessionMessage(message)
                        await read_stream_writer.send(session_message)
        except anyio.ClosedResourceError:  # pragma: no cover - reader closed
            await anyio.lowlevel.checkpoint()

    async def stdin_writer() -> None:
        assert process.stdin is not None
        try:
            async with write_stream_reader:
                async for session_message in write_stream_reader:
                    json_payload = session_message.message.model_dump_json(
                        by_alias=True, exclude_none=True
                    )
                    await process.stdin.send(
                        (json_payload + "\n").encode(
                            encoding=server.encoding,
                            errors=server.encoding_error_handler,
                        )
                    )
        except anyio.ClosedResourceError:  # pragma: no cover - writer closed
            await anyio.lowlevel.checkpoint()

    async with (anyio.create_task_group() as tg, process):
        tg.start_soon(stdout_reader)
        tg.start_soon(stdin_writer)
        try:
            yield read_stream, write_stream
        finally:
            if process.stdin is not None:
                with contextlib.suppress(Exception):
                    await process.stdin.aclose()

            try:
                with anyio.fail_after(PROCESS_TERMINATION_TIMEOUT):
                    await process.wait()
            except TimeoutError:  # pragma: no cover - shutdown timeout
                await _terminate_process_tree(process)
            except ProcessLookupError:
                pass
            finally:
                monitor.set_exit(getattr(process, "returncode", None))

            await read_stream.aclose()
            await write_stream.aclose()
            await read_stream_writer.aclose()
            await write_stream_reader.aclose()

