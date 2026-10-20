"""Hash-only client configuration snapshot. Never writes client settings."""
import argparse
import copy
import hashlib
import json
import os
import sys
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / 'mcp-server'))
import tomlkit
from register_clients import CLIENTS, KEY, OWNER, target_path, claude_candidate_paths
from sps_mcp.bridge import strict_json, reject_reparse
from sps_mcp.jsonc import Document


def canonical(value):
    def special(item):
        if isinstance(item, (datetime, date)):
            return {'__toml_datetime_type__': type(item).__name__, 'iso': item.isoformat()}
        raise TypeError(type(item).__name__)
    return json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(',', ':'),
                      allow_nan=False, default=special).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def summarize_mapping(value, group, comments=None):
    if not isinstance(value, dict) or not isinstance(value.get(group, {}), dict):
        raise ValueError('CONFIG_ROOT')
    entries = value.get(group, {})
    others = {key: val for key, val in entries.items() if key != KEY}
    nonowned = copy.deepcopy(value)
    outside = copy.deepcopy(value)
    outside.pop(group, None)
    if others:
        nonowned[group] = others
    else:
        nonowned.pop(group, None)
    owned = entries.get(KEY)
    environment = owned.get('env', owned.get('environment', {})) if isinstance(owned, dict) else {}
    result = {'other_mcp_count': len(others), 'other_mcp_sha256': digest(others),
        'nonowned_semantic_sha256': digest(nonowned), 'outside_mcp_sha256': digest(outside),
        'other_records': sorted(({'key_sha256': digest(key), 'record_sha256': digest(val)}
                                 for key, val in others.items()), key=lambda row: row['key_sha256']),
        'owned_present': KEY in entries, 'owned_sha256': digest(owned) if KEY in entries else None,
        'owned_marker_matches': isinstance(environment, dict) and environment.get('SPS_MCP_OWNER') == OWNER}
    if comments is not None:
        result.update({'comment_count': len(comments), 'comments_sha256': digest(comments)})
    return result


def xml_value(node):
    # Ignore indentation-only text, while preserving attributes, significant text,
    # and child order. No XML values are returned to the evidence file.
    return {'tag': node.tag, 'attributes': dict(node.attrib),
        'text': node.text if node.text and node.text.strip() else None,
        'children': [xml_value(child) for child in node]}


def owned_xml(node):
    return node.get('name') == KEY or node.get('id') == KEY or any(
        (child.tag in {'name', 'id'} and child.text == KEY) or
        (child.tag == 'option' and child.get('name') in {'name', 'id'} and child.get('value') == KEY)
        for child in node)


def summarize_pycharm(raw):
    root = ET.fromstring(raw)
    components = list(root)
    if root.tag != 'application' or len(components) != 1 or components[0].tag != 'component':
        raise ValueError('XML_STRUCTURE_NOT_RECOGNIZED')
    containers = list(components[0])
    if {node.tag for node in containers} != {'commands', 'urls'}:
        # Current AI Assistant stores per-server permissions separately from
        # ~/.ai/mcp/mcp.json. Keep the full XML fingerprint: this audit does not
        # authorize any permission/enable changes, even to our own server.
        return {'other_mcp_count': None, 'other_mcp_sha256': digest(xml_value(root)),
            'nonowned_semantic_sha256': digest(xml_value(root)),
            'outside_mcp_sha256': digest(xml_value(root)), 'other_records': [],
            'owned_present': any(owned_xml(node) for node in root.iter()),
            'owned_sha256': None, 'xml_structure_scope': 'whole_permission_file_preserved'}
    others, owned = [], []
    nonowned = copy.deepcopy(root)
    for container in list(nonowned)[0]:
        for node in list(container):
            row = {'container': container.tag, 'node': xml_value(node)}
            if owned_xml(node):
                owned.append(row)
                container.remove(node)
            else:
                others.append(row)
    outside = copy.deepcopy(root)
    for container in list(outside)[0]:
        for node in list(container):
            container.remove(node)
    return {'other_mcp_count': len(others), 'other_mcp_sha256': digest(others),
        'nonowned_semantic_sha256': digest(xml_value(nonowned)),
        'outside_mcp_sha256': digest(xml_value(outside)),
        'other_records': [{'record_sha256': digest(row)} for row in others],
        'owned_present': bool(owned), 'owned_sha256': digest(owned) if owned else None,
        'owned_identification': 'direct_name_or_id_equal_to_owned_key',
        'xml_structure_scope': 'observed_commands_and_urls_containers'}


def snapshot_file(path, group=None):
    reject_reparse(path)
    if not path.exists():
        return {'state': 'absent', 'path': str(path)}
    raw = path.read_bytes()
    if len(raw) > 8 * 1024 * 1024:
        raise ValueError('CONFIG_SIZE')
    result = {'state': 'read', 'path': str(path), 'bytes': len(raw),
        'raw_sha256': hashlib.sha256(raw).hexdigest(), 'utf8_bom': raw.startswith(b'\xef\xbb\xbf'),
        'format': path.suffix.lower().lstrip('.'), 'crlf_count': raw.count(b'\r\n'),
        'lf_count_excluding_crlf': raw.count(b'\n') - raw.count(b'\r\n')}
    if path.suffix.lower() == '.xml':
        result.update(summarize_pycharm(raw))
        return result
    text = raw.decode('utf-8-sig')
    comments = None
    if path.suffix.lower() == '.toml':
        value = tomlkit.parse(text).unwrap()
    elif path.suffix.lower() == '.jsonc':
        document = Document(text)
        value = document.root.value
        comments = [text[token.start:token.end] for token in document.comments]
    else:
        value = strict_json(text)
    result.update(summarize_mapping(value, group, comments))
    return result


