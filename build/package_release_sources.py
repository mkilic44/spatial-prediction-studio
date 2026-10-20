"""Build the redistributable project sources without Esri or private runtime binaries."""
import hashlib
import json
from pathlib import Path
import zipfile

ROOT = Path(__file__).resolve().parents[1]
OUTPUT = ROOT / "release/SpatialPredictionStudio-0.5.0-source.zip"

def main():
    selected = set()
    source_suffixes = {".py", ".cs", ".csproj", ".sln", ".ps1", ".R", ".json", ".md", ".txt", ".iss", ".csv", ".geojson", ".tif", ".xml", ".toml", ".yml", ".yaml", ".png"}
    for folder in ("backend", "native", "tools", "tests", "examples", "examples-d", "docs", "licenses", "licenses-d", "mcp-server"):
        for path in (ROOT / folder).rglob("*"):
            if path.is_file() and not {"bin", "obj", "__pycache__", ".pytest_cache"}.intersection(path.relative_to(ROOT).parts) and path.suffix in source_suffixes:
                selected.add(path)
    for path in (ROOT / "installer").iterdir():
        if path.is_file() and path.suffix in {".py", ".iss", ".R", ".md", ".txt"}:
            selected.add(path)
    selected.add(ROOT / "installer/corresponding-source/source-manifest.json")
    for path in ROOT.iterdir():
        if path.is_file() and path.suffix in {".md", ".txt", ".csv", ".xml"}:
            selected.add(path)
    for name in ("LICENSE.txt", "weights-manifest.json"):
        selected.add(ROOT / "models/tabpfn-v2" / name)
    package = ROOT / "build/stage-050-utf8-release/SpatialPredictionStudio.esriAddIn"
    with zipfile.ZipFile(package) as archive:
        binding = json.loads(archive.read("Install/build-manifest.json"))
    missing = []
    for name, digest in binding["sources"].items():
        path = ROOT / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("Source differs from released add-in: " + name)
        if path not in selected: missing.append(name)
    if missing: raise ValueError("Required source files omitted: " + repr(missing))
    records = {p.relative_to(ROOT).as_posix(): {"bytes": p.stat().st_size, "sha256": hashlib.sha256(p.read_bytes()).hexdigest()} for p in sorted(selected)}
    manifest = {"version": "0.5.0", "kind": "source", "addin_sha256": hashlib.sha256(package.read_bytes()).hexdigest(), "files": records,
                "excluded": ["Esri proprietary binaries", "installed Python and R environments", "TabPFN weights", "original user client settings"],
                "runtime_distribution": "The standalone Setup.exe supplies the private runtimes and matching third-party sources."}
    OUTPUT.parent.mkdir(exist_ok=True)
    temporary = OUTPUT.with_suffix(".partial")
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as archive:
        for path in sorted(selected): archive.write(path, path.relative_to(ROOT).as_posix())
        archive.writestr("source-manifest.json", json.dumps(manifest, ensure_ascii=False, indent=2))
    with zipfile.ZipFile(temporary) as archive:
        assert archive.testzip() is None
        for name, record in records.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == record["sha256"]
    temporary.replace(OUTPUT)
    result = {"status": "passed", "path": str(OUTPUT), "files": len(records), "bytes": OUTPUT.stat().st_size, "sha256": hashlib.sha256(OUTPUT.read_bytes()).hexdigest()}
    OUTPUT.with_suffix(".json").write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))

if __name__ == "__main__": main()
