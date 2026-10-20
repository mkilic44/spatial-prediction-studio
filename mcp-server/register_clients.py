"""Generate all client templates. Apply only explicitly selected owned records."""
from __future__ import annotations

import argparse
import copy
import hashlib
import json
import os
import sys
import tempfile
import uuid
from datetime import datetime, timezone
from pathlib import Path

import tomlkit

from sps_mcp.bridge import BridgeError, reject_reparse, strict_json
from sps_mcp.jsonc import Document as JsoncDocument, same_json_value

KEY = "spatial_prediction_studio"
OWNER = "SpatialPredictionStudio"
CLAUDE_STORE_FAMILY = "Claude_pzs8sxrjxfjjc"
CLIENTS = ("codex", "chatgpt_desktop", "antigravity", "claude_desktop",
           "claude_code", "opencode", "pycharm")
SOURCES = {
    "codex": "https://learn.chatgpt.com/docs/extend/mcp",
    "chatgpt_desktop": "https://learn.chatgpt.com/docs/extend/mcp",
    "antigravity": "https://www.antigravity.google/docs/mcp",
    "claude_desktop": "https://modelcontextprotocol.io/docs/2026-07-28/develop/connect-local-servers",
    "claude_code": "https://code.claude.com/docs/en/mcp",
    "opencode": "https://opencode.ai/docs/mcp-servers/",
    "pycharm": "https://www.jetbrains.com/help/ai-assistant/mcp.html",
}


def runtime_paths(root):
    root = Path(root).resolve()
    python = root / "runtime-mcp" / "Scripts" / "python.exe"
    if not python.is_file():
        python = root / "runtime-mcp" / "python.exe"
    server = root / "mcp-server" / "server.py"
    if not python.is_file() or not server.is_file():
        raise BridgeError("INSTALLATION_MISSING", "The MCP interpreter or server file was not found.")
    reject_reparse(python)
    reject_reparse(server)
    return python, server


def template(client, root):
    if client not in CLIENTS:
        raise BridgeError("CLIENT_UNKNOWN", "Unknown client.")
    python, server = runtime_paths(root)
    env = {"SPS_MCP_OWNER": OWNER}
    # -s: the user's own site-packages folder (%APPDATA%\\Python) must not shadow the pinned packages.
    entry = {"command": str(python), "args": ["-s", "-X", "utf8", str(server)], "env": env}
    if client in {"codex", "chatgpt_desktop"}:
        entry.update({"cwd": str(Path(root).resolve()), "startup_timeout_sec": 30,
                      "tool_timeout_sec": 60})
        return "toml", {"mcp_servers": {KEY: entry}}
    if client == "opencode":
        return "json", {"$schema": "https://opencode.ai/config.json", "mcp": {
            KEY: {"type": "local", "command": [str(python), "-s", "-X", "utf8", str(server)],
                  "environment": env, "enabled": True, "timeout": 60000}}}
    if client == "claude_code":
        entry["type"] = "stdio"
    if client == "antigravity":
        entry["cwd"] = str(Path(root).resolve())
    return "json", {"mcpServers": {KEY: entry}}


def claude_candidate_paths(home=None, appdata=None, localappdata=None):
    explicit_home = home is not None
    home = Path.home() if home is None else Path(home)
    roaming = (home / "AppData/Roaming" if explicit_home else Path(os.environ.get("APPDATA", home / "AppData/Roaming"))) if appdata is None else Path(appdata)
    local = (home / "AppData/Local" if explicit_home else Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))) if localappdata is None else Path(localappdata)
    return [roaming / "Claude/claude_desktop_config.json",
            local / "Packages" / CLAUDE_STORE_FAMILY / "LocalCache/Roaming/Claude/claude_desktop_config.json"]


