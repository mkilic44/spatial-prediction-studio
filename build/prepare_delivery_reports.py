"""Preserve the verified scientific outputs as a portable local delivery bundle."""
import argparse
import hashlib
import json
import shutil
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"
SCIENTIFIC_BUNDLES = (
    ("study_package", "study_package.spsstudy.zip", "Ornek_Calisma_Tum_Sonuclar.spsstudy.zip"),
    ("model_bundle", "model_package.spsmodel.zip", "Ornek_Kaydedilmis_Model.spsmodel.zip"),
)

def digest(path):
    h = hashlib.sha256()
    with path.open("rb") as stream:
        for data in iter(lambda: stream.read(1024 * 1024), b""):
            h.update(data)
    return h.hexdigest()

def copy_verified(source, target, expected_sha256=None):
    if expected_sha256 is not None and digest(source) != expected_sha256:
        raise ValueError("Accepted scientific output changed: " + str(source))
    shutil.copy2(source, target)
    copied_sha256 = digest(target)
    assert digest(source) == copied_sha256
    if expected_sha256 is not None and copied_sha256 != expected_sha256:
        raise ValueError("Accepted scientific output changed while copying: " + str(source))
    return {"path": str(target), "bytes": target.stat().st_size, "sha256": copied_sha256}


def verify_scientific_sources(root, accepted):
    """Bind the two promised deliverables to the independent accepted output hashes."""
    if accepted["status"] != "passed":
        raise ValueError("Scientific acceptance has not passed")
    source = Path(accepted["output_directory"]).resolve(strict=True)
    allowed = (root / "build/arcmap-evidence/installed").resolve()
    if not source.is_relative_to(allowed) or source.name != "output" or source.parent.name != accepted["run_id"]:
        raise ValueError("Scientific output directory does not belong to the accepted run")
    verified = {}
    for kind, original, delivered in SCIENTIFIC_BUNDLES:
        matches = [item for item in accepted["artifacts"] if item.get("kind") == kind]
        if len(matches) != 1 or matches[0].get("path") != original:
            raise ValueError("Missing, duplicated or mismatched accepted scientific output: " + kind)
        item = matches[0]
        path = (source / original).resolve(strict=True)
        if not path.is_relative_to(source) or digest(path) != item["sha256"]:
            raise ValueError("Accepted scientific output changed: " + original)
        verified[delivered] = {"source": path, "sha256": item["sha256"], "kind": kind}
    return verified

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer-report', type=Path, default=ROOT / 'installer/FINAL_ACCEPTANCE.md')
    parser.add_argument('--installer-evidence', type=Path, default=ROOT / 'installer/dist/SpatialPredictionStudio-0.5.0-installer-source-evidence.zip')
    args = parser.parse_args()
    for source in (args.installer_report, args.installer_evidence):
        if not source.resolve(strict=True).is_relative_to((ROOT / 'installer').resolve()):
            raise ValueError('Installer delivery source is outside the project installer directory')
    OUT.mkdir(exist_ok=True)
    records = []
    scientific_acceptance = ROOT / "build/installed-release/scientific-readback.json"
    accepted_bytes = scientific_acceptance.read_bytes()
    installed = json.loads(accepted_bytes.decode("utf-8-sig"))
    accepted_sha256 = hashlib.sha256(accepted_bytes).hexdigest()
    assert installed["status"] == "passed"
    assert installed["source_addin_sha256"] == "b8831e222582b334cfdd29e61b903ef907ee391f3a11bed09d76db4728bbef0c"
    for name, item in verify_scientific_sources(ROOT, installed).items():
        records.append(copy_verified(item["source"], OUT / name, item["sha256"]))
    for name in ("ASAMA_A_KABUL.md", "ASAMA_B_KABUL.md", "ASAMA_C_KABUL.md", "ASAMA_D_KABUL.md", "ASAMA_05_KABUL.md"):
        records.append(copy_verified(ROOT / name, OUT / name))
    records.append(copy_verified(ROOT / "docs/KURULUM_REHBERI.md", OUT / "KURULUM_REHBERI.md"))
    records.append(copy_verified(args.installer_report, OUT / "KURUCU_KABULU.md"))
    records.append(copy_verified(args.installer_evidence,
                                 OUT / "SpatialPredictionStudio-0.5.0-installer-source-evidence.zip"))
    selected = set()
    for job in ("20261017_220635_13f2d3a5", "20261017_221849_514e109f", "20261017_222534_d19a1948"):
        selected.update(p for p in (ROOT / "build/arcmap-evidence/d-runs" / job).rglob("*") if p.is_file())
    for relative in ("build/d-final-scientific-evidence.json", "build/mcp-scientific-readback.json", "build/verify_mcp_scientific.py", "build/create_d_acceptance_evidence.py", "ASAMA_D_KABUL.md", "ASAMA_05_KABUL.md"):
        selected.add(ROOT / relative)
    target = OUT / "Sinir_Ag_Modelleri_Tum_Sonuclar.zip"
    manifest = {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": digest(p)} for p in sorted(selected)}
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(selected): archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr("delivery-manifest.json", json.dumps({"scope": "Three prior synthetic ArcMap runs, retaining their separate candidate and validation scopes", "files": manifest}, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(target) as archive:
        assert archive.testzip() is None
        for name, expected in manifest.items():
            h = hashlib.sha256()
            with archive.open(name) as stream:
                for data in iter(lambda: stream.read(1024 * 1024), b""): h.update(data)
            assert h.hexdigest() == expected["sha256"]
    records.append({"path": str(target), "bytes": target.stat().st_size, "sha256": digest(target), "files": len(manifest)})
    if digest(scientific_acceptance) != accepted_sha256:
        raise ValueError("Scientific acceptance record changed while preparing delivery")
    result = {"status": "verified", "example_run_id": installed["run_id"],
              "scientific_acceptance_sha256": accepted_sha256,
              "source_addin_sha256": installed["source_addin_sha256"], "artifacts": records}
    (OUT / "scientific-delivery-readback.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))

if __name__ == "__main__": main()
