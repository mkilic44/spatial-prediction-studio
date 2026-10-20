"""Spatial Prediction Studio MCP entry point. STDIO only."""
from __future__ import annotations

import logging
import json
import re
from pathlib import Path
from typing import Annotated, Any, Literal

import anyio
from mcp.server import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field
from jsonschema import Draft202012Validator

from sps_mcp.artifacts import bounded_json, read_table, read_text, validated_artifacts
from sps_mcp.bridge import Bridge, BridgeError, reject_reparse

READ = ToolAnnotations(read_only_hint=True, destructive_hint=False,
                       idempotent_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False,
                        idempotent_hint=False, open_world_hint=False)
ID = Annotated[str, Field(min_length=1, max_length=100, pattern=r"^[A-Za-z0-9_-]+$")]
OFFSET = Annotated[int, Field(ge=0, le=1000000, strict=True)]
LIMIT = Annotated[int, Field(ge=1, le=200, strict=True)]
TEXT_LIMIT = Annotated[int, Field(ge=1, le=16000, strict=True)]


class StrictMCPServer(MCPServer):
    async def list_tools(self):
        tools = await super().list_tools()
        for tool in tools:
            tool.input_schema["additionalProperties"] = False
        return tools

    async def call_tool(self, name, arguments, context=None):
        tools = {tool.name: tool for tool in await self.list_tools()}
        if name not in tools:
            raise ToolError("TOOL_UNKNOWN: Unknown tool.")
        try:
            encoded = json.dumps(arguments, ensure_ascii=False, allow_nan=False).encode("utf-8")
            if len(encoded) > 1024 * 1024:
                raise ValueError("Too large")
            if list(Draft202012Validator(tools[name].input_schema).iter_errors(arguments)):
                raise ValueError("Schema mismatch")
        except (ValueError, TypeError):
            raise ToolError("TOOL_ARGUMENTS: Tool parameters are invalid or too large.") from None
        return await super().call_tool(name, arguments, context)


def local_path(value: str, suffixes: tuple[str, ...]):
    path = Path(value)
    if (not value or "\0" in value or len(value) > 30000 or
            not re.match(r"^[A-Za-z]:[\\/]", value) or ":" in value[2:]):
        raise BridgeError("LOCAL_PATH", "A local, absolute file path is required.")
    if not str(path).lower().endswith(suffixes):
        raise BridgeError("FILE_EXTENSION", "The file extension is not valid for this operation.")
    reject_reparse(path)
    return str(path)


