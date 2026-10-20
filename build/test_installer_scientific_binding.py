"""Small synthetic evidence fixtures; never execute an installer or model."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest

from finalize_delivery_artifacts import sha, verify_installer_acceptance
from install_final_release import SOURCE_ADDIN_SHA256, parse_args, verify_install_inputs


class InstallerBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.old = self.root / "versions/0.5.0-old"
        self.new = self.root / "versions/0.5.0-new"
        self.old.mkdir(parents=True); self.new.mkdir(parents=True)
        self.paths = {name: self.root / (name + ".json") for name in ("build", "staged", "installed")}
        self.installer = self.root / "setup.exe"; self.installer.write_bytes(b"new installer")
        self.registered = self.root / "registered.esriAddIn"; self.registered.write_bytes(b"relocated add-in")
        files = {}
        for name in ("backend/sps/evaluation.py", "runtime-d/Scripts/python.exe", "runtime-mcp/python.exe",
                     "reference-r/bin/R.exe", "models/tabpfn/model.ckpt", "package/SpatialPredictionStudio.esriAddIn"):
            content = ("unchanged " + name).encode()
            for folder in (self.old, self.new):
                p = folder / name; p.parent.mkdir(parents=True, exist_ok=True); p.write_bytes(content)
            files[name] = {"bytes": len(content), "sha256": sha(self.old / name), "component": "core"}
        self.addin_sha = files["package/SpatialPredictionStudio.esriAddIn"]["sha256"]
        for folder in (self.old, self.new):
            self.write(folder / "payload-manifest.json", {"schema_version": 1, "product": "SpatialPredictionStudio",
                "source_addin_sha256": self.addin_sha, "files": deepcopy(files)})
            self.write(folder / "installed-files.json", {"schema_version": 1, "product": "SpatialPredictionStudio",
                "files": {name: item["sha256"] for name, item in files.items()}})
        self.old_deployment = self.deployment(self.old)
        self.write(self.old / "deployment-record.json", self.old_deployment)
        self.science = {"status": "passed", "run_id": "old_real_run", "installation_root": str(self.old),
            "source_addin_sha256": self.addin_sha,
            "deployment_record": {"path": str(self.old / "deployment-record.json"), "sha256": sha(self.old / "deployment-record.json")},
            "installed_backend_files_verified": [{"path": str(self.old / "backend/sps/evaluation.py"),
                "sha256": files["backend/sps/evaluation.py"]["sha256"]}]}
        self.rebind_new()

    def write(self, path, value):
        path.write_text(json.dumps(value), encoding="utf-8")

    def deployment(self, folder):
        return {"status": "installed", "staging": False, "installation_root": str(folder),
            "source_addin_sha256": self.addin_sha, "payload_manifest_sha256": sha(folder / "payload-manifest.json"),
            "installed_file_manifest_sha256": sha(folder / "installed-files.json"),
            "addin_path": str(self.registered), "addin_sha256": sha(self.registered),
            "verified_files": len(json.loads((folder / "installed-files.json").read_text())["files"]),
            "runtime_probe": {"status": "passed"}}

    def rebind_new(self):
        manifest = json.loads((self.new / "payload-manifest.json").read_text())
        manifest["revision"] = {"parent_manifest_sha256": sha(self.old / "payload-manifest.json")}
        self.write(self.new / "payload-manifest.json", manifest)
        deployed = self.deployment(self.new); self.write(self.new / "deployment-record.json", deployed)
        build = {"installer": str(self.installer), "sha256": sha(self.installer), "bytes": self.installer.stat().st_size,
                 "build_id": self.new.name, "manifest_sha256": sha(self.new / "payload-manifest.json")}
        staged = {"status": "passed", "installer_sha256": build["sha256"], "build_id": build["build_id"],
            "install": {"exit_code": 0}, "uninstall": {"exit_code": 0},
            "deployment": {**deployed, "staging": True, "installation_root": str(self.root / "staging")},
            "installed_readback": {"exit_code": 0},
            "addin_member_readback": {"status": "passed", "native_and_backend_members_identical": True}}
        installed = {"status": "passed", "exit_code": 0, "installer_sha256": build["sha256"],
            "build_id": build["build_id"], "installation_root": str(self.new),
            "deployment_record": str(self.new / "deployment-record.json"),
            "addin_sha256": sha(self.registered), "verified_files": deployed["verified_files"]}
        readback = self.root / "installed-readback.json"
        self.write(readback, {"status": "installed_files_verified", "files": deployed["verified_files"], "writes": False})
        installed["installed_readback"] = {"path": str(readback), "sha256": sha(readback)}
        for name, value in (("build", build), ("staged", staged), ("installed", installed)):
            self.write(self.paths[name], value)

    def verify(self):
        return verify_installer_acceptance(self.paths["build"], self.paths["staged"], self.paths["installed"], self.science)

    def test_new_install_preserves_science_as_historical_evidence(self):
        before = deepcopy(self.science)
        result = self.verify()
        self.assertEqual(self.science, before)
        self.assertEqual(result["science_installation_root"], str(self.old.resolve()))
        self.assertEqual(result["delivery_installation_root"], str(self.new.resolve()))
        self.assertFalse(result["new_scientific_run_claimed"])
        self.assertEqual(result["scientific_payload_file_count"], 6)

    def test_changed_runtime_manifest_is_rejected_even_when_new_acceptance_matches(self):
        manifest = json.loads((self.new / "payload-manifest.json").read_text())
        manifest["files"]["runtime-d/Scripts/python.exe"]["sha256"] = "0" * 64
        self.write(self.new / "payload-manifest.json", manifest); self.rebind_new()
        with self.assertRaisesRegex(ValueError, "Scientific payload"):
            self.verify()

    def test_missing_model_or_added_backend_member_is_rejected(self):
        for action in ("remove", "add"):
            with self.subTest(action=action):
                original = (self.new / "payload-manifest.json").read_bytes()
                manifest = json.loads(original)
                if action == "remove": del manifest["files"]["models/tabpfn/model.ckpt"]
                else: manifest["files"]["backend/new.py"] = {"bytes": 0, "sha256": "0" * 64, "component": "core"}
                self.write(self.new / "payload-manifest.json", manifest); self.rebind_new()
                with self.assertRaisesRegex(ValueError, "Scientific payload"): self.verify()
                (self.new / "payload-manifest.json").write_bytes(original)

    def test_stale_or_failed_new_acceptance_cannot_pass(self):
        for key, value in (("installer_sha256", "0" * 64), ("status", "running"), ("exit_code", 20), ("build_id", "old")):
            with self.subTest(key=key):
                self.rebind_new(); data = json.loads(self.paths["installed"].read_text()); data[key] = value
                self.write(self.paths["installed"], data)
                with self.assertRaises(ValueError): self.verify()

    def test_unbound_historical_deployment_is_rejected(self):
        data = json.loads((self.old / "deployment-record.json").read_text()); data["source_addin_sha256"] = "0" * 64
        self.write(self.old / "deployment-record.json", data)
        with self.assertRaisesRegex(ValueError, "Historical deployment"): self.verify()

    def test_changed_installed_backend_or_registered_addin_is_rejected(self):
        for path in (self.new / "backend/sps/evaluation.py", self.registered):
            with self.subTest(path=str(path)):
                original = path.read_bytes(); path.write_bytes(b"changed")
                with self.assertRaises(ValueError): self.verify()
                path.write_bytes(original)

    def test_wrong_manifest_parent_is_rejected(self):
        manifest = json.loads((self.new / "payload-manifest.json").read_text())
        manifest["revision"]["parent_manifest_sha256"] = "0" * 64
        self.write(self.new / "payload-manifest.json", manifest)
        # Rebind only the legitimate new artifact and deployment hashes.
        deployed = self.deployment(self.new); self.write(self.new / "deployment-record.json", deployed)
        for name in ("build", "staged"):
            data = json.loads(self.paths[name].read_text())
            if name == "build": data["manifest_sha256"] = deployed["payload_manifest_sha256"]
            else: data["deployment"]["payload_manifest_sha256"] = deployed["payload_manifest_sha256"]
            self.write(self.paths[name], data)
        with self.assertRaisesRegex(ValueError, "parent manifest"): self.verify()

    def test_new_install_requires_bound_full_readback(self):
        original = json.loads(self.paths["installed"].read_text())
        for action in ("missing", "changed", "incomplete"):
            with self.subTest(action=action):
                self.rebind_new()
                data = deepcopy(original)
                if action == "missing": del data["installed_readback"]
                elif action == "changed": data["installed_readback"]["sha256"] = "0" * 64
                else:
                    p = Path(data["installed_readback"]["path"])
                    self.write(p, {"status": "installed_files_verified", "files": 1})
                    data["installed_readback"]["sha256"] = sha(p)
                self.write(self.paths["installed"], data)
                with self.assertRaises(ValueError): self.verify()

    def install_fixture(self):
        build = json.loads(self.paths["build"].read_text()); build["build_id"] = "0.5.0-123456abcdef"
        self.write(self.paths["build"], build)
        staged = json.loads(self.paths["staged"].read_text()); staged["build_id"] = build["build_id"]
        staged["deployment"]["source_addin_sha256"] = SOURCE_ADDIN_SHA256
        self.write(self.paths["staged"], staged)
        return build, staged

    def test_explicit_install_paths_are_parsed_without_execution(self):
        args = parse_args(["--build-record", str(self.paths["build"]), "--staged-acceptance", str(self.paths["staged"]),
                           "--output-dir", str(self.root / "new-evidence")])
        self.assertEqual(args.build_record, self.paths["build"])
        self.assertEqual(args.output_dir, self.root / "new-evidence")
        self.install_fixture()
        build, _, exe = verify_install_inputs(args.build_record, args.staged_acceptance)
        self.assertEqual(exe, self.installer)
        self.assertEqual(build["build_id"], "0.5.0-123456abcdef")

    def test_install_preconditions_reject_failed_stale_and_unsafe_evidence(self):
        for action in ("failed", "uninstall", "hash", "payload", "traversal"):
            with self.subTest(action=action):
                build, staged = self.install_fixture()
                if action == "failed": staged["status"] = "running"
                elif action == "uninstall": staged["uninstall"]["exit_code"] = 20
                elif action == "hash": staged["installer_sha256"] = "0" * 64
                elif action == "payload": staged["deployment"]["payload_manifest_sha256"] = "0" * 64
                else: build["build_id"] = "../outside"
                self.write(self.paths["build"], build); self.write(self.paths["staged"], staged)
                with self.assertRaises(ValueError): verify_install_inputs(self.paths["build"], self.paths["staged"])
                self.rebind_new()


if __name__ == "__main__":
    unittest.main(verbosity=2)
