"""Reject delivery archives that no longer match the scientific acceptance."""
import hashlib
import json
from pathlib import Path
import shutil
import tempfile
import unittest
import zipfile

from prepare_delivery_reports import verify_scientific_sources
from finalize_delivery_artifacts import verify_scientific_delivery


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class ScientificDeliveryBindingTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory(prefix="delivery-binding-", dir=Path(__file__).parent)
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name).resolve()
        self.out = self.root / "release"
        self.out.mkdir()
        self.source = self.root / "build/arcmap-evidence/installed/mcp-runs/test-run/output"
        self.source.mkdir(parents=True)
        self.mapping = [
            ("model_bundle", "model_package.spsmodel.zip", "Ornek_Kaydedilmis_Model.spsmodel.zip"),
            ("study_package", "study_package.spsstudy.zip", "Ornek_Calisma_Tum_Sonuclar.spsstudy.zip"),
        ]
        self.science = {"status": "passed", "run_id": "test-run", "output_directory": str(self.source),
                        "source_addin_sha256": "b8831e222582b334cfdd29e61b903ef907ee391f3a11bed09d76db4728bbef0c", "artifacts": []}
        records = []
        for kind, name, delivered in self.mapping:
            with zipfile.ZipFile(self.source / name, "w") as archive:
                archive.writestr("accepted.txt", kind)
            source = self.source / name
            target = self.out / delivered
            shutil.copy2(source, target)
            self.science["artifacts"].append({"kind": kind, "path": name, "sha256": sha(source)})
            records.append({"path": str(target), "bytes": target.stat().st_size, "sha256": sha(target)})
        self.acceptance = self.root / "build/installed-release/scientific-readback.json"
        self.acceptance.parent.mkdir(parents=True)
        self.acceptance.write_text(json.dumps(self.science), encoding="utf-8")
        self.delivery = {"status": "verified", "example_run_id": "test-run",
                         "source_addin_sha256": self.science["source_addin_sha256"],
                         "scientific_acceptance_sha256": sha(self.acceptance), "artifacts": records}

    def test_matching_accepted_source_and_delivered_archives_pass(self):
        verify_scientific_sources(self.root, self.science)
        verify_scientific_delivery(self.root, self.out, self.science, self.delivery)

    def test_source_zip_changed_after_acceptance_is_rejected(self):
        for _, name, _ in self.mapping:
            with self.subTest(name=name):
                path = self.source / name
                original = path.read_bytes()
                path.write_bytes(original + b"changed-after-acceptance")
                with self.assertRaisesRegex(ValueError, "Accepted scientific output changed"):
                    verify_scientific_sources(self.root, self.science)
                path.write_bytes(original)

    def test_changed_delivered_zip_with_rewritten_delivery_hash_is_rejected(self):
        for _, _, name in self.mapping:
            with self.subTest(name=name):
                target = self.out / name
                original = target.read_bytes()
                target.write_bytes(original + b"new-delivery-with-stale-acceptance")
                item = next(x for x in self.delivery["artifacts"] if Path(x["path"]).name == name)
                item.update(bytes=target.stat().st_size, sha256=sha(target))
                with self.assertRaisesRegex(ValueError, "Delivered scientific output differs from acceptance"):
                    verify_scientific_delivery(self.root, self.out, self.science, self.delivery)
                target.write_bytes(original)
                item.update(bytes=target.stat().st_size, sha256=sha(target))

    def test_delivered_file_changed_after_recording_is_rejected(self):
        (self.out / self.mapping[0][2]).write_bytes(b"unrecorded-change")
        with self.assertRaisesRegex(ValueError, "Recorded delivery artifact changed"):
            verify_scientific_delivery(self.root, self.out, self.science, self.delivery)

    def test_wrong_output_kind_and_duplicate_accepted_output_are_rejected(self):
        original = list(self.science["artifacts"])
        self.science["artifacts"] = [{**original[0], "kind": "table"}, original[1]]
        with self.assertRaises(ValueError):
            verify_scientific_sources(self.root, self.science)
        self.science["artifacts"] = original + [dict(original[0])]
        with self.assertRaises(ValueError):
            verify_scientific_sources(self.root, self.science)

    def test_acceptance_record_changed_after_preparation_is_rejected(self):
        self.acceptance.write_text(json.dumps({**self.science, "changed": True}), encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "Scientific acceptance record changed"):
            verify_scientific_delivery(self.root, self.out, self.science, self.delivery)

    def test_missing_required_delivered_zip_is_rejected(self):
        self.delivery["artifacts"].pop()
        with self.assertRaises(ValueError):
            verify_scientific_delivery(self.root, self.out, self.science, self.delivery)


if __name__ == "__main__":
    unittest.main(verbosity=2)
