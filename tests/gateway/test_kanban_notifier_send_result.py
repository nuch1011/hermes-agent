"""Real watcher/SQLite regressions; adapter doubles model transport, never approval."""

import asyncio
from contextlib import closing
import logging
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from gateway.config import Platform
from gateway.platforms.base import SendResult
from gateway.run import GatewayRunner
from hermes_cli import kanban_db as kb


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    for key in tuple(os.environ):
        if key.startswith("HERMES_KANBAN_"):
            monkeypatch.delenv(key)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.setenv("HERMES_HOME", str(tmp_path / ".hermes"))


def subscription(board="default", *, subscribe=True, completed=False):
    kb.create_board(board)
    with closing(kb.connect(board=board)) as conn:
        tid = kb.create_task(conn, title="transport test", assignee="worker")
        if subscribe:
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat", thread_id="thread")
        if completed:
            kb.complete_task(conn, tid, summary="finished")
        else:
            kb.block_task(conn, tid, reason="needs input", kind="needs_input")
    return tid


def state(tid, board="default"):
    with closing(kb.connect(board=board)) as conn:
        subs = kb.list_notify_subs(conn, tid)
        _, events = kb.unseen_events_for_sub(
            conn, task_id=tid, platform="telegram", chat_id="chat", thread_id="thread",
            kinds=["blocked", "crashed", "completed"],
        )
        return subs, events


def runner_for(result=None, *, side_effect=None):
    runner = GatewayRunner.__new__(GatewayRunner)
    runner._kanban_notifier_profile = "default"
    adapter = SimpleNamespace(send=AsyncMock(return_value=result, side_effect=side_effect))
    runner.adapters = {Platform.TELEGRAM: adapter}
    runner._deliver_kanban_artifacts = AsyncMock()
    return runner, adapter


def tick(monkeypatch, runner):
    async def stop_after_tick(delay):
        if delay != 5:  # initial adapter-wiring delay
            runner._running = False

    runner._running = True
    with monkeypatch.context() as m:
        m.setattr(asyncio, "sleep", stop_after_tick)
        asyncio.run(runner._kanban_notifier_watcher(interval=1))


@pytest.mark.parametrize("result", [
    SendResult(success=False, error="private transport detail"),
    None,
    {"success": True},
    SimpleNamespace(success=True),
    SendResult(success=1),
    SendResult(success="true"),
], ids=["negative", "none", "dict", "duck-type", "truthy-int", "truthy-string"])
def test_invalid_send_result_rewinds_without_false_delivery(result, monkeypatch, caplog):
    tid = subscription(completed=True)
    runner, adapter = runner_for(result)
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    tick(monkeypatch, runner)

    assert adapter.send.await_count == 1
    subs, events = state(tid)
    assert len(subs) == 1 and subs[0]["last_event_id"] == 0
    assert [ev.kind for ev in events] == ["completed"]
    runner._deliver_kanban_artifacts.assert_not_awaited()
    assert "delivered completed" not in caplog.text
    assert "send_failed" in caplog.text
    assert "private transport detail" not in caplog.text


@pytest.mark.parametrize("failure", [SendResult(success=False), None, RuntimeError("private detail")])
def test_failure_exhausts_after_three_attempts(failure, monkeypatch, caplog):
    tid = subscription()
    runner, adapter = runner_for(side_effect=[failure] * 3)
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    for attempt in range(1, 4):
        tick(monkeypatch, runner)
        assert adapter.send.await_count == attempt
        subs, events = state(tid)
        if attempt < 3:
            assert len(subs) == 1 and subs[0]["last_event_id"] == 0
            assert [ev.kind for ev in events] == ["blocked"]
        else:
            assert subs == []
    tick(monkeypatch, runner)
    assert adapter.send.await_count == 3
    assert runner._kanban_sub_fail_counts == {}
    assert "delivered blocked" not in caplog.text
    assert "private detail" not in caplog.text
    with closing(kb.connect()) as conn:
        assert all("private detail" not in str(ev.payload) for ev in kb.list_events(conn, tid))