def claude_target_paths(home=None, appdata=None, localappdata=None):
    candidates = claude_candidate_paths(home, appdata, localappdata)
    # Check both candidate chains before any mutation. Do not follow a Store
    # junction while guessing which profile is active.
    for path in candidates:
        reject_reparse(path)
    packages = candidates[1].parents[4]
    if packages.exists():
        for package in packages.glob("Claude_*"):
            reject_reparse(package)
            unknown = package / "LocalCache/Roaming/Claude"
            if package.name != CLAUDE_STORE_FAMILY and unknown.exists():
                raise BridgeError("CLIENT_PROFILE_UNSUPPORTED", "Found an unrecognized Store profile for Claude; existing files were not changed.")
    existing = [path for path in candidates if path.is_file() or path.parent.is_dir()]
    return existing or [candidates[0]]


def target_paths(client, home=None, appdata=None, localappdata=None):
    if client == "claude_desktop":
        return claude_target_paths(home, appdata, localappdata)
    path = target_path(client, home, appdata)
    return [] if path is None else [path]


def target_path(client, home=None, appdata=None):
    if client == "claude_desktop":
        paths = claude_target_paths(home, appdata)
        if len(paths) != 1:
            raise BridgeError("CLIENT_MULTIPLE_PROFILES", "Claude has more than one profile; use the multi-target registration.")
        return paths[0]
    home = Path.home() if home is None else Path(home)
    appdata = Path(os.environ.get("APPDATA", home / "AppData" / "Roaming")) if appdata is None else Path(appdata)
    if client in {"codex", "chatgpt_desktop"}:
        return Path(os.environ.get("CODEX_HOME", home / ".codex")) / "config.toml"
    if client == "antigravity":
        current = home / ".gemini" / "config" / "mcp_config.json"
        legacy = home / ".gemini" / "antigravity" / "mcp_config.json"
        if legacy.exists() and not current.exists():
            raise BridgeError("MANUAL_MERGE_REQUIRED", "Found a legacy Antigravity configuration. Check the active file with View raw config.")
        return current
    if client == "claude_code":
        return home / ".claude.json"
    if client == "opencode":
        directory = home / ".config" / "opencode"
        if os.environ.get("OPENCODE_CONFIG") or os.environ.get("OPENCODE_CONFIG_DIR") or os.environ.get("XDG_CONFIG_HOME"):
            raise BridgeError("MANUAL_MERGE_REQUIRED", "A custom OpenCode configuration path is active; merge the generated snippet into the active file.")
        json_path, commented = directory / "opencode.json", directory / "opencode.jsonc"
        if commented.exists() and json_path.exists():
            raise BridgeError("MANUAL_MERGE_REQUIRED", "OpenCode has both a JSON file and a JSON with comments (JSONC) file. The active file is ambiguous; both are kept unchanged.")
        if commented.exists():
            return commented
        return json_path
    if client == "pycharm":
        return None
    raise BridgeError("CLIENT_UNKNOWN", "Unknown client.")


def owned(entry):
    if not isinstance(entry, dict):
        try:
            entry = entry.unwrap()
        except AttributeError:
            return False
    environment = entry.get("env", entry.get("environment", {}))
    return isinstance(environment, dict) and environment.get("SPS_MCP_OWNER") == OWNER


def serialize_config(fmt, obj):
    return tomlkit.dumps(obj) if fmt == "toml" else json.dumps(obj, ensure_ascii=False, indent=2) + "\n"


