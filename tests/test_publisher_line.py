"""Smoke tests for common/publishers/line.py.

Phase 1 audit gap: `LinePublisher` は tests/ 内で hit 0 (0% coverage).
Phase 2 で実装予定の skeleton stage だが, 少なくとも:
  - is_configured の 4 分岐 (token+to / token 単独 / to 単独 / どちらも無)
  - send の dry_run branch と PublishResult フィールド
  - env var override
を smoke で固定して, 将来の Phase 2 実装時に基本契約が壊れないようにする.
"""

from __future__ import annotations

from unittest.mock import patch

import pytest

from common.publishers.base import PublishResult
from common.publishers.line import LinePublisher


class TestLinePublisherConfigured:
    def test_configured_when_both_token_and_to_provided(self):
        p = LinePublisher(access_token="tok", to="U12345")
        assert p.is_configured() is True

    def test_not_configured_when_token_missing(self):
        p = LinePublisher(access_token="", to="U12345")
        assert p.is_configured() is False

    def test_not_configured_when_target_missing(self):
        p = LinePublisher(access_token="tok", to=None)
        assert p.is_configured() is False

    def test_not_configured_when_both_missing(self):
        with patch.dict("os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": ""}, clear=False):
            p = LinePublisher(access_token=None, to=None)
            assert p.is_configured() is False

    def test_token_from_env_var(self):
        with patch.dict(
            "os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": "env_tok"}, clear=False
        ):
            p = LinePublisher(access_token=None, to="U12345")
            assert p.access_token == "env_tok"
            assert p.is_configured() is True

    def test_custom_env_var_name(self):
        with patch.dict("os.environ", {"MY_LINE_TOKEN": "custom_tok"}, clear=False):
            p = LinePublisher(access_token=None, to="U12345", env_var="MY_LINE_TOKEN")
            assert p.access_token == "custom_tok"

    def test_explicit_token_takes_priority_over_env(self):
        with patch.dict(
            "os.environ", {"LINE_CHANNEL_ACCESS_TOKEN": "env_tok"}, clear=False
        ):
            p = LinePublisher(access_token="explicit_tok", to="U12345")
            assert p.access_token == "explicit_tok"


class TestLinePublisherSend:
    @pytest.fixture
    def signals_payload(self) -> dict:
        return {
            "date": "2026-07-02",
            "provider": "polygon",
            "meta": {"run_id": "run-abc"},
            "portfolio": {"total_signals": 0},
            "systems": {},
        }

    def test_send_dry_run_returns_ok(self, signals_payload):
        p = LinePublisher(access_token="tok", to="U12345")
        result = p.send(signals_payload, dry_run=True)
        assert isinstance(result, PublishResult)
        assert result.ok is True  # dry_run=True → ok=True (stub)
        assert result.publisher == "line"
        assert result.target == "U12345"

    def test_send_live_returns_not_ok_until_phase2(self, signals_payload):
        """Phase 2 実装完了までは send(dry_run=False) は ok=False stub."""
        p = LinePublisher(access_token="tok", to="U12345")
        result = p.send(signals_payload, dry_run=False)
        assert result.ok is False  # non-dry_run は Phase 2 stub
        assert "Phase 2" in result.detail

    def test_send_target_falls_back_to_unset(self, signals_payload):
        """to 未指定でも send は crash せず target='unset' で結果を返す."""
        p = LinePublisher(access_token="tok", to=None)
        result = p.send(signals_payload, dry_run=True)
        assert result.target == "unset"

    def test_send_includes_run_id_in_detail(self, signals_payload):
        p = LinePublisher(access_token="tok", to="U12345")
        result = p.send(signals_payload, dry_run=True)
        assert "run-abc" in result.detail


class TestLinePublisherRegistryCompat:
    """Registry の Publisher ABC 契約を満たすことの smoke."""

    def test_has_name_attr(self):
        assert LinePublisher.name == "line"

    def test_is_publisher_subclass(self):
        from common.publishers.base import Publisher

        assert issubclass(LinePublisher, Publisher)

    def test_can_be_built_via_registry_kind(self):
        """build_publisher('line', ...) で構築可能."""
        try:
            from common.publishers import build_publisher  # type: ignore
        except ImportError:
            from common.publishers.registry import build_publisher  # type: ignore
        p = build_publisher("line", access_token="tok", to="U12345")
        assert isinstance(p, LinePublisher)
