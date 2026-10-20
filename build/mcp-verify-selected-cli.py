"""Selected Claude Code/OpenCode MCP health only. Never sends a model prompt."""
import argparse
import importlib.util
import json
import os
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location('client_verifier', Path(__file__).with_name('mcp-verify-clients.py'))
helper = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(helper)
KEY = helper.KEY


class HealthError(Exception):
    pass


def checked_config(status, raw, expected):
    if status.get('exit_code') != 0:
        raise HealthError('CONFIG_READ_FAILED')
    try:
        value = helper.strict_json(raw)
    except (ValueError, TypeError):
        raise HealthError('CONFIG_JSON_INVALID') from None
    entries = value.get('mcp') if isinstance(value, dict) else None
    if not isinstance(entries, dict) or not isinstance(entries.get(KEY), dict):
        raise HealthError('OWNED_CONFIG_MISSING')
    if any(not helper.same_json_value(entries[KEY].get(key), val) for key, val in expected.items()):
        raise HealthError('OWNED_CONFIG_MISMATCH')
    if any(not isinstance(name, str) or not isinstance(entry, dict) for name, entry in entries.items()):
        raise HealthError('MCP_CONFIG_INVALID')
    return entries


def health(client, install_root, command=helper.command):
    if helper.registration(client, install_root)['state'] != 'matching':
        raise HealthError('REGISTRATION_NOT_MATCHING')
    launcher = helper.executable('claude' if client == 'claude_code' else 'opencode')
    if not launcher:
        raise HealthError('CLI_NOT_FOUND')
    if client == 'claude_code':
        result, raw = command(launcher + ['mcp', 'get', KEY], timeout=50, cwd=install_root)
        return {**result, 'state': helper.health_status(raw), 'scope': 'selected_cli_health',
                'tools_call': 'not_performed_no_documented_direct_cli_method'}
    if os.environ.get('OPENCODE_CONFIG_CONTENT'):
        raise HealthError('EXISTING_INLINE_CONFIG_REQUIRES_SEPARATE_REVIEW')
    _, fragment = helper.template('opencode', install_root)
    expected = fragment['mcp'][KEY]
    process_env = os.environ.copy()
    process_env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'autoupdate': False})
    result, raw = command(launcher + ['debug', 'config', '--pure'], timeout=45, env=process_env, cwd=install_root)
    entries = checked_config(result, raw, expected)
    disabled = {name: {'enabled': False} for name in entries if name != KEY}
    process_env['OPENCODE_CONFIG_CONTENT'] = json.dumps({'autoupdate': False, 'mcp': disabled})
    result, raw = command(launcher + ['debug', 'config', '--pure'], timeout=45, env=process_env, cwd=install_root)
    checked = checked_config(result, raw, expected)
    if any(name != KEY and entry.get('enabled', True) is not False for name, entry in checked.items()):
        raise HealthError('OTHER_MCP_SERVER_STILL_ENABLED')
    result, raw = command(launcher + ['mcp', 'list', '--pure'], timeout=50, env=process_env, cwd=install_root)
    owned_lines = '\n'.join(line for line in raw.splitlines() if KEY in line)
    return {**result, 'state': helper.health_status(owned_lines),
        'scope': 'cli_health_with_other_servers_temporarily_disabled',
        'other_configured_servers_disabled_for_probe': len(disabled),
        'tools_call': 'not_performed_no_documented_direct_cli_method'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--install-root', required=True, type=Path)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--client', required=True, choices=('claude_code', 'opencode'), action='append')
    args = parser.parse_args(argv)
    if not args.output.resolve().is_relative_to((ROOT / 'build').resolve()) or args.output.exists():
        parser.error('Output must be a new evidence file under build.')
    output = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'global_config_writes': False, 'model_prompt_sent': False,
        'active_chat_tools': 'not_verified', 'all_seven_clients_accepted': False, 'clients': {}}
    for client in dict.fromkeys(args.client):
        try:
            output['clients'][client] = health(client, args.install_root.resolve())
        except HealthError as error:
            output['clients'][client] = {'state': 'failed', 'reason': str(error)}
        except Exception as error:
            output['clients'][client] = {'state': 'failed', 'error_type': type(error).__name__}
    passed = all(row.get('state') == 'connected' and row.get('exit_code') == 0 for row in output['clients'].values())
    output['requested_checks_passed'] = passed
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(output, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'evidence': str(args.output.resolve()), 'requested_checks_passed': passed,
                      'scope': 'client_health_only'}))
    return 0 if passed else 1


if __name__ == '__main__':
    raise SystemExit(main())