def test_partial_success_does_not_reset_batch_failure_count(monkeypatch):
    tid = subscription()
    with closing(kb.connect()) as conn:
        kb._append_event(conn, tid, kind="crashed")
    runner, adapter = runner_for(side_effect=[SendResult(success=True), SendResult(success=False)] * 3)
    for _ in range(3):
        tick(monkeypatch, runner)
    assert adapter.send.await_count == 6
    assert state(tid)[0] == []
    assert runner._kanban_sub_fail_counts == {}


@pytest.mark.parametrize("second_board_success", [False, True])
def test_equal_subscription_ids_on_two_boards_have_independent_retries(monkeypatch, second_board_success):
    monkeypatch.setattr(kb, "_new_task_id", lambda: "t_same")
    tid = subscription("alpha")
    assert subscription("beta") == tid

    async def send(chat_id, text, metadata=None):
        return SendResult(success=second_board_success and "[beta]" in text)

    runner, adapter = runner_for(side_effect=send)
    for _ in range(2):
        tick(monkeypatch, runner)
        assert len(state(tid, "alpha")[0]) == 1
        assert len(state(tid, "beta")[0]) == 1
    tick(monkeypatch, runner)
    assert state(tid, "alpha")[0] == []
    assert bool(state(tid, "beta")[0]) is second_board_success
    assert adapter.send.await_count == (4 if second_board_success else 6)


@pytest.mark.parametrize("missing", ["subscription", "all-adapters", "platform", "profile", "disconnected"])
def test_unavailable_delivery_keeps_events_unclaimed_or_rewound(missing, monkeypatch, caplog):
    tid = subscription(subscribe=missing != "subscription")
    runner, adapter = runner_for(SendResult(success=True))
    if missing == "all-adapters":
        runner.adapters = {}
    elif missing == "platform":
        runner.adapters = {Platform.DISCORD: adapter}
    elif missing == "profile":
        with closing(kb.connect()) as conn:
            kb.add_notify_sub(conn, task_id=tid, platform="telegram", chat_id="chat",
                              thread_id="thread", notifier_profile="absent")
    elif missing == "disconnected":
        # Collection sees the platform; routing observes the disconnect.
        runner.adapters = {Platform.TELEGRAM: None}
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    for _ in range(4):
        tick(monkeypatch, runner)
    adapter.send.assert_not_awaited()
    subs, events = state(tid)
    if missing == "subscription":
        assert subs == []
        assert "no_subscription" in caplog.text
    else:
        assert len(subs) == 1 and subs[0]["last_event_id"] == 0
        assert [ev.kind for ev in events] == ["blocked"]
        assert "adapter_unavailable" in caplog.text
    assert runner._kanban_sub_fail_counts == {}
    assert "delivered blocked" not in caplog.text


@pytest.mark.parametrize("completed", [False, True])
def test_positive_transport_advances_once_and_only_final_task_unsubscribes(completed, monkeypatch, caplog):
    tid = subscription(completed=completed)
    runner, adapter = runner_for(SendResult(success=True))
    caplog.set_level(logging.DEBUG, logger="gateway.run")
    tick(monkeypatch, runner)
    subs, events = state(tid)
    assert events == []
    if completed:
        assert subs == []
        runner._deliver_kanban_artifacts.assert_awaited_once()
    else:
        assert len(subs) == 1 and subs[0]["last_event_id"] > 0
    assert "delivered" in caplog.text
    assert "send_failed" not in caplog.text
    assert adapter.send.call_args.kwargs["metadata"] == {"thread_id": "thread"}
    tick(monkeypatch, runner)
    assert adapter.send.await_count == 1


def test_successful_batch_resets_failure_budget(monkeypatch):
    tid = subscription()
    runner, adapter = runner_for(side_effect=[
        None, None, SendResult(success=True), None, None, None,
    ])
    for _ in range(3):
        tick(monkeypatch, runner)
    subs, events = state(tid)
    assert len(subs) == 1 and events == []
    assert runner._kanban_sub_fail_counts == {}
    cursor = subs[0]["last_event_id"]
    with closing(kb.connect()) as conn:
        kb._append_event(conn, tid, kind="crashed")
    for _ in range(2):
        tick(monkeypatch, runner)
        subs, events = state(tid)
        assert len(subs) == 1 and subs[0]["last_event_id"] == cursor
        assert [ev.kind for ev in events] == ["crashed"]
    tick(monkeypatch, runner)
    assert state(tid)[0] == []
    assert adapter.send.await_count == 6
