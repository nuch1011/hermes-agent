"""Failure-only approval fixtures: no notifier, no consent, temporary boards only."""

import json
import os
from unittest.mock import Mock

import pytest

from hermes_cli import kanban_db as kb
from tools import approval as A


@pytest.fixture(autouse=True)
def isolated_no_notifier(monkeypatch, tmp_path):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_") or key in (
            "HERMES_GATEWAY_SESSION", "HERMES_INTERACTIVE", "HERMES_CRON_SESSION",
        ):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_KANBAN_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setenv("HERMES_EXEC_ASK", "1")
    monkeypatch.setenv("TERMINAL_ENV", "local")
    monkeypatch.setenv("TERMINAL_CWD", str(tmp_path))
    from tools import terminal_tool as tt
    monkeypatch.setattr(tt, "_start_cleanup_thread", lambda: None)
    monkeypatch.setattr(tt, "_active_environments", {})
    monkeypatch.setattr(tt, "_last_activity", {})
    monkeypatch.setattr(A, "_get_approval_mode", lambda: "manual")
    monkeypatch.setattr(A, "_YOLO_MODE_FROZEN", False)
    monkeypatch.setattr(A, "_gateway_notify_cbs", {})
    monkeypatch.setattr(A, "_gateway_queues", {})
    monkeypatch.setattr(A, "_session_approved", {})
    monkeypatch.setattr(A, "_permanent_approved", set())
    # A warning-only scanner fixture escalates a harmless print marker. It never
    # returns consent and neither facade's execution machinery is mocked away.
    import tools.tirith_security as tirith
    monkeypatch.setattr(tirith, "check_command_security", lambda command: {
        "action": "warn", "findings": [], "summary": "test requires approval",
    })
    pending = Mock()
    monkeypatch.setattr(A, "submit_pending", pending)
    token = A.set_current_session_key("kanban-no-notifier-test")
    yield pending
    for env in tt._active_environments.values():
        env.cleanup()
    A.reset_current_session_key(token)


@pytest.mark.parametrize("guard", [A.check_all_command_guards, A.check_execute_code_guard])
def test_missing_claim_denies_without_queue(guard, monkeypatch, isolated_no_notifier):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    result = guard("print('kanban-marker')", "local")
    assert result["approved"] is False
    assert result["status"] == "approval_unavailable"
    assert result["approval_pending"] is False
    assert result["board_updated"] is False
    assert result["notification_status"] == "unverified"
    assert "interactive" in result["message"]
    assert "kanban-marker" not in json.dumps(result)
    isolated_no_notifier.assert_not_called()


@pytest.mark.parametrize("facade", ["terminal", "execute_code"])
@pytest.mark.parametrize("pinned", [False, True])
def test_facades_preserve_failure_without_executing_marker(facade, pinned, monkeypatch, claimed_board):
    from tools import terminal_tool as tt, code_execution_tool as cet
    if not pinned:
        monkeypatch.delenv("HERMES_KANBAN_DB")
    execute = Mock(wraps=tt._LocalEnvironment.execute)
    monkeypatch.setattr(tt._LocalEnvironment, "execute", execute)
    popen = Mock(wraps=cet.subprocess.Popen)
    monkeypatch.setattr(cet.subprocess, "Popen", popen)
    if facade == "terminal":
        result = json.loads(tt.terminal_tool("python -c \"print('kanban-marker')\""))
        assert result["output"] == ""
        assert result["exit_code"] == -1
    else:
        result = json.loads(cet.execute_code("print('kanban-marker')"))
        assert result["tool_calls_made"] == 0
    assert result["status"] == "approval_unavailable"
    assert result["approval_pending"] is False
    assert result["board_updated"] is pinned
    assert result["notification_status"] == ("no_subscription" if pinned else "unverified")
    if not pinned:
        assert result["board_error"] == "missing_worker_scope"
    assert "interactive" in result["error"]
    execute.assert_not_called()
    if facade == "execute_code":
        popen.assert_not_called()
    else:
        # LocalEnvironment captures its login-shell environment before guards;
        # that setup subprocess must not contain the requested marker command.
        assert all("kanban-marker" not in str(call) for call in popen.call_args_list)


@pytest.fixture
def claimed_board(monkeypatch, tmp_path):
    path = tmp_path / "pinned.db"
    with kb.connect_closing(path) as conn:
        tid = kb.create_task(conn, title="Failure-only marker", assignee="default")
        task = kb.claim_task(conn, tid)
        assert task is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    return path, task


@pytest.mark.parametrize("guard", [A.check_all_command_guards, A.check_execute_code_guard])
@pytest.mark.parametrize("subscribed", [False, True])
def test_pinned_claim_blocks_with_truthful_notification(guard, subscribed, claimed_board,
                                                       isolated_no_notifier):
    path, task = claimed_board
    with kb.connect_closing(path) as conn:
        if subscribed:
            kb.add_notify_sub(conn, task_id=task.id, platform="telegram", chat_id="test-chat")
    result = guard("print('kanban-marker')", "local")
    assert result["approved"] is False
    assert result["status"] == "approval_unavailable"
    assert result["board_updated"] is True
    assert result["notification_status"] == (
        "subscription_present_delivery_unverified" if subscribed else "no_subscription"
    )
    assert "board_error" not in result
    with kb.connect_closing(path) as conn:
        assert kb.get_task(conn, task.id).status == "blocked"
        events = conn.execute("SELECT payload FROM task_events WHERE task_id=? AND kind='blocked'",
                              (task.id,)).fetchall()
        assert len(events) == 1
        payload = json.loads(events[0]["payload"])
        assert payload["kind"] == "capability"
        assert "kanban-marker" not in events[0]["payload"]
    isolated_no_notifier.assert_not_called()