def merge_owned_text(original, fmt, patch):
    group = "mcp_servers" if fmt == "toml" else ("mcp" if "mcp" in patch else "mcpServers")
    try:
        if fmt == "jsonc":
            parsed = JsoncDocument(original or "{}")
            previous = copy.deepcopy(parsed.root.value)
            if group not in previous:
                text = parsed.set_member(parsed.root, group, {KEY: patch[group][KEY]})
            else:
                if not isinstance(previous[group], dict):
                    raise ValueError("Server group must be object")
                entry = patch[group][KEY]
                if KEY in previous[group] and not same_json_value(previous[group][KEY], entry) and not owned(previous[group][KEY]):
                    raise BridgeError("OWNERSHIP_CONFLICT", "An entry with the same name belongs to another installation; it was not overwritten.")
                text = parsed.set_member(parsed.root.members[group].node, KEY, entry)
            previous.setdefault(group, {})[KEY] = patch[group][KEY]
            if not same_json_value(JsoncDocument(text).root.value, previous):
                raise ValueError("Unrelated JSONC configuration changed")
            return text
        document = tomlkit.parse(original) if fmt == "toml" else strict_json(original or "{}")
        if not isinstance(document, dict):
            raise ValueError("Root must be object")
        previous = copy.deepcopy(document.unwrap() if fmt == "toml" else document)
        if group not in document:
            document[group] = tomlkit.table() if fmt == "toml" else {}
        if not isinstance(document[group], dict):
            raise ValueError("Server group must be object")
        entry = patch[group][KEY]
        if KEY in document[group] and document[group][KEY] != entry and not owned(document[group][KEY]):
            raise BridgeError("OWNERSHIP_CONFLICT", "An entry with the same name belongs to another installation; it was not overwritten.")
        document[group][KEY] = entry
        text = serialize_config(fmt, document)
        loaded = tomlkit.parse(text).unwrap() if fmt == "toml" else strict_json(text)
        expected = previous
        expected.setdefault(group, {})[KEY] = entry
        if loaded != expected:
            raise ValueError("Unrelated configuration changed")
        return text
    except BridgeError:
        raise
    except (ValueError, TypeError, KeyError, tomlkit.exceptions.ParseError):
        raise BridgeError("MANUAL_MERGE_REQUIRED", "Could not parse the existing configuration safely; the file was not changed.") from None


def remove_owned_text(original, fmt, patch):
    group = "mcp_servers" if fmt == "toml" else ("mcp" if "mcp" in patch else "mcpServers")
    try:
        if fmt == "jsonc":
            parsed = JsoncDocument(original or "{}")
            previous = copy.deepcopy(parsed.root.value)
            if not isinstance(previous.get(group, {}), dict):
                raise ValueError("Invalid config")
            existing = previous.get(group, {}).get(KEY)
            if existing is None:
                return original, "not_present"
            expected = patch[group][KEY]
            if not owned(existing) or any(existing.get(k) != expected.get(k) for k in ("command", "args")):
                return original, "preserved_other_installation"
            text = parsed.remove_member(parsed.root.members[group].node, KEY)
            del previous[group][KEY]
            if not same_json_value(JsoncDocument(text).root.value, previous):
                raise ValueError("Unrelated JSONC configuration changed")
            return text, "removed_restart_required"
        document = tomlkit.parse(original) if fmt == "toml" else strict_json(original or "{}")
        previous = copy.deepcopy(document.unwrap() if fmt == "toml" else document)
        if not isinstance(previous, dict) or not isinstance(previous.get(group, {}), dict):
            raise ValueError("Invalid config")
        existing = previous.get(group, {}).get(KEY)
        if existing is None:
            return original, "not_present"
        expected = patch[group][KEY]
        if not owned(existing) or any(existing.get(k) != expected.get(k) for k in ("command", "args")):
            return original, "preserved_other_installation"
        del document[group][KEY]
        del previous[group][KEY]
        if fmt == "toml" and not previous[group]:
            document[group] = tomlkit.table()
        text = serialize_config(fmt, document)
        loaded = tomlkit.parse(text).unwrap() if fmt == "toml" else strict_json(text)
        if loaded != previous:
            raise ValueError("Unrelated config changed")
        return text, "removed_restart_required"
    except (ValueError, TypeError, KeyError, AttributeError, tomlkit.exceptions.ParseError):
        raise BridgeError("MANUAL_MERGE_REQUIRED", "Could not parse the configuration safely; the entry was not removed.") from None


