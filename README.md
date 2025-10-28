# MCP Server Tester

The MCP Server Tester is a FastMCP-based harness that launches, proxies, and
manages other MCP servers over stdio. It exposes a control surface for starting,
stopping, and switching between subprocess-based MCP servers run via `uvx` or
`npx`, while dynamically surfacing the child server's tools under a
`tested_server_` prefix.

## Features

- **Launch & switch**: Start or swap subprocesses with a single tool call.
- **Dynamic proxying**: Mounts the child server's tools under the
  `tested_server_` prefix using `FastMCP.as_proxy`.
- **Structured status**: Captures command, environment, exit codes, stderr
  output, and logging notifications for debugging.
- **Graceful shutdown**: Coordinates shutdown via the MCP stdio transport and
  reports the child's termination state.

## Available Tools

Control tools are exposed with the `tester_control_` prefix:

- `tester_control_start_server(command: str, env: dict | None)` – Launch a new
  MCP server subprocess.
- `tester_control_switch_server(command: str, env: dict | None)` – Stop the
  current subprocess (if any) and start a new one in a single step.
- `tester_control_stop_server()` – Terminate the running subprocess and unmount
  its tools.
- `tester_control_get_status()` – Return structured metadata about the current
  or most recent subprocess, including log tails and exit codes.

When a child server is active, its tools are mounted dynamically with the
`tested_server_` prefix.

## Running Locally

Install dependencies and run the tester via `uv`/`pip` or Python directly:

```bash
pip install -e .
python -m mcp_server_tester
```

With `uv` you can run directly from the project directory:

```bash
uv run python -m mcp_server_tester
```

Once running, connect an MCP client (e.g. Codex CLI) and invoke the control
tools to manage subprocesses.

## License

This project is released under the terms of the MIT License. See `LICENSE` for
full details.
