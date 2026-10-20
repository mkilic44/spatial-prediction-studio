"""Built artifact acceptance. Run after tools/build.ps1."""
import hashlib
import json
import struct
import unittest
import zipfile
from pathlib import Path
from xml.etree import ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]
PACKAGE = ROOT / "dist" / "SpatialPredictionStudio.esriAddIn"
NS = {"e": "http://schemas.esri.com/Desktop/AddIns"}

class PackageAcceptance(unittest.TestCase):
    def test_real_x86_addin_and_sources(self):
        self.assertTrue(PACKAGE.is_file(), "A real built .esriAddIn is required")
        with zipfile.ZipFile(PACKAGE) as package:
            self.assertIsNone(package.testzip())
            names = package.namelist()
            self.assertEqual(len(names), len(set(names)))
            self.assertFalse(any(".." in Path(n).parts for n in names))
            self.assertFalse(any(Path(n).name.lower().startswith("esri.") and n.endswith(".dll") for n in names))
            config = ET.fromstring(package.read("Config.xml"))
            addin = config.find("e:AddIn", NS)
            self.assertEqual(addin.get("library"), "SpatialPredictionStudio.dll")
            self.assertEqual(addin.get("namespace"), "SpatialPredictionStudio")
            dock = config.find(".//e:DockableWindow", NS)
            self.assertEqual(dock.get("id"), "SpatialPredictionStudio_StudioDockWindow")
            data = package.read("Install/SpatialPredictionStudio.dll")
            self.assertEqual(data[:2], b"MZ")
            pe = struct.unpack_from("<I", data, 0x3C)[0]
            self.assertEqual(data[pe:pe+4], b"PE\0\0")
            self.assertEqual(struct.unpack_from("<H", data, pe+4)[0], 0x14C)
            manifest = json.loads(package.read("Install/build-manifest.json"))
            self.assertEqual(manifest["version"], "0.5.0")
            for relative, expected in manifest["sources"].items():
                self.assertEqual(hashlib.sha256((ROOT / relative).read_bytes()).hexdigest(), expected, relative)
            for member, expected in manifest["members"].items():
                self.assertEqual(hashlib.sha256(package.read(member)).hexdigest(), expected, member)
            runtime = json.loads(package.read("Install/runtime.json"))
            self.assertTrue(Path(runtime["python_executable"]).is_file())
            self.assertEqual(Path(runtime['python_executable']).parent.parent.name,'runtime-d')
            self.assertTrue((Path(runtime['models_dir'])/'tabpfn-v2/tabpfn-v2-regressor.ckpt').is_file())
            for path,expected in manifest['external_assets'].items():
                self.assertEqual(hashlib.sha256(Path(path).read_bytes()).hexdigest(),expected,path)
            self.assertFalse(any(name.endswith('.ckpt') for name in names))
            self.assertEqual(runtime["worker_path"], "backend/worker.py")
            self.assertIn("Install/backend/worker.py", names)
            self.assertIn('Install/backend/sps/knndm_bridge.R',names)
            self.assertIn("Install/examples/samples.geojson", names)
            self.assertIn("Install/examples/covariates.tif", names)
            self.assertIn("Install/backend/sps/model_bundle.py", names)
            self.assertIn("Install/backend/sps/publication_report.py", names)
            self.assertIn("Install/backend/sps/study_export.py", names)
            self.assertIn("Install/docs/RAPOR_VE_MODEL.md", names)

if __name__ == "__main__":
    unittest.main()

