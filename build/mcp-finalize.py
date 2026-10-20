"""Local documentation/evidence only. Never writes client config or completed runs."""
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))
from sps_mcp.artifacts import read_text
from register_clients import CLIENTS, template, main

doc = ROOT / "build/MCP_CLIENT_SUPPORT_DRAFT.md"
# The draft and mcp-server/README.md were recorded in Turkish. The first argument of each
# replace() and the index() anchors match that recorded text; keep them as recorded.
text = doc.read_text(encoding="utf-8").replace("arcmap_server", "spatial_prediction_studio")
text = text.replace("Durum: resmî belgeler ve indirilen paket kaynağı doğrulandı; yedi istemcide canlı kabul henüz yapılmadı.",
                    "Version 0.5.0. Official documentation, the package source and the local implementation were tested. Real ArcMap acceptance in the seven clients has not been done yet.")
start, end = text.index("## 2."), text.index("## 3.")
text = text[:start] + """## 2. Configuration templates

`build/mcp-client-templates/` contains templates generated with real local paths for seven clients. `build/mcp-client-templates-draft.json` collects the same records in machine-readable form. When the installation is in another directory, the templates are generated from the new root.

The implemented helper is `mcp-server/register_clients.py`. By default it only generates templates. Only when `--apply --client <client>` is given explicitly is the `spatial_prediction_studio` entry merged. The existing file is backed up first. The same name owned by another product is rejected; other settings are kept and the file is parsed again.

If Codex and ChatGPT share the same file, a single write is made. The PyCharm template is imported from the AI Assistant settings. No automatic write is made to an OpenCode file with comments or in a custom location. `--remove --client <client>` removes only the entry whose ownership and launch path match this installation. The trust decisions of the user are not changed.

""" + text[end:]
text = text.replace("Sürümün wheel dosyası indirildi, kurulmadı.", "The release wheel file was downloaded and installed into the separate `runtime-mcp` environment.")
start, end = text.index("Mevcut `runtime-d`"), text.index("## 4.")
text = text[:start] + """`runtime-mcp` is a separate development environment created with Python 3.12.3. The analysis environment was not changed. `requirements-mcp-lock.txt` pins the versions of 35 packages and the Windows x64 wheel digests. `pip check` passed. `mcp-server/licenses/` contains the real package metadata and full license files. The portable installer does not copy this virtual environment directly; it uses its own Python interpreter.

""" + text[end:]
text = text.replace("On bir araç için bu yeterlidir.", "This is sufficient for fifteen tools.")
text = text.replace("Yeni `Client` ile otomatik keşif, `ClientSession.initialize()` ile eski yol ayrı sınanmalıdır.",
                    "Automatic discovery with the new `Client` and the earlier path with `ClientSession.initialize()` were tested separately in a real STDIO child process.")
start = text.index("Planlanan araçlar:")
text = text[:start] + """The 15 implemented tools cover session discovery, status, layers, project read/open/save, preview, run, cancel, output listing, text/report/table reading and model/study archive export. There is no general Python or shell execution.

A model job returns a job ID; status and cancellation are separate calls. The local bridge has a 30-second limit and the Python client a 35-second limit. A state-changing call that times out is not retried automatically. The status is reconciled first. Reports are read only from the verified result inventory, and the content digest is checked again.

Coordinates and raw source properties are hidden in default table/JSON reads. Coordinate rows in report tables and raw preformatted content are also hidden. The `include_coordinates=true` option explicitly requests raw content. Whether each number in free text is a location cannot be determined automatically.

## 6. Verified and open work

`build/mcp-tests-final3.xml`: 58 tests passed, with zero errors and skips. Two protocol paths with real SDK processes, a synthetic Windows pipe, the time limit, a wrong process ID, closed parameter schemas and seven configuration schemas were tested. Backup, safe removal, file scope and coordinate hiding were also covered. These are not real ArcMap acceptance.

Connection with a real ArcMap session and tool calls in the seven clients are still open. No new ArcMap acceptance was started because the user stopped computer control. Clean-machine installation and the trust/approval steps of each client must be verified separately.

No real client configuration was changed during this implementation work. No user data was sent to a remote MCP service.
"""
doc.write_text(text, encoding="utf-8")
readme = ROOT / "mcp-server/README.md"
# Fragment of a Turkish sentence in the recorded 0.5.0 README; keep both arguments as recorded.
readme.write_text(readme.read_text(encoding="utf-8").replace("55 hedefli sınama", "58 hedefli sınama"), encoding="utf-8")

out = ROOT / "build/mcp-client-templates"
main(["--root", str(ROOT), "--output-dir", str(out)])
templates = {client: {"format": template(client, ROOT)[0], "config": template(client, ROOT)[1]} for client in CLIENTS}
(ROOT / "build/mcp-client-templates-draft.json").write_text(json.dumps({"schema_version": 1, "applied": False,
    "templates": templates}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Verify redaction against immutable previously generated real study outputs.
run = ROOT / "build/arcmap-evidence/d-runs/20261017_220635_13f2d3a5/output"
record = json.loads((run / "final_record.json").read_text(encoding="utf-8"))
coordinate_tokens = set()
def collect(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "metric_coordinates_m":
                for pair in item:
                    coordinate_tokens.update(str(number) for number in pair)
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)
collect(record)
assert coordinate_tokens
readbacks = []
for name in ("final_record.json", "study_summary.json", "study_report.html", "report.html"):
    path = run / name
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    listing = {"manifest_path": str(run / "result.json"), "artifacts": [{"name": name,
        "path": str(path), "sha256": digest, "bytes": len(original)}]}
    chunks, offset = [], 0
    while True:
        page = read_text(listing, name, offset=offset)
        chunks.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    rendered = "".join(chunks)
    assert not any(token in rendered for token in coordinate_tokens), name
    assert path.read_bytes() == original
    readbacks.append({"name": name, "sha256": digest, "source_unchanged": True,
                      "coordinate_tokens_absent": True, "pages": len(chunks)})
(ROOT / "build/mcp-real-report-redaction.json").write_text(json.dumps({"run": str(run),
    "coordinate_token_count": len(coordinate_tokens), "artifacts": readbacks,
    "live_arcmap_acceptance": False}, indent=2) + "\n", encoding="utf-8")
print("real_report_redaction=4/4; original_sources_unchanged=true")
