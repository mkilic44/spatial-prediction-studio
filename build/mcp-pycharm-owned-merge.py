"""Prepare a PyCharm owned-entry update; explicit application requires both hashes."""
from __future__ import annotations

import argparse
import copy
import csv
import hashlib
import json
import re
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
VERSIONS = Path(r'C:\Users\MIRAC\AppData\Local\Programs\SpatialPredictionStudio\versions')
TARGET = Path(r'C:\Users\MIRAC\.ai\mcp\mcp.json')
XML = Path(r'C:\Users\MIRAC\AppData\Roaming\JetBrains\PyCharm2026.2\options\llm.mcpServers.xml')
sys.path.insert(0, str(ROOT / 'mcp-server'))
import register_clients as registration
from sps_mcp.bridge import BridgeError, reject_reparse, strict_json
from sps_mcp.jsonc import same_json_value


def sha(data):
    return hashlib.sha256(data).hexdigest()


def read(path):
    reject_reparse(path)
    data = path.read_bytes()
    if len(data) > 8 * 1024 * 1024:
        raise BridgeError('CONFIG_LIMIT', 'Configuration exceeds the size limit.')
    return data


def xml_hash():
    reject_reparse(XML)
    return sha(read(XML)) if XML.exists() else 'absent'


def summary(raw):
    document = strict_json(raw.decode('utf-8-sig'))
    if not isinstance(document, dict) or not isinstance(document.get('mcpServers'), dict):
        raise BridgeError('CONFIG_SHAPE', 'Expected a server object in the existing global configuration.')
    others = copy.deepcopy(document)
    entry = others['mcpServers'].pop(registration.KEY, None)
    def digest(value):
        return sha(json.dumps(value, sort_keys=True, ensure_ascii=False,
                              separators=(',', ':'), allow_nan=False).encode('utf-8'))
    return {'bytes': len(raw), 'sha256': sha(raw), 'utf8_bom': raw.startswith(b'\xef\xbb\xbf'),
            'owned_present': entry is not None, 'owned_marker_matches': registration.owned(entry),
            'other_server_count': len(others['mcpServers']),
            'other_servers_sha256': digest(others['mcpServers']),
            'nonowned_semantics_sha256': digest(others)}, document


