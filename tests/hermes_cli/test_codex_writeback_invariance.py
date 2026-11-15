"""Codex maintenance must not choose a provider. Synthetic, real file I/O only."""
import base64
import copy
from dataclasses import replace
import json
import time

import pytest

from agent.credential_pool import PooledCredential, load_pool
from hermes_cli import auth as A


def jwt(exp=None, account="target"):
    claims = {"exp": exp or int(time.time()) + 7200, "sub": account}
    return "e30." + base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=") + ".synthetic"


def oauth(id="target", refresh="synthetic-old", source="manual:device_code"):
    return {"id": id, "auth_type": "oauth", "source": source,
            "access_token": jwt(1, id), "refresh_token": refresh, "priority": 0}


def data(token=None, rows=None, refresh="synthetic-old"):
    result = {"version": 1, "active_provider": "other",
              "providers": {"other": {"api_key": "synthetic-other", "nested": [1, 2]}},
              "credential_pool": {"other": [{"id": "other", "access_token": "synthetic-other"}]},
              "suppressed_sources": {"other": ["env:OTHER"]}, "metadata": {"keep": True}}
    if token is not None:
        result["providers"]["openai-codex"] = {"tokens": {"access_token": token, "refresh_token": refresh}, "auth_mode": "chatgpt"}
    if rows is not None:
        result["credential_pool"]["openai-codex"] = rows
    return result


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value))


def outside_codex(value):
    result = copy.deepcopy(value)
    result["providers"].pop("openai-codex", None)
    result["credential_pool"].pop("openai-codex", None)
    result.pop("updated_at", None)
    return result


@pytest.fixture(params=[False, True], ids=["local", "borrowed"])
def owner(tmp_path, monkeypatch, request):
    root = tmp_path / "root" / "auth.json"
    profile = root.parent / "profiles" / "test" / "auth.json"
    save(root, data())
    save(profile, data())
    monkeypatch.setenv("HOME", str(tmp_path / "empty"))
    monkeypatch.setenv("HERMES_HOME", str(profile.parent))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root)
    monkeypatch.setattr(A, "_import_codex_cli_tokens", lambda: pytest.fail("External import forbidden"))
    target, untouched = (root, profile) if request.param else (profile, root)
    return target, untouched


def siblings():
    return [oauth("independent", "synthetic-independent"),
            {"id": "static", "auth_type": "api_key", "source": "manual:api_key",
             "access_token": "synthetic-static", "priority": 20}]


@pytest.mark.parametrize("path", ["singleton", "pool-only", "pool-singleton", "compatibility"])
def test_successful_writeback_preserves_provider_choice(owner, monkeypatch, path):
    target, untouched = owner
    has_singleton = path != "pool-only"
    rows = None if path == "singleton" else [oauth(source="device_code" if has_singleton else "manual:device_code"), *siblings()]
    original = data(jwt(1) if has_singleton else None, rows)
    save(target, original)
    untouched_bytes = untouched.read_bytes()
    calls = []
    fresh = jwt()
    def refresh(at, rt, **kwargs):
        calls.append(rt)
        return {"access_token": fresh, "refresh_token": "synthetic-next"}
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", refresh)
    if path == "singleton":
        assert A.resolve_codex_runtime_credentials()["api_key"] == fresh
    else:
        pool = load_pool("openai-codex")
        original = json.loads(target.read_text())  # Seeding/normalization boundary.
        entry = next(e for e in pool.entries() if e.id == "target")
        if path == "compatibility":
            pool._sync_device_code_entry_to_auth_store(replace(entry, access_token=fresh, refresh_token="synthetic-next"))
        else:
            updated = pool._refresh_entry(entry, force=True)
            assert updated is not None and updated.access_token == fresh
    actual = json.loads(target.read_text())
    assert outside_codex(actual) == outside_codex(original)
    assert untouched.read_bytes() == untouched_bytes
    assert calls == ([] if path == "compatibility" else ["synthetic-old"])
    if has_singleton:
        assert actual["providers"]["openai-codex"]["tokens"]["refresh_token"] == "synthetic-next"
    if rows is not None:
        before = {r["id"]: r for r in original["credential_pool"]["openai-codex"]}
        after = {r["id"]: r for r in actual["credential_pool"]["openai-codex"]}
        for id in ["independent", "static"]:
            assert after[id] == before[id]
        assert after["target"]["refresh_token"] == "synthetic-next"


@pytest.mark.parametrize("singleton", ["absent", "same-grant", "independent"])
def test_terminal_error_quarantines_only_failed_grant(owner, monkeypatch, singleton):
    target, untouched = owner
    token = None if singleton == "absent" else jwt(1)
    refresh = "synthetic-singleton" if singleton == "independent" else "synthetic-old"
    save(target, data(token, [oauth(), oauth("alias"), *siblings()], refresh))
    pool = load_pool("openai-codex")
    before = json.loads(target.read_text())
    untouched_bytes = untouched.read_bytes()
    calls = []
    def reject(at, rt, **kwargs):
        calls.append(rt)
        raise A.AuthError("Synthetic rejection", provider="openai-codex", code="invalid_grant", relogin_required=True)
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", reject)
    entry = next(e for e in pool.entries() if e.id == "target")
    assert pool._refresh_entry(entry, force=True) is None
    actual = json.loads(target.read_text())
    assert outside_codex(actual) == outside_codex(before)
    assert untouched.read_bytes() == untouched_bytes
    assert calls == ["synthetic-old"]
    remaining = {r["id"]: r for r in actual["credential_pool"]["openai-codex"]}
    original = {r["id"]: r for r in before["credential_pool"]["openai-codex"]}
    assert "target" not in remaining and "alias" not in remaining
    for id in ["independent", "static"]:
        assert remaining[id] == original[id]
    if singleton == "independent":
        assert actual["providers"]["openai-codex"] == before["providers"]["openai-codex"]
    else:
        state = actual["providers"]["openai-codex"]
        assert not state["tokens"].get("access_token")
        assert not state["tokens"].get("refresh_token")
        assert state["last_auth_error"]["code"] == "invalid_grant"


def test_explicit_login_still_activates_locally(owner):
    target, untouched = owner
    # Regardless of the runtime owner, an explicit login saves locally.
    save(target, data(jwt(1)))
    root = A._global_auth_file_path()
    assert root is not None
    root_bytes = root.read_bytes()
    A._save_codex_tokens({"access_token": jwt(account="new-local"), "refresh_token": "synthetic-local"})
    actual = json.loads(A._auth_file_path().read_text())
    assert actual["active_provider"] == "openai-codex"
    assert actual["providers"]["other"] == data()["providers"]["other"]
    assert root.read_bytes() == root_bytes


def test_local_cli_recovery_does_not_activate_provider(owner, monkeypatch):
    target, untouched = owner
    if target != A._auth_file_path():
        # Borrowed grants fail closed, without importing application credentials.
        save(target, data(jwt(1)))
        assert A._recover_codex_tokens_from_cli("synthetic") is None
        return
    save(target, data(jwt(1)))
    before = json.loads(target.read_text())
    other_bytes = untouched.read_bytes()
    monkeypatch.setattr(A, "_import_codex_cli_tokens", lambda: {"access_token": jwt(), "refresh_token": "synthetic-recovered"})
    assert A._recover_codex_tokens_from_cli("synthetic")
    assert outside_codex(json.loads(target.read_text())) == outside_codex(before)
    assert untouched.read_bytes() == other_bytes
