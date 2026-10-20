"""Operator-run acceptance against one actual ArcMap, through the real MCP SDK."""
import argparse
import asyncio
import copy
import hashlib
import json
from pathlib import Path
import time
from datetime import datetime, timezone

from mcp import Client, StdioServerParameters

ROOT = Path(__file__).resolve().parents[1]
INSTALL_ROOT = ROOT
EVIDENCE = ROOT / "build/arcmap-evidence"

def write(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False), encoding="utf-8")

def prepare():
    value = json.loads((INSTALL_ROOT / "examples-d/tabpfn.sps.json").read_text(encoding="utf-8"))
    value["science"]["model"] = {"id": "ridge", "parameters": {"alpha": 1.0}}
    value["science"]["optimization"].update(method="grid", trials=3, spaces={"alpha": {"type": "categorical", "choices": [0.1, 1.0, 10.0]}})
    value["science"]["validation"]["independent_ids"] = ["D046", "D047"]
    value["samples"]["excluded"] = [{"id": "D000", "reason": "Predefined exclusion for a synthetic software test"}]
    value["output_root"] = str(EVIDENCE / "mcp-runs")
    write(EVIDENCE / "mcp-grid.sps.json", value)
    return value

async def main(mode):
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    evidence = {"status": "running", "mode": mode, "started_utc": datetime.now(timezone.utc).isoformat(), "calls": []}
    destination = EVIDENCE / ("mcp-actual-" + mode + "-" + stamp + ".json")
    evidence["installation_root"] = str(INSTALL_ROOT)
    sources = [INSTALL_ROOT / "mcp-server/server.py", *sorted((INSTALL_ROOT / "mcp-server/sps_mcp").glob("*.py"))]
    evidence["server_sources"] = {str(path.relative_to(INSTALL_ROOT)): hashlib.sha256(path.read_bytes()).hexdigest() for path in sources}
    parameters = StdioServerParameters(command=str(INSTALL_ROOT / "runtime-mcp/Scripts/python.exe"), args=["-X", "utf8", str(INSTALL_ROOT / "mcp-server/server.py")])
    try:
        async with Client(parameters) as client:
            async def call(name, args=None, expect_error=False):
                started = time.monotonic()
                result = await client.call_tool(name, args or {})
                entry = {"tool": name, "seconds": time.monotonic()-started, "is_error": result.is_error,
                         "result": result.structured_content, "text": [item.text for item in result.content if hasattr(item, "text")]}
                evidence["calls"].append(entry); write(destination, evidence)
                print(json.dumps({"tool": name, "is_error": result.is_error, "seconds": round(entry["seconds"], 3)}, ensure_ascii=False), flush=True)
                if bool(result.is_error) != expect_error:
                    raise AssertionError(entry)
                return result.structured_content
            tools = (await client.list_tools()).tools
            evidence["tools"] = [tool.name for tool in tools]
            assert len(tools) == 15
            sessions = (await call("discover_sessions"))["sessions"]
            assert len(sessions) == 1, sessions
            sid = sessions[0]["session_id"]; evidence["arcmap_session"] = sessions[0]
            args = {"session_id": sid}
            await call("status", args)
            await call("layers", {**args, "limit": 1})
            if mode == "inventory":
                pass
            elif mode == "reopen":
                saved = sorted(EVIDENCE.glob("mcp-exports-*/completed.sps.json"))[-1]
                await call("open_project", {**args, "path": str(saved)})
                restored = await call("get_project", args)
                assert restored["last_manifest"]
                evidence["restored_project"] = str(saved)
            elif mode == "run":
                prepare()
                await call("open_project", {**args, "path": str(EVIDENCE / "mcp-grid.sps.json")})
                await call("get_project", args)
                preview = await call("preview", args)
                async def wait_job(job):
                    deadline = time.monotonic() + 240
                    while time.monotonic() < deadline:
                        status = await call("status", args)
                        if not status["busy"]:
                            assert status["last_job"]["id"] == job["id"], status
                            assert status["last_job"]["status"] == "completed", status
                            return status
                        await asyncio.sleep(2)
                    raise TimeoutError(job)
                state = await wait_job(preview); assert state["preview_ready"]
                job = await call("run_analysis", args); evidence["analysis_job"] = job
                state = await wait_job(job); evidence["completed_status"] = state
                listed = []; offset = 0
                while True:
                    page = await call("list_artifacts", {**args, "offset": offset, "limit": 40})
                    listed.extend(page["artifacts"])
                    if page["next_offset"] is None: break
                    offset = page["next_offset"]
                assert len(listed) == page["total"]
                evidence["artifact_count"] = len(listed)
                await call("read_report", {**args, "limit": 12000})
                for name in ["trial_results.csv", "inner_fold_results.csv", "publication_metrics.csv", "role_data.csv"]:
                    await call("read_table_page", {**args, "name": name, "limit": 5})
                exports = EVIDENCE / ("mcp-exports-" + stamp); exports.mkdir()
                for tool, filename in [("export_model", "model.spsmodel.zip"), ("export_report", "study.spsstudy.zip")]:
                    exported = await call(tool, {**args, "path": str(exports / filename)})
                    assert hashlib.sha256(Path(exported["path"]).read_bytes()).hexdigest() == exported["sha256"]
                await call("save_project", {**args, "path": str(exports / "completed.sps.json")})
                evidence["exports_directory"] = str(exports)
            elif mode == "rollback":
                prepare()
                await call("open_project", {**args, "path": str(EVIDENCE / "mcp-grid.sps.json")})
                before = await call("get_project", args); before_status = await call("status", args)
                bad = copy.deepcopy(before)
                bad["science"]["model"]["parameters"]["alpha"] = 3.5
                bad["science"]["mapping"]["figure_dpi"] = 153
                invalid = EVIDENCE / ("mcp-invalid-late-" + stamp + ".sps.json"); write(invalid, bad)
                await call("open_project", {**args, "path": str(invalid)}, expect_error=True)
                after = await call("get_project", args); after_status = await call("status", args)
                ignored = {"preview_fingerprint", "preview_manifest", "preview_signature", "preview_sources"}
                assert {k:v for k,v in before.items() if k not in ignored} == {k:v for k,v in after.items() if k not in ignored}
                assert after_status["project_path"] == before_status["project_path"]
                assert not after_status["preview_ready"]
                evidence["restored_persistent_project"] = True
            elif mode == "cancel":
                value = json.loads((EVIDENCE / "d-tabm-cancel.sps.json").read_text(encoding="utf-8"))
                value["output_root"] = str(EVIDENCE / "mcp-runs")
                value["last_manifest"] = ""
                fixture = EVIDENCE / ("mcp-cancel-" + stamp + ".sps.json"); write(fixture, value)
                await call("open_project", {**args, "path": str(fixture)})
                preview = await call("preview", args)
                deadline = time.monotonic() + 120
                while time.monotonic() < deadline:
                    state = await call("status", args)
                    if not state["busy"]: break
                    await asyncio.sleep(2)
                assert state["preview_ready"] and state["last_job"]["status"] == "completed"
                job = await call("run_analysis", args); evidence["cancelled_job"] = job
                await asyncio.sleep(12)
                active = await call("status", args); assert active["busy"]
                cancelled_at = time.monotonic()
                await call("cancel", args)
                deadline = time.monotonic() + 25
                while time.monotonic() < deadline:
                    state = await call("status", args)
                    if not state["busy"]: break
                    await asyncio.sleep(1)
                assert not state["busy"] and state["last_job"]["status"] == "cancelled", state
                evidence["cancel_seconds"] = time.monotonic() - cancelled_at
                evidence["cancelled_status"] = state
            else:
                raise ValueError(mode)
        for path in sources:
            assert hashlib.sha256(path.read_bytes()).hexdigest() == evidence["server_sources"][str(path.relative_to(INSTALL_ROOT))]
        evidence["status"] = "passed"; write(destination, evidence)
        print(json.dumps({"evidence": str(destination), "status": "passed"}), flush=True)
    except BaseException as error:
        evidence["status"] = "failed"; evidence["error"] = repr(error); write(destination, evidence); raise

if __name__ == "__main__":
    parser = argparse.ArgumentParser(); parser.add_argument("mode", choices=["inventory", "run", "rollback", "cancel", "reopen"])
    parser.add_argument("--install-root", type=Path)
    options = parser.parse_args()
    if options.install_root:
        INSTALL_ROOT = options.install_root.resolve(strict=True)
        EVIDENCE = EVIDENCE / "installed"
        EVIDENCE.mkdir(exist_ok=True)
    asyncio.run(main(options.mode))
