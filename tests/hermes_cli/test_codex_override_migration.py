"""Offline migration tests; fixtures contain synthetic identities and tokens only."""
import base64
import importlib.util
import json
from pathlib import Path
import stat
import time

import pytest

spec = importlib.util.spec_from_file_location("codex_migration", Path(__file__).parents[2] / "scripts" / "migrate_codex_override.py")
assert spec is not None and spec.loader is not None
M = importlib.util.module_from_spec(spec)
spec.loader.exec_module(M)


def token(account="synthetic-account", exp=None):
    payload = {"sub": "synthetic-user", "exp": exp if exp is not None else int(time.time()) + 7200, "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return "e30." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".synthetic"


def data(account="synthetic-account", exp=None):
    return {"version": 1, "active_provider": "other", "providers": {"other": {"api_key": "synthetic-static"}, "openai-codex": {"tokens": {"access_token": token(account, exp), "refresh_token": "synthetic-refresh"}, "auth_mode": "chatgpt"}}, "credential_pool": {"other": [{"id": "other", "auth_type": "api_key", "access_token": "synthetic-other"}], "openai-codex": [{"id": "oauth", "source": "manual:device_code", "auth_type": "oauth", "access_token": token(account, exp), "refresh_token": "synthetic-refresh"}, {"id": "static", "source": "manual:api_key", "auth_type": "api_key", "access_token": "synthetic-static-codex"}]}, "suppressed_sources": {"other": ["env:OTHER"]}}


@pytest.fixture
def stores(tmp_path):
    root = tmp_path / "root"
    profile = root / "profiles" / "synthetic"
    profile.mkdir(parents=True)
    for home in (root, profile):
        (home / "auth.json").write_text(json.dumps(data()))
    return root, profile


def test_dry_run_is_redacted_and_no_writes_or_backup(stores):
    root, profile = stores
    before = [(h / "auth.json").read_bytes() for h in stores]
    result = M.migrate(root, profile)
    assert result["dry_run"] and result["same_root_identity"]
    assert result["oauth_records_to_remove"] == 2
    assert result["static_codex_entries_retained"] == 1
    assert not result["shared_fallback_after_migration"]  # explicit static key still shadows root
    assert "synthetic" not in json.dumps(result)
    assert [(h / "auth.json").read_bytes() for h in stores] == before
    assert not (profile / ".codex-migration-backups").exists()


def test_apply_preserves_other_providers_keys_and_protected_exact_backup(stores):
    root, profile = stores
    original = (profile / "auth.json").read_bytes()
    root_before = (root / "auth.json").read_bytes()
    result = M.migrate(root, profile, apply=True, root_verified=True, writers_quiesced=True)
    assert result["applied"] and result["protected_backup_created"]
    updated = json.loads((profile / "auth.json").read_text())
    old = json.loads(original)
    assert updated["providers"] == {"other": old["providers"]["other"]}
    assert updated["credential_pool"]["openai-codex"] == [old["credential_pool"]["openai-codex"][1]]
    assert updated["credential_pool"]["other"] == old["credential_pool"]["other"]
    assert updated["suppressed_sources"] == old["suppressed_sources"]
    assert updated["active_provider"] == old["active_provider"]
    backups = list((profile / ".codex-migration-backups").glob("*.json"))
    assert len(backups) == 1 and backups[0].read_bytes() == original
    assert stat.S_IMODE(backups[0].stat().st_mode) == 0o600
    assert stat.S_IMODE(backups[0].parent.stat().st_mode) == 0o700
    assert stat.S_IMODE((profile / "auth.json").stat().st_mode) == 0o600
    assert (root / "auth.json").read_bytes() == root_before


def test_oauth_only_migration_enables_shared_fallback(stores):
    root, profile = stores
    local = data()
    local["credential_pool"]["openai-codex"].pop()
    (profile / "auth.json").write_text(json.dumps(local))
    result = M.migrate(root, profile, apply=True, root_verified=True, writers_quiesced=True)
    assert result["shared_fallback_after_migration"]
    assert not M.A._codex_has_local_state(json.loads((profile / "auth.json").read_text()))


@pytest.mark.parametrize("failure", ["identity", "unknown", "expired", "invalid", "apply_gate", "non_oauth", "symlink_backup"])
def test_preflight_fails_closed_without_mutations(stores, failure, tmp_path):
    root, profile = stores
    if failure == "identity":
        (profile / "auth.json").write_text(json.dumps(data("different-synthetic-account")))
    if failure == "unknown":
        local = data()
        local["providers"]["openai-codex"]["tokens"]["access_token"] = "opaque-synthetic"
        (profile / "auth.json").write_text(json.dumps(local))
    if failure == "expired":
        (root / "auth.json").write_text(json.dumps(data(exp=1)))
    if failure == "invalid":
        (profile / "auth.json").write_text("invalid synthetic JSON")
    if failure == "non_oauth":
        local = data()
        local["providers"]["openai-codex"]["api_key"] = "synthetic-preserve"
        (profile / "auth.json").write_text(json.dumps(local))
    if failure == "symlink_backup":
        (profile / ".codex-migration-backups").symlink_to(tmp_path)
    before = [(h / "auth.json").read_bytes() for h in stores]
    with pytest.raises(M.MigrationBlocked):
        M.migrate(root, profile, apply=True, root_verified=failure != "apply_gate", writers_quiesced=True)
    assert [(h / "auth.json").read_bytes() for h in stores] == before


def test_root_cannot_be_migration_target(stores):
    root, _ = stores
    with pytest.raises(M.MigrationBlocked, match="not_a_named_profile"):
        M.migrate(root, root)
