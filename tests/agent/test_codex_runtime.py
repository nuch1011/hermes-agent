"""Tests for Codex runtime approval-mode bridging."""

from __future__ import annotations

import agent.codex_runtime as codex_runtime


class TestCodexAutoApproveEnabled:
    def test_config_off_enables_codex_auto_approve(self, monkeypatch):
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.setattr(
            "tools.approval.is_current_session_yolo_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"approvals": {"mode": "off"}},
        )

        assert codex_runtime._codex_auto_approve_enabled() is True

    def test_legacy_yolo_alias_enables_codex_auto_approve(self, monkeypatch):
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.setattr(
            "tools.approval.is_current_session_yolo_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"approvals": {"mode": "yolo"}},
        )

        assert codex_runtime._codex_auto_approve_enabled() is True

    def test_manual_keeps_codex_approval_prompts(self, monkeypatch):
        monkeypatch.delenv("HERMES_YOLO_MODE", raising=False)
        monkeypatch.setattr(
            "tools.approval.is_current_session_yolo_enabled",
            lambda: False,
        )
        monkeypatch.setattr(
            "hermes_cli.config.load_config",
            lambda: {"approvals": {"mode": "manual"}},
        )

        assert codex_runtime._codex_auto_approve_enabled() is False
