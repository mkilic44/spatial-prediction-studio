import copy
import csv
import io
import hashlib
import json
import os
import sys
import threading
import time
import uuid
from pathlib import Path

import pytest
import tomlkit
from mcp import Client, ClientSession, StdioServerParameters, stdio_client

from server import create_server, local_path
from register_clients import KEY, CLIENTS, atomic_merge, merge_owned_text, template, main
from sps_mcp.artifacts import read_table, read_text, validated_artifacts
from sps_mcp.bridge import (
    Bridge, BridgeError, SessionRegistry, Session, NamedPipeTransport,
    MAX_RESPONSE, strict_json,
)


def make_session(tmp_path, **changes):
    record = {"schema_version": 1, "session_id": "test", "pid": os.getpid(),
              "pipe_name": "SPS-test", "token": "private-token-do-not-return",
              "created_utc": "2026-10-17T00:00:00Z", "addin_version": "test"}
    record.update(changes)
    (tmp_path / "test.json").write_text(json.dumps(record), encoding="utf-8")
    return SessionRegistry(tmp_path, alive=lambda pid: True), record


def make_listing(tmp_path, name="publication_metrics.csv", data=None):
    data = data if data is not None else b"sample_id,x,y,observed,predicted\ns1,37,38,1,2\ns2,39,40,3,4\n"
    file = tmp_path / name
    file.parent.mkdir(parents=True, exist_ok=True)
    file.write_bytes(data)
    return {"run_id": "run1", "manifest_path": str(tmp_path / "result.json"), "artifacts": [
        {"name": name, "path": str(file), "sha256": hashlib.sha256(data).hexdigest(),
         "kind": "table", "label": name, "bytes": len(data)}]}


def test_session_discovery_never_returns_token_or_pipe(tmp_path):
    registry, record = make_session(tmp_path)
    result = json.dumps(registry.discover())
    assert record["token"] not in result
    assert "pipe_name" not in result
    assert registry.get("test").token == record["token"]
    assert record["token"] not in repr(registry.get("test"))


@pytest.mark.parametrize("change", [
    {"schema_version": 2}, {"session_id": "other"}, {"pid": True},
    {"pipe_name": r"\\remote\pipe\SPS-bad"}, {"token": "short"},
])
def test_bad_session_rejected(tmp_path, change):
    registry, _ = make_session(tmp_path, **change)
    with pytest.raises(BridgeError):
        registry.get("test")


def test_stale_and_traversal_session_rejected(tmp_path):
    registry, _ = make_session(tmp_path)
    registry.alive = lambda pid: False
    assert registry.discover()["ignored_records"] == 1
    with pytest.raises(BridgeError, match="session ID"):
        registry.get("../test")


@pytest.mark.parametrize("raw", ['{"a":1,"a":2}', '{"a":NaN}'])
def test_json_ambiguous_or_nonfinite_rejected(raw):
    with pytest.raises(ValueError):
        strict_json(raw)


class EchoTransport:
    def __init__(self, mutate=None):
        self.mutate = mutate
        self.requests = []
    def exchange(self, session, payload):
        request = json.loads(payload)
        self.requests.append(request)
        response = {"id": request["id"], "ok": True, "result": {"state": "ok"}}
        if self.mutate:
            response = self.mutate(response, session)
        return response if isinstance(response, bytes) else json.dumps(response).encode()


def test_bridge_correlation_and_error_redaction(tmp_path):
    registry, record = make_session(tmp_path)
    transport = EchoTransport()
    assert Bridge(registry, transport).call("test", "status") == {"state": "ok"}
    assert transport.requests[0]["token"] == record["token"]
    bad = EchoTransport(lambda response, session: {**response, "id": "wrong"})
    with pytest.raises(BridgeError) as error:
        Bridge(registry, bad).call("test", "status")
    assert error.value.code == "BRIDGE_PROTOCOL"
    rejected = EchoTransport(lambda response, session: {
        "id": response["id"], "ok": False,
        "error": {"code": "DENIED", "message": "token=" + session.token}})
    with pytest.raises(BridgeError) as error:
        Bridge(registry, rejected).call("test", "status")
    assert record["token"] not in str(error.value)


@pytest.mark.parametrize("method,params", [("exec", {}), ("status", {"shell": "x"}), ("job.start", {})])
def test_closed_native_command_allowlist(tmp_path, method, params):
    registry, _ = make_session(tmp_path)
    transport = EchoTransport()
    with pytest.raises(BridgeError):
        Bridge(registry, transport).call("test", method, params)
    assert not transport.requests


