"""Render the verified Turkish delivery and detailed numerical reports offline."""
from pathlib import Path
import argparse
import hashlib
import json
import re
import shutil
import markdown

ROOT = Path(__file__).resolve().parents[1]
OUT = ROOT / "release"
SECTIONS = [("teslim", OUT / "TESLIM_RAPORU.md"), ("son-analiz", ROOT / "ASAMA_05_KABUL.md"), ("d-modelleri", ROOT / "ASAMA_D_KABUL.md"), ("kurulum", ROOT / "installer/FINAL_ACCEPTANCE.md")]
CSS = """
body{font:16px/1.65 Segoe UI,Arial,sans-serif;background:#eef2f4;color:#182f3a;margin:0}
main{max-width:1160px;margin:auto;padding:36px;background:white}
nav{position:sticky;top:0;padding:14px;background:#153f49;color:white;z-index:2}
nav a{color:white;margin-right:24px;text-decoration:none}h1,h2,h3{line-height:1.25;scroll-margin-top:70px}
h1{font-size:30px;color:#124f51}h2{font-size:23px;margin-top:42px;border-bottom:1px solid #cbd8db;padding-bottom:10px}
a{color:#065c86;overflow-wrap:anywhere}p,li{max-width:1040px}code{font-size:.88em;background:#f2f5f6;padding:2px 4px;overflow-wrap:anywhere}
table{border-collapse:collapse;width:100%;font-size:14px;margin:20px 0;display:block;overflow-x:auto}
th,td{border:1px solid #d7e0e3;padding:8px 10px;text-align:left;vertical-align:top}th{background:#e4efee}tr:nth-child(even){background:#f7fafb}
td{overflow-wrap:anywhere;min-width:65px}section{margin-bottom:60px}pre{overflow-x:auto;background:#f1f4f5;padding:15px}
.stamp{font-size:13px;color:#596b73}section+section{border-top:5px solid #137577;padding-top:26px}
@media print{body{background:white}main{max-width:none;padding:0}nav{display:none}section{page-break-before:always}table{display:table;font-size:10px}a{color:inherit}}
"""

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--installer-report', type=Path, default=ROOT / 'installer/FINAL_ACCEPTANCE.md')
    args = parser.parse_args()
    installer_report = args.installer_report.resolve(strict=True)
    if not installer_report.is_relative_to((ROOT / 'installer').resolve()):
        raise ValueError('Installer report is outside the project installer directory')
    sections = SECTIONS[:-1] + [('kurulum', installer_report)]
    parts = []
    manifest = {}
    assets = {}
    def preserve(path):
        path = path.resolve(strict=True)
        relative = path.relative_to(ROOT)
        target = OUT / "kanitlar" / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        expected = hashlib.sha256(path.read_bytes()).hexdigest()
        assert hashlib.sha256(target.read_bytes()).hexdigest() == expected
        assets[target.relative_to(OUT).as_posix()] = expected
        return target.relative_to(OUT).as_posix()
    for relative in ("build/arcmap-evidence/mcp-runs/20261018_035902_48c39f66/output", "build/arcmap-evidence/d-runs/20261017_220635_13f2d3a5/output", "build/arcmap-evidence/d-runs/20261017_221849_514e109f/output", "build/arcmap-evidence/d-runs/20261017_222534_d19a1948/output"):
        for path in (ROOT / relative).rglob("*"):
            if path.is_file(): preserve(path)
    installed = json.loads((ROOT / "build/installed-release/scientific-readback.json").read_text(encoding="utf-8"))
    assert installed["status"] == "passed"
    for path in Path(installed["output_directory"]).rglob("*"):
        if path.is_file(): preserve(path)
    def portable_link(match):
        original = Path(match.group(1))
        if original.resolve() == (ROOT / "ASAMA_05_KABUL.md").resolve(): return 'href="#son-analiz"'
        if original.resolve() == (ROOT / "ASAMA_D_KABUL.md").resolve(): return 'href="#d-modelleri"'
        return 'href="' + preserve(original) + '"'
    for anchor, path in sections:
        raw = path.read_text(encoding="utf-8")
        manifest[str(path.relative_to(ROOT))] = hashlib.sha256(path.read_bytes()).hexdigest()
        rendered = markdown.markdown(raw, extensions=["tables", "fenced_code", "sane_lists"])
        if anchor == "son-analiz":
            rendered = re.sub(r"<h1>.*?</h1>", "<h1>Önceki adayın sayısal kabulü</h1>", rendered, count=1)
        rendered = re.sub(r'href="([A-Za-z]:[/\\][^"]*)"', portable_link, rendered)
        rendered = rendered.replace('href="ASAMA_05_KABUL.md"', 'href="#son-analiz"').replace('href="ASAMA_D_KABUL.md"', 'href="#d-modelleri"')
        rendered = rendered.replace('href="KURUCU_KABULU.md"', 'href="#kurulum"')
        parts.append('<section id="' + anchor + '">' + rendered + '</section>')
    document = '<!doctype html><html lang="tr"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>Spatial Prediction Studio 0.5.0 ayrıntılı rapor</title><style>' + CSS + '</style></head><body><nav><a href="#teslim">Teslim</a><a href="#son-analiz">Önceki adayın sayısal kabulü</a><a href="#d-modelleri">TabM, RealMLP ve TabPFN</a></nav><main>' + ''.join(parts) + '</main></body></html>'
    document = document.replace('</nav>', '<a href="#kurulum">Kurucu kabulü</a></nav>')
    target = OUT / "AYRINTILI_RAPOR.html"
    target.write_text(document, encoding="utf-8")
    assert all(token in target.read_text(encoding="utf-8") for token in ("712", "3140", "27", "18", "140", "0.18159793", "0.5.0"))
    assert 'file:///' not in document
    for href in re.findall(r'href="([^"]+)"', document):
        if not href.startswith(("#", "https://", "http://")):
            assert (OUT / href).is_file(), href
    (OUT / "report-render-readback.json").write_text(json.dumps({"sources":manifest,"html_sha256":hashlib.sha256(target.read_bytes()).hexdigest(),"bytes":target.stat().st_size,"external_libraries":False,"portable_assets":assets,"local_links":"all_resolve"},indent=2),encoding="utf-8")
    print(str(target))

if __name__ == "__main__": main()
