"""Inspect application metadata and dry-run OpenCode; never persist user settings."""
import copy
import hashlib
import json
import sys
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mcp-server'))
from register_clients import KEY, template, merge_owned_text, remove_owned_text
from sps_mcp.jsonc import Document

product_root = Path(r'C:\Program Files\JetBrains\PyCharm 2026.2.0.1')
product = json.loads((product_root / 'product-info.json').read_text(encoding='utf-8'))
jar = Path.home() / r'AppData\Roaming\JetBrains\PyCharm2026.2\plugins\ml-llm\lib\ml-llm.jar'
with zipfile.ZipFile(jar) as package:
    plugin = ET.fromstring(package.read('META-INF/plugin.xml'))
disabled_file = jar.parents[3] / 'disabled_plugins.txt'
disabled = disabled_file.read_text(encoding='utf-8').splitlines() if disabled_file.exists() else []
evidence = {'schema_version': 1, 'pycharm': {
    'version': product['version'], 'build': product['buildNumber'],
    'launcher': str(product_root / product['launch'][0]['launcherPath']),
    'ai_assistant': {'name': plugin.findtext('name'), 'id': plugin.findtext('id'),
                     'version': plugin.findtext('version'), 'jar': str(jar),
                     'jar_sha256': hashlib.sha256(jar.read_bytes()).hexdigest(),
                     'listed_disabled': plugin.findtext('id') in disabled},
    'registration': 'official_ui_add_json_or_import_from_claude',
    'official_documented_direct_write_api': 'not_found',
    'official_source': 'https://www.jetbrains.com/help/ai-assistant/mcp.html',
    'live_ai_assistant_connection': 'pending'}}
path = Path.home() / r'.config\opencode\opencode.jsonc'
raw = path.read_bytes()
original = raw.decode('utf-8-sig')
parsed = Document(original)
fmt, patch = template('opencode', ROOT)
merged = merge_owned_text(original, 'jsonc', patch)
updated = Document(merged)
old_comments = [original[token.start:token.end] for token in parsed.comments]
new_comments = [merged[token.start:token.end] for token in updated.comments]
assert old_comments == new_comments
expected = copy.deepcopy(parsed.root.value)
expected.setdefault('mcp', {})[KEY] = patch['mcp'][KEY]
assert updated.root.value == expected
removed, status = remove_owned_text(merged, 'jsonc', patch)
expected['mcp'].pop(KEY)
assert Document(removed).root.value == expected
assert [removed[token.start:token.end] for token in Document(removed).comments] == old_comments
assert path.read_bytes() == raw
evidence['opencode'] = {'path': str(path), 'bytes': len(raw), 'sha256': hashlib.sha256(raw).hexdigest(),
    'comment_count': len(old_comments), 'dry_run_merge': 'passed', 'dry_run_remove': 'passed',
    'comments_unchanged': True, 'unrelated_values_unchanged': True, 'source_bytes_unchanged': True,
    'config_written': False, 'secret_values_recorded': False}
destination = ROOT / 'build/mcp-client-readonly-probe.json'
destination.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
print(json.dumps(evidence, ensure_ascii=False))
