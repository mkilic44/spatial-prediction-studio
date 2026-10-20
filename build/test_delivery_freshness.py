"""Negative checks for stale or incomplete final report archives."""
import json
from pathlib import Path
import tempfile
import unittest
import zipfile

from finalize_delivery_artifacts import sha, verify_report


class DeliveryFreshnessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.addCleanup(self.temporary.cleanup)
        self.root = Path(self.temporary.name)
        self.out = self.root / "release"
        self.out.mkdir()
        for name in ["TESLIM_RAPORU.md", "AYRINTILI_RAPOR.html", "evidence.json", "table.csv"]:
            (self.out / name).write_text("original", encoding="utf-8")
        render = {"html_sha256": sha(self.out / "AYRINTILI_RAPOR.html"),
                  "sources": {"release/TESLIM_RAPORU.md": sha(self.out / "TESLIM_RAPORU.md")},
                  "portable_assets": {"evidence.json": sha(self.out / "evidence.json")}}
        (self.out / "report-render-readback.json").write_text(json.dumps(render), encoding="utf-8")
        files = {p.name: {"sha256": sha(p), "bytes": p.stat().st_size} for p in self.out.iterdir()}
        with zipfile.ZipFile(self.out / "Ayrintili_Rapor_ve_Sonuclar.zip", "w") as archive:
            for name in files:
                archive.write(self.out / name, name)
            archive.writestr("report-package-manifest.json", json.dumps({"files": files}))

    def test_matching_delivery_passes(self):
        verify_report(self.root, self.out)

    def test_changed_prose_source_is_rejected(self):
        (self.out / "TESLIM_RAPORU.md").write_text("revision", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "Rendered source changed"):
            verify_report(self.root, self.out)

    def test_changed_copied_evidence_is_rejected(self):
        (self.out / "evidence.json").write_text("revision", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "Report evidence changed"):
            verify_report(self.root, self.out)

    def test_stale_zip_with_same_sized_current_file_is_rejected(self):
        (self.out / "table.csv").write_text("revision", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "Report file changed"):
            verify_report(self.root, self.out)

    def test_new_unarchived_result_is_rejected(self):
        (self.out / "new-table.csv").write_text("new data", encoding="utf-8")
        with self.assertRaisesRegex(AssertionError, "membership is stale"):
            verify_report(self.root, self.out)

    def test_excluded_final_checksum_does_not_create_self_dependency(self):
        (self.out / "SHA256SUMS.txt").write_text("final checksum", encoding="utf-8")
        (self.out / "final-artifacts.json").write_text("{}", encoding="utf-8")
        verify_report(self.root, self.out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
