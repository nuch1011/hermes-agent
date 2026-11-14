"""Shared Codex ownership contracts. All stores and grants are synthetic."""
import base64
import json
import os
from pathlib import Path
import subprocess
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs

import pytest

from hermes_cli import auth as A
from agent.credential_pool import PooledCredential, load_pool


def jwt(exp, account="synthetic-account"):
    payload = {"exp": exp, "sub": "synthetic-user", "https://api.openai.com/auth": {"chatgpt_account_id": account}}
    return "e30." + base64.urlsafe_b64encode(json.dumps(payload).encode()).decode().rstrip("=") + ".synthetic"


def store(path, *, token=None, refresh="synthetic-old", rows=None):
    data = {"version": 1, "providers": {"other": {"api_key": "synthetic-static"}}, "credential_pool": {"other": [{"id": "other", "access_token": "synthetic-key"}]}}
    if token is not None:
        data["providers"]["openai-codex"] = {"tokens": {"access_token": token, "refresh_token": refresh}, "auth_mode": "chatgpt"}
    if rows is not None:
        data["credential_pool"]["openai-codex"] = rows
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data))
    return path.read_bytes()


def row(id="manual", token=None, refresh="synthetic-old", source="manual:device_code"):
    return {"id": id, "source": source, "auth_type": "oauth", "access_token": token or jwt(1), "refresh_token": refresh, "priority": 0}


@pytest.fixture
def stores(tmp_path, monkeypatch):
    root = tmp_path / "root" / "auth.json"
    profile = tmp_path / "profiles" / "a" / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: profile)
    monkeypatch.setattr(A, "_global_auth_file_path", lambda: root)
    monkeypatch.setenv("HOME", str(tmp_path / "unrelated-home"))
    monkeypatch.setenv("CODEX_HOME", str(tmp_path / "no-codex"))
    monkeypatch.setattr(A, "_import_codex_cli_tokens", lambda: pytest.fail("No external credential import"))
    store(profile)
    return root, profile


@pytest.mark.parametrize("pool_only", [False, True])
def test_shared_refresh_stays_at_root_and_other_profile_sees_rotation(stores, monkeypatch, pool_only):
    root, profile = stores
    store(root, token=None if pool_only else jwt(1), rows=[row()] if pool_only else None)
    before = profile.read_bytes()
    calls = []
    fresh = jwt(int(time.time()) + 7200)
    def refresh(at, rt, **kwargs):
        calls.append(rt)
        return {"access_token": fresh, "refresh_token": "synthetic-next", "last_refresh": "synthetic-time"}
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", refresh)
    assert A.resolve_codex_runtime_credentials()["api_key"] == fresh
    assert profile.read_bytes() == before
    second = profile.parent.parent / "b" / "auth.json"
    monkeypatch.setattr(A, "_auth_file_path", lambda: second)
    assert A.resolve_codex_runtime_credentials()["api_key"] == fresh
    assert not second.exists()
    assert calls == ["synthetic-old"]
    assert b"synthetic-next" in root.read_bytes()


def test_local_login_and_explicit_saves_do_not_change_root(stores, monkeypatch):
    root, profile = stores
    root_before = store(root, token=jwt(1))
    A._save_codex_tokens({"access_token": jwt(1, "local"), "refresh_token": "local-old"})
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: {"access_token": jwt(int(time.time()) + 7200, "local"), "refresh_token": "local-next"})
    A.resolve_codex_runtime_credentials()
    assert root.read_bytes() == root_before
    assert b"local-next" in profile.read_bytes()


def test_auth_add_pool_only_is_local_without_copying_borrowed_accounts(stores):
    root, profile = stores
    store(root, token=jwt(int(time.time()) + 7200), rows=[row("root-manual", token=jwt(int(time.time()) + 7200))])
    pool = load_pool("openai-codex")
    root_before = root.read_bytes()
    pool.add_entry(PooledCredential.from_dict("openai-codex", row("local", token=jwt(int(time.time()) + 7200, "local"), refresh="local-refresh")))
    local = json.loads(profile.read_text())
    assert [r["id"] for r in local["credential_pool"]["openai-codex"]] == ["local"]
    assert "openai-codex" not in local["providers"]
    assert root.read_bytes() == root_before
    assert A.resolve_codex_runtime_credentials()["api_key"] == local["credential_pool"]["openai-codex"][0]["access_token"]


def test_pool_auxiliary_refresh_syncs_singleton_alias_not_other_account(stores, monkeypatch):
    from agent import auxiliary_client as aux
    root, profile = stores
    old = jwt(1)
    other = row("independent", token=jwt(int(time.time()) + 7200, "other"), refresh="other-refresh")
    other["priority"] = 5
    store(root, token=old, rows=[row("alias", token=old), other])
    before = profile.read_bytes()
    fresh = jwt(int(time.time()) + 7200)
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: {"access_token": fresh, "refresh_token": "synthetic-next"})
    assert aux._read_codex_access_token() == fresh
    data = json.loads(root.read_text())
    assert data["providers"]["openai-codex"]["tokens"]["refresh_token"] == "synthetic-next"
    assert next(r for r in data["credential_pool"]["openai-codex"] if r["id"] == "independent")["refresh_token"] == "other-refresh"
    assert profile.read_bytes() == before