def test_csv_paging_omits_coordinates_and_preserves_order(tmp_path):
    listing = make_listing(tmp_path)
    first = read_table(listing, "publication_metrics.csv", limit=1)
    second = read_table(listing, "publication_metrics.csv", offset=first["next_offset"], limit=1,
                        expected_sha256=first["sha256"])
    assert first["rows"] + second["rows"] == [
        {"sample_id": "s1", "observed": "1", "predicted": "2"},
        {"sample_id": "s2", "observed": "3", "predicted": "4"}]
    assert first["omitted_coordinate_columns"] == ["x", "y"]
    assert second["next_offset"] is None
    with pytest.raises(BridgeError):
        read_table(listing, "publication_metrics.csv", columns=["x"])
    assert "x" in read_table(listing, "publication_metrics.csv", include_coordinates=True)["rows"][0]


def test_artifact_hash_tamper_and_stale_page_rejected(tmp_path):
    listing = make_listing(tmp_path)
    with pytest.raises(BridgeError) as stale:
        read_table(listing, "publication_metrics.csv", expected_sha256="0" * 64)
    assert stale.value.code == "PAGE_STALE"
    (tmp_path / "publication_metrics.csv").write_text("altered", encoding="utf-8")
    with pytest.raises(BridgeError) as changed:
        read_table(listing, "publication_metrics.csv")
    assert changed.value.code == "ARTIFACT_CHANGED"


@pytest.mark.parametrize("name,path_change", [("../secret.csv", None), ("x.csv", "outside"), ("x.csv", "relative")])
def test_artifact_manifest_containment(tmp_path, name, path_change):
    listing = make_listing(tmp_path)
    listing["artifacts"][0]["name"] = name
    if path_change == "outside":
        listing["artifacts"][0]["path"] = str(tmp_path.parent / "secret.csv")
    if path_change == "relative":
        listing["artifacts"][0]["path"] = "relative.csv"
    with pytest.raises(BridgeError):
        validated_artifacts(listing)


def test_read_html_drops_executable_content_and_pages(tmp_path):
    listing = make_listing(tmp_path, "study_report.html",
                           b"<h1>Report</h1><script>secret_script()</script><p>abcdefgh</p>")
    first = read_text(listing, "study_report.html", limit=5)
    rest = read_text(listing, "study_report.html", offset=first["next_offset"])
    assert "secret_script" not in first["text"] + rest["text"]
    assert "Report" in first["text"] + rest["text"]


def test_json_coordinate_redaction_and_geojson_refusal(tmp_path):
    listing = make_listing(tmp_path, "summary.json", b'{"points":[{"coordinates":[37,38],"metric":5}]}')
    assert "[coordinate or raw source field hidden]" in read_text(listing, "summary.json")["text"]
    other = make_listing(tmp_path, "samples.geojson", b'{"type":"FeatureCollection","features":[]}')
    with pytest.raises(BridgeError):
        read_text(other, "samples.geojson")


@pytest.mark.parametrize("extension", ["json", "csv", "html"])
def test_model_diagnostic_coordinates_are_redacted_in_all_readable_formats(tmp_path, extension):
    coordinates = [[371234.125, 4123456.875], [371235.125, 4123457.875]]
    diagnostic = {"metric_coordinates_m": coordinates, "coverage": 0.9, "valid": True}
    if extension == "json":
        data = json.dumps({"diagnostic": diagnostic, "parameters": [
            {"parameter": "metric_coordinates_m", "value": coordinates},
            {"parameter": "alpha", "value": 2}]}).encode()
    elif extension == "csv":
        stream = io.StringIO(newline="")
        writer = csv.writer(stream)
        writer.writerows([["parameter", "value"], ["metric_coordinates_m", json.dumps(coordinates)], ["alpha", "2"]])
        data = stream.getvalue().encode()
    else:
        data = ("<h1>Report</h1><table><tr><th>Field</th><th>Value</th></tr>"
                "<tr><td>calibration / metric_coordinates_m</td><td>" + json.dumps(coordinates) + "</td></tr>"
                "<tr><td>alpha</td><td>2</td></tr></table>"
                "<pre>" + json.dumps(diagnostic) + "</pre>"
                "<table><tr><th>x</th><th>y</th></tr><tr><td>371234.125</td><td>4123456.875</td></tr></table>").encode()
    name = "diagnostic." + extension
    listing = make_listing(tmp_path, name, data)
    reader = read_table if extension == "csv" else read_text
    hidden = reader(listing, name)
    revealed = reader(listing, name, include_coordinates=True)
    assert "371234.125" not in json.dumps(hidden) and "4123456.875" not in json.dumps(hidden)
    assert "371234.125" in json.dumps(revealed) and "4123456.875" in json.dumps(revealed)
    assert "alpha" in json.dumps(hidden) and "2" in json.dumps(hidden)
    if extension == "json":
        assert json.loads(hidden["text"])["diagnostic"]["coverage"] == 0.9
        assert json.loads(hidden["text"])["diagnostic"]["valid"] is True


