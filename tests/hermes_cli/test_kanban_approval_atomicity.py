"""Deny-only regression checks at the approval blocker transaction boundary."""

from contextlib import contextmanager
import os

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / "home"))
    path = tmp_path / "approval.db"
    kb.init_db(path)
    with kb.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="ownership fixture", assignee="worker")
        claimed = kb.claim_task(conn, tid)
        yield path, conn, claimed


def test_successor_claim_before_write_lock_is_not_blocked(board, monkeypatch):
    path, conn, claimed = board
    original_txn = kb.write_txn
    successor = {}

    @contextmanager
    def change_claim_before_lock(target, *args, **kwargs):
        if target is conn and not successor:
            with kb.connect_closing(path) as other, original_txn(other):
                row = other.execute(
                    "INSERT INTO task_runs "
                    "(task_id, status, claim_lock, claim_expires, started_at) "
                    "VALUES (?, 'running', 'successor-lock', ?, ?)",
                    (claimed.id, claimed.claim_expires, claimed.started_at),
                )
                successor["id"] = row.lastrowid
                other.execute(
                    "UPDATE task_runs SET status = 'blocked', ended_at = 1 WHERE id = ?",
                    (claimed.current_run_id,),
                )
                other.execute(
                    "UPDATE tasks SET current_run_id = ?, claim_lock = 'successor-lock' "
                    "WHERE id = ?", (successor["id"], claimed.id),
                )
        with original_txn(target, *args, **kwargs):
            yield

    monkeypatch.setattr(kb, "write_txn", change_claim_before_lock)
    result = kb.block_approval_unavailable(conn, claimed.id, claimed.current_run_id)
    assert result["board_updated"] is False
    task = kb.get_task(conn, claimed.id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == successor["id"]
    assert task.claim_lock == "successor-lock"
    assert conn.execute(
        "SELECT count(*) FROM task_events WHERE task_id = ? AND kind = 'blocked'",
        (claimed.id,),
    ).fetchone()[0] == 0


def test_event_failure_rolls_back_task_and_run(board, monkeypatch):
    _, conn, claimed = board

    def unavailable_event_store(*args, **kwargs):
        raise RuntimeError("event fixture unavailable")

    monkeypatch.setattr(kb, "_append_event", unavailable_event_store)
    with pytest.raises(RuntimeError, match="event fixture unavailable"):
        kb.block_approval_unavailable(conn, claimed.id, claimed.current_run_id)
    task = kb.get_task(conn, claimed.id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == claimed.current_run_id
    assert task.claim_lock == claimed.claim_lock
    run = conn.execute("SELECT status, ended_at FROM task_runs WHERE id = ?",
                       (claimed.current_run_id,)).fetchone()
    assert tuple(run) == ("running", None)
