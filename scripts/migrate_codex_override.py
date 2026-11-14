#!/usr/bin/env python3
"""Offline, opt-in Codex override migration. NOT logout; never refreshes tokens.

Default: dry-run, redacted booleans/counts only. Apply requires an operator's
independent root-login verification AND quiesced profile writers/gateways.
Backups contain credentials: local only, directory 0700 / file 0600. Never
upload, log, commit or automatically restore them (refresh grants rotate).
"""
import argparse
import copy
import json
import os
from pathlib import Path
import sys
import time
import uuid

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hermes_cli import auth as A

PROVIDER = "openai-codex"


class MigrationBlocked(Exception):
    """Only static reason codes may be exposed to the operator."""


def _read(path):
    if path.is_symlink() or not path.is_file():
        raise MigrationBlocked("missing_or_symlink_store")
    try:
        value = json.loads(path.read_text())
    except (OSError, ValueError):
        raise MigrationBlocked("unreadable_or_invalid_store") from None
    if not isinstance(value, dict) or not isinstance(value.get("providers", {}), dict) or not isinstance(value.get("credential_pool", {}), dict):
        raise MigrationBlocked("invalid_store_shape")
    return value


def _identity(tokens):
    claims = A._decode_jwt_claims(str(tokens.get("access_token") or ""))
    namespace = claims.get("https://api.openai.com/auth", {})
    account = namespace.get("chatgpt_account_id") if isinstance(namespace, dict) else None
    subject = claims.get("sub")
    # Identity is used ONLY in memory. No IDs, hashes or token fingerprints
    # are returned, including on a mismatch. Unsigned decoding is not a live
    # health proof; apply additionally requires independent verification.
    if not isinstance(account, str) or not account or not isinstance(subject, str) or not subject:
        raise MigrationBlocked("identity_unknown")
    return account, subject


def _oauth_rows(data):
    state = data.get("providers", {}).get(PROVIDER)
    rows = []
    if state is not None:
        if not isinstance(state, dict) or not isinstance(state.get("tokens"), dict):
            raise MigrationBlocked("unrecognized_singleton")
        if set(state) - {"tokens", "last_refresh", "auth_mode", "label", "last_auth_error"}:
            raise MigrationBlocked("singleton_has_non_oauth_fields")
        if set(state["tokens"]) - {"access_token", "refresh_token", "id_token", "token_type", "account_id"}:
            raise MigrationBlocked("singleton_tokens_have_unknown_fields")
        rows.append(state["tokens"])
    pool = data.get("credential_pool", {}).get(PROVIDER, [])
    if not isinstance(pool, list) or any(not isinstance(e, dict) for e in pool):
        raise MigrationBlocked("invalid_pool_shape")
    for entry in pool:
        if entry.get("auth_type") == "oauth":
            if entry.get("source") not in {"device_code", "manual:device_code"}:
                raise MigrationBlocked("unrecognized_oauth_source")
            rows.append(entry)
    return rows


def migrate(root_home, profile_home, *, apply=False, root_verified=False, writers_quiesced=False):
    root_home, profile_home = Path(root_home).absolute(), Path(profile_home).absolute()
    if root_home != root_home.resolve() or profile_home != profile_home.resolve():
        raise MigrationBlocked("symlink_path")
    if profile_home.parent != root_home / "profiles" or profile_home.name in {"", "default"}:
        raise MigrationBlocked("not_a_named_profile")
    root_path, profile_path = root_home / "auth.json", profile_home / "auth.json"
    # Same ordering and same canonical lock files as the runtime.
    with A._auth_store_lock(auth_file=profile_path):
        with A._auth_store_lock(auth_file=root_path):
            root, profile = _read(root_path), _read(profile_path)
            root_rows, local_rows = _oauth_rows(root), _oauth_rows(profile)
            healthy = [r for r in root_rows if r.get("refresh_token") and r.get("access_token") and isinstance(A._decode_jwt_claims(r["access_token"]).get("exp"), (int, float)) and A._decode_jwt_claims(r["access_token"])["exp"] > time.time() + 60 and r.get("last_status") not in {"dead", "exhausted"}]
            if not healthy:
                raise MigrationBlocked("root_not_locally_healthy")
            identities = {_identity(r) for r in healthy}
            if len(identities) != 1:
                raise MigrationBlocked("root_identity_ambiguous")
            same = all(_identity(r) in identities for r in local_rows)
            if not same:
                raise MigrationBlocked("identity_mismatch")
            updated = copy.deepcopy(profile)
            if local_rows:
                updated.get("providers", {}).pop(PROVIDER, None)
                pool = updated.get("credential_pool", {})
                remaining = [r for r in pool.get(PROVIDER, []) if r.get("auth_type") != "oauth"]
                if remaining:
                    pool[PROVIDER] = remaining
                else:
                    pool.pop(PROVIDER, None)
            # Suppression is profile-local policy, not an OAuth credential. Do
            # not remove it implicitly or change active_provider/other providers.
            report = {"dry_run": not apply, "root_locally_healthy": True, "same_root_identity": same, "oauth_records_to_remove": len(local_rows), "static_codex_entries_retained": len(updated.get("credential_pool", {}).get(PROVIDER, [])), "shared_fallback_after_migration": not A._codex_has_local_state(updated), "applied": False}
            if not apply or not local_rows:
                return report
            if not root_verified or not writers_quiesced:
                raise MigrationBlocked("apply_requires_verified_root_and_quiesced_writers")
            backup_dir = profile_home / ".codex-migration-backups"
            if backup_dir.is_symlink():
                raise MigrationBlocked("symlink_backup_directory")
            backup_dir.mkdir(mode=0o700, exist_ok=True)
            backup_dir.chmod(0o700)
            backup = backup_dir / (f"auth.pre-codex-migration.{time.time_ns()}.{uuid.uuid4().hex}.json")
            fd = os.open(str(backup), os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            with os.fdopen(fd, "wb") as handle:
                handle.write(profile_path.read_bytes())
                handle.flush()
                os.fsync(handle.fileno())
            # Fsync backup directory before replacing the only live copy.
            fd = os.open(str(backup_dir), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
            A._save_auth_store(updated, target_path=profile_path)
            report["applied"] = True
            report["protected_backup_created"] = True
            return report


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root-home", required=True, type=Path)
    parser.add_argument("--profile-home", required=True, type=Path)
    parser.add_argument("--apply", action="store_true", help="Default is dry-run")
    parser.add_argument("--root-verified", action="store_true", help="Operator independently verified root login")
    parser.add_argument("--writers-quiesced", action="store_true", help="Operator stopped/reloaded profile consumers under rollout gate")
    args = parser.parse_args()
    try:
        report = migrate(args.root_home, args.profile_home, apply=args.apply, root_verified=args.root_verified, writers_quiesced=args.writers_quiesced)
    except MigrationBlocked as exc:
        print(json.dumps({"blocked": True, "reason": str(exc)}))
        return 2
    except Exception:
        print(json.dumps({"blocked": True, "reason": "migration_io_failure"}))
        return 2
    print(json.dumps(report, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
