"""Read-only Codex app-server acceptance probe, with no model turn or config writes.

Use only after the owned server has been registered at the final installation path.
Raw JSON-RPC messages, configuration values and stderr are never persisted.
"""
from __future__ import annotations

import argparse
import hashlib
import json
import queue
import re
import shutil
import subprocess
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mcp-server'))
from register_clients import KEY, template

DISABLED_FEATURES = ('apps', 'plugins', 'remote_plugin', 'skill_mcp_dependency_install')
ALLOWED_METHODS = {'initialize', 'initialized', 'config/read', 'thread/start',
                   'mcpServerStatus/list', 'mcpServer/tool/call'}
ALLOWED_TOOLS = {'discover_sessions', 'status'}


class ProbeError(Exception):
    def __init__(self, code, rpc_code=None):
        super().__init__(code)
        self.code, self.rpc_code = code, rpc_code


def validate_outgoing(method, params):
    if method not in ALLOWED_METHODS:
        raise ProbeError('METHOD_NOT_ALLOWED')
    if method == 'mcpServer/tool/call':
        if params.get('server') != KEY or params.get('tool') not in ALLOWED_TOOLS:
            raise ProbeError('TOOL_NOT_ALLOWED')
        args = params.get('arguments')
        if params['tool'] == 'discover_sessions' and args != {}:
            raise ProbeError('ARGUMENTS_NOT_ALLOWED')
        if params['tool'] == 'status' and (not isinstance(args, dict) or
                set(args) != {'session_id'} or not isinstance(args['session_id'], str) or
                not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args['session_id'])):
            raise ProbeError('ARGUMENTS_NOT_ALLOWED')
    if method == 'thread/start' and (params.get('ephemeral') is not True or
            set(params) != {'ephemeral', 'cwd'}):
        raise ProbeError('THREAD_PARAMETERS_NOT_ALLOWED')
    if method == 'mcpServerStatus/list' and params.get('serverName') != KEY:
        raise ProbeError('SERVER_FILTER_REQUIRED')


class Rpc:
    """Bounded UTF-8 pipe. A Windows job owns only this probe and its children."""
    def __init__(self, args, cwd, timeout=60):
        import win32api
        import win32con
        import win32job
        self.win32job = win32job
        self.timeout = timeout
        self.sequence = 0
        self.messages = queue.Queue(maxsize=128)
        self.overflow = threading.Event()
        self.startup = []
        self.process = None
        self.job = win32job.CreateJobObject(None, '')
        limits = win32job.QueryInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation)
        limits['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
        win32job.SetInformationJobObject(self.job, win32job.JobObjectExtendedLimitInformation, limits)
        try:
            self.process = subprocess.Popen(args, cwd=cwd, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, creationflags=subprocess.CREATE_NO_WINDOW)
            handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE,
                                          False, self.process.pid)
            try:
                win32job.AssignProcessToJobObject(self.job, handle)
            finally:
                handle.Close()
            self.readers = [threading.Thread(target=self._stdout, daemon=True),
                            threading.Thread(target=self._stderr, daemon=True)]
            for reader in self.readers:
                reader.start()
        except Exception:
            self.close()
            raise

    def _put(self, value):
        try:
            self.messages.put_nowait(value)
        except queue.Full:
            self.overflow.set()

    def _stdout(self):
        try:
            while True:
                raw = self.process.stdout.readline(2 * 1024 * 1024 + 1)
                if not raw:
                    self._put(ProbeError('HOST_CLOSED'))
                    return
                if len(raw) > 2 * 1024 * 1024:
                    self.overflow.set()
                    return
                self._put(json.loads(raw.decode('utf-8')))
        except Exception:
            self._put(ProbeError('HOST_MESSAGE_INVALID'))

    def _stderr(self):
        # No raw diagnostic text retained: it can contain user paths or credentials.
        total = 0
        while True:
            raw = self.process.stderr.read(16384)
            if not raw:
                return
            total += len(raw)
            if total > 8 * 1024 * 1024:
                self.overflow.set()
                return

    def send(self, value):
        self.process.stdin.write((json.dumps(value, ensure_ascii=False) + '\n').encode('utf-8'))
        self.process.stdin.flush()

    def notify(self, method, params):
        validate_outgoing(method, params)
        self.send({'method': method, 'params': params})

    def request(self, method, params):
        validate_outgoing(method, params)
        self.sequence += 1
        own_id = self.sequence
        self.send({'id': own_id, 'method': method, 'params': params})
        deadline = time.monotonic() + self.timeout
        while True:
            if self.overflow.is_set():
                raise ProbeError('HOST_OUTPUT_LIMIT')
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise ProbeError('HOST_TIMEOUT')
            try:
                item = self.messages.get(timeout=min(remaining, 0.25))
            except queue.Empty:
                continue
            if isinstance(item, ProbeError):
                raise item
            if not isinstance(item, dict):
                raise ProbeError('HOST_MESSAGE_INVALID')
            if 'method' in item and 'id' in item:
                self.send({'id': item['id'], 'error': {'code': -32601,
                    'message': 'The read-only acceptance client does not accept server requests.'}})
                raise ProbeError('SERVER_REQUEST_REQUIRES_SEPARATE_REVIEW')
            if item.get('method') == 'mcpServer/statusUpdated':
                state = item.get('params', {})
                if state.get('name') != KEY:
                    raise ProbeError('UNEXPECTED_MCP_SERVER')
                self.startup.append({'name': KEY, 'status': state.get('status')})
            if item.get('id') != own_id:
                continue
            if 'error' in item:
                raise ProbeError('RPC_ERROR', item['error'].get('code'))
            if 'result' not in item:
                raise ProbeError('HOST_RESPONSE_INVALID')
            return item['result']

    def initialize(self):
        self.request('initialize', {'clientInfo': {'name': 'sps_acceptance_probe',
            'title': 'Spatial Prediction Studio local acceptance', 'version': '0.5.0'}})
        self.notify('initialized', {})

    def close(self):
        process = self.process
        if process is not None:
            try:
                process.stdin.close()
            except (OSError, AttributeError):
                pass
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                self.win32job.TerminateJobObject(self.job, 124)
                process.wait(timeout=3)
        self.job.Close()
        for reader in getattr(self, 'readers', []):
            reader.join(timeout=1)
        if process is not None:
            for stream in (process.stdout, process.stderr):
                if stream is not None:
                    stream.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()