def snapshot():
    files, clients, supplementary = {}, {}, {}
    for client in CLIENTS:
        try:
            if client == 'claude_desktop':
                profiles = []
                for label, path in zip(('classic_roaming', 'store_localcache'), claude_candidate_paths()):
                    file_id = digest(str(path).casefold())
                    files[file_id] = snapshot_file(path, 'mcpServers')
                    profiles.append({'profile': label, 'file_id': file_id})
                clients[client] = {'profiles': profiles}
                continue
            path = target_path(client)
            if client == 'pycharm':
                # Current Add dialog explicitly identifies this Global path.
                path = Path.home() / '.ai/mcp/mcp.json'
                legacy = Path(os.environ.get('APPDATA', Path.home() / 'AppData/Roaming')) / 'JetBrains/PyCharm2026.2/options/llm.mcpServers.xml'
                legacy_id = digest(str(legacy).casefold())
                files[legacy_id] = snapshot_file(legacy)
                supplementary['pycharm_legacy_xml'] = {'file_id': legacy_id}
            file_id = digest(str(path).casefold())
            clients[client] = {'file_id': file_id}
            if file_id not in files:
                group = 'mcp_servers' if client in {'codex', 'chatgpt_desktop'} else ('mcp' if client == 'opencode' else 'mcpServers')
                files[file_id] = snapshot_file(path, group)
        except Exception as error:
            clients[client] = {'state': 'read_error', 'error_type': type(error).__name__}
    return {'schema_version': 2, 'created_utc': datetime.now(timezone.utc).isoformat(),
        'global_config_writes': False, 'secret_content_persisted': False, 'clients': clients,
        'supplementary_files': supplementary, 'files': files,
        'read_checks_passed': all('file_id' in value or bool(value.get('profiles')) for value in clients.values())}


def file_references(snapshot):
    refs = dict(snapshot.get('supplementary_files', {}))
    for client, item in snapshot['clients'].items():
        if 'profiles' in item:
            for profile in item['profiles']:
                refs[client + ':' + profile['profile']] = profile
        else:
            refs[client] = item
    return refs


def compare(before, after):
    rows = []
    previous_refs = file_references(before)
    current_refs = file_references(after)
    for client, previous in previous_refs.items():
        current = current_refs.get(client, {})
        row = {'client': client}
        if 'file_id' not in previous or 'file_id' not in current:
            row['state'] = 'read_error'
        elif previous['file_id'] != current['file_id']:
            row['state'] = 'config_path_changed'
        else:
            first, last = before['files'][previous['file_id']], after['files'][current['file_id']]
            if first.get('format') == 'xml':
                row['xml_bytes_unchanged'] = last.get('state') == 'read' and first['raw_sha256'] == last.get('raw_sha256')
            if first['state'] == 'absent':
                row.update({'state': 'no_preexisting_file', 'current_state': last['state'],
                    'other_mcp_unchanged': last.get('other_mcp_count', 0) == 0})
            elif last['state'] != 'read':
                row['state'] = 'existing_file_missing'
            else:
                row.update({'state': 'compared', 'other_mcp_unchanged': first['other_mcp_sha256'] == last['other_mcp_sha256'],
                    'nonowned_semantics_unchanged': first['nonowned_semantic_sha256'] == last['nonowned_semantic_sha256'],
                    'outside_mcp_unchanged': first['outside_mcp_sha256'] == last['outside_mcp_sha256'],
                    'utf8_bom_unchanged': first['utf8_bom'] == last['utf8_bom'],
                    'owned_present_after': last['owned_present']})
                if 'comments_sha256' in first:
                    row['jsonc_comments_unchanged'] = first['comments_sha256'] == last.get('comments_sha256')
        rows.append(row)
    for client in sorted(set(current_refs) - set(previous_refs)):
        rows.append({'client': client, 'state': 'not_in_baseline'})
    return {'rows': rows, 'other_mcp_records_preserved': all(row.get('other_mcp_unchanged') is True for row in rows),
        'all_nonowned_semantics_preserved': all(row.get('nonowned_semantics_unchanged') is True or
            row.get('state') == 'no_preexisting_file' for row in rows),
        'existing_xml_bytes_preserved': all(row.get('xml_bytes_unchanged', True) for row in rows),
        'note': 'Concurrent application changes outside MCP may change the full-file semantic hash; inspect the narrower MCP hash separately.'}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--output', required=True, type=Path)
    parser.add_argument('--compare', type=Path)
    args = parser.parse_args(argv)
    if not args.output.resolve().is_relative_to((ROOT / 'build').resolve()):
        parser.error('Output must remain under workspace build.')
    if args.output.exists():
        parser.error('Existing evidence must not be overwritten; choose a new output.')
    result = snapshot()
    if args.compare:
        result['comparison'] = compare(json.loads(args.compare.read_text(encoding='utf-8')), result)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + '\n', encoding='utf-8')
    print(json.dumps({'output': str(args.output.resolve()), 'unique_files': len(result['files']),
        'read_checks_passed': result['read_checks_passed'], 'secret_content_persisted': False}))
    return 0 if result['read_checks_passed'] else 1


if __name__ == '__main__':
    raise SystemExit(main())