@pytest.mark.parametrize("limit", [0, -1, 201, True])
def test_csv_limits(tmp_path, limit):
    with pytest.raises(BridgeError):
        read_table(make_listing(tmp_path), "publication_metrics.csv", limit=limit)


def make_installation(tmp_path):
    root = tmp_path / "install"
    (root / "runtime-mcp" / "Scripts").mkdir(parents=True)
    (root / "runtime-mcp" / "Scripts" / "python.exe").write_bytes(b"test marker")
    (root / "mcp-server").mkdir()
    (root / "mcp-server" / "server.py").write_text("# test marker", encoding="utf-8")
    return root


@pytest.mark.parametrize("client", CLIENTS)
def test_all_seven_template_schemas(client, tmp_path):
    root = make_installation(tmp_path)
    fmt, patch = template(client, root)
    if client in {"codex", "chatgpt_desktop"}:
        entry = patch["mcp_servers"][KEY]
        assert fmt == "toml" and entry["tool_timeout_sec"] == 60
    elif client == "opencode":
        entry = patch["mcp"][KEY]
        assert isinstance(entry["command"], list)
        assert entry["type"] == "local"
        assert entry["timeout"] == 60000
    else:
        entry = patch["mcpServers"][KEY]
        assert isinstance(entry["command"], str)
    assert "server.py" in json.dumps(entry)


def test_json_merge_preserves_unrelated_data_and_backup(tmp_path):
    root = make_installation(tmp_path)
    fmt, patch = template("claude_desktop", root)
    config = tmp_path / "claude.json"
    original = b'{"preferences":{"a":3},"mcpServers":{"other":{"command":"leave-me"}}}\n'
    config.write_bytes(original)
    result = atomic_merge(config, fmt, patch)
    assert Path(result["backup"]).read_bytes() == original
    actual = json.loads(config.read_text())
    assert actual["preferences"] == {"a": 3}
    assert actual["mcpServers"]["other"] == {"command": "leave-me"}
    assert result["connected"] is False
    assert atomic_merge(config, fmt, patch)["status"] == "unchanged"


def test_toml_comments_and_unrelated_values_preserved(tmp_path):
    root = make_installation(tmp_path)
    fmt, patch = template("codex", root)
    original = '# user comment\nmodel = "keep"\n[mcp_servers.other]\ncommand = "leave" # retained\n'
    result = merge_owned_text(original, fmt, patch)
    assert "# user comment" in result and "# retained" in result
    assert tomlkit.parse(result)["model"] == "keep"
    assert tomlkit.parse(result)["mcp_servers"]["other"]["command"] == "leave"


@pytest.mark.parametrize("original", [
    '{"mcpServers": {"spatial_prediction_studio":{"command":"other"}}}',
    '{"mcpServers": []}',
    '{"mcpServers": {}, // preserve comment\n "x":1}',
    '{"x":1,"x":2}',
])
def test_unowned_invalid_or_jsonc_config_unchanged(tmp_path, original):
    root = make_installation(tmp_path)
    fmt, patch = template("claude_desktop", root)
    config = tmp_path / "config.json"
    config.write_text(original, encoding="utf-8")
    with pytest.raises(BridgeError):
        atomic_merge(config, fmt, patch)
    assert config.read_text(encoding="utf-8") == original


def test_generation_only_never_applies_config(tmp_path):
    root = make_installation(tmp_path)
    out = tmp_path / "generated"
    assert main(["--root", str(root), "--output-dir", str(out)]) == 0
    result = json.loads((out / "registration-result.json").read_text(encoding="utf-8"))
    assert len(result["clients"]) == 7
    assert all(row["status"] == "template_only" for row in result["clients"])
    assert not result["applied"]


