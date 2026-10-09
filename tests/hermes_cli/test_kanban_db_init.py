from __future__ import annotations

import os
import sqlite3
import threading
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


def test_connect_initialization_is_thread_safe(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path(board="default")
    kb._INITIALIZED_PATHS.discard(str(db_path.resolve()))

    errors: list[BaseException] = []
    barrier = threading.Barrier(8)

    def worker() -> None:
        try:
            barrier.wait(timeout=5)
            conn = kb.connect(board="default")
            conn.close()
        except BaseException as exc:  # pragma: no cover - surfaced below
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=10)

    assert errors == []
    with kb.connect(board="default") as conn:
        cols = {row["name"] for row in conn.execute("PRAGMA table_info(tasks)")}
    assert "max_retries" in cols


@pytest.mark.skipif(not Path("/proc/self/fd").exists(), reason="requires procfs fd links")
def test_deleted_wal_sidecar_fd_probe_reports_unlinked_sidecars(tmp_path):
    db_path = tmp_path / "stale-sidecar.db"
    conn = sqlite3.connect(str(db_path), isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA wal_autocheckpoint=0")
        conn.execute("CREATE TABLE t(id INTEGER PRIMARY KEY, value TEXT)")
        conn.execute("INSERT INTO t(value) VALUES ('keeps-wal-open')")

        wal_path = Path(str(db_path) + "-wal")
        assert wal_path.exists(), "test setup must create a WAL sidecar"
        os.unlink(wal_path)

        stale = kb.deleted_wal_sidecar_fds(db_path)
    finally:
        conn.close()

    assert any(entry["suffix"] == "-wal" for entry in stale)
    assert all(str(db_path.resolve()) in entry["target"] for entry in stale)