def pycharm_running():
    for name in ('pycharm64.exe', 'pycharm.exe'):
        result = subprocess.run(['tasklist.exe', '/FI', 'IMAGENAME eq ' + name, '/FO', 'CSV', '/NH'],
                                capture_output=True, text=True, encoding='utf-8', errors='replace',
                                timeout=10, check=True,
                                creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
        if any(row and row[0].casefold() == name for row in csv.reader(result.stdout.splitlines())):
            return True
    return False


def execute(installation_root, apply=False, expected_json=None, expected_xml=None):
    record = {'schema_version': 1, 'created_utc': datetime.now(timezone.utc).isoformat(),
              'installation_root': str(installation_root), 'target': str(TARGET),
              'xml_permissions_path': str(XML), 'applied': False, 'write_attempted': False,
              'raw_configuration_in_evidence': False, 'connection_verified': False}
    before_raw = None
    try:
        reject_reparse(installation_root)
        if not installation_root.is_absolute() or installation_root.resolve().parent != VERSIONS.resolve():
            raise BridgeError('INSTALLATION_PATH', 'Expected a direct installed version directory.')
        before_raw = read(TARGET)
        before, document = summary(before_raw)
        record['before'] = before
        before_xml = xml_hash()
        record['xml_sha256_before'] = before_xml
        if not before['owned_present'] or not before['owned_marker_matches']:
            raise BridgeError('OWNERSHIP_CONFLICT', 'The existing entry is not owned by Spatial Prediction Studio.')
        fmt, patch = registration.template('pycharm', installation_root)
        fragment_path = installation_root / 'client-configurations/pycharm.json'
        fragment_bytes = read(fragment_path)
        fragment = strict_json(fragment_bytes.decode('utf-8-sig'))
        if not same_json_value(fragment, patch):
            raise BridgeError('TEMPLATE_MISMATCH', 'Installed PyCharm template differs from the expected registration.')
        record['template_sha256'] = sha(fragment_bytes)
        record['registration_helper_sha256'] = sha(read(Path(registration.__file__)))
        preview = registration.merge_owned_text(before_raw.decode('utf-8-sig'), fmt, patch).encode('utf-8')
        if before['utf8_bom']:
            preview = b'\xef\xbb\xbf' + preview
        preview_summary, _ = summary(preview)
        fields = ('other_servers_sha256', 'nonowned_semantics_sha256', 'utf8_bom')
        if any(before[key] != preview_summary[key] for key in fields):
            raise BridgeError('NONOWNED_CHANGED', 'Preparation changed unrelated settings.')
        record['prepared_nonowned_preserved'] = True
        if not apply:
            record.update(status='prepared_read_only', passed=True,
                          owned_entry_already_matches=same_json_value(document['mcpServers'][registration.KEY], patch['mcpServers'][registration.KEY]))
            return record
        if not re.fullmatch('[0-9a-fA-F]{64}', expected_json or '') or not (
                expected_xml == 'absent' or re.fullmatch('[0-9a-fA-F]{64}', expected_xml or '')):
            raise BridgeError('EXPECTED_HASH_REQUIRED', 'Application requires both original fingerprints.')
        if before['sha256'] != expected_json.lower() or before_xml != expected_xml.lower():
            raise BridgeError('EXPECTED_HASH_CHANGED', 'The JSON or XML configuration changed after preparation.')
        if pycharm_running():
            raise BridgeError('PYCHARM_RUNNING', 'Close PyCharm normally before changing its global registration.')
        original_merge = registration.merge_owned_text
        def guarded_merge(original, actual_format, actual_patch):
            # atomic_merge invokes this while holding its exclusive lock, after
            # reading the exact configuration which it will replace.
            if read(TARGET) != before_raw or original != before_raw.decode('utf-8-sig') or xml_hash() != before_xml:
                raise BridgeError('CONFIG_CHANGED', 'The configuration changed before the locked merge.')
            return original_merge(original, actual_format, actual_patch)
        registration.merge_owned_text = guarded_merge
        try:
            record['write_attempted'] = True
            result = registration.atomic_merge(TARGET, fmt, patch)
        finally:
            registration.merge_owned_text = original_merge
        after_raw = read(TARGET)
        after, document = summary(after_raw)
        record.update(after=after, merge_result=result, applied=after_raw != before_raw,
                      xml_sha256_after=xml_hash())
        record['owned_entry_matches'] = same_json_value(document['mcpServers'][registration.KEY], patch['mcpServers'][registration.KEY])
        record['nonowned_preserved'] = all(before[key] == after[key] for key in fields)
        record['xml_permissions_bytes_unchanged'] = record['xml_sha256_after'] == before_xml
        record['original_backup_verified'] = (read(Path(result['backup'])) == before_raw) if result.get('backup') else None
        checks = [record['owned_entry_matches'], record['nonowned_preserved'], record['xml_permissions_bytes_unchanged']]
        if record['applied']:
            checks.append(record['original_backup_verified'])
        if not all(checks):
            raise BridgeError('READBACK_FAILED', 'Post-write verification failed.')
        record.update(status='passed_restart_required', passed=True)
    except Exception as error:
        record.update(status='failed', passed=False, error_code=getattr(error, 'code', type(error).__name__))
        if record['write_attempted'] and before_raw is not None:
            try:
                record['applied'] = read(TARGET) != before_raw
            except Exception:
                record['applied'] = None
    return record


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--root', type=Path, required=True)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--apply', action='store_true')
    parser.add_argument('--expected-json-sha256')
    parser.add_argument('--expected-xml-sha256')
    args = parser.parse_args(argv)
    if not args.output.resolve().is_relative_to((ROOT / 'build').resolve()) or args.output.exists():
        parser.error('Output must be a new evidence file under workspace build.')
    reject_reparse(args.output)
    result = execute(args.root, args.apply, args.expected_json_sha256, args.expected_xml_sha256)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with args.output.open('x', encoding='utf-8') as stream:
        json.dump(result, stream, ensure_ascii=False, indent=2)
        stream.write('\n')
    print(json.dumps(result, ensure_ascii=False))
    return 0 if result['passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
