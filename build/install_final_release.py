"""Install the accepted single setup for the authorized current Windows user."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import re
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "build/installed-release"
SOURCE_ADDIN_SHA256 = "b8831e222582b334cfdd29e61b903ef907ee391f3a11bed09d76db4728bbef0c"

def read(path): return json.loads(path.read_text(encoding="utf-8-sig"))
def sha(path):
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()

def file_record(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}


def atomic_record(path, value):
    temporary = path.with_suffix(".json.partial")
    temporary.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    if read(temporary) != value: raise ValueError("Installation evidence readback differs")
    temporary.replace(path)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-record", type=Path, default=ROOT / "installer/dist/installer-build-record.json")
    # Staging folder of the recorded 0.5.0 installer acceptance; keep the path as recorded.
    parser.add_argument("--staged-acceptance", type=Path, default=ROOT.parent / "sps-ç2/setup-acceptance.json")
    parser.add_argument("--output-dir", type=Path, default=OUT)
    return parser.parse_args(argv)


def verify_install_inputs(build_path, acceptance_path):
    build = read(Path(build_path)); acceptance = read(Path(acceptance_path))
    exe = Path(build["installer"]).resolve(strict=True)
    if acceptance.get("status") != "passed" or acceptance["install"]["exit_code"] != 0 or acceptance["uninstall"]["exit_code"] != 0:
        raise ValueError("Staged installation and removal have not passed")
    if not re.fullmatch(r"0\.5\.0-[0-9a-f]{12}", build["build_id"]) or acceptance["build_id"] != build["build_id"]:
        raise ValueError("Accepted installer build identity differs")
    if sha(exe) != acceptance["installer_sha256"] or acceptance["installer_sha256"] != build["sha256"] or exe.stat().st_size != build["bytes"]:
        raise ValueError("Installer bytes differ from staged acceptance")
    deployment = acceptance["deployment"]
    if deployment["status"] != "installed" or deployment["staging"] is not True or deployment["source_addin_sha256"] != SOURCE_ADDIN_SHA256:
        raise ValueError("Staged deployment does not identify the frozen source add-in")
    if deployment["payload_manifest_sha256"] != build["manifest_sha256"] or acceptance["installed_readback"]["exit_code"] != 0:
        raise ValueError("Staged payload manifest or installed file readback differs")
    return build, acceptance, exe


def main():
    if sys.flags.optimize: raise ValueError("Do not disable installation checks with Python -O")
    args = parse_args()
    build, acceptance, exe = verify_install_inputs(args.build_record, args.staged_acceptance)
    output = args.output_dir.resolve()
    if not output.is_relative_to(ROOT / "build"):
        raise ValueError("Installation evidence must stay under build")
    if (output / "acceptance.json").exists():
        raise ValueError("Refusing to replace previous installation acceptance")
    local_appdata = Path(os.environ["LOCALAPPDATA"]).resolve()
    assert local_appdata == Path("C:/Users/MIRAC/AppData/Local").resolve(), "Use the verified real-user environment; sandbox AppData is not an installation target."
    target = local_appdata / "Programs/SpatialPredictionStudio"
    installed = target / "versions" / build["build_id"]
    assert not (installed / "deployment-record.json").exists(), "This release is already installed; do not overwrite it."
    output.mkdir(parents=True, exist_ok=True)
    clients = "codex,chatgpt_desktop,antigravity,claude_desktop,claude_code,opencode,pycharm"
    command = [str(exe), "/VERYSILENT", "/SUPPRESSMSGBOXES", "/NORESTART", "/SP-", "/DIR=" + str(target),
               "/COMPONENTS=core,reference_r,tabpfn,mcp", "/TASKS=" + clients, "/ACCEPTTABPFNLICENSE=yes", "/LOG=" + str(output / "setup.log")]
    started = time.monotonic()
    record = {"status":"installing", "installer_sha256":build["sha256"], "installation_root":str(installed), "build_id":build["build_id"],
              "build_record":file_record(args.build_record), "staged_acceptance":file_record(args.staged_acceptance)}
    path = output / "acceptance.json"
    atomic_record(path, record)
    with (output / "setup.stdout.log").open("wb") as log:
        result = subprocess.run(command,stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
    record.update(exit_code=result.returncode,seconds=time.monotonic()-started)
    if result.returncode:
        record["status"]="failed";atomic_record(path, record)
        raise RuntimeError("Setup failed; inspect the recorded setup log.")
    try:
        deployment = read(installed / "deployment-record.json")
        assert deployment["status"] == "installed" and not deployment["staging"]
        assert Path(deployment["installation_root"]).resolve() == installed.resolve()
        assert sha(Path(deployment["addin_path"])) == deployment["addin_sha256"]
        assert deployment["source_addin_sha256"] == SOURCE_ADDIN_SHA256
        assert sha(installed / "payload-manifest.json") == deployment["payload_manifest_sha256"] == build["manifest_sha256"]
        readback = output / "installed-readback.json"
        with readback.open("wb") as log:
            verify = subprocess.run([str(installed/"runtime-d/Scripts/python.exe"),"-B","-X","utf8",str(installed/"installer/deploy.py"),"--root",str(installed),"--verify-installed"],stdout=log,stderr=subprocess.STDOUT,creationflags=subprocess.CREATE_NO_WINDOW)
        assert verify.returncode == 0
        checked = read(readback)
        assert checked["status"] == "installed_files_verified" and checked["files"] == deployment["verified_files"]
        record.update(status="passed",deployment_record=str(installed/"deployment-record.json"),addin_sha256=deployment["addin_sha256"],verified_files=deployment["verified_files"],
                      installed_readback=file_record(readback), payload_manifest_sha256=build["manifest_sha256"],
                      client_registration=[{"client":c["client"],"status":c["status"]} for c in deployment["mcp_registration"]["clients"]])
    except Exception as error:
        record.update(status="failed",verification_error=type(error).__name__ + ": " + str(error))
        atomic_record(path, record)
        raise
    atomic_record(path, record)
    print(json.dumps(record,ensure_ascii=False))

if __name__ == "__main__": main()
