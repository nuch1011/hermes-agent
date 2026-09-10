"""Independent t_53e1ae78 probes. Real temporary SQLite + CLI entry, no model/API."""

import json
from dataclasses import asdict
from pathlib import Path
from types import SimpleNamespace

import pytest
import cli
from hermes_cli import goals, kanban_db as kb
from tools import kanban_tools as kt


@pytest.fixture
def board(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setenv("HERMES_KANBAN_DB", str(home / "qa.db"))
    monkeypatch.delenv("HERMES_KANBAN_BOARD", raising=False)
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("HERMES_KANBAN_CRASH_GRACE_SECONDS", "0")
    monkeypatch.setattr(kb, "_pid_alive", lambda _pid: False)
    kb.init_db()
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="QA synthetic task", assignee="default", goal_mode=True
        )
        run1 = kb.claim_task(conn, tid)
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run1.current_run_id))
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    monkeypatch.setattr(kt, "_goal_judge_available", lambda: False)
    return tid, run1.current_run_id


def reclaim(tid):
    with kb.connect() as conn:
        kb._set_worker_pid(conn, tid, 98765)
        assert kb.detect_crashed_workers(conn) == [tid]
        run2 = kb.claim_task(conn, tid)
        assert run2 is not None
        return run2.current_run_id


def snapshot(tid, rid):
    with kb.connect() as conn:
        return asdict(kb.get_task(conn, tid)), asdict(kb.get_run(conn, rid))


def run_main(monkeypatch, mutation, result=None):
    turns, finalized, tool_results = [], [], []

    class Agent:
        session_id = "qa-session"

        def run_conversation(self, **kwargs):
            turns.append("turn")
            if mutation == "complete":
                tool_results.append(
                    kt._handle_complete({"summary": "QA synthetic complete"})
                )
            elif mutation == "block":
                tool_results.append(
                    kt._handle_block({
                        "reason": "QA synthetic block",
                        "kind": "needs_input",
                    })
                )
            return result if result is not None else {"final_response": "QA response"}

    class CLI:
        provider = "test-provider"
        model = "test-model"
        session_id = "qa-session"
        conversation_history = []
        _active_agent_route_signature = "same-route"
        agent = Agent()

        def __init__(self, **kwargs):
            pass

        def _claim_active_session(self, *args, **kwargs):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, query):
            return {
                "signature": "same-route",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **kwargs):
            return True

    monkeypatch.setattr(cli, "HermesCLI", CLI)
    monkeypatch.setattr(cli.atexit, "register", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        cli, "_finalize_single_query", lambda obj: finalized.append(obj.session_id)
    )
    with pytest.raises(SystemExit) as exc:
        cli.main(query="QA synthetic task", quiet=True, toolsets="kanban")
    return exc.value.code, turns, finalized, tool_results


@pytest.mark.parametrize("mutation", ["complete", "block"])
@pytest.mark.parametrize("run_env", ["missing", "invalid", "stale"])
def test_first_turn_requires_ownership(board, monkeypatch, mutation, run_env):
    tid, run1 = board
    run2 = reclaim(tid)
    assert run1 != run2
    before = snapshot(tid, run2)
    if run_env == "missing":
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID")
    elif run_env == "invalid":
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "invalid")
    code, turns, finalized, outputs = run_main(monkeypatch, mutation)
    after = snapshot(tid, run2)
    print(
        json.dumps({
            "case": [run_env, mutation],
            "exit": code,
            "turns": len(turns),
            "task_status": after[0]["status"],
            "run_status": after[1]["status"],
            "unchanged": before == after,
            "finalized": finalized,
        })
    )
    assert finalized == ["qa-session"]
    assert code == 1
    assert after == before, "unauthorized first-turn mutation of Run 2"
    assert turns == [], "stale worker reached model turn before ownership check"


@pytest.mark.parametrize(
    "mutation,expected", [("complete", "done"), ("block", "blocked")]
)
def test_current_run_first_turn_exit_zero(board, monkeypatch, mutation, expected):
    tid, rid = board
    code, turns, finalized, outputs = run_main(monkeypatch, mutation)
    task, run = snapshot(tid, rid)
    print(
        json.dumps({
            "case": ["current", mutation],
            "exit": code,
            "turns": len(turns),
            "task_status": task["status"],
            "run_status": run["status"],
            "finalized": finalized,
        })
    )
    assert task["status"] == expected and run["status"] == expected
    assert finalized == ["qa-session"]
    assert code == 0, "valid same-run terminal action must exit successfully"


@pytest.mark.parametrize(
    "reason,expected", [("billing", 75), ("rate_limit", 75), ("provider_error", 1)]
)
def test_provider_exit_preserves_run_and_finalizes(
    board, monkeypatch, reason, expected
):
    tid, rid = board
    before = snapshot(tid, rid)
    code, turns, finalized, _ = run_main(
        monkeypatch, None, {"failed": True, "failure_reason": reason}
    )
    assert code == expected
    assert turns == ["turn"]
    assert finalized == ["qa-session"]
    assert snapshot(tid, rid) == before


@pytest.mark.parametrize("kind", [None, "needs_input", "dependency", "transient"])
def test_stale_cas_preserves_entire_new_run(board, kind):
    tid, run1 = board
    run2 = reclaim(tid)
    before = snapshot(tid, run2)
    with kb.connect() as conn:
        assert not kb.block_task(
            conn, tid, reason="stale QA block", kind=kind, expected_run_id=run1
        )
        assert not kb.complete_task(
            conn, tid, summary="stale QA complete", expected_run_id=run1
        )
    assert snapshot(tid, run2) == before