def test_stale_pool_status_write_does_not_restore_consumed_refresh(stores, monkeypatch):
    root, profile = stores
    store(root, rows=[row()])
    first = load_pool("openai-codex")
    stale = load_pool("openai-codex")
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: {"access_token": jwt(int(time.time()) + 7200), "refresh_token": "synthetic-next"})
    first.select()
    stale._mark_exhausted(stale.entries()[0], 401)
    saved = json.loads(root.read_text())["credential_pool"]["openai-codex"][0]
    assert saved["refresh_token"] == "synthetic-next"
    assert saved.get("last_status") != "exhausted"


def test_owner_change_fails_before_network(stores, monkeypatch):
    root, profile = stores
    store(root, rows=[row()])
    old_pool = load_pool("openai-codex")
    A._save_codex_tokens({"access_token": jwt(1, "local"), "refresh_token": "local"})
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: pytest.fail("must not refresh stale owner"))
    with pytest.raises(RuntimeError, match="owner changed"):
        old_pool._refresh_entry(old_pool.entries()[0], force=True)


WORKER = r'''
import json, os, sys, time
from pathlib import Path
from hermes_cli import auth as A
root, profile, endpoint, kind, ready, go = sys.argv[1:]
os.environ['HERMES_HOME'] = str(Path(profile).parent)
os.environ['CODEX_HOME'] = str(Path(ready).parent / 'absent-codex')
A._global_auth_file_path = lambda: Path(root) if profile != root else None
A.CODEX_OAUTH_TOKEN_URL = endpoint
A._import_codex_cli_tokens = lambda: (_ for _ in ()).throw(AssertionError('no import'))
if kind == 'pool':
    from agent.credential_pool import load_pool
    pool = load_pool('openai-codex')
Path(ready).touch()
while not Path(go).exists(): time.sleep(.01)
if kind == 'pool':
    entry = pool.select()
    assert entry and entry.refresh_token == 'synthetic-next'
else:
    result = A.resolve_codex_runtime_credentials()
    assert result['api_key']
print('SYNTHETIC_OK')
'''


@pytest.mark.parametrize("kinds,pool_only", [(("singleton", "singleton"), False), (("pool", "pool"), True), (("singleton", "pool"), False)])
def test_real_processes_share_one_use_refresh(tmp_path, kinds, pool_only):
    root = tmp_path / "root" / "auth.json"
    store(root, token=None if pool_only else jwt(1), rows=[row()] if pool_only else None)
    hits = []
    guard = threading.Lock()
    fresh = jwt(int(time.time()) + 7200)
    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):
            request = parse_qs(self.rfile.read(int(self.headers["Content-Length"])).decode())
            with guard:
                hits.append(request["refresh_token"][0])
                valid = hits == ["synthetic-old"]
            time.sleep(.2)
            self.send_response(200 if valid else 401)
            self.send_header("Content-Type", "application/json")
            self.end_headers()
            self.wfile.write(json.dumps({"access_token": fresh, "refresh_token": "synthetic-next"} if valid else {"error": "refresh_token_reused"}).encode())
        def log_message(self, format, *args):
            pass
    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    processes = []
    profiles = []
    try:
        # Root singleton participant uses the same canonical auth.lock as the
        # pre-backport root resolver. The second process is a fallback profile.
        for i, kind in enumerate(kinds):
            profile = root if i == 0 and kind == "singleton" else tmp_path / "profiles" / str(i) / "auth.json"
            if profile != root:
                store(profile)
                profiles.append((profile, profile.read_bytes()))
            ready = tmp_path / f"ready-{i}"
            endpoint = f"http://127.0.0.1:{server.server_port}/token"
            env = {k: v for k, v in os.environ.items() if k in {"PATH", "LANG", "PYTHONPATH", "PYTEST_CURRENT_TEST"}}
            env["HOME"] = str(tmp_path / "home")
            processes.append(subprocess.Popen([sys.executable, "-c", WORKER, str(root), str(profile), endpoint, kind, str(ready), str(tmp_path / "go")], env=env, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True))
        deadline = time.monotonic() + 15
        while not all((tmp_path / f"ready-{i}").exists() for i in range(2)):
            assert time.monotonic() < deadline, "workers failed to start"
            time.sleep(.02)
        (tmp_path / "go").touch()
        for process in processes:
            stdout, stderr = process.communicate(timeout=20)
            assert process.returncode == 0, stderr
            assert stdout.strip() == "SYNTHETIC_OK"
        assert hits == ["synthetic-old"]
        for profile, before in profiles:
            assert profile.read_bytes() == before
        assert b"synthetic-next" in root.read_bytes()
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait()
        server.shutdown()
        server.server_close()


