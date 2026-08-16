"""Tests for kanban goal_mode — per-card Ralph-style goal loop.

Covers three layers:

1. DB: goal_mode / goal_max_turns persist through create_task + from_row,
   and a legacy DB (without the columns) migrates cleanly.
2. Spawn: _default_spawn sets the HERMES_KANBAN_GOAL_MODE env vars only
   when the card opts in.
3. Loop: goals.run_kanban_goal_loop continuation / completion / budget
   behaviour, driven entirely through injected callbacks (no live model).
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

import cli as cli_module
from hermes_cli import kanban_db as kb
from hermes_cli import goals


@pytest.fixture
def kanban_home(tmp_path, monkeypatch):
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    kb.init_db()
    return home


def _claim_for_goal_loop(conn, task_id, monkeypatch):
    task = kb.claim_task(conn, task_id)
    assert task is not None and task.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", task_id)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(task.current_run_id))
    return task


# ---------------------------------------------------------------------------
# DB layer
# ---------------------------------------------------------------------------

def test_goal_mode_defaults_off(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain task", assignee="worker")
        task = kb.get_task(conn, tid)
    assert task.goal_mode is False
    assert task.goal_max_turns is None


def test_goal_mode_persists(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="open-ended task",
            assignee="worker",
            goal_mode=True,
            goal_max_turns=7,
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns == 7


def test_goal_mode_without_max_turns(kanban_home):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn, title="t", assignee="worker", goal_mode=True
        )
        task = kb.get_task(conn, tid)
    assert task.goal_mode is True
    assert task.goal_max_turns is None


def test_legacy_db_migrates_goal_columns(tmp_path, monkeypatch):
    """A tasks table created without goal columns must gain them on init."""
    home = tmp_path / ".hermes"
    home.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(home))
    monkeypatch.setattr(Path, "home", lambda: tmp_path)

    db_path = kb.kanban_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    # Minimal legacy schema: tasks table missing goal_mode / goal_max_turns.
    legacy = sqlite3.connect(db_path)
    legacy.execute(
        """
        CREATE TABLE tasks (
            id TEXT PRIMARY KEY,
            title TEXT NOT NULL,
            body TEXT,
            assignee TEXT,
            status TEXT NOT NULL DEFAULT 'ready',
            priority INTEGER NOT NULL DEFAULT 0,
            created_by TEXT,
            created_at INTEGER NOT NULL,
            started_at INTEGER,
            completed_at INTEGER,
            workspace_kind TEXT NOT NULL DEFAULT 'scratch',
            workspace_path TEXT,
            claim_lock TEXT,
            claim_expires INTEGER
        )
        """
    )
    legacy.execute(
        "INSERT INTO tasks (id, title, status, priority, created_at, workspace_kind) "
        "VALUES ('legacy1', 'old', 'ready', 0, 1, 'scratch')"
    )
    legacy.commit()
    legacy.close()

    # init_db runs the additive migration.
    kb.init_db()
    with kb.connect() as conn:
        cols = {r["name"] for r in conn.execute("PRAGMA table_info(tasks)")}
        assert "goal_mode" in cols
        assert "goal_max_turns" in cols
        task = kb.get_task(conn, "legacy1")
    # Existing row keeps the safe default.
    assert task.goal_mode is False
    assert task.goal_max_turns is None


# ---------------------------------------------------------------------------
# Spawn env
# ---------------------------------------------------------------------------

def test_spawn_sets_goal_env_only_when_enabled(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4242

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="goal task",
            assignee="default",
            goal_mode=True,
            goal_max_turns=5,
        )
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert env.get("HERMES_KANBAN_GOAL_MODE") == "1"
    assert env.get("HERMES_KANBAN_GOAL_MAX_TURNS") == "5"
    assert captured["cmd"].index("-Q") > captured["cmd"].index("chat")


def test_spawn_no_goal_env_for_plain_task(kanban_home, monkeypatch):
    captured = {}

    class _FakeProc:
        pid = 4243

    def _fake_popen(cmd, **kwargs):
        captured["cmd"] = cmd
        captured["env"] = kwargs.get("env", {})
        return _FakeProc()

    monkeypatch.setattr("subprocess.Popen", _fake_popen)

    with kb.connect() as conn:
        tid = kb.create_task(conn, title="plain", assignee="default")
        task = kb.get_task(conn, tid)

    kb._default_spawn(task, str(kanban_home))
    env = captured["env"]
    assert "HERMES_KANBAN_GOAL_MODE" not in env
    assert "HERMES_KANBAN_GOAL_MAX_TURNS" not in env
    assert "-Q" not in captured["cmd"]


def test_single_query_exit_code_preserves_kanban_provider_failures(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)
    assert cli_module._single_query_exit_code(
        {"failed": True, "failure_reason": "billing"}
    ) == 1

    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_test")
    assert cli_module._single_query_exit_code({"failed": False}) == 0
    assert cli_module._single_query_exit_code({"failed": True}) == 1
    assert cli_module._single_query_exit_code(
        {"failed": True, "failure_reason": "rate_limit"}
    ) == kb.KANBAN_RATE_LIMIT_EXIT_CODE
    assert cli_module._single_query_exit_code(
        {"failed": True, "failure_reason": "billing"}
    ) == kb.KANBAN_RATE_LIMIT_EXIT_CODE


@pytest.mark.parametrize(
    ("failure_reason", "expected_exit"),
    [
        ("billing", kb.KANBAN_RATE_LIMIT_EXIT_CODE),
        ("rate_limit", kb.KANBAN_RATE_LIMIT_EXIT_CODE),
        ("provider_error", 1),
    ],
)
def test_goal_loop_provider_failure_exits_for_dispatcher_requeue(
    kanban_home, monkeypatch, failure_reason, expected_exit
):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="provider failure",
            assignee="default",
            goal_mode=True,
        )
        _claim_for_goal_loop(conn, tid, monkeypatch)

    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: ("continue", "not done", False, None),
    )

    class _Agent:
        session_id = "session"

        def run_conversation(self, **_kwargs):
            return {"failed": True, "failure_reason": failure_reason}

    class _CLI:
        agent = _Agent()
        conversation_history = []
        session_id = "session"

    with pytest.raises(SystemExit) as exc_info:
        cli_module._run_kanban_goal_loop_q(
            _CLI(), "first response"  # type: ignore[arg-type]
        )
    assert exc_info.value.code == expected_exit

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"


@pytest.mark.parametrize("failure_site", ["judge", "continuation"])
def test_goal_loop_failures_block_open_task(kanban_home, monkeypatch, failure_site):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="goal loop failure",
            assignee="default",
            goal_mode=True,
        )
        _claim_for_goal_loop(conn, tid, monkeypatch)

    def _judge(*_args, **_kwargs):
        if failure_site == "judge":
            raise RuntimeError("judge failed")
        return "continue", "not done", False, None

    monkeypatch.setattr(goals, "judge_goal", _judge)

    class _Agent:
        session_id = "session"

        def run_conversation(self, **_kwargs):
            raise RuntimeError("continuation failed")

    class _CLI:
        agent = _Agent()
        conversation_history = []
        session_id = "session"

    cli_module._run_kanban_goal_loop_q(
        _CLI(), "first response"  # type: ignore[arg-type]
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "blocked"


def test_goal_loop_blocks_requeued_task_before_clean_exit(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="reclaimed goal task",
            assignee="default",
            goal_mode=True,
        )
        claimed = kb.claim_task(conn, tid)

    assert claimed is not None and claimed.current_run_id is not None
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(claimed.current_run_id))

    def _reclaim_then_return(**_kwargs):
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', worker_pid = NULL WHERE id = ?",
                (tid,),
            )
        return {"reason": "task status changed"}

    monkeypatch.setattr(goals, "run_kanban_goal_loop", _reclaim_then_return)

    class _CLI:
        agent = None
        conversation_history = []
        session_id = "session"

    cli_module._run_kanban_goal_loop_q(
        _CLI(), "first response"  # type: ignore[arg-type]
    )

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "blocked"


def test_goal_loop_stale_worker_cannot_block_newer_run(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="stale goal worker",
            assignee="default",
            goal_mode=True,
        )
        stale_task = kb.claim_task(conn, tid, claimer="stale-worker")

    assert stale_task is not None and stale_task.current_run_id is not None
    stale_run_id = stale_task.current_run_id
    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(stale_run_id))
    newer_run_id = None

    def _reclaim_then_claim(**_kwargs):
        nonlocal newer_run_id
        with kb.connect() as conn, kb.write_txn(conn):
            conn.execute(
                "UPDATE tasks SET status = 'ready', claim_lock = NULL, "
                "claim_expires = NULL, worker_pid = NULL WHERE id = ?",
                (tid,),
            )
        with kb.connect() as conn:
            newer_task = kb.claim_task(conn, tid, claimer="new-worker")
        assert newer_task is not None and newer_task.current_run_id is not None
        newer_run_id = newer_task.current_run_id
        return {"reason": "stale worker returned after reclaim"}

    monkeypatch.setattr(goals, "run_kanban_goal_loop", _reclaim_then_claim)

    class _CLI:
        agent = None
        conversation_history = []
        session_id = "session"

    with pytest.raises(RuntimeError, match="run ownership changed"):
        cli_module._run_kanban_goal_loop_q(
            _CLI(), "stale worker response"  # type: ignore[arg-type]
        )

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
        newer_run = kb.get_run(conn, newer_run_id)
    assert task is not None
    assert task.status == "running"
    assert task.current_run_id == newer_run_id
    assert newer_run is not None
    assert newer_run.status == "running"
    assert newer_run.outcome is None


@pytest.mark.parametrize("run_id", [None, "", "invalid", "0", "-1"])
def test_goal_loop_requires_valid_run_id(kanban_home, monkeypatch, run_id):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="run ownership required",
            assignee="default",
            goal_mode=True,
        )
        kb.claim_task(conn, tid)

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    if run_id is None:
        monkeypatch.delenv("HERMES_KANBAN_RUN_ID", raising=False)
    else:
        monkeypatch.setenv("HERMES_KANBAN_RUN_ID", run_id)

    with pytest.raises(RuntimeError, match="HERMES_KANBAN_RUN_ID"):
        cli_module._run_kanban_goal_loop_q(None, "first response")  # type: ignore[arg-type]

    with kb.connect() as conn:
        task = kb.get_task(conn, tid)
    assert task is not None
    assert task.status == "running"


def test_goal_loop_requires_task_id(monkeypatch):
    monkeypatch.delenv("HERMES_KANBAN_TASK", raising=False)

    with pytest.raises(RuntimeError, match="missing HERMES_KANBAN_TASK"):
        cli_module._run_kanban_goal_loop_q(None, "first response")  # type: ignore[arg-type]


def test_goal_loop_rejects_missing_task(kanban_home, monkeypatch):
    monkeypatch.setenv("HERMES_KANBAN_TASK", "t_missing_goal_task")
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", "1")

    with pytest.raises(RuntimeError, match="was not found"):
        cli_module._run_kanban_goal_loop_q(None, "first response")  # type: ignore[arg-type]


def test_goal_loop_rejects_missing_final_task(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="disappearing goal task",
            assignee="default",
            goal_mode=True,
        )
        task = _claim_for_goal_loop(conn, tid, monkeypatch)
    assert task is not None

    monkeypatch.setattr(
        goals,
        "run_kanban_goal_loop",
        lambda **_kwargs: {"reason": "loop returned"},
    )
    original_get_task = kb.get_task
    calls = 0

    def _get_task(conn, task_id):
        nonlocal calls
        calls += 1
        return original_get_task(conn, task_id) if calls == 1 else None

    monkeypatch.setattr(kb, "get_task", _get_task)

    with pytest.raises(RuntimeError, match="disappeared"):
        cli_module._run_kanban_goal_loop_q(None, "first response")  # type: ignore[arg-type]


def test_goal_loop_rejects_failed_block_transition(kanban_home, monkeypatch):
    with kb.connect() as conn:
        tid = kb.create_task(
            conn,
            title="unblockable goal task",
            assignee="default",
            goal_mode=True,
        )
        _claim_for_goal_loop(conn, tid, monkeypatch)

    monkeypatch.setattr(
        goals,
        "run_kanban_goal_loop",
        lambda **_kwargs: {"reason": "loop returned"},
    )
    monkeypatch.setattr(kb, "block_task", lambda *_args, **_kwargs: False)

    with pytest.raises(RuntimeError, match="could not block"):
        cli_module._run_kanban_goal_loop_q(None, "first response")  # type: ignore[arg-type]


@pytest.mark.parametrize("task_exists", [True, False])
def test_quiet_goal_mode_main_signals_loop_failure(
    kanban_home, monkeypatch, task_exists
):
    if task_exists:
        with kb.connect() as conn:
            tid = kb.create_task(
                conn,
                title="quiet goal loop failure",
                assignee="default",
                goal_mode=True,
            )
            task = _claim_for_goal_loop(conn, tid, monkeypatch)
        run_id = task.current_run_id
    else:
        tid = "t_missing_goal_task"
        run_id = 1

    monkeypatch.setenv("HERMES_KANBAN_TASK", tid)
    monkeypatch.setenv("HERMES_KANBAN_RUN_ID", str(run_id))
    monkeypatch.setenv("HERMES_KANBAN_GOAL_MODE", "1")
    monkeypatch.setattr(
        goals,
        "judge_goal",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("judge failed")),
    )

    class _CLI:
        provider = "test-provider"
        model = "test-model"
        session_id = "session"
        conversation_history = []
        _active_agent_route_signature = "same-route"
        agent = type(
            "Agent",
            (),
            {
                "session_id": "session",
                "quiet_mode": False,
                "suppress_status_output": False,
                "stream_delta_callback": None,
                "tool_gen_callback": None,
                "run_conversation": lambda self, **_kwargs: {"final_response": "started"},
            },
        )()

        def __init__(self, **_kwargs):
            pass

        def _claim_active_session(self, *_args, **_kwargs):
            return True

        def _ensure_runtime_credentials(self):
            return True

        def _resolve_turn_agent_config(self, _query):
            return {
                "signature": "same-route",
                "model": None,
                "runtime": None,
                "request_overrides": None,
            }

        def _init_agent(self, **_kwargs):
            return True

    monkeypatch.setattr(cli_module, "HermesCLI", _CLI)
    monkeypatch.setattr(cli_module.atexit, "register", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(cli_module, "_finalize_single_query", lambda _cli: None)

    with pytest.raises(SystemExit) as exc_info:
        cli_module.main(query="work task", quiet=True, toolsets="kanban")
    assert exc_info.value.code == (0 if task_exists else 1)

    if task_exists:
        with kb.connect() as conn:
            task = kb.get_task(conn, tid)
        assert task is not None
        assert task.status == "blocked"


# ---------------------------------------------------------------------------
# Goal loop logic (callback-injected, no live model)
# ---------------------------------------------------------------------------

def _patch_judge(monkeypatch, verdicts):
    """Make judge_goal return a scripted sequence of verdicts."""
    seq = list(verdicts)

    def _fake_judge(goal, response, subgoals=None, background_processes=None, **_kw):
        v = seq.pop(0) if seq else "done"
        # 4-tuple contract: (verdict, reason, parse_failed, wait_directive)
        return v, f"scripted:{v}", False, None

    monkeypatch.setattr(goals, "judge_goal", _fake_judge)


def test_loop_stops_when_worker_already_completed(monkeypatch):
    # Worker called kanban_complete on its first turn — no judging needed.
    _patch_judge(monkeypatch, ["continue"])  # should never be consulted
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t1",
        goal_text="do the thing",
        run_turn=lambda p: turns.append(p) or "x",
        task_status_fn=lambda: "done",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="done already",
    )
    assert res["outcome"] == "completed_by_worker"
    assert turns == []  # no extra turns


def test_loop_continues_then_worker_completes(monkeypatch):
    _patch_judge(monkeypatch, ["continue", "continue"])
    statuses = iter(["running", "running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t2",
        goal_text="ship feature",
        run_turn=lambda p: turns.append(p) or f"turn{len(turns)}",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="started",
    )
    assert res["outcome"] == "completed_by_worker"
    # Two continuation turns fed before the worker completed.
    assert len(turns) == 2
    assert all("not done yet" in p for p in turns)


def test_loop_blocks_on_budget_exhaustion(monkeypatch):
    _patch_judge(monkeypatch, ["continue"] * 10)
    blocked = {}

    def _block(reason):
        blocked["reason"] = reason

    res = goals.run_kanban_goal_loop(
        task_id="t3",
        goal_text="endless task",
        run_turn=lambda p: "still going",
        task_status_fn=lambda: "running",
        block_fn=_block,
        max_turns=3,
        first_response="turn1",
    )
    assert res["outcome"] == "blocked_budget"
    assert res["turns_used"] == 3
    assert "turn budget" in blocked["reason"].lower()


def test_loop_finalize_nudge_when_judge_done_but_open(monkeypatch):
    # Judge says done, but worker never terminated → one finalize nudge,
    # then worker completes.
    _patch_judge(monkeypatch, ["done", "done"])
    statuses = iter(["running", "done"])
    turns = []

    res = goals.run_kanban_goal_loop(
        task_id="t4",
        goal_text="task",
        run_turn=lambda p: turns.append(p) or "ok",
        task_status_fn=lambda: next(statuses),
        block_fn=lambda r: pytest.fail("should not block"),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "completed_by_worker"
    assert len(turns) == 1
    assert "still open" in turns[0]


def test_loop_blocks_when_judge_done_but_never_finalizes(monkeypatch):
    # Judge keeps saying done, worker never calls kanban_complete → block
    # after the single finalize nudge.
    _patch_judge(monkeypatch, ["done", "done"])
    blocked = {}

    res = goals.run_kanban_goal_loop(
        task_id="t5",
        goal_text="task",
        run_turn=lambda p: "still not finalizing",
        task_status_fn=lambda: "running",
        block_fn=lambda r: blocked.update(reason=r),
        max_turns=10,
        first_response="looks done",
    )
    assert res["outcome"] == "blocked_budget"
    assert "finalize" in blocked["reason"].lower()


def test_loop_stops_if_task_reclaimed(monkeypatch):
    _patch_judge(monkeypatch, ["continue"])
    res = goals.run_kanban_goal_loop(
        task_id="t6",
        goal_text="task",
        run_turn=lambda p: pytest.fail("should not run a turn"),
        task_status_fn=lambda: "archived",
        block_fn=lambda r: pytest.fail("should not block"),
        first_response="x",
    )
    assert res["outcome"] == "stopped"