def atomic_merge(path, fmt, patch, remove=False):
    path = Path(path)
    if path.suffix.lower() == ".jsonc":
        if "mcp" not in patch:
            raise BridgeError("MANUAL_MERGE_REQUIRED", "JSON with comments (JSONC) is supported only for OpenCode registration.")
        fmt = "jsonc"
    reject_reparse(path)
    if remove and not path.exists():
        return {"status": "not_present", "path": str(path), "connected": False}
    path.parent.mkdir(parents=True, exist_ok=True)
    lock = path.with_name(path.name + ".sps-mcp.lock")
    backup, temp_name, acquired = None, None, False
    try:
        with lock.open("x", encoding="utf-8") as stream:
            stream.write(str(os.getpid()))
        acquired = True
        existed = path.exists()
        original_bytes = path.read_bytes() if existed else b""
        if len(original_bytes) > 8 * 1024 * 1024:
            raise BridgeError("CONFIG_LIMIT", "The configuration file exceeds the size limit.")
        try:
            original_text = original_bytes.decode("utf-8-sig")
        except UnicodeError:
            raise BridgeError("MANUAL_MERGE_REQUIRED", "The configuration is not UTF-8; it was not changed.") from None
        removal_status = None
        if remove:
            updated_text, removal_status = remove_owned_text(original_text, fmt, patch)
        else:
            updated_text = merge_owned_text(original_text, fmt, patch)
        updated = updated_text.encode("utf-8")
        if original_bytes.startswith(b"\xef\xbb\xbf"):
            updated = b"\xef\xbb\xbf" + updated
        if updated == original_bytes:
            return {"status": removal_status or "unchanged", "path": str(path), "connected": False}
        if existed:
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            backup = path.with_name(path.name + ".sps-backup-" + stamp + "-" + uuid.uuid4().hex[:8])
            with backup.open("xb") as stream:
                stream.write(original_bytes)
                stream.flush()
                os.fsync(stream.fileno())
        fd, temp_name = tempfile.mkstemp(prefix=path.name + ".sps-", suffix=".tmp", dir=path.parent)
        with os.fdopen(fd, "wb") as stream:
            stream.write(updated)
            stream.flush()
            os.fsync(stream.fileno())
        if path.exists() != existed or (existed and path.read_bytes() != original_bytes):
            raise BridgeError("CONFIG_CHANGED", "The configuration changed during the operation; it was not written.")
        os.replace(temp_name, path)
        temp_name = None
        if path.read_bytes() != updated:
            raise BridgeError("CONFIG_READBACK", "Configuration read-back verification failed.")
        return {"status": removal_status or "registered_restart_required", "path": str(path),
                "backup": str(backup) if backup else None,
                "sha256": hashlib.sha256(updated).hexdigest(), "connected": False}
    except FileExistsError:
        raise BridgeError("CONFIG_BUSY", "Another registration is in progress.") from None
    finally:
        if temp_name:
            Path(temp_name).unlink(missing_ok=True)
        if acquired:
            lock.unlink(missing_ok=True)


def recorded_targets(record, client, root):
    if record.get("owner_key") != KEY or not record.get("applied") or record.get("installation_root") != str(Path(root).resolve()):
        raise BridgeError("REGISTRATION_RECORD_INVALID", "The registration record does not belong to this installation.")
    matches = [item for item in record.get("clients", []) if item.get("client") == client]
    if len(matches) != 1:
        raise BridgeError("REGISTRATION_RECORD_INVALID", "The client's registration record is missing or duplicated.")
    item = matches[0]
    profiles = item.get("profiles", [item] if item.get("path") else [])
    permitted = claude_candidate_paths() if client == "claude_desktop" else target_paths(client)
    paths = []
    for profile in profiles:
        path = Path(profile.get("path", ""))
        if not path.is_absolute() or path not in permitted or path in paths:
            raise BridgeError("REGISTRATION_RECORD_INVALID", "A target in the registration record is not a supported client profile.")
        reject_reparse(path)
        paths.append(path)
    return paths


def validate_target_contents(paths, fmt, patch, remove):
    """Reject unsafe/invalid coexisting profiles before writing the first one."""
    for path in paths:
        reject_reparse(path)
        data = path.read_bytes() if path.exists() else b""
        if len(data) > 8 * 1024 * 1024:
            raise BridgeError("CONFIG_LIMIT", "The configuration file exceeds the size limit.")
        try:
            original = data.decode("utf-8-sig")
        except UnicodeError:
            raise BridgeError("MANUAL_MERGE_REQUIRED", "The configuration is not UTF-8; it was not changed.") from None
        actual_format = "jsonc" if path.suffix.lower() == ".jsonc" else fmt
        (remove_owned_text if remove else merge_owned_text)(original, actual_format, patch)


