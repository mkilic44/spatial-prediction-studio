import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mcp-server'))
spec = importlib.util.spec_from_file_location('store_merge', Path(__file__).with_name('mcp-claude-store-merge.py'))
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    target = tmp_path / 'store/config.json'
    target.parent.mkdir()
    value = {'mcpServers': {'existing_private': {'command': 'existing.exe', 'env': {'SECRET': 'never-print-this'}}},
             'preferences': {'nested': [True, 1, None, 'Türkçe']}}
    original = b'\xef\xbb\xbf' + json.dumps(value, ensure_ascii=False).encode('utf-8')
    target.write_bytes(original)
    regular = tmp_path / 'regular.json'
    regular.write_bytes(b'{"untouched":true}')
    install = tmp_path / 'installed'
    fragments = install / 'client-configurations'
    fragments.mkdir(parents=True)
    entry = {'command': 'python.exe', 'args': ['-X', 'utf8', 'server.py'], 'env': {'SPS_MCP_OWNER': 'SpatialPredictionStudio'}}
    patch = {'mcpServers': {mod.KEY: entry}}
    (fragments / 'claude_desktop.json').write_text(json.dumps(patch), encoding='utf-8')
    (tmp_path / 'build').mkdir()
    monkeypatch.setattr(mod, 'ROOT', tmp_path)
    monkeypatch.setattr(mod, 'INSTALL', install)
    monkeypatch.setattr(mod, 'TARGET', target)
    monkeypatch.setattr(mod, 'REGULAR', regular)
    monkeypatch.setattr(mod, 'template', lambda *_: ('json', patch))
    return tmp_path, target, original, value, entry


def test_prepare_never_writes_and_never_persists_private_values(setup, capsys):
    folder, target, original, _, _ = setup
    assert mod.main([]) == 0
    assert target.read_bytes() == original
    assert list(target.parent.iterdir()) == [target]
    evidence = (folder / 'build/mcp-claude-store-merge-prepared.json').read_text()
    assert 'never-print-this' not in evidence
    assert 'existing_private' not in evidence
    assert 'never-print-this' not in capsys.readouterr().out


def test_changed_config_is_refused_without_backup_or_mutation(setup):
    folder, target, original, _, _ = setup
    assert mod.main(['--apply', '--expected-sha256', '0' * 64]) == 1
    assert target.read_bytes() == original
    assert list(target.parent.iterdir()) == [target]
    result = json.loads((folder / 'build/mcp-claude-store-merge-applied.json').read_text())
    assert result['error_code'] == 'EXPECTED_HASH_CHANGED'
    assert result['applied'] is False


def test_actual_atomic_merge_preserves_private_settings_backup_and_bom(setup):
    folder, target, original, previous, entry = setup
    assert mod.main(['--apply', '--expected-sha256', mod.sha(original)]) == 0
    result = json.loads((folder / 'build/mcp-claude-store-merge-applied.json').read_text())
    changed = target.read_bytes()
    assert changed.startswith(b'\xef\xbb\xbf')
    parsed = json.loads(changed.decode('utf-8-sig'))
    assert parsed['mcpServers'].pop(mod.KEY) == entry
    assert parsed == previous
    assert Path(result['merge_result']['backup']).read_bytes() == original
    assert result['original_backup_verified'] is True
    assert result['regular_roaming_unchanged'] is True
    assert result['owned_entry_matches'] is True


def test_nonowned_same_name_is_refused(setup):
    folder, target, _, previous, _ = setup
    previous['mcpServers'][mod.KEY] = {'command': 'not-ours.exe'}
    original = json.dumps(previous).encode()
    target.write_bytes(original)
    assert mod.main(['--apply', '--expected-sha256', mod.sha(original)]) == 1
    assert target.read_bytes() == original
    result = json.loads((folder / 'build/mcp-claude-store-merge-applied.json').read_text())
    assert result['error_code'] == 'OWNERSHIP_CONFLICT'