def config_entries(response, expected):
    config = response.get('config', {})
    entries = config.get('mcp_servers')
    if not isinstance(entries, dict) or KEY not in entries:
        raise ProbeError('OWNED_CONFIG_MISSING')
    owned = entries[KEY]
    if not isinstance(owned, dict) or any(owned.get(k) != v for k, v in expected.items()):
        raise ProbeError('OWNED_CONFIG_DIFFERS_FROM_INSTALLATION')
    if owned.get('enabled', True) is not True or owned.get('url'):
        raise ProbeError('OWNED_CONFIG_NOT_ENABLED_STDIO')
    if not all(isinstance(k, str) and isinstance(v, dict) for k, v in entries.items()):
        raise ProbeError('MCP_CONFIG_INVALID')
    return entries


def host_args(codex, other_names=()):
    result = [str(codex), 'app-server', '--stdio']
    for name in DISABLED_FEATURES:
        result += ['-c', 'features.' + name + '=false']
    for name in sorted(other_names):
        # The installed CLI interprets override paths as dotted segments. Quotes
        # become part of the server name, unlike TOML document quoted keys.
        # Fail closed for names which cannot be expressed as one safe segment.
        if not re.fullmatch(r'[A-Za-z0-9_-]+', name):
            raise ProbeError('MCP_NAME_CANNOT_BE_ISOLATED_WITH_CLI_OVERRIDE')
        result += ['-c', 'mcp_servers.' + name + '.enabled=false']
    return result


def isolated_config(response, expected):
    entries = config_entries(response, expected)
    if any(name != KEY and entry.get('enabled', True) is not False for name, entry in entries.items()):
        raise ProbeError('OTHER_MCP_SERVER_STILL_ENABLED')
    features = response['config'].get('features', {})
    if any(features.get(name) is not False for name in DISABLED_FEATURES):
        raise ProbeError('MCP_PLUGIN_ISOLATION_NOT_CONFIRMED')
    return entries


def tool_value(result):
    if not isinstance(result, dict) or result.get('isError') is True:
        raise ProbeError('MCP_TOOL_ERROR')
    value = result.get('structuredContent')
    if not isinstance(value, dict):
        raise ProbeError('MCP_STRUCTURED_RESULT_MISSING')
    return value


