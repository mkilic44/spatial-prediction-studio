import json
from pathlib import Path
from unittest.mock import patch

import pytest
import register_clients as registration
from sps_mcp.bridge import BridgeError


@pytest.fixture
def profile(tmp_path, monkeypatch):
    home = tmp_path / "kullanıcı"
    roaming, local = home / "AppData/Roaming", home / "AppData/Local"
    root = tmp_path / "installation"
    (root / "runtime-mcp/Scripts").mkdir(parents=True)
    (root / "runtime-mcp/Scripts/python.exe").write_bytes(b"fixture")
    (root / "mcp-server").mkdir()
    (root / "mcp-server/server.py").write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("USERPROFILE", str(home))
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("LOCALAPPDATA", str(local))
    monkeypatch.setenv("CODEX_HOME", str(home / ".codex"))
    classic = roaming / "Claude/claude_desktop_config.json"
    store = local / "Packages/Claude_pzs8sxrjxfjjc/LocalCache/Roaming/Claude/claude_desktop_config.json"
    return root, home, roaming, local, classic, store


def seed(path, label):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"\xef\xbb\xbf" + json.dumps({"mcpServers": {label: {"command": "other.exe", "args": ["Türkçe"]}}, "theme": "system"}, ensure_ascii=False).encode("utf-8"))


def invoke(root, operation, record=None):
    out = root / ("client-removal" if operation == "--remove" else "client-configurations")
    args = ["--root", str(root), "--output-dir", str(out), operation, "--client", "claude_desktop"]
    if record is not None:
        args += ["--registration-record", str(record)]
    code = registration.main(args)
    result = json.loads((out / "registration-result.json").read_text(encoding="utf-8"))
    return code, next(item for item in result["clients"] if item["client"] == "claude_desktop")


@pytest.mark.parametrize("existing", ["none", "classic", "store", "both"])
def test_existing_supported_profiles_are_discovered(profile, existing):
    _, home, roaming, local, classic, store = profile
    if existing in {"classic", "both"}: seed(classic, "classic-other")
    if existing in {"store", "both"}: seed(store, "store-other")
    expected = [classic] if existing in {"none", "classic"} else ([store] if existing == "store" else [classic, store])
    assert registration.target_paths("claude_desktop", home=home, appdata=roaming, localappdata=local) == expected


def test_both_profiles_merge_preserve_and_remove_from_record(profile):
    root, _, _, _, classic, store = profile
    seed(classic, "classic-other"); seed(store, "store-other")
    old = {path: path.read_bytes() for path in [classic, store]}
    code, result = invoke(root, "--apply")
    assert code == 0 and result["status"] == "registered_restart_required"
    assert {item["path"] for item in result["profiles"]} == {str(classic), str(store)}
    for path, label in [(classic, "classic-other"), (store, "store-other")]:
        data = json.loads(path.read_text(encoding="utf-8-sig"))
        assert data["mcpServers"][label]["args"] == ["Türkçe"]
        assert data["mcpServers"][registration.KEY]["env"]["SPS_MCP_OWNER"] == registration.OWNER
        assert path.read_bytes().startswith(b"\xef\xbb\xbf")
        assert any(p.read_bytes() == old[path] for p in path.parent.glob(path.name + ".sps-backup-*"))
    code, removed = invoke(root, "--remove", root / "client-configurations/registration-result.json")
    assert code == 0 and len(removed["profiles"]) == 2
    for path in [classic, store]:
        assert registration.KEY not in json.loads(path.read_text(encoding="utf-8-sig"))["mcpServers"]


def test_store_only_does_not_create_inactive_classic_profile(profile):
    root, _, _, _, classic, store = profile
    seed(store, "ders-planlayici")
    assert invoke(root, "--apply")[0] == 0
    assert not classic.exists()
    assert registration.KEY in json.loads(store.read_text(encoding="utf-8-sig"))["mcpServers"]


def test_existing_store_directory_without_config_is_supported(profile):
    root, _, _, _, classic, store = profile
    store.parent.mkdir(parents=True)
    assert invoke(root, "--apply")[0] == 0
    assert store.exists() and not classic.exists()


def test_explicit_home_never_scans_real_environment_profiles(profile, tmp_path):
    _, _, _, _, _, store = profile
    seed(store, "environment-store")
    isolated = tmp_path / "separate-home"
    assert registration.target_paths("claude_desktop", home=isolated) == [isolated / "AppData/Roaming/Claude/claude_desktop_config.json"]


