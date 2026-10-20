"""Local documentation/evidence only. Never writes client config or completed runs."""
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mcp-server"))
from sps_mcp.artifacts import read_text
from register_clients import CLIENTS, template, main

doc = ROOT / "build/MCP_CLIENT_SUPPORT_DRAFT.md"
text = doc.read_text(encoding="utf-8").replace("arcmap_server", "spatial_prediction_studio")
text = text.replace("Durum: resmî belgeler ve indirilen paket kaynağı doğrulandı; yedi istemcide canlı kabul henüz yapılmadı.",
                    "Sürüm 0.5.0. Resmî belgeler, paket kaynağı ve yerel uygulama sınandı. Yedi istemcide gerçek ArcMap kabulü henüz yapılmadı.")
start, end = text.index("## 2."), text.index("## 3.")
text = text[:start] + """## 2. Yapılandırma şablonları

`build/mcp-client-templates/` yedi istemci için gerçek yerel yollarla üretilmiş şablonları içerir. `build/mcp-client-templates-draft.json` aynı kayıtları makine tarafından okunabilir biçimde toplar. Kurulum başka dizine yapıldığında şablonlar yeni kökten üretilir.

Uygulanan yardımcı `mcp-server/register_clients.py` olur. Varsayılan çalışma yalnız şablon üretir. `--apply --client <istemci>` açıkça verildiğinde yalnız `spatial_prediction_studio` kaydı birleştirilir. Önce mevcut dosya yedeklenir. Başka ürüne ait aynı ad reddedilir; diğer ayarlar korunur ve dosya yeniden ayrıştırılır.

Codex ile ChatGPT aynı dosyayı paylaşıyorsa tek yazma yapılır. PyCharm şablonu AI Assistant ayarlarından içe alınır. OpenCode yorumlu veya özel konumlu dosyasında otomatik yazma yapılmaz. `--remove --client <istemci>` yalnız sahiplik ve çalıştırma yolu bu kurulumla eşleşen kaydı kaldırır. Kullanıcının güven kararları değiştirilmez.

""" + text[end:]
text = text.replace("Sürümün wheel dosyası indirildi, kurulmadı.", "Sürümün wheel dosyası indirildi ve ayrı `runtime-mcp` ortamına kuruldu.")
start, end = text.index("Mevcut `runtime-d`"), text.index("## 4.")
text = text[:start] + """`runtime-mcp`, Python 3.12.3 ile oluşturulmuş ayrı geliştirme ortamıdır. Analiz ortamı değiştirilmedi. `requirements-mcp-lock.txt`, 35 paketin sürümünü ve Windows x64 wheel özetini sabitler. `pip check` geçti. `mcp-server/licenses/` gerçek paket üst verilerini ve tam lisans dosyalarını içerir. Taşınabilir kurucu bu sanal ortamı doğrudan kopyalamaz; kendi Python yorumlayıcısını kullanır.

""" + text[end:]
text = text.replace("On bir araç için bu yeterlidir.", "On beş araç için bu yeterlidir.")
text = text.replace("Yeni `Client` ile otomatik keşif, `ClientSession.initialize()` ile eski yol ayrı sınanmalıdır.",
                    "Yeni `Client` ile otomatik keşif ve `ClientSession.initialize()` ile eski yol gerçek STDIO alt sürecinde ayrı sınandı.")