def actual_calls(rpc, install_root, session_id):
    started = rpc.request('thread/start', {'ephemeral': True, 'cwd': str(install_root)})
    thread = started.get('thread', {})
    thread_id = thread.get('id')
    if not isinstance(thread_id, str) or thread.get('ephemeral') is not True:
        raise ProbeError('EPHEMERAL_THREAD_NOT_CONFIRMED')
    listing = rpc.request('mcpServerStatus/list', {'threadId': thread_id,
        'serverName': KEY, 'detail': 'toolsAndAuthOnly', 'limit': 1})
    records = listing.get('data', [])
    if len(records) != 1 or records[0].get('name') != KEY or listing.get('nextCursor'):
        raise ProbeError('OWNED_SERVER_DISCOVERY_FAILED')
    entry = records[0]
    tools = entry.get('tools', {})
    names = sorted(item.get('name', key) for key, item in tools.items())
    if entry.get('toolsError') or not ALLOWED_TOOLS.issubset(names) or len(names) != 15:
        raise ProbeError('OWNED_TOOLS_INCOMPLETE')
    discovered = tool_value(rpc.request('mcpServer/tool/call', {'threadId': thread_id,
        'server': KEY, 'tool': 'discover_sessions', 'arguments': {}}))
    sessions = discovered.get('sessions')
    if not isinstance(sessions, list):
        raise ProbeError('ARCMap_SESSION_RESPONSE_INVALID')
    result = {'tools_discovered': names, 'tool_count': len(names), 'discover_sessions': 'passed',
        'active_arcmap_session_count': len(sessions), 'arcmap_status': 'not_requested',
        'host_thread_ephemeral': True, 'host_thread_id': thread_id}
    if session_id:
        if session_id not in {row.get('session_id') for row in sessions if isinstance(row, dict)}:
            raise ProbeError('REQUESTED_ARCMAP_SESSION_NOT_DISCOVERED')
        status = tool_value(rpc.request('mcpServer/tool/call', {'threadId': thread_id,
            'server': KEY, 'tool': 'status', 'arguments': {'session_id': session_id}}))
        if type(status.get('busy')) is not bool or type(status.get('preview_ready')) is not bool:
            raise ProbeError('ARCMap_STATUS_RESPONSE_INVALID')
        result.update({'arcmap_status': 'passed', 'session_id': session_id,
            'busy': status['busy'], 'preview_ready': status['preview_ready']})
    return result


def run_probe(codex, install_root, session_id, rpc_factory=Rpc):
    _, expected_doc = template('codex', install_root)
    expected = expected_doc['mcp_servers'][KEY]
    # First process reads the fully merged configuration, but starts no thread,
    # MCP discovery, model turn, or auth flow. No secret leaves process memory.
    with rpc_factory(host_args(codex), install_root) as rpc:
        rpc.initialize()
        config = rpc.request('config/read', {'cwd': str(install_root), 'includeLayers': False})
        entries = config_entries(config, expected)
        other_names = set(entries) - {KEY}
    # A fresh process applies ONLY restrictive, temporary MCP/feature overrides.
    # Read back effective settings BEFORE any thread or MCP connection is created.
    with rpc_factory(host_args(codex, other_names), install_root) as rpc:
        rpc.initialize()
        config = rpc.request('config/read', {'cwd': str(install_root), 'includeLayers': False})
        isolated_config(config, expected)
        result = actual_calls(rpc, install_root, session_id)
        result['startup_notifications'] = rpc.startup
    result['other_configured_servers_disabled_for_probe'] = len(other_names)
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--session-id')
    parser.add_argument('--codex', type=Path)
    args = parser.parse_args(argv)
    if not args.output.resolve().is_relative_to((ROOT / 'build').resolve()) or args.output.exists():
        parser.error('--output must be a new evidence file under the workspace build directory.')
    if args.session_id and not re.fullmatch(r'[A-Za-z0-9_-]{1,100}', args.session_id):
        parser.error('Invalid session id.')
    codex = args.codex or shutil.which('codex.exe')
    evidence = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'scope': 'separate_local_codex_app_server', 'global_config_writes': False,
        'auth_or_trust_changes': False, 'model_turn_requested': False,
        'current_desktop_chat_tools': 'not_verified', 'all_seven_clients_accepted': False,
        'probe_sha256': hashlib.sha256(Path(__file__).read_bytes()).hexdigest()}
    try:
        if not codex:
            raise ProbeError('CODEX_NOT_FOUND')
        evidence['result'] = run_probe(codex, args.install_root.resolve(), args.session_id)
        evidence['requested_checks_passed'] = True
    except ProbeError as error:
        evidence.update({'requested_checks_passed': False, 'error_code': error.code,
                         'rpc_error_code': error.rpc_code})
    except Exception as error:
        evidence.update({'requested_checks_passed': False, 'error_type': type(error).__name__})
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'evidence': str(args.output.resolve()),
        'requested_checks_passed': evidence['requested_checks_passed'],
        'current_desktop_chat_tools': 'not_verified'}))
    return 0 if evidence['requested_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