@pytest.mark.parametrize("client", ["codex", "claude_code", "opencode"])
def test_remove_only_same_installation_record(client, tmp_path):
    root = make_installation(tmp_path)
    fmt, patch = template(client, root)
    path = tmp_path / ("config.toml" if fmt == "toml" else "config.json")
    path.write_text('model="keep"\n' if fmt == "toml" else '{"preferences":{"keep":true}}', encoding="utf-8")
    atomic_merge(path, fmt, patch)
    changed = copy.deepcopy(patch)
    group = "mcp_servers" if fmt == "toml" else ("mcp" if client == "opencode" else "mcpServers")
    changed[group][KEY]["command"] = ["newer-version"] if client == "opencode" else "newer-version"
    before = path.read_bytes()
    assert atomic_merge(path, fmt, changed, remove=True)["status"] == "preserved_other_installation"
    assert path.read_bytes() == before
    assert atomic_merge(path, fmt, patch, remove=True)["status"] == "removed_restart_required"
    value = tomlkit.parse(path.read_text()).unwrap() if fmt == "toml" else json.loads(path.read_text())
    assert KEY not in value[group]
    assert value.get("model") == "keep" if fmt == "toml" else value["preferences"]["keep"] is True


class FakeRegistry:
    def discover(self):
        return {"sessions": [], "ignored_records": 0}


class FakeBridge:
    registry = FakeRegistry()
    def __init__(self):
        self.calls = []
    def call(self, session_id, method, params=None):
        self.calls.append((session_id, method, params))
        if method == "layers":
            return {"points": [{"name": str(i), "path": "fixture.shp", "fields": ["id", "target"]} for i in range(3)],
                    "raster_bands": [{"id": "r1", "name": "covariate", "path": "fixture.tif", "band": 1, "unit": "m"}],
                    "scope": "current_map"}
        return {"busy": False, "state": "fixture"}


@pytest.mark.anyio
async def test_sdk_schema_rejects_unknown_and_coerced_arguments():
    bridge = FakeBridge()
    async with Client(create_server(bridge)) as client:
        tools = (await client.list_tools()).tools
        assert len(tools) == 15
        assert all(tool.input_schema["additionalProperties"] is False for tool in tools)
        for arguments in ({"session_id": "s", "shell": "whoami"},
                          {"session_id": "s", "limit": "2"},
                          {"session_id": "s", "limit": True},
                          {"session_id": "../s"}):
            result = await client.call_tool("layers", arguments)
            assert result.is_error
        assert not bridge.calls
        result = await client.call_tool("layers", {"session_id": "s", "offset": 2, "limit": 2})
        rows = result.structured_content["layers"]
        assert rows[0] == {"name": "2", "path": "fixture.shp", "fields": ["id", "target"], "layer_type": "point"}
        assert rows[1] == {"id": "r1", "name": "covariate", "path": "fixture.tif", "band": 1, "unit": "m", "layer_type": "raster_band"}
        assert result.structured_content["scope"] == "current_map"
        assert result.structured_content["next_offset"] is None


def test_csv_original_properties_and_structured_cells_are_private_by_default(tmp_path):
    raw = {"geometry": {"type": "Point", "coordinates": [371234, 4123456]},
           "properties": {"custom_east": 371234, "custom_north": 4123456, "clay": 20}}
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=["sample_id", "property.x", "property.y", "property.custom_east",
                                               "raw_record", "diagnostic", "flag", "text", "observed"])
    writer.writeheader()
    source = {"sample_id": "s1", "property.x": "371234", "property.y": "4123456", "property.custom_east": "371234",
              "raw_record": json.dumps(raw), "diagnostic": json.dumps({"coordinates": [371234, 4123456], "ok": True, "score": 2}),
              "flag": "true", "text": "normal text", "observed": "20"}
    writer.writerow(source)
    listing = make_listing(tmp_path, "role_data.csv", output.getvalue().encode())
    result = read_table(listing, "role_data.csv")
    row = result["rows"][0]
    assert "371234" not in json.dumps(result) and "4123456" not in json.dumps(result)
    assert row["flag"] == "true" and row["text"] == "normal text" and row["observed"] == "20"
    assert json.loads(row["diagnostic"])["ok"] is True
    assert json.loads(row["diagnostic"])["score"] == 2
    assert read_table(listing, "role_data.csv", include_coordinates=True)["rows"] == [source]
    with pytest.raises(BridgeError):
        read_table(listing, "role_data.csv", columns=["property.x"])