def test_malformed_second_profile_prevents_partial_registration(profile):
    root, _, _, _, classic, store = profile
    seed(classic, "other"); seed(store, "other")
    store.write_text("{invalid-json", encoding="utf-8")
    before = classic.read_bytes()
    code, result = invoke(root, "--apply")
    assert code == 1 and result["error"]["code"] == "MANUAL_MERGE_REQUIRED"
    assert classic.read_bytes() == before


def test_later_io_failure_records_first_success_for_owned_cleanup(profile):
    root, _, _, _, classic, store = profile
    seed(classic, "other"); seed(store, "other")
    actual = registration.atomic_merge
    def merge(path, *args, **kwargs):
        if path == store:
            raise OSError("injected second-profile failure")
        return actual(path, *args, **kwargs)
    with patch.object(registration, "atomic_merge", side_effect=merge):
        code, result = invoke(root, "--apply")
    assert code == 1 and len(result["profiles"]) == 2
    assert result["profiles"][0]["status"] == "registered_restart_required"
    assert invoke(root, "--remove", root / "client-configurations/registration-result.json")[0] == 0
    assert registration.KEY not in json.loads(classic.read_text(encoding="utf-8-sig"))["mcpServers"]


def test_recorded_removal_preserves_new_installation_in_other_profile(profile):
    root, _, _, _, classic, store = profile
    seed(classic, "classic-other"); seed(store, "store-other")
    assert invoke(root, "--apply")[0] == 0
    data = json.loads(store.read_text(encoding="utf-8-sig"))
    data["mcpServers"][registration.KEY]["args"][-1] = str(root.parent / "new-version/mcp-server/server.py")
    store.write_text(json.dumps(data), encoding="utf-8")
    previous = store.read_bytes()
    assert invoke(root, "--remove", root / "client-configurations/registration-result.json")[0] == 0
    assert store.read_bytes() == previous
    assert registration.KEY not in json.loads(classic.read_text(encoding="utf-8-sig"))["mcpServers"]


def test_record_removal_does_not_discover_new_profiles(profile):
    root, _, _, _, classic, store = profile
    seed(classic, "classic-other")
    assert invoke(root, "--apply")[0] == 0
    seed(store, "later-profile")
    before = store.read_bytes()
    assert invoke(root, "--remove", root / "client-configurations/registration-result.json")[0] == 0
    assert store.read_bytes() == before


def test_reparse_store_rejected_before_classic_is_written(profile):
    root, _, _, _, classic, store = profile
    seed(classic, "classic-other"); seed(store, "store-other")
    before = classic.read_bytes()
    actual = registration.reject_reparse
    def guard(path):
        if "Claude_pzs8sxrjxfjjc" in str(path):
            raise BridgeError("UNSAFE_PATH", "fixture junction")
        actual(path)
    with patch.object(registration, "reject_reparse", side_effect=guard):
        code, result = invoke(root, "--apply")
    assert code == 1 and result["error"]["code"] == "UNSAFE_PATH"
    assert classic.read_bytes() == before


def test_unknown_store_family_is_not_silently_replaced_by_classic(profile):
    root, _, _, local, classic, _ = profile
    seed(local / "Packages/Claude_unrecognized/LocalCache/Roaming/Claude/claude_desktop_config.json", "other")
    code, result = invoke(root, "--apply")
    assert code == 1 and result["error"]["code"] == "CLIENT_PROFILE_UNSUPPORTED"
    assert not classic.exists()


def test_untrusted_record_path_cannot_remove_arbitrary_config(profile):
    root, _, _, _, classic, _ = profile
    seed(classic, "other")
    assert invoke(root, "--apply")[0] == 0
    path = root / "client-configurations/registration-result.json"
    record = json.loads(path.read_text(encoding="utf-8"))
    client = next(item for item in record["clients"] if item["client"] == "claude_desktop")
    outside = root / "foreign.json"; seed(outside, "unrelated")
    client["profiles"][0]["path"] = str(outside)
    path.write_text(json.dumps(record), encoding="utf-8")
    before = outside.read_bytes()
    code, result = invoke(root, "--remove", path)
    assert code == 1 and result["error"]["code"] == "REGISTRATION_RECORD_INVALID"
    assert outside.read_bytes() == before


def test_removal_of_missing_config_does_not_create_directories(tmp_path):
    path = tmp_path / "missing/Claude/config.json"
    result = registration.atomic_merge(path, "json", {"mcpServers": {registration.KEY: {}}}, remove=True)
    assert result["status"] == "not_present" and not path.parent.exists()
