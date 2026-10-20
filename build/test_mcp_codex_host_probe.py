"""Protocol and acceptance-boundary tests. These are not real ArcMap acceptance."""
import copy
import importlib.util
import json
import sys
from pathlib import Path

import pytest
from jsonschema import Draft7Validator

PATH = Path(__file__).with_name('mcp-codex-host-probe.py')
SPEC = importlib.util.spec_from_file_location('host_probe', PATH)
probe = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(probe)

KEY = probe.KEY
EXPECTED = {'command': 'python.exe', 'args': ['-X', 'utf8', 'server.py'], 'env': {'SPS_MCP_OWNER': 'SpatialPredictionStudio'}}


def config(disabled=False):
    return {'config': {'mcp_servers': {KEY: copy.deepcopy(EXPECTED), 'unrelated': {'command': 'other.exe', 'enabled': not disabled}},
                      'features': {name: False for name in probe.DISABLED_FEATURES}}}


def test_temporary_overrides_disable_only_other_servers_and_no_trust_or_auth():
    args = probe.host_args('codex.exe', ['other_name', 'other-name'])
    assert 'mcp_servers.other_name.enabled=false' in args
    assert 'mcp_servers.other-name.enabled=false' in args
    assert not any(KEY in arg or 'trust' in arg or 'approval' in arg or 'auth' in arg for arg in args)
    assert '--ignore-user-config' not in args


@pytest.mark.parametrize('name', ['other.name', 'quoted"name'])
def test_ambiguous_override_segment_is_refused(name):
    with pytest.raises(probe.ProbeError, match='MCP_NAME_CANNOT_BE_ISOLATED'):
        probe.host_args('codex.exe', [name])


def test_isolation_readback_refuses_other_active_server():
    with pytest.raises(probe.ProbeError, match='OTHER_MCP_SERVER_STILL_ENABLED'):
        probe.isolated_config(config(False), EXPECTED)
    data = config(True)
    before = copy.deepcopy(data)
    assert set(probe.isolated_config(data, EXPECTED)) == {KEY, 'unrelated'}
    assert data == before


def test_missing_or_wrong_owned_registration_refused():
    data = config(True)
    data['config']['mcp_servers'][KEY]['command'] = 'unexpected.exe'
    with pytest.raises(probe.ProbeError, match='OWNED_CONFIG_DIFFERS_FROM_INSTALLATION'):
        probe.config_entries(data, EXPECTED)
    data['config']['mcp_servers'].pop(KEY)
    with pytest.raises(probe.ProbeError, match='OWNED_CONFIG_MISSING'):
        probe.config_entries(data, EXPECTED)


def test_plugin_isolation_must_be_effective():
    data = config(True)
    data['config']['features']['plugins'] = True
    with pytest.raises(probe.ProbeError, match='MCP_PLUGIN_ISOLATION_NOT_CONFIRMED'):
        probe.isolated_config(data, EXPECTED)


@pytest.mark.parametrize('method,params', [
    ('turn/start', {'input': []}),
    ('config/value/write', {}),
    ('mcpServer/tool/call', {'server': 'other', 'tool': 'status', 'arguments': {}}),
    ('mcpServer/tool/call', {'server': KEY, 'tool': 'run_analysis', 'arguments': {}}),
    ('mcpServer/tool/call', {'server': KEY, 'tool': 'status', 'arguments': {'session_id': 'ok', 'extra': True}}),
    ('mcpServerStatus/list', {'limit': 100}),
])
def test_no_model_turn_config_write_other_server_or_mutation(method, params):
    with pytest.raises(probe.ProbeError):
        probe.validate_outgoing(method, params)


class FakeRpc:
    def __init__(self, missing=False, malformed_status=False):
        self.requests = []
        self.missing = missing
        self.malformed_status = malformed_status

    def request(self, method, params):
        probe.validate_outgoing(method, params)
        self.requests.append((method, copy.deepcopy(params)))
        if method == 'thread/start':
            return {'thread': {'id': 'test-thread', 'ephemeral': True}}
        if method == 'mcpServerStatus/list':
            tools = {'discover_sessions': {'name': 'discover_sessions'}, 'status': {'name': 'status'}}
            tools.update({f'fixture_tool_{i}': {'name': f'fixture_tool_{i}'} for i in range(13)})
            return {'data': [{'name': KEY, 'tools': tools, 'toolsError': None}], 'nextCursor': None}
        if params['tool'] == 'discover_sessions':
            return {'content': [], 'structuredContent': {'sessions': [] if self.missing else [{'session_id': 'safe_session'}]}}
        return {'content': [], 'structuredContent': {'busy': 'false' if self.malformed_status else False,
            'preview_ready': True, 'secret_fixture_only': 'never-retain', 'project_path': 'private-path'}}


def test_actual_call_flow_is_read_only_and_sanitizes_record():
    rpc = FakeRpc()
    result = probe.actual_calls(rpc, Path('C:/installation'), 'safe_session')
    assert result['arcmap_status'] == 'passed'
    assert result['tool_count'] == 15
    assert [p['tool'] for m, p in rpc.requests if m == 'mcpServer/tool/call'] == ['discover_sessions', 'status']
    assert 'never-retain' not in json.dumps(result) and 'private-path' not in json.dumps(result)
    for method, params in rpc.requests:
        filename = {'thread/start': 'ThreadStartParams.json', 'mcpServerStatus/list': 'ListMcpServerStatusParams.json',
                    'mcpServer/tool/call': 'McpServerToolCallParams.json'}[method]
        schema = json.loads((PATH.parent / 'mcp-codex-host-schema/v2' / filename).read_text())
        Draft7Validator(schema).validate(params)


def test_missing_session_does_not_call_status_or_succeed():
    rpc = FakeRpc(missing=True)
    with pytest.raises(probe.ProbeError, match='REQUESTED_ARCMAP_SESSION_NOT_DISCOVERED'):
        probe.actual_calls(rpc, Path('C:/installation'), 'safe_session')
    assert not any(p.get('tool') == 'status' for _, p in rpc.requests)


def test_string_false_status_not_success():
    with pytest.raises(probe.ProbeError, match='ARCMap_STATUS_RESPONSE_INVALID'):
        probe.actual_calls(FakeRpc(malformed_status=True), Path('C:/installation'), 'safe_session')


def test_host_rpc_real_utf8_pipe_and_timeout(tmp_path):
    code = "import sys,json\nfor line in sys.stdin:\n m=json.loads(line); print(json.dumps({'id':m['id'],'result':{'text':'Türkçe'}}),flush=True)"
    with probe.Rpc([sys.executable, '-X', 'utf8', '-c', code], tmp_path, timeout=2) as rpc:
        assert rpc.request('config/read', {}) == {'text': 'Türkçe'}
    with probe.Rpc([sys.executable, '-c', 'import time;time.sleep(20)'], tmp_path, timeout=0.15) as rpc:
        with pytest.raises(probe.ProbeError, match='HOST_TIMEOUT'):
            rpc.request('config/read', {})


def test_host_rpc_server_approval_request_is_never_accepted(tmp_path):
    code = "import sys,json\nm=json.loads(sys.stdin.readline());print(json.dumps({'id':99,'method':'item/commandExecution/requestApproval','params':{}}),flush=True);sys.stdin.readline()"
    with probe.Rpc([sys.executable, '-c', code], tmp_path, timeout=2) as rpc:
        with pytest.raises(probe.ProbeError, match='SERVER_REQUEST_REQUIRES_SEPARATE_REVIEW'):
            rpc.request('config/read', {})