@pytest.mark.parametrize("run_id", [None, "", "0", "-1", "broken", "1.0", "1_0"])
def test_bad_run_id_never_opens_board(run_id, claimed_board, monkeypatch):
    path, task = claimed_board
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)
    connect = Mock(wraps=kb.connect_closing)
    monkeypatch.setattr(kb, "connect_closing", connect)
    result = A._kanban_approval_unavailable()
    assert result["board_updated"] is False
    assert result["board_error"] == "invalid_run_id"
    connect.assert_not_called()


def test_missing_pin_never_follows_current_board(claimed_board, monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_DB")
    resolve = Mock(side_effect=AssertionError("Must not resolve current board"))
    monkeypatch.setattr(kb, "kanban_db_path", resolve)
    result = A._kanban_approval_unavailable()
    assert result["status"] == "approval_unavailable"
    assert result["board_error"] == "missing_worker_scope"
    resolve.assert_not_called()


@pytest.mark.parametrize("change", ["run", "expired", "cleared", "status", "run_ended"])
def test_stale_claim_remains_unchanged(change, claimed_board):
    path, task = claimed_board
    with kb.connect_closing(path) as conn:
        if change == "run":
            conn.execute("UPDATE tasks SET current_run_id=NULL WHERE id=?", (task.id,))
        elif change == "expired":
            conn.execute("UPDATE tasks SET claim_expires=1 WHERE id=?", (task.id,))
        elif change == "cleared":
            conn.execute("UPDATE tasks SET claim_lock=NULL WHERE id=?", (task.id,))
        elif change == "status":
            conn.execute("UPDATE tasks SET status='review' WHERE id=?", (task.id,))
        else:
            conn.execute("UPDATE task_runs SET ended_at=1 WHERE id=?", (task.current_run_id,))
        conn.commit()
        before = conn.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()
    result = A._kanban_approval_unavailable()
    assert result["board_updated"] is False
    assert result["board_error"] == "stale_or_missing_claim"
    with kb.connect_closing(path) as conn:
        assert tuple(conn.execute("SELECT * FROM tasks WHERE id=?", (task.id,)).fetchone()) == tuple(before)
        assert not conn.execute("SELECT 1 FROM task_events WHERE kind='blocked'").fetchall()


def test_database_failure_is_generic(claimed_board, monkeypatch):
    monkeypatch.setattr(kb, "connect", Mock(side_effect=RuntimeError("private-db-path-and-payload")))
    result = A._kanban_approval_unavailable()
    assert result["approved"] is False
    assert result["board_updated"] is False
    assert result["board_error"] == "board_update_failed"
    assert "private-db-path-and-payload" not in json.dumps(result)


@pytest.mark.parametrize("guard", [A.check_all_command_guards, A.check_execute_code_guard])
def test_non_kanban_fallback_unchanged(guard, isolated_no_notifier):
    result = guard("print('kanban-marker')", "local")
    assert result["status"] == "pending_approval"
    assert result["approval_pending"] is True
    assert "board_updated" not in result
    isolated_no_notifier.assert_called_once()


@pytest.mark.parametrize("pin", ["db", "board"])
def test_same_task_id_other_board_untouched(pin, claimed_board, monkeypatch):
    source, task = claimed_board
    monkeypatch.delenv("HERMES_KANBAN_DB")
    alpha = kb.kanban_db_path(board="alpha")
    beta = kb.kanban_db_path(board="beta")
    with kb.connect_closing(source) as src:
        for path in (alpha, beta):
            with kb.connect_closing(path) as dest:
                src.backup(dest)
    if pin == "db":
        monkeypatch.setenv("HERMES_KANBAN_DB", str(alpha))
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "beta")
    else:
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "alpha")
    current = Mock(return_value="beta")
    monkeypatch.setattr(kb, "get_current_board", current)
    result = A._kanban_approval_unavailable()
    assert result["board_updated"] is True
    # block_task's existing lifecycle hook reads current for hook metadata;
    # persisted task/event writes must still use only the pinned connection.
    for path, status in ((source, "running"), (alpha, "blocked"), (beta, "running")):
        with kb.connect_closing(path) as conn:
            assert kb.get_task(conn, task.id).status == status


@pytest.mark.parametrize("pin", ["absent-db", "relative-db", "malformed-board"])
def test_invalid_pin_denies_without_creating_database(pin, claimed_board, monkeypatch, tmp_path):
    if pin == "malformed-board":
        monkeypatch.delenv("HERMES_KANBAN_DB")
        monkeypatch.setenv("HERMES_KANBAN_BOARD", "../not-a-board")
    else:
        value = str(tmp_path / "absent.db") if pin == "absent-db" else "relative.db"
        monkeypatch.setenv("HERMES_KANBAN_DB", value)
    connect = Mock(side_effect=AssertionError("Must not open invalid board"))
    monkeypatch.setattr(kb, "connect_closing", connect)
    result = A._kanban_approval_unavailable()
    assert result["status"] == "approval_unavailable"
    assert result["board_updated"] is False
    assert result["board_error"] == "board_update_failed"
    connect.assert_not_called()
    assert not (tmp_path / "absent.db").exists()