start = text.index("Planlanan araçlar:")
text = text[:start] + """Uygulanan 15 araç; oturum bulma, durum, katmanlar, proje okuma/açma/kaydetme, önizleme, çalışma, iptal, çıktı listeleme, metin/rapor/tablo okuma ve model/çalışma arşivi dışa aktarmayı kapsar. Genel Python veya kabuk çalıştırma yoktur.

Model işi iş kimliği döndürür; durum ve iptal ayrı çağrılardır. Yerel köprü 30 saniye, Python istemcisi 35 saniye sınırına sahiptir. Zaman aşımındaki değişiklik çağrısı otomatik yinelenmez. Önce durum uzlaştırılır. Raporlar yalnız doğrulanmış sonuç envanterinden, içerik özeti yeniden denetlenerek okunur.

Koordinatlar ve ham kaynak özellikleri varsayılan tablo/JSON okumasında gizlenir. Rapor tablolarındaki koordinat satırları ve ham önbiçimli içerik de gizlenir. `include_coordinates=true` seçimi ham içeriği açıkça ister. Serbest metindeki her sayının konum olup olmadığı otomatik belirlenemez.

## 6. Doğrulanan ve açık kalan işler

`build/mcp-tests-final3.xml`: 58 sınama geçti, sıfır hata ve atlama. Gerçek SDK süreçleriyle iki protokol yolu, sentetik Windows kanalı, süre sınırı, yanlış süreç kimliği, kapalı parametre şemaları ve yedi yapılandırma şeması sınandı. Yedekleme, güvenli kaldırma, dosya kapsamı ve koordinat gizleme de kapsandı. Bunlar gerçek ArcMap kabulü değildir.

Gerçek ArcMap oturumuyla bağlantı ve yedi istemcide araç çağrısı henüz açık. Kullanıcı bilgisayar kontrolünü durdurduğu için yeni ArcMap kabulü başlatılmadı. Temiz bilgisayar kurulumu ve istemcilerin kendi güven/onay adımları ayrıca doğrulanmalıdır.

Bu uygulama çalışmasında gerçek istemci yapılandırması değiştirilmedi. Kullanıcı verisi uzak MCP hizmetine gönderilmedi.
"""
doc.write_text(text, encoding="utf-8")
readme = ROOT / "mcp-server/README.md"
readme.write_text(readme.read_text(encoding="utf-8").replace("55 hedefli sınama", "58 hedefli sınama"), encoding="utf-8")

out = ROOT / "build/mcp-client-templates"
main(["--root", str(ROOT), "--output-dir", str(out)])
templates = {client: {"format": template(client, ROOT)[0], "config": template(client, ROOT)[1]} for client in CLIENTS}
(ROOT / "build/mcp-client-templates-draft.json").write_text(json.dumps({"schema_version": 1, "applied": False,
    "templates": templates}, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

# Verify redaction against immutable previously generated real study outputs.
run = ROOT / "build/arcmap-evidence/d-runs/20261017_220635_13f2d3a5/output"
record = json.loads((run / "final_record.json").read_text(encoding="utf-8"))
coordinate_tokens = set()
def collect(value):
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "metric_coordinates_m":
                for pair in item:
                    coordinate_tokens.update(str(number) for number in pair)
            collect(item)
    elif isinstance(value, list):
        for item in value:
            collect(item)
collect(record)
assert coordinate_tokens
readbacks = []
for name in ("final_record.json", "study_summary.json", "study_report.html", "report.html"):
    path = run / name
    original = path.read_bytes()
    digest = hashlib.sha256(original).hexdigest()
    listing = {"manifest_path": str(run / "result.json"), "artifacts": [{"name": name,
        "path": str(path), "sha256": digest, "bytes": len(original)}]}
    chunks, offset = [], 0
    while True:
        page = read_text(listing, name, offset=offset)
        chunks.append(page["text"])
        if page["next_offset"] is None:
            break
        offset = page["next_offset"]
    rendered = "".join(chunks)
    assert not any(token in rendered for token in coordinate_tokens), name
    assert path.read_bytes() == original
    readbacks.append({"name": name, "sha256": digest, "source_unchanged": True,
                      "coordinate_tokens_absent": True, "pages": len(chunks)})
(ROOT / "build/mcp-real-report-redaction.json").write_text(json.dumps({"run": str(run),
    "coordinate_token_count": len(coordinate_tokens), "artifacts": readbacks,
    "live_arcmap_acceptance": False}, indent=2) + "\n", encoding="utf-8")
print("real_report_redaction=4/4; original_sources_unchanged=true")
