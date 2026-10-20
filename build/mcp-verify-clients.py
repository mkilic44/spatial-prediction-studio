"""Read-only CLI/config verification. Raw output and secrets are never persisted."""
from __future__ import annotations
import argparse
import asyncio
import json
import os
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
from register_clients import CLIENTS, KEY, target_paths, template
from sps_mcp.bridge import BridgeError, strict_json, reject_reparse
from sps_mcp.jsonc import loads as jsonc_loads, same_json_value
import tomlkit

EXPECTED_TOOLS = {'discover_sessions', 'status', 'layers', 'get_project', 'open_project',
    'save_project', 'preview', 'run_analysis', 'cancel', 'list_artifacts', 'read_artifact',
    'read_report', 'read_table_page', 'export_model', 'export_report'}


def executable(name):
    found = shutil.which(name + '.exe')
    if found:
        return [found]
    if name == 'opencode':
        script = shutil.which('opencode.ps1')
        if script:
            return [str(Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'),
                    '-NoProfile', '-File', script]
    return None


def command(args, timeout=15, *, env=None, cwd=ROOT):
    """Bound stdout in memory and kill only this probe's process job on timeout."""
    import win32api
    import win32con
    import win32job
    job = win32job.CreateJobObject(None, '')
    info = win32job.QueryInformationJobObject(job, win32job.JobObjectExtendedLimitInformation)
    info['BasicLimitInformation']['LimitFlags'] |= win32job.JOB_OBJECT_LIMIT_KILL_ON_JOB_CLOSE
    win32job.SetInformationJobObject(job, win32job.JobObjectExtendedLimitInformation, info)
    started = time.monotonic()
    process = None
    buffer, overflow = bytearray(), threading.Event()
    try:
        process = subprocess.Popen(args, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, cwd=cwd, env=env, creationflags=subprocess.CREATE_NO_WINDOW)
        process_handle = win32api.OpenProcess(win32con.PROCESS_SET_QUOTA | win32con.PROCESS_TERMINATE, False, process.pid)
        try:
            win32job.AssignProcessToJobObject(job, process_handle)
        finally:
            process_handle.Close()
        def drain():
            while True:
                part = process.stdout.read(16384)
                if not part:
                    break
                if len(buffer) + len(part) > 2 * 1024 * 1024:
                    overflow.set()
                    break
                buffer.extend(part)
        reader = threading.Thread(target=drain, daemon=True)
        reader.start()
        while process.poll() is None:
            if overflow.is_set() or time.monotonic() - started > timeout:
                win32job.TerminateJobObject(job, 124)
                process.wait(timeout=3)
                reader.join(timeout=2)
                return {'exit_code': 124, 'status': 'output_limit' if overflow.is_set() else 'timeout'}, ''
            time.sleep(0.03)
        reader.join(timeout=2)
        return {'exit_code': process.returncode, 'status': 'exited',
                'seconds': round(time.monotonic() - started, 3)}, buffer.decode('utf-8', 'replace')
    except Exception as error:
        if process is not None and process.poll() is None:
            process.kill()
            process.wait(timeout=3)
        return {'exit_code': None, 'status': 'probe_error', 'error_type': type(error).__name__}, ''
    finally:
        job.Close()
        if process is not None and process.stdout:
            process.stdout.close()


def health_status(output):
    # A negative/pending state must never be mistaken for Connected.
    text = re.sub(r'\x1b\[[0-9;]*[A-Za-z]', '', output).lower()
    if any(word in text for word in ('failed', 'error', 'disconnected', 'not connected')):
        return 'failed'
    if 'pending approval' in text or 'needs authentication' in text:
        return 'pending_approval_or_authentication'
    if 'disabled' in text:
        return 'disabled'
    return 'connected' if re.search(r'\bconnected\b', text) else 'not_reported'


def supported_commands(output):
    section = output.split('Commands:', 1)[-1].split('Options:', 1)[0]
    return {key: re.search(r'^\s*(?:(?:codex|claude|opencode)\s+mcp\s+)?' + key + r'(?:\s|\[|$)',
                           section, re.MULTILINE) is not None
            for key in ('list', 'get', 'add', 'remove', 'debug')}


def requested_checks_passed(output, args):
    if getattr(args, 'install_root', None) and (set(output.get('registration', {})) != set(CLIENTS) or
            any(row.get('state') != 'matching' for row in output.get('registration', {}).values())):
        return False
    if getattr(args, 'profile_sdk_handshakes', False):
        expected_count = sum(len(row.get('profiles', [])) for row in output.get('registration', {}).values())
        results = output.get('profile_sdk', [])
        if not expected_count or len(results) != expected_count or any(
                row.get('handshake') != 'passed' or row.get('tool_count') != 15 or
                row.get('session_discovery') != 'passed' or
                (args.session_id and row.get('arcmap_status') != 'passed') for row in results):
            return False
    if args.sdk_handshake:
        sdk = output.get('sdk', {})
        if sdk.get('handshake') != 'passed' or sdk.get('tool_count') != 15 or sdk.get('session_discovery') != 'passed':
            return False
        if args.session_id and sdk.get('arcmap_status') != 'passed':
            return False
    for name, enabled in (('claude', args.claude_health), ('opencode', args.opencode_all_health)):
        if enabled:
            health = output['cli'].get(name, {}).get('health', {})
            if health.get('state') != 'connected' or health.get('exit_code') != 0:
                return False
    return True


def profile_paths(client):
    return [Path.home() / '.ai/mcp/mcp.json'] if client == 'pycharm' else target_paths(client)


def profile_registration(client, install_root, path):
    """Return sanitized metadata and a private, verified launch entry separately."""
    reject_reparse(path)
    if not path.exists():
        return {'state': 'file_missing', 'path': str(path)}, None
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        return {'state': 'size_limit', 'path': str(path)}, None
    text = raw.decode('utf-8-sig')
    value = tomlkit.parse(text).unwrap() if path.suffix == '.toml' else (
        jsonc_loads(text) if path.suffix == '.jsonc' else strict_json(text))
    fmt, patch = template(client, install_root)
    group = 'mcp_servers' if fmt == 'toml' else ('mcp' if client == 'opencode' else 'mcpServers')
    entry = value.get(group, {}).get(KEY)
    expected = patch[group][KEY]
    matches = isinstance(entry, dict) and all(same_json_value(entry.get(key), item) for key, item in expected.items())
    result = {'state': 'matching' if matches else ('not_registered' if entry is None else 'different'),
              'path': str(path), 'verified_scope': 'configuration_only'}
    return result, expected if matches else None


def registration(client, install_root):
    try:
        profiles = [profile_registration(client, install_root, path)[0] for path in profile_paths(client)]
        return {'state': 'matching' if profiles and all(row['state'] == 'matching' for row in profiles) else 'different',
                'profiles': profiles, 'verified_scope': 'configuration_only'}
    except BridgeError as error:
        return {'state': 'read_error', 'code': error.code}
    except Exception as error:
        return {'state': 'read_error', 'error_type': type(error).__name__}


def antigravity_log_evidence():
    user = Path.home()
    candidates = []
    direct = user / '.gemini/antigravity/logs/vs_ls.log'
    if direct.exists():
        candidates.append(direct)
    root = user / 'AppData/Roaming/Antigravity/logs'
    if root.exists():
        folders = sorted((p for p in root.iterdir() if p.is_dir()), key=lambda p: p.stat().st_mtime, reverse=True)[:2]
        for folder in folders:
            # Do not inspect auth/network logs or arbitrary files.
            candidates.extend(p for p in folder.rglob('*.log') if 'mcp' in p.name.lower() or p.name in {'ls-main.log', 'main.log'})
    records = []
    for path in candidates[:20]:
        stat = path.stat()
        with path.open('rb') as stream:
            stream.seek(max(0, stat.st_size - 1024 * 1024))
            data = stream.read(1024 * 1024).decode('utf-8', 'replace')
        matching = [line for line in data.splitlines() if KEY in line]
        records.append({'path': str(path), 'modified_utc': datetime.fromtimestamp(stat.st_mtime, timezone.utc).isoformat(),
                        'bytes': stat.st_size, 'owned_server_mentions': len(matching),
                        'connection_status': 'not_established_by_log_count'})
    return records


async def sdk_probe(install_root, session_id, launch=None):
    from mcp import Client, StdioServerParameters
    result = {'scope': 'fresh_sdk_client_not_desktop_chat', 'active_chat_tools': 'not_verified'}
    params = StdioServerParameters(**(launch or {
        'command': str(install_root / 'runtime-mcp/Scripts/python.exe'),
        'args': ['-X', 'utf8', str(install_root / 'mcp-server/server.py')], 'cwd': str(install_root)}))
    async with asyncio.timeout(45):
        async with Client(params) as client:
            listing = await client.list_tools()
            names = sorted(tool.name for tool in listing.tools)
            result.update({'handshake': 'passed', 'tool_count': len(names), 'tools': names})
            if set(names) != EXPECTED_TOOLS or len(names) != 15:
                result['handshake'] = 'failed_tools_mismatch'
                return result
            discovered = await client.call_tool('discover_sessions', {})
            if discovered.is_error or not isinstance(discovered.structured_content, dict):
                result['session_discovery'] = 'failed'
                return result
            sessions = discovered.structured_content.get('sessions', [])
            if not isinstance(sessions, list) or any(not isinstance(row, dict) for row in sessions):
                result['session_discovery'] = 'failed'
                return result
            result['session_discovery'] = 'passed'
            result['active_arcmap_session_count'] = len(sessions)
            if not session_id:
                result['arcmap_status'] = 'not_requested_without_explicit_session'
                return result
            if session_id not in {row.get('session_id') for row in sessions}:
                result['arcmap_status'] = 'session_not_discovered'
                return result
            status = await client.call_tool('status', {'session_id': session_id})
            value = status.structured_content if isinstance(status.structured_content, dict) else {}
            result['arcmap_status'] = 'passed' if not status.is_error and all(
                type(value.get(field)) is bool for field in ('busy', 'preview_ready')) else 'failed'
            result['busy'] = value.get('busy') if type(value.get('busy')) is bool else None
            result['preview_ready'] = value.get('preview_ready') if type(value.get('preview_ready')) is bool else None
    return result


async def profile_sdk_probes(install_root, session_id):
    results = []
    for name in CLIENTS:
        for path in profile_paths(name):
            result = {'client': name, 'configuration_path': str(path)}
            try:
                state, entry = profile_registration(name, install_root, path)
                if state['state'] != 'matching':
                    raise BridgeError('REGISTRATION_NOT_MATCHING', 'Configuration does not match the installed root.')
                if name == 'opencode':
                    launch = {'command': entry['command'][0], 'args': entry['command'][1:],
                              'env': entry['environment']}
                else:
                    launch = {key: entry[key] for key in ('command', 'args', 'env', 'cwd') if key in entry}
                result.update(await sdk_probe(install_root, session_id, launch))
                result['registered_launch_verified_before_probe'] = True
                result['actual_client_host_test'] = False
            except Exception as error:
                result.update(handshake='failed', error_code=getattr(error, 'code', type(error).__name__))
            results.append(result)
    return results


def main(argv=None):
    parser = argparse.ArgumentParser(description='Read-only MCP client verification; no global configuration writes.')
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--install-root', type=Path)
    parser.add_argument('--sdk-handshake', action='store_true')
    parser.add_argument('--profile-sdk-handshakes', action='store_true')
    parser.add_argument('--skip-cli-inventory', action='store_true')
    parser.add_argument('--antigravity-log-evidence', action='store_true')
    parser.add_argument('--session-id')
    parser.add_argument('--claude-health', action='store_true')
    parser.add_argument('--opencode-all-health', action='store_true',
        help='Explicitly health-checks all configured OpenCode MCP servers, because this CLI has no name filter.')
    args = parser.parse_args(argv)
    if not args.output.resolve().is_relative_to((ROOT / 'build').resolve()) or args.output.exists():
        parser.error('--output must be a new evidence file under this workspace build directory.')
    if (args.sdk_handshake or args.profile_sdk_handshakes or args.claude_health or args.opencode_all_health) and not args.install_root:
        parser.error('Live probes require --install-root.')
    output = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'global_config_writes': False, 'active_chat_tools': 'not_verified', 'cli': {},
        'registration': {}, 'antigravity_logs': antigravity_log_evidence() if args.antigravity_log_evidence else []}
    launchers = {name: None if args.skip_cli_inventory else executable(name) for name in ('codex', 'claude', 'opencode')}
    for name, launcher in launchers.items():
        if launcher is None:
            output['cli'][name] = {'inventory': 'skipped'} if args.skip_cli_inventory else {'installed': False}
            continue
        status, value = command(launcher + ['--version'])
        match = re.search(r'\d+\.\d+\.\d+(?:[-.][A-Za-z0-9]+)*', value)
        output['cli'][name] = {'installed': True, 'launcher': launcher[0],
            'version': match.group() if match else None, 'version_probe': status}
        status, value = command(launcher + ['mcp', '--help'])
        output['cli'][name]['commands'] = supported_commands(value)
    if args.install_root:
        args.install_root = args.install_root.resolve()
        output['registration'] = {name: registration(name, args.install_root) for name in CLIENTS}
    if launchers['codex']:
        status, value = command(launchers['codex'] + ['mcp', 'get', KEY, '--json'])
        present = False
        try:
            parsed = strict_json(value)
            present = status['exit_code'] == 0 and isinstance(parsed, dict) and parsed.get('name') == KEY
        except (ValueError, TypeError):
            pass
        output['cli']['codex']['owned_get'] = {**status, 'registered': present, 'connection_verified': False}
        status, value = command(launchers['codex'] + ['mcp', 'list', '--json'])
        try:
            parsed = strict_json(value)
            present = isinstance(parsed, list) and any(isinstance(row, dict) and row.get('name') == KEY for row in parsed)
        except (ValueError, TypeError):
            present = False
        output['cli']['codex']['owned_list'] = {**status, 'registered': present, 'connection_verified': False}
    for name, enabled in (('claude', args.claude_health), ('opencode', args.opencode_all_health)):
        if enabled and launchers[name]:
            tail = ['mcp', 'get', KEY] if name == 'claude' else ['mcp', 'list', '--pure']
            status, value = command(launchers[name] + tail, timeout=50)
            if name == 'opencode':
                value = '\n'.join(line for line in value.splitlines() if KEY in line)
            output['cli'][name]['health'] = {**status, 'state': health_status(value),
                'scope': 'selected_server' if name == 'claude' else 'all_configured_servers_called_only_owned_status_retained',
                'active_chat_tools': 'not_verified'}
    if args.sdk_handshake:
        try:
            output['sdk'] = asyncio.run(sdk_probe(args.install_root, args.session_id))
        except Exception as error:
            output['sdk'] = {'state': 'failed', 'error_type': type(error).__name__, 'active_chat_tools': 'not_verified'}
    if args.profile_sdk_handshakes:
        try:
            output['profile_sdk'] = asyncio.run(profile_sdk_probes(args.install_root, args.session_id))
        except Exception as error:
            output['profile_sdk_error'] = {'error_code': getattr(error, 'code', type(error).__name__)}
    passed = requested_checks_passed(output, args)
    output['requested_checks_passed'] = passed
    output['all_seven_clients_accepted'] = False
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'evidence': str(args.output.resolve()), 'global_config_writes': False,
        'active_chat_tools': 'not_verified', 'live_probes_requested': args.sdk_handshake or args.profile_sdk_handshakes or args.claude_health or args.opencode_all_health}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
