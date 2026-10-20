"""Package the portable report with all of its locally linked evidence."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"
TARGET = OUT / "Ayrintili_Rapor_ve_Sonuclar.zip"
EXCLUDE = {TARGET.name, "report-package-readback.json", "final-artifacts.json", "SHA256SUMS.txt", "SpatialPredictionStudio-0.5.0-Setup.exe"}

def sha(path):
    with path.open("rb") as stream: return hashlib.file_digest(stream, "sha256").hexdigest()

def main():
    files = sorted(p for p in OUT.rglob("*") if p.is_file() and p.name not in EXCLUDE and p.suffix != ".partial")
    records = {p.relative_to(OUT).as_posix(): {"bytes":p.stat().st_size,"sha256":sha(p)} for p in files}
    temporary = TARGET.with_suffix(".partial")
    with zipfile.ZipFile(temporary,"w",zipfile.ZIP_DEFLATED,compresslevel=6) as archive:
        for path in files: archive.write(path,path.relative_to(OUT).as_posix())
        archive.writestr("report-package-manifest.json",json.dumps({"files":records,"setup_distributed_separately":True},ensure_ascii=False,indent=2))
    with zipfile.ZipFile(temporary) as archive:
        assert archive.testzip() is None
        for name, record in records.items():
            with archive.open(name) as stream:
                assert hashlib.file_digest(stream,"sha256").hexdigest() == record["sha256"]
    temporary.replace(TARGET)
    result={"status":"passed","files":len(files),"bytes":TARGET.stat().st_size,"sha256":sha(TARGET)}
    (OUT/"report-package-readback.json").write_text(json.dumps(result,indent=2),encoding="utf-8")
    print(json.dumps(result))

if __name__ == "__main__": main()