def main(argv=None):
    parser = argparse.ArgumentParser(description="Spatial Prediction Studio MCP client registration helper")
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output-dir", type=Path, required=True)
    parser.add_argument("--client", action="append", choices=CLIENTS, default=[])
    parser.add_argument("--registration-record", type=Path)
    operations = parser.add_mutually_exclusive_group()
    operations.add_argument("--apply", action="store_true")
    operations.add_argument("--remove", action="store_true")
    args = parser.parse_args(argv)
    if (args.apply or args.remove) and not args.client:
        parser.error("--apply or --remove requires at least one --client.")
    if args.registration_record and not args.remove:
        parser.error("--registration-record can be used only with --remove.")
    prior_record = None
    if args.registration_record:
        reject_reparse(args.registration_record)
        if not args.registration_record.resolve().is_relative_to(args.root.resolve()):
            raise BridgeError("REGISTRATION_RECORD_INVALID", "The registration record is outside the installation folder.")
        prior_record = strict_json(args.registration_record.read_bytes())
    args.output_dir.mkdir(parents=True, exist_ok=True)
    reject_reparse(args.output_dir)
    records, written = [], {}
    for client in CLIENTS:
        fmt, patch = template(client, args.root)
        output = args.output_dir / (client + (".toml" if fmt == "toml" else ".json"))
        output.write_text(serialize_config(fmt, patch), encoding="utf-8")
        record = {"client": client, "template": str(output.resolve()), "source": SOURCES[client],
                  "status": "template_only", "connected": False}
        if (args.apply or args.remove) and client in args.client:
            try:
                paths = recorded_targets(prior_record, client, args.root) if prior_record is not None else target_paths(client)
                if client == "pycharm":
                    record.update({"status": "manual_removal_required" if args.remove else "manual_import_required",
                                   "instructions": "PyCharm: Settings > Tools > AI Assistant > Model Context Protocol > Add > STDIO; use the JSON snippet."})
                else:
                    validate_target_contents(paths, fmt, patch, args.remove)
                    profiles = []
                    for target in paths:
                        key = str(target).casefold()
                        try:
                            if key in written:
                                result = dict(written[key])
                                record["shared_with"] = "codex/chatgpt_desktop"
                            else:
                                result = atomic_merge(target, fmt, patch, remove=args.remove)
                                written[key] = result
                            profiles.append(result)
                        except (BridgeError, OSError) as error:
                            profiles.append({"path": str(target), "status": "not_registered", "connected": False,
                                             "error": {"code": getattr(error, "code", "CONFIG_IO"), "message": str(error)}})
                    if profiles:
                        record.update(profiles[0])
                        failures = [item for item in profiles if item["status"] == "not_registered"]
                        if failures:
                            record.update(status="not_registered", error=failures[0]["error"])
                        elif any(item["status"] == "registered_restart_required" for item in profiles):
                            record["status"] = "registered_restart_required"
                        elif any(item["status"] == "removed_restart_required" for item in profiles):
                            record["status"] = "removed_restart_required"
                    else:
                        record.update(status="not_present", connected=False)
                    if client == "claude_desktop":
                        record["profiles"] = profiles
            except BridgeError as error:
                record.update({"status": "not_registered", "error": {"code": error.code, "message": error.message}})
        records.append(record)
    result = {"schema_version": 1, "owner_key": KEY, "applied": args.apply, "removed": args.remove,
              "installation_root": str(args.root.resolve()),
              "client_acceptance": "pending", "clients": records}
    (args.output_dir / "registration-result.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False))
    return 1 if any(r["status"] == "not_registered" for r in records) else 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except BridgeError as error:
        print(json.dumps({"ok": False, "error": {"code": error.code, "message": error.message}}, ensure_ascii=False))
        sys.exit(1)