@pytest.mark.parametrize("path", [r"C:\test\x.sps.json:stream.sps.json", r"\\server\share\x.sps.json",
                                  r"\test\x.sps.json", r"C:relative.sps.json", r"\\?\C:\x.sps.json"])
def test_local_path_rejects_alternate_stream_and_non_drive_absolute(path):
    with pytest.raises(BridgeError) as error:
        local_path(path, (".sps.json",))
    assert error.value.code == "LOCAL_PATH"


@pytest.mark.anyio
async def test_export_report_exact_native_extension_and_no_overwrite(tmp_path):
    bridge = FakeBridge()
    async with Client(create_server(bridge)) as client:
        rejected = await client.call_tool("export_report", {"session_id": "s", "path": str(tmp_path / "wrong.zip")})
        assert rejected.is_error and not bridge.calls
        path = str(tmp_path / "study.spsstudy.zip")
        result = await client.call_tool("export_report", {"session_id": "s", "path": path})
        assert not result.is_error
        assert bridge.calls == [("s", "result.export_report", {"path": path, "overwrite": False})]


@pytest.mark.anyio
@pytest.mark.parametrize("legacy", [True, False])
async def test_real_sdk_stdio_handshake_tools_and_call_with_synthetic_bridge(legacy):
    fixture = Path(__file__).with_name("stdio_fixture.py")
    parameters = StdioServerParameters(command=sys.executable, args=[str(fixture)])
    if legacy:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as client:
                await client.initialize()
                names = [tool.name for tool in (await client.list_tools()).tools]
                result = await client.call_tool("status", {"session_id": "synthetic"})
                assert len(names) == 15
                assert result.structured_content["state"] == "synthetic_protocol_fixture"
    else:
        async with Client(parameters) as client:
            names = [tool.name for tool in (await client.list_tools()).tools]
            result = await client.call_tool("status", {"session_id": "synthetic"})
            assert len(names) == 15
            assert result.structured_content["state"] == "synthetic_protocol_fixture"


def serve_test_pipe(name, ready, done, response, delay=0):
    import win32pipe
    import win32file
    handle = win32pipe.CreateNamedPipe(
        "\\\\.\\pipe\\" + name, win32pipe.PIPE_ACCESS_DUPLEX,
        win32pipe.PIPE_TYPE_BYTE | win32pipe.PIPE_READMODE_BYTE | win32pipe.PIPE_WAIT,
        1, 65536, 65536, 2000, None)
    try:
        ready.set()
        win32pipe.ConnectNamedPipe(handle, None)
        data = bytearray()
        while b"\n" not in data:
            _, part = win32file.ReadFile(handle, 4096)
            data.extend(part)
        request = json.loads(bytes(data))
        if delay:
            done.wait(delay)
        if response is not None:
            payload = response(request)
            for offset in range(0, len(payload), 17000):
                win32file.WriteFile(handle, payload[offset:offset + 17000])
    except Exception:
        pass
    finally:
        handle.Close()


@pytest.mark.parametrize("behavior", ["normal", "large", "timeout", "wrong_pid"])
def test_real_windows_named_pipe_transport_with_synthetic_peer(behavior):
    name = "SPS-test-" + uuid.uuid4().hex
    ready, done = threading.Event(), threading.Event()
    size = 150000 if behavior == "large" else 5
    response = lambda req: json.dumps({"id": req["id"], "ok": True, "result": "a" * size}).encode() + b"\n"
    thread = threading.Thread(target=serve_test_pipe, args=(
        name, ready, done, response if behavior != "timeout" else None, 2 if behavior == "timeout" else 0), daemon=True)
    thread.start()
    assert ready.wait(3)
    session = Session("test", os.getpid() if behavior != "wrong_pid" else os.getpid() + 1,
                      name, "private-token-for-test")
    start = time.monotonic()
    try:
        transport = NamedPipeTransport(0.2 if behavior == "timeout" else 5)
        payload = b'{"id":"test","token":"private-token-for-test","method":"status","params":{}}\n'
        if behavior in {"timeout", "wrong_pid"}:
            with pytest.raises(BridgeError) as error:
                transport.exchange(session, payload)
            assert error.value.code == ("BRIDGE_TIMEOUT" if behavior == "timeout" else "SESSION_MISMATCH")
            assert time.monotonic() - start < 3
        else:
            result = json.loads(transport.exchange(session, payload))
            assert result["result"] == "a" * size
    finally:
        done.set()
        thread.join(timeout=3)
        assert not thread.is_alive()

