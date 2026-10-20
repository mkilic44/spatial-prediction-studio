# Local MCP server

The optional Model Context Protocol (MCP) server lets an AI client on your computer, such as Claude Desktop, Claude Code, Codex or OpenCode, work with the Spatial Prediction Studio panel in a running ArcMap. The client can read the project, start a fold preview or a run, follow its status and read the verified outputs. It cannot run code or shell commands, and it cannot read files outside the result of a completed run.

## Install

Install with the MCP server and register it in one or more clients:

```
Install.cmd -WithMcp -McpClient claude_desktop
```

Supported client names: `claude_desktop`, `claude_code`, `codex`, `chatgpt_desktop`, `opencode`, `antigravity`, `pycharm`. Repeat `-McpClient` for several clients. Restart the client afterwards. PyCharm cannot be registered automatically; import the snippet written to `client-configurations\pycharm.json` in the installation folder through **Settings > Tools > AI Assistant > Model Context Protocol > Add > STDIO**.

`-WithMcp` alone installs the server and writes a configuration snippet for every client to `%LOCALAPPDATA%\Programs\SpatialPredictionStudio\<version>\client-configurations`, without changing any client. Registration merges only the Spatial Prediction Studio entry into the client configuration and keeps all other entries; `Uninstall.cmd` removes only that entry.

The server runs in its own private Python runtime (`runtime-mcp`) with the hash-locked packages of `requirements-mcp-lock.txt`.

## Use

1. Start ArcMap and open the Spatial Prediction Studio panel. On **1 Project**, **Enable the local Model Context Protocol (MCP) connection** must be ticked (it is by default).
2. Ask your AI client to work with Spatial Prediction Studio. It first finds the ArcMap session, then reads the project, runs a preview and waits for it, and only then starts a run.

The connection between the server and ArcMap is a local named pipe that only the same Windows user can open; network access is denied. Each ArcMap session has its own secret token, which the server never returns to the client.

## Tools

| Tool | Kind | Description |
|---|---|---|
| `discover_sessions` | read | Find the active ArcMap sessions of this Windows user. Does not return the secret connection token. |
| `status` | read | Read the ArcMap state, whether the preview is valid, and the status of the last job. |
| `layers` | read | Read one page of metadata for the layers in the map. Does not return observation coordinates. |
| `get_project` | read | Read the scientific settings and source definitions of the active project. Does not request raw sample data. |
| `open_project` | write | Open an existing local .sps.json project in the ArcMap panel. |
| `save_project` | write | Save the active project to a new local .sps.json file. Does not overwrite an existing file. |
| `preview` | write | Preview the validation folds without training a model. Returns a job ID; use status to monitor completion. |
| `run_analysis` | write | Run the active project that has a valid preview. Returns a job ID; monitor it with status. |
| `cancel` | write | Cancel only the active analysis of the selected ArcMap session. |
| `list_artifacts` | read | Read the verified file inventory of the completed result, page by page. |
| `read_artifact` | read | Read an HTML, text or JSON file from the inventory page by page, re-verifying its file digest. Raw GeoJSON is not readable. |
| `read_report` | read | Read the report index or the report of the completed run as safe text. |
| `read_table_page` | read | Read a CSV table page by page. Coordinates and the raw property.* and raw_record fields are hidden by default. |
| `export_model` | write | Export the latest verified model package to a new .spsmodel.zip file. |
| `export_report` | write | Export the verified study archive to a new .spsstudy.zip file. Does not overwrite an existing file. |

## What is hidden from the client

By default the server hides sample coordinates and raw source fields from what the client reads: coordinate columns in tables, raw `property.*` and `raw_record` fields, and table rows in reports that mention coordinate fields. Such content is replaced by `[coordinate or raw source field hidden]`. Paths must be local and absolute; the server refuses symbolic links and junctions, and it never overwrites an existing file.

The hiding works by field and word names. In the English reports it also hides a few harmless rows, for example the row that gives the coordinate reference system of the samples. The client then sees `[coordinate or raw source field hidden]` instead of that row.
