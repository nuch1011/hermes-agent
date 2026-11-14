# Codex OAuth ownership and offline override migration

This backport keeps shared Codex OAuth credentials in the auth store that owns
them. It does not import credentials, copy auth stores, change other providers,
or deploy/restart any runtime.

## Ownership and synchronization

- A local `providers.openai-codex` block OR a nonempty local Codex pool shadows
  root. Pool-only logins created by `auth add` are real local logins.
- Without a local login, reads, pool seeding, refreshes and status persistence
  use the root store. No borrowed credential is materialized in the profile.
- Lock ordering is profile `auth.lock`, then the owning root `auth.lock` when
  needed. Reentrancy is keyed by canonical lock path, not just thread depth.
  Root refresh uses the same lock filename as older singleton resolvers.
- Re-read and refresh happen under the owner's lock. Waiters adopt a rotation
  already persisted by another process. Cached pools cannot overwrite newer
  disk tokens with stale status updates, or change owners without reloading.
- A pool refresh atomically writes its grant, exact refresh-token aliases and
  any matching singleton. Unrelated accounts and API keys are not replaced.
  Explicit manual accounts precede compatibility singleton entries.
- Explicit new singleton and pool logins still save locally. A new local pool
  login does not copy borrowed root accounts into the profile.
- Shared refresh errors do not recover by importing another application's
  credentials. They fail closed at the owner.

Minimal runtime file set: `hermes_cli/auth.py`, `agent/credential_pool.py`.
The auxiliary client uses the existing pool selection path and needs no change.

## Isolated regression tests

Always use the canonical runner and an empty HOME as well as its existing
per-test HERMES_HOME isolation. The runner itself preserves HOME; external
provider discovery in older tests must not see a developer's actual home.

```sh
sandbox_home="$(mktemp -d)"
HOME="$sandbox_home" scripts/run_tests.sh -j 4 \
  tests/hermes_cli/test_codex_shared_auth.py \
  tests/hermes_cli/test_codex_override_migration.py \
  tests/hermes_cli/test_auth_codex_provider.py \
  tests/hermes_cli/test_auth_codex_self_heal.py \
  tests/hermes_cli/test_auth_profile_fallback.py \
  tests/hermes_cli/test_auth_commands.py \
  tests/agent/test_credential_pool.py \
  tests/agent/test_credential_pool_oauth_writethrough.py \
  tests/agent/test_credential_pool_routing.py \
  tests/agent/test_auxiliary_client.py
```

The new multiprocess tests use an actual localhost HTTP token endpoint with a
synthetic one-use grant. They cover profile/profile pool refreshes, root/profile
singleton refreshes and mixed singleton/pool refreshes, checking one POST, no
lost rotation, no deadlock and byte-identical profile stores.

## Optional migration artifact (not runtime, not logout)

`scripts/migrate_codex_override.py` removes ONLY recognized local Codex OAuth
overrides. It never refreshes/revokes tokens or performs a global logout.
It is a separately reviewed, opt-in migration tool, not part of installation.

Default is dry-run. Provide explicit root and named-profile home paths:

```sh
python scripts/migrate_codex_override.py \
  --root-home /path/to/root \
  --profile-home /path/to/root/profiles/example
```

No auth store is changed in dry-run; advisory lock files may be created.
Output contains booleans/counts only, never identities or token fingerprints.
Preflight requires an unexpired root OAuth JWT with refresh material, known
identity, and all removed local OAuth grants belonging to that same identity.
Unknown/mismatching identities, malformed stores, unrecognized singleton fields,
symlink paths and ambiguous healthy root accounts fail closed. JWT decoding is
an offline ownership hint, NOT signature validation or proof of server health.

Applying additionally requires `--apply --root-verified --writers-quiesced`.
These flags are operator attestations, not automatic service-health checks.
Before use, independently verify the root login and quiesce profile consumers
under a coordinated rollout gate. Stop/reload gateway caches intentionally;
do not interrupt an unrelated active conversation to run migration.

Apply creates a durable exact local backup in
`<profile>/.codex-migration-backups/` (directory 0700, file 0600) before atomically
replacing the profile auth store (0600). Backup files contain credentials:
never upload, commit, attach or log them. Other providers, static API keys,
active-provider metadata and suppression policy are preserved. A retained local
static Codex key still shadows root; the report explicitly marks shared
fallback as inactive in that case. Suppression policy is not silently reset.

## Rollback and deployment gates

Review and isolated QA must precede deployment or migration. Deploy both runtime
files together from the same reviewed revision, with a rollback copy/hash of
the previous code and a planned cache/reload boundary. This change does not
schedule or perform deployment.

Code rollback and credential rollback are different: reverting code must NOT
restore a stale OAuth refresh token. Reverting to code without shared writeback
also reintroduces the original race; keep affected profile consumers quiesced.
Migration backups are sensitive recovery evidence, not reusable token copies.
Never blindly restore an old backup after the shared grant has rotated; prefer
an explicit new local login when returning a profile to independent ownership.
