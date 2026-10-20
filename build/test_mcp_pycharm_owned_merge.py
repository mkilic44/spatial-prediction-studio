import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('pycharm_merge', Path(__file__).with_name('mcp-pycharm-owned-merge.py'))
merge = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(merge)


@pytest.fixture
def fixture(tmp_path, monkeypatch):
    versions = tmp_path / 'versions'
    root = versions / 'new-version'
    (root / 'runtime-mcp/Scripts').mkdir(parents=True)
    (root / 'runtime-mcp/Scripts/python.exe').write_bytes(b'test-only')
    (root / 'mcp-server').mkdir()
    (root / 'mcp-server/server.py').write_text('test-only', encoding='utf-8')
    (root / 'client-configurations').mkdir()
    _, patch = merge.registration.template('pycharm', root)
    (root / 'client-configurations/pycharm.json').write_text(json.dumps(patch), encoding='utf-8')
    target, xml = tmp_path / 'mcp.json', tmp_path / 'permissions.xml'
    document = {'mcpServers': {
        merge.registration.KEY: {'command': 'old/python.exe', 'args': ['old/server.py'],
            'env': {'SPS_MCP_OWNER': merge.registration.OWNER}},
        'private-server': {'env': {'API_KEY': 'sensitive-value'}}}, 'setting': True}
    target.write_bytes(b'\xef\xbb\xbf' + json.dumps(document).encode('utf-8'))
    xml.write_bytes(b'<permissions enabled="false" allowed="private-list"/>')
    monkeypatch.setattr(merge, 'VERSIONS', versions)
    monkeypatch.setattr(merge, 'TARGET', target)
    monkeypatch.setattr(merge, 'XML', xml)
    monkeypatch.setattr(merge, 'pycharm_running', lambda: False)
    return root, target, xml


def apply(root, target, xml):
    return merge.execute(root, True, merge.sha(target.read_bytes()), merge.sha(xml.read_bytes()))


def test_prepare_never_writes_or_discloses_private_values(fixture):
    root, target, xml = fixture
    originals = target.read_bytes(), xml.read_bytes()
    result = merge.execute(root)
    assert result['passed'] and not result['applied'] and not result['write_attempted']
    assert (target.read_bytes(), xml.read_bytes()) == originals
    assert all(value not in json.dumps(result) for value in ('private-server', 'sensitive-value', 'private-list'))
    assert not list(target.parent.glob('*.sps-backup-*'))


def test_apply_replaces_only_owned_entry_with_verified_backup(fixture):
    root, target, xml = fixture
    previous = target.read_bytes(), xml.read_bytes()
    result = apply(root, target, xml)
    assert result['passed'] and result['applied'] and result['owned_entry_matches']
    assert result['nonowned_preserved'] and result['xml_permissions_bytes_unchanged']
    assert target.read_bytes().startswith(b'\xef\xbb\xbf')
    assert Path(result['merge_result']['backup']).read_bytes() == previous[0]
    assert xml.read_bytes() == previous[1]
    document = json.loads(target.read_text(encoding='utf-8-sig'))
    assert document['mcpServers']['private-server']['env']['API_KEY'] == 'sensitive-value'


@pytest.mark.parametrize('which', ['json', 'xml'])
def test_expected_hash_mismatch_refuses_without_writing(fixture, which):
    root, target, xml = fixture
    before = target.read_bytes(), xml.read_bytes()
    hashes = [merge.sha(value) for value in before]
    hashes[0 if which == 'json' else 1] = '0' * 64
    result = merge.execute(root, True, *hashes)
    assert not result['passed'] and result['error_code'] == 'EXPECTED_HASH_CHANGED'
    assert (target.read_bytes(), xml.read_bytes()) == before
    assert not list(target.parent.glob('*.sps-backup-*'))


def test_running_pycharm_refuses_without_writing(fixture, monkeypatch):
    root, target, xml = fixture
    previous = target.read_bytes()
    monkeypatch.setattr(merge, 'pycharm_running', lambda: True)
    result = apply(root, target, xml)
    assert result['error_code'] == 'PYCHARM_RUNNING' and not result['write_attempted']
    assert target.read_bytes() == previous


def test_unowned_entry_is_not_replaced(fixture):
    root, target, xml = fixture
    document = json.loads(target.read_text(encoding='utf-8-sig'))
    del document['mcpServers'][merge.registration.KEY]['env']['SPS_MCP_OWNER']
    target.write_text(json.dumps(document), encoding='utf-8')
    previous = target.read_bytes()
    result = apply(root, target, xml)
    assert result['error_code'] == 'OWNERSHIP_CONFLICT' and target.read_bytes() == previous


def test_tampered_installed_template_refuses(fixture):
    root, target, xml = fixture
    (root / 'client-configurations/pycharm.json').write_text('{}', encoding='utf-8')
    result = apply(root, target, xml)
    assert result['error_code'] == 'TEMPLATE_MISMATCH' and not result['write_attempted']


def test_xml_change_between_preflight_and_locked_merge_is_rejected(fixture, monkeypatch):
    root, target, xml = fixture
    before = target.read_bytes()
    original = merge.registration.atomic_merge
    def changed(*args, **kwargs):
        xml.write_bytes(b'<permissions changed="external"/>')
        return original(*args, **kwargs)
    monkeypatch.setattr(merge.registration, 'atomic_merge', changed)
    result = apply(root, target, xml)
    assert not result['passed'] and result['error_code'] == 'CONFIG_CHANGED'
    assert target.read_bytes() == before
    assert not list(target.parent.glob('*.sps-backup-*'))


def test_unrelated_installation_path_refuses(fixture):
    root, target, xml = fixture
    result = merge.execute(root.parent)
    assert result['error_code'] == 'INSTALLATION_PATH' and not result['write_attempted']
