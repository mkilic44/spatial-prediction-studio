import hashlib
import importlib.metadata
import json
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from pathlib import Path

root = Path(__file__).resolve().parents[1]
xml = root / "build/mcp-tests-final3.xml"
suite = ET.parse(xml).getroot().find("testsuite")
assert suite is not None
counts = {key: int(suite.attrib[key]) for key in ("tests", "failures", "errors", "skipped")}
assert counts == {"tests": 58, "failures": 0, "errors": 0, "skipped": 0}
paths = [path for path in (root / "mcp-server").rglob("*") if path.is_file()
         and "__pycache__" not in path.parts and ".pytest_cache" not in path.parts]
paths += [root / value for value in ("requirements-mcp-lock.txt", "tools/setup-runtime-mcp.ps1",
    "build/MCP_CLIENT_SUPPORT_DRAFT.md", "build/MCP_SDK_API_DRAFT.md",
    "build/mcp-tests-final3.xml", "build/mcp-real-report-redaction.json",
    "build/mcp-client-templates-draft.json", "build/mcp-wheels-manifest.json")]
licenses = json.loads((root / "mcp-server/licenses/manifest.json").read_text(encoding="utf-8"))
assert len(licenses) == 35 and all(item["full_license_files"] for item in licenses)
value = {"schema_version": 1, "version": "0.5.0", "created_utc": datetime.now(timezone.utc).isoformat(),
    "source_status": "frozen", "tests": {**counts, "evidence": "build/mcp-tests-final3.xml",
    "scope": "actual SDK STDIO and synthetic Windows pipe; not real ArcMap"},
    "real_generated_report_redaction": {"artifacts": 4, "passed": 4, "sources_unchanged": True,
        "evidence": "build/mcp-real-report-redaction.json"},
    "python": sys.version, "mcp_version": importlib.metadata.version("mcp"),
    "locked_packages": 35, "full_license_package_records": len(licenses),
    "actual_client_configs_written": False, "real_arcmap_mcp_acceptance": "pending",
    "seven_client_live_acceptance": "pending", "transport": "stdio_only",
    "native_timeout_seconds": 30, "python_timeout_seconds": 35, "automatic_mutation_retry": False,
    "checkpoint_reason": "User stopped desktop control with Escape; no new desktop or long-running test started.",
    "open_items": ["Real ArcMap bridge discovery and status/layers/project/job/artifact acceptance",
                   "Live acceptance in each available client after explicit registration",
                   "Installer clean-machine acceptance; PyCharm manual AI Assistant import remains explicit"],
    "source_sha256": {str(path.relative_to(root)).replace("\\", "/"): hashlib.sha256(path.read_bytes()).hexdigest()
                       for path in sorted(paths)}}
target = root / "build/mcp-implementation-evidence.json"
target.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
loaded = json.loads(target.read_text(encoding="utf-8"))
assert loaded == value
print(json.dumps({"checkpoint": str(target), "source_files": len(paths), "tests": counts,
                  "licenses": len(licenses), "client_configs_written": False}))
