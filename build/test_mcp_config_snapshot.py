import importlib.util
import json
from pathlib import Path

SPEC = importlib.util.spec_from_file_location('snap', Path(__file__).with_name('mcp-config-snapshot.py'))
snap = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(snap)


def test_owned_change_does_not_hide_other_config_change():
    initial = {'mcp': {'other': {'environment': {'KEY': 'secret'}}}, 'setting': True}
    first = snap.summarize_mapping(initial, 'mcp')
    initial['mcp'][snap.KEY] = {'command': ['server.py']}
    second = snap.summarize_mapping(initial, 'mcp')
    assert first['nonowned_semantic_sha256'] == second['nonowned_semantic_sha256']
    initial['mcp']['other']['environment']['KEY'] = 'different'
    third = snap.summarize_mapping(initial, 'mcp')
    assert third['other_mcp_sha256'] != second['other_mcp_sha256']
    assert 'secret' not in json.dumps(first) and 'different' not in json.dumps(third)


def test_jsonc_bom_comments_and_values_hashed_without_content(tmp_path):
    path = tmp_path / 'opencode.jsonc'
    path.write_bytes(b'\xef\xbb\xbf' + '{// Türkçe yorum\n"mcp":{"private_name":{"command":["private_command"],}},}'.encode('utf-8'))
    result = snap.snapshot_file(path, 'mcp')
    assert result['utf8_bom'] is True and result['comment_count'] == 1
    assert result['other_mcp_count'] == 1
    serialized = json.dumps(result)
    assert all(value not in serialized for value in ('Türkçe', 'private_name', 'private_command'))


def test_pycharm_empty_and_owned_entry_preserve_existing_semantics():
    first = snap.summarize_pycharm(b'<application><component name="x"><commands/><urls/></component></application>')
    second = snap.summarize_pycharm(b'<application><component name="x"><commands><command name="spatial_prediction_studio" value="private"/></commands><urls/></component></application>')
    assert first['nonowned_semantic_sha256'] == second['nonowned_semantic_sha256']
    assert first['other_mcp_count'] == second['other_mcp_count'] == 0
    assert second['owned_present'] is True and 'private' not in json.dumps(second)


def test_bool_integer_change_is_visible():
    assert snap.digest({'enabled': True}) != snap.digest({'enabled': 1})


def test_current_pycharm_permissions_are_wholly_preserved_and_private():
    raw = b'<application><component name="Mcp"><option name="servers"><list><server><option name="name" value="spatial_prediction_studio"/><option name="allowedToolsNames" value="private-tools"/><option name="enabled" value="true"/></server></list></option></component></application>'
    before = snap.summarize_pycharm(raw)
    after = snap.summarize_pycharm(raw.replace(b'"true"', b'"false"'))
    assert before['owned_present'] is True
    assert before['nonowned_semantic_sha256'] != after['nonowned_semantic_sha256']
    assert before['xml_structure_scope'] == 'whole_permission_file_preserved'
    assert 'private-tools' not in json.dumps(before)


def test_both_claude_profiles_are_compared_and_store_changes_cannot_hide(tmp_path):
    classic, store = tmp_path / 'classic.json', tmp_path / 'store.json'
    for path in (classic, store):
        path.write_text('{"mcpServers":{"private":{"command":"private.exe"}}}', encoding='utf-8')
    def fixture():
        return {'clients': {'claude_desktop': {'profiles': [
            {'profile': 'classic_roaming', 'file_id': 'classic'},
            {'profile': 'store_localcache', 'file_id': 'store'}]}},
            'files': {'classic': snap.snapshot_file(classic, 'mcpServers'),
                      'store': snap.snapshot_file(store, 'mcpServers')}}
    before = fixture()
    assert snap.compare(before, fixture())['other_mcp_records_preserved'] is True
    store.write_text('{"mcpServers":{"private":{"command":"changed.exe"}}}', encoding='utf-8')
    result = snap.compare(before, fixture())
    assert result['other_mcp_records_preserved'] is False
    assert result['rows'][0]['other_mcp_unchanged'] is True
    assert result['rows'][1]['other_mcp_unchanged'] is False


def test_new_profile_without_baseline_is_not_silently_accepted():
    before = {'clients': {}, 'files': {}}
    after = {'clients': {'claude_desktop': {'profiles': [
        {'profile': 'store_localcache', 'file_id': 'store'}]}}, 'files': {}}
    result = snap.compare(before, after)
    assert result['other_mcp_records_preserved'] is False
    assert result['rows'][0]['state'] == 'not_in_baseline'


def test_own_xml_permissions_change_or_file_removal_fails_byte_preservation(tmp_path):
    path = tmp_path / 'permissions.xml'
    path.write_bytes(b'<application><component name="x"><commands><command name="spatial_prediction_studio" enabled="true"/></commands><urls/></component></application>')
    def fixture():
        return {'clients': {}, 'supplementary_files': {'pycharm_legacy_xml': {'file_id': 'xml'}},
                'files': {'xml': snap.snapshot_file(path)}}
    before = fixture()
    path.write_bytes(path.read_bytes().replace(b'"true"', b'"false"'))
    assert snap.compare(before, fixture())['existing_xml_bytes_preserved'] is False
    path.unlink()
    assert snap.compare(before, fixture())['existing_xml_bytes_preserved'] is False
