import copy
import json

import pytest

from register_clients import KEY, OWNER, atomic_merge, merge_owned_text, remove_owned_text, target_path
from sps_mcp.bridge import BridgeError
from sps_mcp.jsonc import Document, loads


PATCH = {"mcp": {KEY: {"type": "local", "command": ["C:/test/python.exe", "-X", "utf8", "server.py"],
                        "environment": {"SPS_MCP_OWNER": OWNER}, "enabled": True}}}


def comments(text):
    parsed = Document(text)
    return [text[token.start:token.end] for token in parsed.comments]


@pytest.mark.parametrize("original", [
    '{}', '{ /* boş */ }', '{"model":"keep"}', '{"mcp":{"other":{}}}',
    '{"model":"keep" // yorum\n}', '{"model":"keep", /* son */}',
    '{"model":"keep", "mcp":{}}', '{"mcp":{ /* sunucular */ }}',
    '{"mcp":{"other":{"command":["leave"],"url":"https://host/a//b"} // korunur\n}}',
    '{"mcp":{"other":{"command":["leave"],}, /* korunur */}, "extra":[1,2,],}',
])
def test_add_jsonc_preserves_comments_unrelated_values_and_idempotence(original):
    before = loads(original)
    merged = merge_owned_text(original, "jsonc", PATCH)
    expected = copy.deepcopy(before)
    expected.setdefault("mcp", {})[KEY] = PATCH["mcp"][KEY]
    assert loads(merged) == expected
    assert comments(merged) == comments(original)
    assert merge_owned_text(merged, "jsonc", PATCH) == merged
    restored, status = remove_owned_text(merged, "jsonc", PATCH)
    expected["mcp"].pop(KEY)
    assert loads(restored) == expected and status == "removed_restart_required"
    assert comments(restored) == comments(original)
    assert remove_owned_text(restored, "jsonc", PATCH) == (restored, "not_present")


@pytest.mark.parametrize("position", ["first", "middle", "last", "only"])
@pytest.mark.parametrize("trailing", [False, True])
def test_remove_jsonc_preserves_comments_at_all_member_boundaries(position, trailing):
    owner = json.dumps(KEY) + ': /* key note */ ' + json.dumps(PATCH["mcp"][KEY]).replace(
        '"enabled": true', '"enabled": /* nested note */ true')
    other1, other2 = '"one": {"a":1}', '"two": {"b":2}'
    members = {"first": [owner, other1, other2], "middle": [other1, owner, other2],
               "last": [other1, other2, owner], "only": [owner]}[position]
    original = '{"mcp": { // before\n' + ', // between\n'.join(members) + (',' if trailing else '') + '\n/* end */}}'
    expected = loads(original)
    del expected["mcp"][KEY]
    restored, status = remove_owned_text(original, "jsonc", PATCH)
    assert status == "removed_restart_required" and loads(restored) == expected
    assert comments(restored) == comments(original)
    for member in (other1, other2):
        if member in original:
            assert member in restored


def test_update_jsonc_retains_internal_comments_and_unrelated_bytes():
    old = copy.deepcopy(PATCH)
    old["mcp"][KEY]["command"] = ["old"]
    original = '/* start */ {\n "secret": "keep exactly",\n "mcp": {\n  ' + json.dumps(KEY) + ': ' + json.dumps(old["mcp"][KEY]).replace(
        '"enabled": true', '"enabled": // internal\n true') + ',\n  "other": {"command": ["leave"]}\n }\n}\n'
    merged = merge_owned_text(original, "jsonc", PATCH)
    assert loads(merged)["mcp"][KEY] == PATCH["mcp"][KEY]
    assert comments(merged) == comments(original)
    assert merged.startswith('/* start */ {\n "secret": "keep exactly",\n "mcp": {\n  ')
    assert merged.endswith(',\n  "other": {"command": ["leave"]}\n }\n}\n')


