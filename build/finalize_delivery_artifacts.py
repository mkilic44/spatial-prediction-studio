"""Copy the accepted installer and record final downloadable artifact hashes."""
import argparse
import hashlib
import json
from pathlib import Path
import shutil
import sys
import zipfile

from package_delivery_report import EXCLUDE
from prepare_delivery_reports import verify_scientific_sources

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"
SCIENTIFIC_ROOTS = ("backend/", "runtime-d/", "runtime-mcp/", "reference-r/", "models/", "package/")


def read(path):
    return json.loads(path.read_text(encoding="utf-8-sig"))


def sha(path):
    with path.open("rb") as stream:
        return hashlib.file_digest(stream, "sha256").hexdigest()


def require(condition, message):
    if not condition:
        raise ValueError(message)


def file_record(path):
    path = Path(path).resolve(strict=True)
    return {"path": str(path), "sha256": sha(path), "bytes": path.stat().st_size}


def verify_installer_acceptance(build_path, staged_path, installed_path, science):
    """Bind a separately accepted installer to unchanged historical science bytes.

    Complete selected payload member sets are compared, including size and SHA.
    The new installed-files ledger is bound to the deployment that was verified
    by the actual installer acceptance. Audited backend bytes and the registered
    add-in are independently read again. No new scientific execution is claimed.
    """
    build, staged, installed = (read(Path(p)) for p in (build_path, staged_path, installed_path))
    require(staged.get("status") == installed.get("status") == science.get("status") == "passed",
            "Installer or scientific acceptance has not passed")
    require(staged["install"]["exit_code"] == staged["uninstall"]["exit_code"] == installed["exit_code"] == 0,
            "Installation or staged removal failed")
    require(staged["installed_readback"]["exit_code"] == 0, "Staged file readback failed")
    require(build["build_id"] == staged["build_id"] == installed["build_id"], "Installer build identities differ")
    source = Path(build["installer"]).resolve(strict=True)
    require(sha(source) == build["sha256"] == staged["installer_sha256"] == installed["installer_sha256"],
            "Installer checksum differs from accepted installer")
    require(source.stat().st_size == build["bytes"], "Installer size differs")

    old_root = Path(science["installation_root"]).resolve(strict=True)
    new_root = Path(installed["installation_root"]).resolve(strict=True)
    old_record_path = Path(science["deployment_record"]["path"]).resolve(strict=True)
    require(old_record_path == old_root / "deployment-record.json" and
            sha(old_record_path) == science["deployment_record"]["sha256"], "Historical deployment changed")
    old_deployment = read(old_record_path)
    new_record_path = Path(installed["deployment_record"]).resolve(strict=True)
    require(new_record_path == new_root / "deployment-record.json", "Actual deployment record is outside its installation")
    new_deployment = read(new_record_path)
    for root, deployed in ((old_root, old_deployment), (new_root, new_deployment)):
        require(deployed["status"] == "installed" and deployed["staging"] is False and
                Path(deployed["installation_root"]).resolve() == root, "Actual installation identity differs")
    require(staged["deployment"]["staging"] is True, "Staged acceptance is not an isolated installation")
    require(new_root.name == installed["build_id"], "Actual installation directory does not match the accepted build")
    expected_addin = science["source_addin_sha256"]
    require(old_deployment["source_addin_sha256"] == new_deployment["source_addin_sha256"] ==
            staged["deployment"]["source_addin_sha256"] == expected_addin, "Scientific source add-in changed")
    old_manifest_path, new_manifest_path = (r / "payload-manifest.json" for r in (old_root, new_root))
    old_digest, new_digest = sha(old_manifest_path), sha(new_manifest_path)
    require(old_digest == old_deployment["payload_manifest_sha256"], "Historical payload manifest changed")
    require(new_digest == build["manifest_sha256"] == new_deployment["payload_manifest_sha256"] ==
            staged["deployment"]["payload_manifest_sha256"], "New payload manifest is not bound to all acceptances")
    old_manifest, new_manifest = read(old_manifest_path), read(new_manifest_path)
    for manifest in (old_manifest, new_manifest):
        require(manifest["schema_version"] == 1 and manifest["product"] == "SpatialPredictionStudio" and
                manifest["source_addin_sha256"] == expected_addin, "Payload product or source add-in differs")
    if new_root != old_root:
        require(new_manifest.get("revision", {}).get("parent_manifest_sha256") == old_digest,
                "New payload parent manifest does not identify the scientific installation")
    components = {}
    for prefix in SCIENTIFIC_ROOTS:
        old_files = {k: v for k, v in old_manifest["files"].items() if k.startswith(prefix)}
        new_files = {k: v for k, v in new_manifest["files"].items() if k.startswith(prefix)}
        require(bool(old_files) and old_files == new_files, "Scientific payload file set, size or checksum changed: " + prefix)
        components[prefix] = {"files": len(new_files), "member_records_sha256": hashlib.sha256(
            json.dumps(new_files, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()}
    require(new_manifest["files"]["package/SpatialPredictionStudio.esriAddIn"]["sha256"] == expected_addin,
            "Payload add-in member differs from scientific source")
    installed_files_path = new_root / "installed-files.json"
    require(sha(installed_files_path) == new_deployment["installed_file_manifest_sha256"], "Installed file ledger changed")
    installed_files = read(installed_files_path)["files"]
    require(set(installed_files) == set(new_manifest["files"]) and len(installed_files) ==
            installed["verified_files"] == new_deployment["verified_files"], "Installed file set differs from complete payload")
    require(new_deployment["runtime_probe"]["status"] == "passed", "Installed runtime probe did not pass")
    if "installed_readback" in installed:
        item = installed["installed_readback"]; path = Path(item["path"]).resolve(strict=True)
        require(sha(path) == item["sha256"], "Actual installed readback record changed")
        readback = read(path)
        require(readback["status"] == "installed_files_verified" and readback["files"] == len(installed_files),
                "Actual installed file readback did not verify the full payload")
    else:
        require(new_root == old_root, "New installation requires a bound installed file readback")
    for item in science["installed_backend_files_verified"]:
        relative = Path(item["path"]).resolve().relative_to(old_root).as_posix()
        require(relative.startswith("backend/") and installed_files[relative] ==
                new_manifest["files"][relative]["sha256"] == item["sha256"] == sha(new_root / relative),
                "Installed scientific backend differs from the accepted run")
    for relative in ("package/SpatialPredictionStudio.esriAddIn", "runtime-d/Scripts/python.exe"):
        require(sha(new_root / relative) == installed_files[relative] == new_manifest["files"][relative]["sha256"],
                "Installed source add-in or Python executable differs")
    registered = Path(new_deployment["addin_path"]).resolve(strict=True)
    require(sha(registered) == new_deployment["addin_sha256"] == installed["addin_sha256"], "Registered add-in changed")
    return {"status": "passed", "science_run_id": science["run_id"],
            "science_installation_root": str(old_root), "delivery_installation_root": str(new_root),
            "same_installation": old_root == new_root, "new_scientific_run_claimed": False,
            "scope": "Historical scientific output retained; unchanged scientific payload member sets and source add-in are bound to the separately accepted delivery installation.",
            "source_addin_sha256": expected_addin, "scientific_payload_file_count": sum(v["files"] for v in components.values()),
            "scientific_payload_components": components, "historical_payload_manifest": file_record(old_manifest_path),
            "delivery_payload_manifest": file_record(new_manifest_path), "installed_file_ledger": file_record(installed_files_path),
            "build_record": file_record(build_path), "staged_acceptance": file_record(staged_path),
            "actual_acceptance": file_record(installed_path), "actual_deployment": file_record(new_record_path),
            "registered_addin": file_record(registered)}


def verify_scientific_delivery(root, out, science, delivery):
    if delivery.get("status") != "verified" or delivery["example_run_id"] != science["run_id"]:
        raise ValueError("Delivered scientific run is not the accepted run")
    if delivery["source_addin_sha256"] != science["source_addin_sha256"]:
        raise ValueError("Delivered scientific source add-in differs from acceptance")
    acceptance = root / "build/installed-release/scientific-readback.json"
    if delivery.get("scientific_acceptance_sha256") != sha(acceptance) or read(acceptance) != science:
        raise ValueError("Scientific acceptance record changed after delivery preparation")
    expected = verify_scientific_sources(root, science)
    recorded = {}
    for item in delivery["artifacts"]:
        path = Path(item["path"]).resolve(strict=True)
        if path.parent != out.resolve() or path.name in recorded:
            raise ValueError("Delivery artifact path is outside the release or duplicated")
        if path.stat().st_size != item["bytes"] or sha(path) != item["sha256"]:
            raise ValueError("Recorded delivery artifact changed: " + path.name)
        recorded[path.name] = item
    for name, item in expected.items():
        if name not in recorded or recorded[name]["sha256"] != item["sha256"]:
            raise ValueError("Delivered scientific output differs from acceptance: " + name)


def verify_report(root, out):
    render = read(out / "report-render-readback.json")
    assert sha(out / "AYRINTILI_RAPOR.html") == render["html_sha256"]
    for name, expected in render["sources"].items():
        path = (root / name).resolve(strict=True)
        assert path.is_relative_to(root.resolve())
        assert sha(path) == expected, f"Rendered source changed: {name}"
    for name, expected in render["portable_assets"].items():
        path = (out / name).resolve(strict=True)
        assert path.is_relative_to(out.resolve())
        assert sha(path) == expected, f"Report evidence changed: {name}"
    current = {p.relative_to(out).as_posix(): p for p in out.rglob("*")
               if p.is_file() and p.name not in EXCLUDE and p.suffix != ".partial"}
    with zipfile.ZipFile(out / "Ayrintili_Rapor_ve_Sonuclar.zip") as archive:
        names = archive.namelist()
        assert len(names) == len(set(names)), "Duplicate report ZIP member"
        manifest = json.loads(archive.read("report-package-manifest.json"))["files"]
        assert set(names) == set(manifest) | {"report-package-manifest.json"}
        assert set(manifest) == set(current), "Report ZIP membership is stale"
        for name, expected in manifest.items():
            assert current[name].stat().st_size == expected["bytes"], f"Report file size changed: {name}"
            actual = sha(current[name])
            assert actual == expected["sha256"], f"Report file changed: {name}"
            with archive.open(name) as stream:
                assert hashlib.file_digest(stream, "sha256").hexdigest() == actual, f"Report ZIP differs: {name}"


def main():
    require(sys.flags.optimize == 0, "Do not disable final delivery assertions with Python -O")
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--build-record", type=Path, default=ROOT / "installer/dist/installer-build-record.json")
    parser.add_argument("--staged-acceptance", type=Path, default=ROOT.parent / "sps-ç2/setup-acceptance.json")
    parser.add_argument("--installed-acceptance", type=Path, default=ROOT / "build/installed-release/acceptance.json")
    args = parser.parse_args()
    build = read(args.build_record)
    installed = read(args.installed_acceptance)
    science = read(ROOT / "build/installed-release/scientific-readback.json")
    report = read(OUT / "report-package-readback.json")
    assert report["status"] == "passed"
    binding = verify_installer_acceptance(args.build_record, args.staged_acceptance, args.installed_acceptance, science)
    delivery = read(OUT / "scientific-delivery-readback.json")
    verify_scientific_delivery(ROOT, OUT, science, delivery)
    source = Path(build["installer"]).resolve(strict=True)
    expected = build["sha256"]
    assert expected == installed["installer_sha256"] == sha(source)
    target = OUT / "SpatialPredictionStudio-0.5.0-Setup.exe"
    if target.exists():
        assert sha(target) == expected, "Existing release installer differs; review before replacing."
    else:
        temporary = target.with_suffix(".partial")
        shutil.copy2(source, temporary)
        assert sha(temporary) == expected
        temporary.replace(target)
    assert sha(OUT / "Ayrintili_Rapor_ve_Sonuclar.zip") == report["sha256"]
    verify_report(ROOT, OUT)
    artifacts = {}
    for path in sorted(OUT.iterdir()):
        if path.is_file() and path.suffix.lower() in {".exe", ".zip", ".html", ".md"}:
            artifacts[path.name] = {"bytes": path.stat().st_size, "sha256": sha(path)}
    (OUT / "SHA256SUMS.txt").write_text("".join(f'{value["sha256"]}  {name}\n' for name, value in artifacts.items()), encoding="utf-8")
    record = {"status": "passed", "build_id": build["build_id"], "artifacts": artifacts,
              "installation_root": installed["installation_root"], "scientific_acceptance_binding": binding}
    (OUT / "final-artifacts.json").write_text(json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(record, ensure_ascii=False))


if __name__ == "__main__":
    main()
