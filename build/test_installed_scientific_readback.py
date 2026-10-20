"""Small meaningful negative tests; no actual installed-run acceptance is emitted."""
from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from verify_installed_scientific_run import equivalent, normalize_settings, verify, verify_archive, sha


class ReadbackTests(unittest.TestCase):
    def test_numeric_roundoff_is_tolerated(self):
        equivalent({"predicted": [1.0], "status": "complete"}, {"predicted": [1.0 + 1e-13], "status": "complete"})

    def test_prediction_change_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "numerical value"):
            equivalent({"predicted": [1.0]}, {"predicted": [1.001]})

    def test_missing_role_and_changed_status_are_rejected(self):
        for actual in ([{"sample_id": "D002", "status": "complete"}], [{"sample_id": "D001", "status": "failed"}], []):
            with self.subTest(actual=actual), self.assertRaises(ValueError):
                equivalent([{"sample_id": "D001", "status": "complete"}], actual)

    def test_boolean_is_not_a_numeric_metric(self):
        with self.assertRaises(ValueError):
            equivalent({"rmse": 1}, {"rmse": True})

    def test_settings_ignore_only_explicit_runtime_identity(self):
        old = {"run_id": "old", "samples": {"path": "old.csv", "target_field": "y"},
               "model": {"id": "ridge", "parameters": {"alpha": .1}}}
        new = deepcopy(old)
        new["run_id"] = "new"; new["samples"]["path"] = "new.csv"
        self.assertEqual(normalize_settings(old), normalize_settings(new))
        new["model"]["parameters"]["alpha"] = 1
        with self.assertRaises(ValueError):
            equivalent(normalize_settings(old), normalize_settings(new))
        self.assertEqual(old["samples"]["path"], "old.csv")

    def test_missing_actual_run_cannot_pass(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            with self.assertRaises(FileNotFoundError):
                verify(Path(folder) / "never-ran.json", Path(folder))

    def test_archive_member_tampering_is_rejected(self):
        with tempfile.TemporaryDirectory(dir=Path(__file__).resolve().parent) as folder:
            output = Path(folder)
            request = output / "request.snapshot.json"; request.write_text("{}")
            data = output / "table.csv"; data.write_text("original")
            result = {"run_id": "fixture", "status": "completed", "outputs": [
                {"path": "table.csv", "kind": "table", "sha256": sha(data)},
                {"path": "study_package.spsstudy.zip", "kind": "study_package", "sha256": "not_used"}]}
            snapshot = json.dumps({**result, "outputs": result["outputs"][:1]}).encode()
            manifest = {"schema_version": 1, "run_id": "fixture", "members": {
                "table.csv": sha(data), "request.snapshot.json": sha(request),
                "result.snapshot.json": __import__("hashlib").sha256(snapshot).hexdigest()}}
            with zipfile.ZipFile(output / "study_package.spsstudy.zip", "w") as archive:
                archive.writestr("table.csv", "modified")
                archive.write(request, "request.snapshot.json")
                archive.writestr("result.snapshot.json", snapshot)
                archive.writestr("study_manifest.json", json.dumps(manifest))
            with self.assertRaisesRegex(ValueError, "member SHA"):
                verify_archive(output, result)


if __name__ == "__main__":
    unittest.main(verbosity=2)