def create_server(bridge=None):
    bridge = bridge if bridge is not None else Bridge()
    server = StrictMCPServer(
        "Spatial Prediction Studio", version="0.5.0",
        instructions=(
            "First select the target ArcMap session with discover_sessions. Read the project, "
            "run preview and use status to wait until it completes. Do not call run_analysis "
            "without preview_ready. Long jobs return a job ID; monitor them with status and stop them with cancel. "
            "Only the files of a verified result can be read. Coordinate fields are hidden by default. "
            "General code or shell execution is not available."),
        log_level="WARNING")

    async def invoke(session_id, method, params=None):
        try:
            result = await anyio.to_thread.run_sync(bridge.call, session_id, method, params)
            return result
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None

    async def artifact_operation(session_id, operation, *args):
        listing = await invoke(session_id, "result.list")
        try:
            return await anyio.to_thread.run_sync(operation, listing, *args)
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None

    @server.tool(annotations=READ)
    async def discover_sessions() -> dict[str, Any]:
        """Find the active ArcMap sessions of this Windows user. Does not return the secret connection token."""
        try:
            return await anyio.to_thread.run_sync(bridge.registry.discover)
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None

    @server.tool(annotations=READ)
    async def status(session_id: ID) -> dict[str, Any]:
        """Read the ArcMap state, whether the preview is valid, and the status of the last job."""
        return bounded_json(await invoke(session_id, "status"))

    @server.tool(annotations=READ)
    async def layers(session_id: ID, offset: OFFSET = 0, limit: LIMIT = 50) -> dict[str, Any]:
        """Read one page of metadata for the layers in the map. Does not return observation coordinates."""
        result = await invoke(session_id, "layers")
        if (not isinstance(result, dict) or not isinstance(result.get("points"), list)
                or not isinstance(result.get("raster_bands"), list)
                or any(not isinstance(row, dict) for group in ("points", "raster_bands") for row in result[group])):
            raise ToolError("LAYERS_RESPONSE: The layer list is invalid.")
        rows = ([{**row, "layer_type": "point"} for row in result["points"]] +
                [{**row, "layer_type": "raster_band"} for row in result["raster_bands"]])
        end = min(offset + limit, len(rows))
        return bounded_json({"layers": rows[offset:end], "total": len(rows),
                             "offset": offset, "next_offset": end if end < len(rows) else None,
                             "scope": result.get("scope")})

    @server.tool(annotations=READ)
    async def get_project(session_id: ID) -> dict[str, Any]:
        """Read the scientific settings and source definitions of the active project. Does not request raw sample data."""
        return bounded_json(await invoke(session_id, "project.get"))

    @server.tool(annotations=WRITE)
    async def open_project(session_id: ID, path: str) -> dict[str, Any]:
        """Open an existing local .sps.json project in the ArcMap panel."""
        try:
            path = local_path(path, (".sps.json",))
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        return await invoke(session_id, "project.open", {"path": path})

    @server.tool(annotations=WRITE)
    async def save_project(session_id: ID, path: str) -> dict[str, Any]:
        """Save the active project to a new local .sps.json file. Does not overwrite an existing file."""
        try:
            path = local_path(path, (".sps.json",))
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        return await invoke(session_id, "project.save", {"path": path, "overwrite": False})

    @server.tool(annotations=WRITE)
    async def preview(session_id: ID) -> dict[str, Any]:
        """Preview the validation folds without training a model. Returns a job ID; use status to monitor completion."""
        return await invoke(session_id, "job.start", {"action": "preview"})

    @server.tool(annotations=WRITE)
    async def run_analysis(session_id: ID) -> dict[str, Any]:
        """Run the active project that has a valid preview. Returns a job ID; monitor it with status."""
        return await invoke(session_id, "job.start", {"action": "run"})

    @server.tool(annotations=WRITE)
    async def cancel(session_id: ID) -> dict[str, Any]:
        """Cancel only the active analysis of the selected ArcMap session."""
        return await invoke(session_id, "job.cancel")

    @server.tool(annotations=READ)
    async def list_artifacts(session_id: ID, offset: OFFSET = 0, limit: LIMIT = 50) -> dict[str, Any]:
        """Read the verified file inventory of the completed result, page by page."""
        listing = await invoke(session_id, "result.list")
        try:
            rows = validated_artifacts(listing)
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        end = min(offset + limit, len(rows))
        public = [{k: row.get(k) for k in ("name", "sha256", "kind", "label", "bytes")}
                  for row in rows[offset:end]]
        return bounded_json({"run_id": listing.get("run_id"), "artifacts": public,
                             "total": len(rows), "next_offset": end if end < len(rows) else None})

    @server.tool(annotations=READ)
    async def read_artifact(session_id: ID, name: str, offset: OFFSET = 0,
                            limit: TEXT_LIMIT = 12000, expected_sha256: str | None = None,
                            include_coordinates: bool = False) -> dict[str, Any]:
        """Read an HTML, text or JSON file from the inventory page by page, re-verifying its file digest. Raw GeoJSON is not readable."""
        return await artifact_operation(session_id, read_text, name, offset, limit,
                                        expected_sha256, include_coordinates)

    @server.tool(annotations=READ)
    async def read_report(session_id: ID, offset: OFFSET = 0,
                          limit: TEXT_LIMIT = 12000, expected_sha256: str | None = None) -> dict[str, Any]:
        """Read the report index or the report of the completed run as safe text."""
        listing = await invoke(session_id, "result.list")
        try:
            available = {row["name"] for row in validated_artifacts(listing)}
            name = next((n for n in ("study_report_index.html", "study_report.html", "report.html") if n in available), None)
            if name is None:
                raise BridgeError("REPORT_MISSING", "The completed result has no HTML report.")
            return await anyio.to_thread.run_sync(read_text, listing, name, offset, limit, expected_sha256, False)
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None

    @server.tool(annotations=READ)
    async def read_table_page(session_id: ID, name: str, offset: OFFSET = 0,
                              limit: LIMIT = 50, columns: list[str] | None = None,
                              expected_sha256: str | None = None,
                              include_coordinates: bool = False) -> dict[str, Any]:
        """Read a CSV table page by page. Coordinates and the raw property.* and raw_record fields are hidden by default."""
        return await artifact_operation(session_id, read_table, name, offset, limit, columns,
                                        expected_sha256, include_coordinates)

    @server.tool(annotations=WRITE)
    async def export_model(session_id: ID, path: str) -> dict[str, Any]:
        """Export the latest verified model package to a new .spsmodel.zip file."""
        try:
            path = local_path(path, (".spsmodel.zip",))
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        return await invoke(session_id, "result.export_model", {"path": path, "overwrite": False})

    @server.tool(annotations=WRITE)
    async def export_report(session_id: ID, path: str) -> dict[str, Any]:
        """Export the verified study archive to a new .spsstudy.zip file. Does not overwrite an existing file."""
        try:
            path = local_path(path, (".spsstudy.zip",))
        except BridgeError as error:
            raise ToolError(f"{error.code}: {error.message}") from None
        return await invoke(session_id, "result.export_report", {"path": path, "overwrite": False})

    return server


if __name__ == "__main__":
    logging.basicConfig(level=logging.WARNING)
    create_server().run(transport="stdio")

