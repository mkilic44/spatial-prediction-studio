import importlib.util
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

spec = importlib.util.spec_from_file_location('client_verifier', Path(__file__).with_name('mcp-verify-clients.py'))
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


@pytest.mark.parametrize('text,expected', [('Status: Connected', 'connected'),
    ('Status: Not connected', 'failed'), ('Status: Disconnected', 'failed'),
    ('Connected before, Error now', 'failed'), ('Pending approval', 'pending_approval_or_authentication'),
    ('disabled', 'disabled'), ('No such server', 'not_reported')])
def test_health_does_not_promote_negative_or_unknown(text, expected):
    assert module.health_status(text) == expected


def test_command_help_does_not_treat_description_as_command():
    actual = module.supported_commands('Commands:\n opencode mcp list list servers\n opencode mcp logout remove OAuth credentials\nOptions:\n --help')
    assert actual['list'] and not actual['remove'] and not actual['get']


def test_failed_requested_checks_are_not_success():
    args = SimpleNamespace(sdk_handshake=True, session_id='s', claude_health=False, opencode_all_health=False)
    output = {'sdk': {'handshake': 'passed', 'tool_count': 15, 'session_discovery': 'passed',
                      'arcmap_status': 'session_not_discovered'}, 'cli': {}}
    assert not module.requested_checks_passed(output, args)
    output['sdk']['arcmap_status'] = 'passed'
    assert module.requested_checks_passed(output, args)
    args.claude_health = True
    assert not module.requested_checks_passed(output, args)


def test_own_probe_process_output_and_timeout_are_bounded():
    import sys
    status, text = module.command([sys.executable, '-c', 'print("Türkçe",flush=True)'])
    assert status['exit_code'] == 0
    # Runtime itself may use a legacy console codec; executable client templates use -X utf8.
    status, text = module.command([sys.executable, '-X', 'utf8', '-c', 'print("Türkçe",flush=True)'])
    assert 'Türkçe' in text
    status, text = module.command([sys.executable, '-c', 'import time; time.sleep(4)'], timeout=0.1)
    assert status['status'] == 'timeout' and status['exit_code'] == 124 and text == ''


def test_two_claude_profiles_require_both_matching_without_secrets(tmp_path, monkeypatch):
    paths = [tmp_path / 'classic.json', tmp_path / 'store.json']
    expected = {'command': 'new-python.exe', 'args': ['new-server.py'], 'env': {'SPS_MCP_OWNER': 'SpatialPredictionStudio'}}
    for path in paths:
        path.write_text(json.dumps({'mcpServers': {module.KEY: expected, 'other': {'key': 'private-value'}}}), encoding='utf-8')
    monkeypatch.setattr(module, 'profile_paths', lambda _: paths)
    monkeypatch.setattr(module, 'template', lambda *_: ('json', {'mcpServers': {module.KEY: expected}}))
    result = module.registration('claude_desktop', tmp_path)
    assert result['state'] == 'matching' and len(result['profiles']) == 2
    assert 'private-value' not in json.dumps(result)
    paths[1].write_text('{"mcpServers":{}}', encoding='utf-8')
    assert module.registration('claude_desktop', tmp_path)['state'] == 'different'


def test_each_profile_sdk_probe_uses_its_verified_launch_and_keeps_host_claim_separate(tmp_path, monkeypatch):
    paths = [tmp_path / 'classic.json', tmp_path / 'store.json']
    expected = {'command': 'new-python.exe', 'args': ['-X', 'utf8', 'new-server.py'],
                'env': {'SPS_MCP_OWNER': 'SpatialPredictionStudio'}}
    for path in paths:
        path.write_text(json.dumps({'mcpServers': {module.KEY: expected}}), encoding='utf-8')
    monkeypatch.setattr(module, 'CLIENTS', ('claude_desktop',))
    monkeypatch.setattr(module, 'profile_paths', lambda _: paths)
    monkeypatch.setattr(module, 'template', lambda *_: ('json', {'mcpServers': {module.KEY: expected}}))
    launches = []
    async def probe(root, session_id, launch):
        launches.append(launch)
        return {'handshake': 'passed', 'tool_count': 15, 'session_discovery': 'passed'}
    monkeypatch.setattr(module, 'sdk_probe', probe)
    result = asyncio.run(module.profile_sdk_probes(tmp_path, None))
    assert launches == [expected, expected]
    assert len(result) == 2 and all(row['actual_client_host_test'] is False for row in result)
    paths[1].write_text('{"mcpServers":{}}', encoding='utf-8')
    launches.clear()
    result = asyncio.run(module.profile_sdk_probes(tmp_path, None))
    assert len(launches) == 1 and result[1]['handshake'] == 'failed'


def test_registration_failure_cannot_pass_even_when_sdk_was_successful():
    args = SimpleNamespace(install_root='new', sdk_handshake=False, profile_sdk_handshakes=False,
                           session_id=None, claude_health=False, opencode_all_health=False)
    output = {'registration': {client: {'state': 'matching'} for client in module.CLIENTS}, 'cli': {}}
    assert module.requested_checks_passed(output, args)
    output['registration']['claude_desktop']['state'] = 'different'
    assert not module.requested_checks_passed(output, args)


def test_profile_sdk_count_cannot_hide_missing_second_profile():
    args = SimpleNamespace(install_root='new', sdk_handshake=False, profile_sdk_handshakes=True,
                           session_id=None, claude_health=False, opencode_all_health=False)
    output = {'registration': {client: {'state': 'matching', 'profiles': [{}]} for client in module.CLIENTS},
              'cli': {}, 'profile_sdk': []}
    output['registration']['claude_desktop']['profiles'].append({})
    output['profile_sdk'] = [{'handshake': 'passed', 'tool_count': 15, 'session_discovery': 'passed'}] * 7
    assert not module.requested_checks_passed(output, args)
    output['profile_sdk'].append({'handshake': 'passed', 'tool_count': 15, 'session_discovery': 'passed'})
    assert module.requested_checks_passed(output, args)
