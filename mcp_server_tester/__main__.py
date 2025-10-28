"""Entry point for running the MCP server tester via `python -m`."""

from __future__ import annotations

from .server import create_server


def main() -> None:
    """Run the FastMCP server tester."""

    server = create_server()
    server.run()


if __name__ == "__main__":
    main()