def test_finalizer_cas_race_after_successful_status_read(board, monkeypatch):
    tid, run1 = board
    original_block = kb.block_task
    observed = {}

    def racing_block(conn, task_id, **kwargs):
        observed["expected_run_id"] = kwargs.get("expected_run_id")
        run2 = reclaim(tid)
        before = snapshot(tid, run2)
        outcome = original_block(conn, task_id, **kwargs)
        observed["unchanged"] = snapshot(tid, run2) == before
        observed["cas_result"] = outcome
        return outcome

    monkeypatch.setattr(kb, "block_task", racing_block)
    monkeypatch.setattr(
        goals, "run_kanban_goal_loop", lambda **kw: {"reason": "QA normal return"}
    )
    with pytest.raises(RuntimeError, match="could not block"):
        cli._run_kanban_goal_loop_q(SimpleNamespace(agent=None), "response")
    assert observed == {"expected_run_id": run1, "unchanged": True, "cas_result": False}


@pytest.mark.parametrize(
    "kind,expected",
    [("dependency", "todo"), ("needs_input", "blocked"), ("transient", "blocked")],
)
def test_own_routed_block_is_success(board, monkeypatch, kind, expected):
    tid, rid = board
    with kb.connect() as conn:
        assert kb.block_task(conn, tid, kind=kind, expected_run_id=rid)
    before = snapshot(tid, rid)
    assert before[0]["status"] == expected
    cli._run_kanban_goal_loop_q(None, "finished")
    assert snapshot(tid, rid) == before
    # A terminal run may be acknowledged, but may not start another model turn.
    with pytest.raises(RuntimeError, match="no longer current"):
        cli._require_kanban_goal_run_q()


@pytest.mark.parametrize("replacement_status", ["running", "done", "blocked"])
def test_replaced_run_is_rejected_even_when_new_run_finished(
    board, monkeypatch, replacement_status
):
    tid, run1 = board

    def replace_during_loop(**kwargs):
        run2 = reclaim(tid)
        with kb.connect() as conn:
            if replacement_status == "done":
                assert kb.complete_task(
                    conn, tid, summary="new run", expected_run_id=run2
                )
            elif replacement_status == "blocked":
                assert kb.block_task(conn, tid, reason="new run", expected_run_id=run2)
        before = snapshot(tid, run2)
        with pytest.raises(RuntimeError, match="no longer current"):
            kwargs["task_status_fn"]()
        assert snapshot(tid, run2) == before
        return {"reason": "replacement run"}

    monkeypatch.setattr(goals, "run_kanban_goal_loop", replace_during_loop)
    with pytest.raises(RuntimeError, match="no longer current"):
        cli._run_kanban_goal_loop_q(None, "old response")


@pytest.mark.parametrize("mutation", ["complete", "block"])
def test_current_run_continuation_exit_zero(board, monkeypatch, mutation):
    monkeypatch.setattr(
        goals, "judge_goal", lambda *args: ("continue", "keep working", False, False)
    )
    original = cli._run_kanban_goal_loop_q

    def continue_then_finish(obj, response):
        obj.agent.run_conversation = lambda **kwargs: (
            (
                kt._handle_complete({"summary": "continuation finished"})
                if mutation == "complete"
                else kt._handle_block({
                    "reason": "continuation blocked",
                    "kind": "needs_input",
                })
            )
            and {"final_response": "finished"}
        )
        return original(obj, response)

    monkeypatch.setattr(cli, "_run_kanban_goal_loop_q", continue_then_finish)
    code, _, finalized, _ = run_main(monkeypatch, None)
    assert code == 0
    assert finalized == ["qa-session"]
    assert snapshot(*board)[0]["status"] == (
        "done" if mutation == "complete" else "blocked"
    )


def test_reclaim_during_judge_never_starts_another_turn(board, monkeypatch):
    turns = []

    def judge(*args):
        reclaim(board[0])
        return "continue", "keep working", False, False

    monkeypatch.setattr(goals, "judge_goal", judge)
    obj = SimpleNamespace(
        agent=SimpleNamespace(run_conversation=lambda **kw: turns.append(kw))
    )
    with pytest.raises(RuntimeError, match="no longer current"):
        cli._run_kanban_goal_loop_q(obj, "first response")
    assert turns == []


def test_terminal_ownership_read_uses_one_snapshot(board, monkeypatch):
    tid, rid = board
    original = kb.get_task
    observed = []

    def read_then_reclaim(conn, task_id):
        task = original(conn, task_id)
        observed.append(conn.in_transaction)
        reclaim(tid)
        return task

    monkeypatch.setattr(kb, "get_task", read_then_reclaim)

    # Avoid recursion in reclaim's own DB helpers; trigger on the reader only.
    def read_once(conn, task_id):
        monkeypatch.setattr(kb, "get_task", original)
        return read_then_reclaim(conn, task_id)

    monkeypatch.setattr(kb, "get_task", read_once)
    assert cli._kanban_goal_task_q(tid, rid).current_run_id == rid
    assert observed == [True]
    with pytest.raises(RuntimeError, match="no longer current"):
        cli._kanban_goal_task_q(tid, rid)
