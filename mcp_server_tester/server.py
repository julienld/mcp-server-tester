"""FastMCP server definition for the MCP server tester."""

from __future__ import annotations

from typing import Any, Dict, Optional

from fastmcp import FastMCP

from .child_manager import ChildServerManager


def create_server() -> FastMCP:
    """Create and configure the MCP server tester harness."""

    harness = FastMCP()
    manager = ChildServerManager(harness)

    @harness.tool(name="tester_control_start_server")
    async def tester_control_start_server(
        command: str, env: Optional[Dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Launch a new MCP server subprocess via stdio."""

        return await manager.start(command, env)

    @harness.tool(name="tester_control_switch_server")
    async def tester_control_switch_server(
        command: str, env: Optional[Dict[str, Any]] = None
    ) -> dict[str, Any]:
        """Switch to a different MCP server command, restarting the subprocess."""

        return await manager.switch(command, env)

    @harness.tool(name="tester_control_stop_server")
    async def tester_control_stop_server() -> dict[str, Any]:
        """Stop the currently running MCP server subprocess."""

        return await manager.stop()

    @harness.tool(name="tester_control_get_status")
    async def tester_control_get_status() -> dict[str, Any]:
        """Return the latest status of the child MCP server."""

        return await manager.status()

    return harness


__all__ = ["create_server"]