def test_fresh_manual_login_precedes_unrelated_old_singleton(stores, monkeypatch):
    root, profile = stores
    fresh = jwt(int(time.time()) + 7200, "new-account")
    store(root, token=jwt(int(time.time()) + 7200, "old-account"), rows=[row("new-login", token=fresh, refresh="new-login-refresh")])
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: pytest.fail("neither token needs refresh"))
    assert A.resolve_codex_runtime_credentials()["api_key"] == fresh
    data = json.loads(root.read_text())
    assert data["providers"]["openai-codex"]["tokens"]["refresh_token"] == "synthetic-old"
    assert next(r for r in data["credential_pool"]["openai-codex"] if r["id"] == "new-login")["refresh_token"] == "new-login-refresh"


def test_pool_only_aliases_rotate_atomically_and_cached_pool_adopts(stores, monkeypatch):
    root, profile = stores
    store(root, rows=[row("a"), row("b")])
    pool = load_pool("openai-codex")
    cached = load_pool("openai-codex")
    writes = []
    save = A._save_auth_store
    def record(data, target_path=None):
        writes.append(json.loads(json.dumps(data)))
        return save(data, target_path=target_path)
    monkeypatch.setattr(A, "_save_auth_store", record)
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: {"access_token": jwt(int(time.time()) + 7200), "refresh_token": "synthetic-next"})
    pool._refresh_entry(pool.entries()[0], force=False)
    assert len(writes) == 1
    assert {r["refresh_token"] for r in writes[0]["credential_pool"]["openai-codex"]} == {"synthetic-next"}
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: pytest.fail("cached pool must adopt rotation"))
    assert cached.select().refresh_token == "synthetic-next"


def test_shared_refresh_error_does_not_import_or_create_local_copy(stores, monkeypatch):
    root, profile = stores
    root_before = store(root, token=jwt(1))
    local_before = profile.read_bytes()
    def fail(*args, **kwargs):
        raise A.AuthError("synthetic rejection", provider="openai-codex", code="invalid_grant", relogin_required=True)
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fail)
    with pytest.raises(A.AuthError, match="synthetic rejection"):
        A.resolve_codex_runtime_credentials()
    assert root.read_bytes() == root_before
    assert profile.read_bytes() == local_before


def test_independent_manual_terminal_failure_keeps_singleton_and_other_account(stores, monkeypatch):
    root, profile = stores
    singleton_token = jwt(int(time.time()) + 7200, "singleton")
    independent = row("other", token=jwt(int(time.time()) + 7200, "other"), refresh="other-refresh")
    store(root, token=singleton_token, refresh="singleton-refresh", rows=[row("failed"), independent])
    pool = load_pool("openai-codex")
    def fail(*a, **kw):
        raise A.AuthError("synthetic revoked", provider="openai-codex", code="invalid_grant", relogin_required=True)
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", fail)
    pool._refresh_entry(next(e for e in pool.entries() if e.id == "failed"), force=True)
    saved = json.loads(root.read_text())
    assert saved["providers"]["openai-codex"]["tokens"]["access_token"] == singleton_token
    assert next(r for r in saved["credential_pool"]["openai-codex"] if r["id"] == "other")["refresh_token"] == "other-refresh"


def test_removed_cached_manual_login_does_not_refresh_or_reappear(stores, monkeypatch):
    root, profile = stores
    store(root, token=jwt(int(time.time()) + 7200), rows=[row("removed")])
    pool = load_pool("openai-codex")
    saved = json.loads(root.read_text())
    saved["credential_pool"]["openai-codex"] = [r for r in saved["credential_pool"]["openai-codex"] if r["id"] != "removed"]
    root.write_text(json.dumps(saved))
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: pytest.fail("removed grant must not refresh"))
    pool.select()
    pool._persist()
    assert all(r["id"] != "removed" for r in json.loads(root.read_text())["credential_pool"]["openai-codex"])


@pytest.mark.parametrize("force", [False, True])
def test_pool_only_respects_no_network_flag_and_forces_exactly_once(stores, monkeypatch, force):
    root, profile = stores
    expired = jwt(1)
    store(root, rows=[row(token=expired)])
    calls = []
    fresh = jwt(int(time.time()) + 7200)
    def refresh(at, rt, **kwargs):
        calls.append(rt)
        return {"access_token": fresh, "refresh_token": "synthetic-next"}
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", refresh)
    resolved = A.resolve_codex_runtime_credentials(force_refresh=force, refresh_if_expiring=False)
    assert resolved["api_key"] == (fresh if force else expired)
    assert calls == (["synthetic-old"] if force else [])


def test_failed_durable_write_does_not_expose_rotated_cached_grant(stores, monkeypatch):
    root, profile = stores
    store(root, rows=[row()])
    pool = load_pool("openai-codex")
    monkeypatch.setattr(A, "refresh_codex_oauth_pure", lambda *a, **kw: {"access_token": jwt(int(time.time()) + 7200), "refresh_token": "synthetic-next"})
    def fail(*a, **kw):
        raise OSError("synthetic disk failure")
    monkeypatch.setattr(A, "_save_auth_store", fail)
    with pytest.raises(OSError, match="synthetic disk failure"):
        pool.select()
    assert not pool.entries()
    assert pool.select() is None
    assert b"synthetic-next" not in root.read_bytes()
