"""Prepare or explicitly apply one owned Claude Store registration.

No raw configuration or log content is printed or persisted. The default is
read-only. Application requires the SHA256 from the preparation evidence.
"""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
INSTALL = Path.home() / r'AppData\Local\Programs\SpatialPredictionStudio\versions\0.5.0-70573b90e438'
TARGET = Path.home() / r'AppData\Local\Packages\Claude_pzs8sxrjxfjjc\LocalCache\Roaming\Claude\claude_desktop_config.json'
REGULAR = Path.home() / r'AppData\Roaming\Claude\claude_desktop_config.json'
sys.path.insert(0, str(INSTALL / 'mcp-server'))
from register_clients import KEY, atomic_merge, merge_owned_text, template
from sps_mcp.bridge import BridgeError, reject_reparse, strict_json
from sps_mcp.jsonc import same_json_value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def canonical(value):
    return sha(json.dumps(value, ensure_ascii=False, sort_keys=True,
                          separators=(',', ':'), allow_nan=False).encode('utf-8'))


def summary(data):
    value = strict_json(data.decode('utf-8-sig'))
    if not isinstance(value, dict) or not isinstance(value.get('mcpServers', {}), dict):
        raise BridgeError('CONFIG_SHAPE', 'Unsupported configuration structure.')
    other = copy.deepcopy(value)
    servers = other.get('mcpServers', {})
    servers.pop(KEY, None)
    outside = copy.deepcopy(other)
    outside.pop('mcpServers', None)
    return {'bytes': len(data), 'sha256': sha(data),
        'utf8_bom': data.startswith(b'\xef\xbb\xbf'),
        'owned_present': KEY in value.get('mcpServers', {}),
        'other_server_count': len(servers), 'other_servers_sha256': canonical(servers),
        'nonowned_semantics_sha256': canonical(other),
        'outside_mcp_sha256': canonical(outside)}, value


def read(path):
    reject_reparse(path)
    data = path.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise BridgeError('CONFIG_LIMIT', 'Configuration is too large.')
    return data


def log_metadata():
    path = TARGET.parent / 'logs' / ('mcp-server-' + KEY + '.log')
    info = {'path': str(path), 'exists': path.is_file(), 'contents_read': False}
    if info['exists']:
        meta = path.stat()
        info.update({'bytes': meta.st_size,
            'modified_utc': datetime.fromtimestamp(meta.st_mtime, timezone.utc).isoformat()})
    return info


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-sha256')
    args = parser.parse_args(argv)
    if args.apply and not re.fullmatch(r'[0-9a-fA-F]{64}', args.expected_sha256 or ''):
        parser.error('--apply requires the original --expected-sha256.')
    record = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'target': str(TARGET), 'install_root': str(INSTALL), 'applied': False,
        'raw_config_persisted': False, 'log_contents_read': False,
        'host_connection_verified': False, 'other_config_writes': False}
    try:
        before_bytes = read(TARGET)
        before, _ = summary(before_bytes)
        record['before'] = before
        regular_hash = sha(read(REGULAR)) if REGULAR.is_file() else None
        fmt, patch = template('claude_desktop', INSTALL)
        expected = patch['mcpServers'][KEY]
        installed_fragment = strict_json(read(INSTALL / 'client-configurations/claude_desktop.json').decode('utf-8-sig'))
        if not same_json_value(installed_fragment, patch):
            raise BridgeError('TEMPLATE_MISMATCH', 'Installed template does not match the installed helper.')
        preview = merge_owned_text(before_bytes.decode('utf-8-sig'), fmt, patch).encode('utf-8')
        if before['utf8_bom']:
            preview = b'\xef\xbb\xbf' + preview
        after_preview, _ = summary(preview)
        for field in ('nonowned_semantics_sha256', 'other_servers_sha256', 'outside_mcp_sha256'):
            if before[field] != after_preview[field]:
                raise BridgeError('NONOWNED_CHANGED', 'Preparation would change unrelated settings.')
        record['prepared_nonowned_preserved'] = True
        record['dedicated_log_before'] = log_metadata()
        if not args.apply:
            record.update({'status': 'prepared_read_only', 'passed': True})
        else:
            if sha(before_bytes) != args.expected_sha256.lower():
                raise BridgeError('EXPECTED_HASH_CHANGED', 'The configuration changed after preparation.')
            if read(TARGET) != before_bytes:
                raise BridgeError('CONFIG_CHANGED', 'The configuration changed before merging.')
            result = atomic_merge(TARGET, fmt, patch)
            record['applied'] = result.get('status') != 'unchanged'
            record['merge_result'] = result
            after_bytes = read(TARGET)
            after, document = summary(after_bytes)
            record['after'] = after
            record['owned_entry_matches'] = same_json_value(document['mcpServers'].get(KEY), expected)
            record['nonowned_preserved'] = all(before[field] == after[field]
                for field in ('nonowned_semantics_sha256', 'other_servers_sha256', 'outside_mcp_sha256'))
            record['bom_preserved'] = before['utf8_bom'] == after['utf8_bom']
            record['regular_roaming_unchanged'] = (sha(read(REGULAR)) if REGULAR.is_file() else None) == regular_hash
            backup = result.get('backup')
            record['original_backup_verified'] = bool(backup) and read(Path(backup)) == before_bytes
            if result.get('status') == 'unchanged':
                record['original_backup_verified'] = None
            checks = [record['owned_entry_matches'], record['nonowned_preserved'],
                record['bom_preserved'], record['regular_roaming_unchanged']]
            if record['applied']:
                checks.append(record['original_backup_verified'])
            if not all(checks):
                raise BridgeError('READBACK_FAILED', 'Post-write verification failed.')
            record.update({'status': 'passed_restart_required', 'passed': True})
    except BridgeError as error:
        record.update({'status': 'failed', 'passed': False, 'error_code': error.code})
    except Exception as error:
        record.update({'status': 'failed', 'passed': False, 'error_type': type(error).__name__})
    destination = ROOT / 'build' / ('mcp-claude-store-merge-applied.json' if args.apply else 'mcp-claude-store-merge-prepared.json')
    destination.write_text(json.dumps(record, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps(record, ensure_ascii=False))
    return 0 if record.get('passed') else 1


if __name__ == '__main__':
    raise SystemExit(main())
