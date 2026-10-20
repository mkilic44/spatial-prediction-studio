"""Write the Store revision report only from an accepted exact Setup.exe."""
import hashlib
import json
from pathlib import Path
import xml.etree.ElementTree as ET

ROOT = Path(__file__).resolve().parents[1]


def read(path):
    return json.loads(path.read_text(encoding='utf-8-sig'))


def sha(path):
    with path.open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def main():
    build = read(ROOT / 'installer/dist-claude-store/installer-build-record.json')
    evidence = ROOT / 'installer/final-acceptance-claude-store'
    delivery = read(evidence / 'delivery-acceptance.json')
    acceptance_path = Path(delivery['acceptance_path'])
    acceptance = read(acceptance_path)
    manifest = Path(acceptance['source_payload']) / 'payload-manifest.json'
    revision = read(manifest)['revision']
    check = read(ROOT / 'installer/claude-store-payload-revision.json')
    signature = read(ROOT / 'installer/dist-claude-store/installer-signature-readback.json')
    if not (acceptance['status'] == 'passed' and delivery['status'] == 'passed_isolated_installation'
            and sha(acceptance_path) == delivery['acceptance_sha256']
            and acceptance['installer_sha256'] == build['sha256'] == sha(Path(build['installer']))
            and signature['sha256'] == build['sha256']
            and signature['status'] == 'NotSigned'
            and sha(manifest) == build['manifest_sha256']
            and acceptance['claude_both_profiles_owned_entries_removed']
            and acceptance['claude_unrelated_values_and_bom_preserved']):
        raise ValueError('The Store revision has not passed exact-artifact acceptance')
    suite = ET.parse(ROOT / 'build/claude-profiles-final.xml').getroot().find('testsuite')
    if any(int(suite.attrib.get(name, 0)) for name in ('failures', 'errors', 'skipped')):
        raise ValueError('MCP suite is not fully passed')
    installer_suite = ET.parse(ROOT / 'installer/installer-tests-claude-store.xml').getroot().find('testsuite')
    if any(int(installer_suite.attrib.get(name, 0)) for name in ('failures', 'errors', 'skipped')):
        raise ValueError('Installer unit suite is not fully passed')
    lines = [
        '# Claude Store düzeltmesinin kurulum kabulü', '',
        f"Yeni içerik sürümü `{build['build_id']}`. Bu dosya yalnız yeni kurucunun yalıtılmış kabulünü bildirir.", '',
        'Gerçek bilgisayarda Claude Store uygulamasının farklı profil kullandığı görüldü. Önceki yardımcı yalnız klasik profil dosyasına yazıyordu. Yeni yardımcı, mevcut iki desteklenen profili ayrı ayrı kaydeder. Diğer sunucular korunur.', '',
        '## Dosya ve sürüm bağı', '',
        f"- Kurucu: `{build['installer']}`",
        f"- Kurucunun SHA-256 özeti: `{build['sha256']}`",
        f"- Dosya boyutu: {build['bytes']} bayt.",
        f"- İçerik listesinin SHA-256 özeti: `{build['manifest_sha256']}`",
        f"- Önceki içerik listesinin SHA-256 özeti: `{revision['parent_manifest_sha256']}`",
        f"- Derleyici süresi: {delivery['compiler_resource']['elapsed_seconds']:.3f} saniye.",
        f"- Derleyici süreçlerinin en yüksek toplam belleği: {delivery['compiler_resource']['peak_owned_tree_rss_MiB']:.3f} MiB.", '',
        f"{check['scientific_component_files_identical']} bilimsel bileşen dosyasının kümesi ve SHA-256 özeti önceki sürümle aynıdır. Bu karşılaştırma analiz kodunu, iki Python ortamını, R ortamını, ağırlıkları ve kaynak eklenti paketini kapsar. Yerel eklentinin derlenmiş kodu değişmedi.", '',
        'Değişen dosyalar:', '',
        *['- `' + item['path'] + '`' for item in revision['changes']], '',
        '## Sınanan işlemler', '',
        f"- {suite.attrib['tests']} Model Context Protocol (MCP) sınaması geçti. Hata veya atlanan sınama yok.",
        f"- Kurucu sınamalarında alt durumlarla birlikte {installer_suite.attrib['tests']} başarılı sonuç kaydedildi. Hata veya atlanan durum yok.",
        '- Yedi istemci seçeneği yalıtılmış profil dizinlerinde çalıştırıldı. PyCharm için elle içe alınacak dosya üretildi.',
        '- Klasik Claude ve Store Claude profilleri aynı kurulumda kaydedildi. Her dosyanın özgün yedeği ve yeni özeti yeniden okundu.',
        '- İki profilde diğer sunucular, Türkçe değerler ve UTF-8 başlangıç işareti korundu.',
        '- Kaldırma yalnız kurulumun kaydettiği profil yollarını kullandı. Sahipli kayıtlar çıkarıldı; diğer değerler korundu.',
        '- Özel Python ve R ortamları, sabit paket sürümleri ve bütün kurulu dosya özetleri doğrulandı.',
        '- Taşınmış yerel TabPFN ağırlığı ağ kapalı ortamda kısa eğitim, sekiz tahmin ve modelin yeniden yüklenmesiyle sınandı.',
        "- 'caret' Applications for Spatial-Temporal Models (CAST) referansı ve tam sentetik Ridge çalışması tekrar yürütüldü. Bütün çalışma çıktıları kanıt dizinine kopyalandı.",
        '- Önceki eklenti, kullanıcı çalışma dosyası ve genel PATH ayarı korunarak kaldırma tamamlandı.', '',
        f"Kaldırılan kayıtlı uygulama dosyası sayısı: {acceptance['uninstalled_registered_files']}. Kurulum ve sınamanın ürettiği {acceptance['remaining_generated_files']} önbellek ve günlük dosyası ayrıca sayılmıştır. Kullanıcı çalışma dosyası ayrı bir denetimle korunmuştur.", '',
        '## Kanıtlar ve kapsam sınırı', '',
        'Yeni kurulum fiziksel `sps-cs` dizininde sınandı. Bu dizinin adı Türkçe karakter içermez. Türkçe adlı `sps-ç2` dizinindeki önceki kurulum kabulü ayrı, tarihsel kanıttır. İki koşu aynı kurulum yapılmış gibi birleştirilmez.', '',
        f"Kabul kaydı: `{acceptance_path}`. SHA-256 özeti: `{delivery['acceptance_sha256']}`.", '',
        f"Bütün kısa Ridge çalışması ve kanıtların kopyası: `{evidence}`. `delivery-acceptance.json` dosya özetlerini içerir.", '',
        'Bu kabul, farklı bir fiziksel bilgisayarda deneme veya yedi ayrı istemcide canlı bağlantı kabulü değildir. Gerçek kullanıcı kurulumu ve istemci arayüzleri ana teslim raporunda ayrıca gösterilir. Sentetik model denemesi bilimsel saha doğrulaması sayılmaz.', '',
        'Yeni Setup.exe dijital olarak imzalanmamıştır. Windows imza durumu bu yeni dosyadan ayrıca okunmuştur. Dosya bütünlüğü yukarıdaki SHA-256 özetiyle denetlenmelidir.', '',
        'Önceki kurucu ve `installer/FINAL_ACCEPTANCE.md` tarihsel kabul olarak korunur. Önceki sayısal analiz sonuçları yeniden üretilmiş gibi sunulmaz.', ''
    ]
    (ROOT / 'installer/CLAUDE_STORE_ACCEPTANCE.md').write_text('\n'.join(lines), encoding='utf-8')


if __name__ == '__main__':
    main()
