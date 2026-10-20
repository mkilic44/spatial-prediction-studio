import copy
import importlib.util
import json
from pathlib import Path

import pytest

SPEC = importlib.util.spec_from_file_location('selected', Path(__file__).with_name('mcp-verify-selected-cli.py'))
selected = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(selected)
KEY = selected.KEY
EXPECTED = {'command': ['python.exe', '-X', 'utf8', 'server.py'], 'enabled': True, 'type': 'local'}


@pytest.fixture
def configured(monkeypatch):
    monkeypatch.delenv('OPENCODE_CONFIG_CONTENT', raising=False)
    monkeypatch.setattr(selected.helper, 'registration', lambda *a: {'state': 'matching'})
    monkeypatch.setattr(selected.helper, 'executable', lambda *a: ['fixture.exe'])
    monkeypatch.setattr(selected.helper, 'template', lambda *a: ('json', {'mcp': {KEY: EXPECTED}}))


def test_selected_opencode_checks_effective_isolation_before_health(configured):
    calls = []
    def command(args, **kw):
        calls.append((args, kw))
        if 'config' in args:
            overrides = json.loads(kw['env']['OPENCODE_CONFIG_CONTENT'])
            other = overrides.get('mcp', {}).get('other', {}).get('enabled', True)
            return {'exit_code': 0}, json.dumps({'mcp': {KEY: EXPECTED, 'other': {'enabled': other, 'url': 'private'}}})
        return {'exit_code': 0}, KEY + ' connected\nother disabled'
    result = selected.health('opencode', Path('C:/installed'), command=command)
    assert result['state'] == 'connected'
    assert result['other_configured_servers_disabled_for_probe'] == 1
    assert len(calls) == 3 and calls[-1][0][-3:] == ['mcp', 'list', '--pure']
    assert 'private' not in json.dumps(result)
    overrides = json.loads(calls[-1][1]['env']['OPENCODE_CONFIG_CONTENT'])
    assert overrides == {'autoupdate': False, 'mcp': {'other': {'enabled': False}}}


def test_managed_override_does_not_start_health(configured):
    calls = []
    def command(args, **kw):
        calls.append(args)
        return {'exit_code': 0}, json.dumps({'mcp': {KEY: EXPECTED, 'managed': {'enabled': True}}})
    with pytest.raises(selected.HealthError, match='OTHER_MCP_SERVER_STILL_ENABLED'):
        selected.health('opencode', Path('C:/installed'), command=command)
    assert len(calls) == 2 and all('list' not in args for args in calls)


def test_claude_uses_only_selected_get(configured):
    calls = []
    def command(args, **kw):
        calls.append(args)
        return {'exit_code': 0}, 'Status: Connected'
    result = selected.health('claude_code', Path('C:/installed'), command=command)
    assert calls == [['fixture.exe', 'mcp', 'get', KEY]]
    assert result['state'] == 'connected' and result['tools_call'].startswith('not_performed')


def test_enabled_integer_not_accepted_as_true():
    wrong = copy.deepcopy(EXPECTED)
    wrong['enabled'] = 1
    with pytest.raises(selected.HealthError, match='OWNED_CONFIG_MISMATCH'):
        selected.checked_config({'exit_code': 0}, json.dumps({'mcp': {KEY: wrong}}), EXPECTED)


def test_existing_inline_config_is_not_overwritten(configured, monkeypatch):
    monkeypatch.setenv('OPENCODE_CONFIG_CONTENT', '{"private":"secret"}')
    with pytest.raises(selected.HealthError, match='EXISTING_INLINE_CONFIG_REQUIRES_SEPARATE_REVIEW'):
        selected.health('opencode', Path('C:/installed'), command=lambda *a, **k: pytest.fail('must not start'))
