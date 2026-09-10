"""Approval-unavailable fixtures only: never grant or simulate human consent."""

import argparse
import importlib.util
import json
import os
import sys
from pathlib import Path

import pytest

from hermes_cli import kanban_db as kb


@pytest.fixture
def board(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    path = tmp_path / "board.db"
    monkeypatch.setenv("HERMES_KANBAN_DB", str(path))
    kb.init_db(path)
    with kb.connect_closing(path) as conn:
        yield conn


def test_opt_in_persists_and_defaults_false(board):
    plain = kb.create_task(board, title="plain")
    flagged = kb.create_task(board, title="known approval", requires_interactive_approval=True)
    assert kb.get_task(board, plain).requires_interactive_approval is False
    assert kb.get_task(board, flagged).requires_interactive_approval is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}])
def test_opt_in_requires_strict_bool(board, value):
    with pytest.raises(ValueError, match="requires_interactive_approval must be a boolean"):
        kb.create_task(board, title="invalid", requires_interactive_approval=value)
    assert kb.list_tasks(board) == []


def test_existing_rows_migrate_default_false(board):
    tid = kb.create_task(board, title="legacy")
    board.execute("ALTER TABLE tasks DROP COLUMN requires_interactive_approval")
    kb._migrate_add_optional_columns(board)
    kb._migrate_add_optional_columns(board)
    assert kb.get_task(board, tid).requires_interactive_approval is False


@pytest.mark.parametrize("subscribed", [False, True])
@pytest.mark.parametrize("source_status", ["ready", "review"])
def test_default_dispatch_blocks_before_workspace_and_spawn(
    board, monkeypatch, tmp_path, subscribed, source_status,
):
    from hermes_cli import profiles
    monkeypatch.setattr(profiles, "profile_exists", lambda name: True)
    tid = kb.create_task(board, title="print marker", assignee="worker",
                         requires_interactive_approval=True)
    board.execute("UPDATE tasks SET status = ? WHERE id = ?", (source_status, tid))
    if subscribed:
        kb.add_notify_sub(board, task_id=tid, platform="telegram", chat_id="test-chat")
    marker = tmp_path / "must-not-execute"
    def spawn(*args, **kwargs):
        marker.write_text("executed")
    monkeypatch.setattr(kb, "_default_spawn", spawn)
    monkeypatch.setattr(kb, "resolve_workspace", lambda *a, **k: pytest.fail("workspace created"))
    monkeypatch.setattr(kb, "_resolve_worktree_workspace", lambda *a, **k: pytest.fail("worktree created"))
    result = kb.dispatch_once(board)
    task = kb.get_task(board, tid)
    assert task.status == "blocked"
    assert task.block_kind == "capability"
    assert task.claim_lock is None
    assert task.current_run_id is None
    assert result.auto_blocked == [tid]
    assert not result.spawned
    assert not marker.exists()
    event = board.execute("SELECT * FROM task_events WHERE task_id = ? AND kind = 'blocked'", (tid,)).fetchone()
    assert event is not None
    expected = "subscription_present_delivery_unverified" if subscribed else "no_subscription"
    assert expected in event["payload"]
    assert kb.dispatch_once(board).auto_blocked == []


@pytest.mark.parametrize("invalid", [None, 0, -1, True, "1", 1.0])
def test_block_helper_rejects_invalid_run_ids(board, invalid):
    tid = kb.create_task(board, title="task", assignee="worker")
    task = kb.claim_task(board, tid)
    result = kb.block_approval_unavailable(board, tid, invalid)
    assert result["board_updated"] is False
    assert result["error"] == "stale_or_missing_claim"
    assert kb.get_task(board, tid).current_run_id == task.current_run_id


@pytest.mark.parametrize("damage", ["wrong_run", "expired", "cleared", "ended", "run_expired", "run_lock", "review"])
def test_block_helper_requires_current_live_claim(board, damage):
    tid = kb.create_task(board, title="task", assignee="worker")
    task = kb.claim_task(board, tid)
    run_id = task.current_run_id
    if damage == "wrong_run":
        run_id += 1
    elif damage == "expired":
        board.execute("UPDATE tasks SET claim_expires = 1 WHERE id = ?", (tid,))
    elif damage == "cleared":
        board.execute("UPDATE tasks SET claim_lock = NULL WHERE id = ?", (tid,))
    elif damage == "ended":
        board.execute("UPDATE task_runs SET status = 'blocked', ended_at = 1 WHERE id = ?", (run_id,))
    elif damage == "run_expired":
        board.execute("UPDATE task_runs SET claim_expires = 1 WHERE id = ?", (run_id,))
    elif damage == "run_lock":
        board.execute("UPDATE task_runs SET claim_lock = 'other' WHERE id = ?", (run_id,))
    elif damage == "review":
        board.execute("UPDATE tasks SET status = 'review' WHERE id = ?", (tid,))
    before = list(board.execute("SELECT * FROM task_events"))
    result = kb.block_approval_unavailable(board, tid, run_id)
    assert result["board_updated"] is False
    assert result["error"] == "stale_or_missing_claim"
    assert list(board.execute("SELECT * FROM task_events")) == before


def test_cli_create_forwards_and_serializes(board, capsys):
    from hermes_cli import kanban
    parser = argparse.ArgumentParser()
    kanban.build_parser(parser.add_subparsers())
    args = parser.parse_args(["kanban", "create", "approval", "--json",
                              "--requires-interactive-approval"])
    assert kanban._cmd_create(args) == 0
    task = json.loads(capsys.readouterr().out)
    assert task["requires_interactive_approval"] is True
    assert kb.get_task(board, task["id"]).requires_interactive_approval is True


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}, True, False])
def test_tool_create_strict_forwarding_and_show(board, value):
    from tools import kanban_tools
    result = json.loads(kanban_tools._handle_create({
        "title": "approval", "assignee": "worker", "requires_interactive_approval": value,
    }))
    if type(value) is not bool:
        assert "requires_interactive_approval must be a boolean" in result["error"]
        assert kb.list_tasks(board) == []
    else:
        task = kb.get_task(board, result["task_id"])
        assert task.requires_interactive_approval is value
        shown = json.loads(kanban_tools._handle_show({"task_id": task.id}))
        assert shown["task"]["requires_interactive_approval"] is value


@pytest.fixture
def api_client(board, monkeypatch):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from hermes_cli import kanban
    monkeypatch.setattr(kanban, "_check_dispatcher_presence", lambda: (True, ""))
    path = Path(__file__).resolve().parents[2] / "plugins/kanban/dashboard/plugin_api.py"
    spec = importlib.util.spec_from_file_location("kanban_approval_api_test", path)
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, spec.name, module)
    spec.loader.exec_module(module)
    app = FastAPI()
    app.include_router(module.router)
    with TestClient(app) as client:
        yield client


@pytest.mark.parametrize("value", [None, 0, 1, "true", "false", [], {}, True, False])
def test_api_create_strict_forwarding(api_client, board, value):
    response = api_client.post("/tasks", json={
        "title": "approval", "requires_interactive_approval": value,
    })
    if type(value) is not bool:
        assert response.status_code == 422
        assert kb.list_tasks(board) == []
    else:
        assert response.status_code == 200, response.text
        task = response.json()["task"]
        assert task["requires_interactive_approval"] is value
        assert kb.get_task(board, task["id"]).requires_interactive_approval is value