@pytest.mark.parametrize("field,actual,expected", [
    ("enabled", 1, True), ("enabled", 0, False),
    ("settings", {"features": [{"enabled": 1, "threshold": True}]},
                 {"features": [{"enabled": True, "threshold": 1}]}),
])
def test_jsonc_owned_record_replaces_bool_integer_mismatch_including_nested(tmp_path, field, actual, expected):
    patch = copy.deepcopy(PATCH)
    patch["mcp"][KEY][field] = expected
    original_value = copy.deepcopy(patch)
    original_value["mcp"][KEY][field] = actual
    original = '/* yorum korunur */\n' + json.dumps(original_value)
    path = tmp_path / 'opencode.jsonc'
    path.write_text(original, encoding='utf-8')
    result = atomic_merge(path, 'json', patch)
    assert result['status'] == 'registered_restart_required'
    updated = path.read_text(encoding='utf-8')
    assert updated != original
    assert comments(updated) == comments(original)
    # JSON encoding preserves the actual scalar types at every nested level.
    assert json.dumps(loads(updated), sort_keys=True) == json.dumps(patch, sort_keys=True)
    assert atomic_merge(path, 'json', patch)['status'] == 'unchanged'


def test_jsonc_atomic_backup_bom_and_owned_removal(tmp_path):
    path = tmp_path / 'opencode.jsonc'
    original = b'\xef\xbb\xbf{\r\n // original\r\n "mcp":{}, "keep":true,\r\n}\r\n'
    path.write_bytes(original)
    result = atomic_merge(path, "json", PATCH)
    assert path.read_bytes().startswith(b'\xef\xbb\xbf')
    assert __import__('pathlib').Path(result["backup"]).read_bytes() == original
    assert atomic_merge(path, "json", PATCH)["status"] == "unchanged"
    assert atomic_merge(path, "json", PATCH, remove=True)["status"] == "removed_restart_required"
    restored = path.read_bytes().decode('utf-8-sig')
    assert loads(restored) == {"mcp": {}, "keep": True}
    assert comments(restored) == ['// original\r']


@pytest.mark.parametrize("original", [
    '{"mcp":{},"mcp":{}}', '{"mcp":{"a":1,"\\u0061":2}}', '{/* never closed',
    '{"mcp": [1,]}', '{"a": NaN}', '{"a":1e9999}', '{"a":01}', '{"a":true false}',
    '{"a":1,,}', '{"a":1} trailing', '{"a": [1 2]}',
])
def test_jsonc_invalid_input_never_changes_file(tmp_path, original):
    path = tmp_path / 'opencode.jsonc'
    path.write_text(original, encoding='utf-8')
    with pytest.raises(BridgeError):
        atomic_merge(path, "json", PATCH)
    assert path.read_text(encoding='utf-8') == original


def test_jsonc_foreign_owner_is_not_overwritten_or_removed():
    original = json.dumps({"mcp": {KEY: {"command": ["other"]}}})
    with pytest.raises(BridgeError) as error:
        merge_owned_text(original, "jsonc", PATCH)
    assert error.value.code == "OWNERSHIP_CONFLICT"
    assert remove_owned_text(original, "jsonc", PATCH) == (original, "preserved_other_installation")


def test_jsonc_target_detection_and_ambiguous_pair_refusal(tmp_path, monkeypatch):
    for name in ('OPENCODE_CONFIG', 'OPENCODE_CONFIG_DIR', 'XDG_CONFIG_HOME'):
        monkeypatch.delenv(name, raising=False)
    directory = tmp_path / '.config/opencode'
    directory.mkdir(parents=True)
    target = directory / 'opencode.jsonc'
    target.write_text('{}', encoding='utf-8')
    assert target_path('opencode', home=tmp_path) == target
    (directory / 'opencode.json').write_text('{}', encoding='utf-8')
    with pytest.raises(BridgeError):
        target_path('opencode', home=tmp_path)
