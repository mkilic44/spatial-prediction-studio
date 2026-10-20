"""Read-only scientific carry-forward proof for the accepted Store-profile installer.

No fitting, installation, finalization or release packaging is performed.
Only a new evidence JSON under build/installed-store-release is created.
"""
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import zipfile

from finalize_delivery_artifacts import (
    SCIENTIFIC_ROOTS, file_record, read, require, sha, verify_installer_acceptance,
)

ROOT = Path(__file__).resolve().parents[1]


def output_hashes(directory):
    return {p.relative_to(directory).as_posix(): sha(p) for p in sorted(directory.rglob("*")) if p.is_file()}


def archive_members(path):
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        require(len(names) == len(set(names)), "Duplicate add-in ZIP member")
        return {name: archive.read(name) for name in names}


def verify():
    science_path = ROOT / "build/installed-release/scientific-readback.json"
    scientific_before = file_record(science_path)
    science = read(science_path)
    old_output = Path(science["output_directory"]).resolve(strict=True)
    old_outputs_before = output_hashes(old_output)
    build_path = ROOT / "installer/dist-claude-store/installer-build-record.json"
    staged_path = ROOT.parent / "sps-cs/setup-acceptance.json"
    actual_path = ROOT / "build/installed-store-release/acceptance.json"
    require(read(actual_path)["status"] == "passed", "Wait for actual installation acceptance")
    binding = verify_installer_acceptance(build_path, staged_path, actual_path, science)
    require(binding["scientific_payload_file_count"] == 47774, "Unexpected scientific member count")
    installed_root = Path(binding["delivery_installation_root"])
    ledger = read(installed_root / "installed-files.json")["files"]
    selected = {name: expected for name, expected in ledger.items() if name.startswith(SCIENTIFIC_ROOTS)}
    require(len(selected) == 47774, "Installed scientific member set differs")
    old_root = Path(science["installation_root"]).resolve()
    backend = []
    for entry in science["installed_backend_files_verified"]:
        relative = Path(entry["path"]).resolve().relative_to(old_root).as_posix()
        require(selected[relative] == entry["sha256"] == sha(installed_root / relative), "Backend differs from actual historical run")
        backend.append(file_record(installed_root / relative))
    require(len(backend) == 28, "Expected 28 actual-run backend records")

    package = installed_root / "package/SpatialPredictionStudio.esriAddIn"
    deployment = read(installed_root / "deployment-record.json")
    registered = Path(deployment["addin_path"])
    source_members, installed_members = archive_members(package), archive_members(registered)
    require(set(source_members) == set(installed_members), "Registered add-in member set differs")
    changed = sorted(name for name in source_members if source_members[name] != installed_members[name])
    require(changed == ["Install/build-manifest.json", "Install/runtime.json"], "Registered add-in changes exceed relocation")
    manifest = json.loads(installed_members["Install/build-manifest.json"])
    require(set(installed_members) == set(manifest["members"]) | {"Install/build-manifest.json"}, "Registered add-in manifest omits members")
    for name, expected in manifest["members"].items():
        require(hashlib.sha256(installed_members[name]).hexdigest() == expected, "Registered member checksum differs: " + name)
    require(manifest["installation"]["source_addin_sha256"] == science["source_addin_sha256"] == sha(package),
            "Registered add-in is not derived from accepted scientific source")
    runtime = json.loads(installed_members["Install/runtime.json"])
    require(all(Path(value).resolve(strict=True).is_relative_to(installed_root) for value in runtime.values()),
            "Registered runtime points outside accepted installation")
    require(file_record(science_path) == scientific_before and output_hashes(old_output) == old_outputs_before,
            "Historical scientific acceptance or outputs changed during readback")
    binding.update({
        "schema_version": 1, "created_utc": datetime.now(timezone.utc).isoformat(),
        "scientific_acceptance": scientific_before,
        "installed_scientific_members_bound": len(selected),
        "installed_scientific_member_ledger_sha256": hashlib.sha256(json.dumps(
            selected, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest(),
        "installed_file_readback": read(actual_path)["installed_readback"],
        "installed_member_hash_scope": "All 47774 packaged scientific member names, sizes and SHA-256 records match the historical scientific payload. The complete installed ledger is bound to the already completed 50287-file readback through its accepted checksum. This helper rehashes 28 audited backend files and the add-in packages; it does not repeat a full environment scan. Path relocation is not described as byte-identical installed runtimes.",
        "backend_files_reverified_against_actual_run": backend,
        "registered_addin_member_check": {"status": "passed", "source_package": file_record(package),
            "registered_package": file_record(registered), "changed_members": changed,
            "native_and_backend_bytes_identical": True, "runtime_paths_within_new_installation": True},
        "historical_outputs_unchanged": {"status": "passed", "directory": str(old_output), "files": len(old_outputs_before),
            "scope": "SHA-256 before and after this read-only verification, including the original scientific acceptance JSON."},
        "new_fits_started": 0, "failed_checks": [],
        "limitations": ["No new ArcMap scientific run was performed by this verification.",
            "The historical synthetic Ridge outputs retain their original run and installation identities.",
            "Client connection behavior is verified separately and is not inferred from scientific payload equality."],
    })
    return binding


def main():
    target = ROOT / "build/installed-store-release/scientific-binding.json"
    require(not target.exists(), "Refusing to replace previous binding evidence")
    result = verify()
    partial = target.with_suffix(".json.partial")
    require(not partial.exists(), "Unexpected partial binding evidence")
    partial.write_text(json.dumps(result, ensure_ascii=False, allow_nan=False, indent=2) + "\n", encoding="utf-8")
    require(read(partial) == result, "Binding evidence readback differs")
    partial.replace(target)
    print(json.dumps({"status": result["status"], "output": str(target), "sha256": sha(target),
        "scientific_manifest_members_bound": result["installed_scientific_members_bound"],
        "backend_files": len(result["backend_files_reverified_against_actual_run"]),
        "new_scientific_run_claimed": result["new_scientific_run_claimed"]}, ensure_ascii=False))


if __name__ == "__main__":
    main()
