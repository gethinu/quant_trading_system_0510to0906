"""common.alpaca_trading.submit_paper_order の offline mock テスト。

実際の Alpaca 発注は一切行わない。TradingClient を mock/patch して検証する。
"""

from __future__ import annotations

from unittest import mock

import pytest

from common import alpaca_trading as at
from common.alpaca_trading import (
    LiveAccountGuardError,
    OrderSubmitError,
    PreparedOrder,
    submit_paper_order,
)


@pytest.fixture(autouse=True)
def _paper_env(monkeypatch):
    """デフォルトで paper 環境を強制 (テスト間の env 汚染を防ぐ)。"""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")


def test_dry_run_returns_prepared_order_without_sending():
    """dry_run=True は PreparedOrder を返すだけで submit を呼ばない。"""
    with mock.patch.object(at.ba, "get_client") as m_client:
        po = submit_paper_order("aapl", 10, "buy", dry_run=True)
    assert isinstance(po, PreparedOrder)
    assert po.symbol == "AAPL"
    assert po.qty == 10
    assert po.side == "buy"
    assert po.order_id is None  # 未送信
    m_client.assert_not_called()  # client すら生成されない


def test_live_env_fails_fast_on_real_submit(monkeypatch):
    """ALPACA_PAPER=false + dry_run=False は LiveAccountGuardError。"""
    monkeypatch.setenv("ALPACA_PAPER", "false")
    with pytest.raises(LiveAccountGuardError):
        submit_paper_order("AAPL", 10, "buy", dry_run=False)


def test_live_base_url_fails_fast(monkeypatch):
    """base URL が live を指す場合も fail-fast。"""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://api.alpaca.markets")
    with pytest.raises(LiveAccountGuardError):
        submit_paper_order("AAPL", 10, "buy", dry_run=False)


def test_real_submit_passes_client_order_id():
    """dry_run=False で client_order_id がブローカ層へ伝播することを検証。"""
    fake_order = mock.Mock()
    fake_order.id = "order-123"
    fake_order.status = "accepted"
    fake_client = mock.Mock()

    with mock.patch.object(at.ba, "get_client", return_value=fake_client), \
         mock.patch.object(at.ba, "submit_order_with_retry", return_value=fake_order) as m_submit:
        po = submit_paper_order(
            "AAPL", 5, "buy",
            client_order_id="system1-AAPL-20260630",
            dry_run=False,
        )

    assert po.order_id == "order-123"
    assert po.status == "accepted"
    # client_order_id が確かに渡っている
    _, kwargs = m_submit.call_args
    assert kwargs["client_order_id"] == "system1-AAPL-20260630"


def test_idempotency_same_client_order_id_is_deterministic():
    """同一シグナルは同一 client_order_id を生成する (冪等)。"""
    import pandas as pd

    row = pd.Series({"symbol": "aapl", "system": "System1", "entry_date": "2026-06-30"})
    a = at._build_client_order_id(row)
    b = at._build_client_order_id(row)
    assert a == b == "system1-AAPL-20260630"


def test_insufficient_buying_power_raises_order_submit_error():
    """資金不足エラーは OrderSubmitError に分類される。"""
    fake_client = mock.Mock()

    def _raise(*a, **k):
        raise RuntimeError("insufficient buying power for this order")

    with mock.patch.object(at.ba, "get_client", return_value=fake_client), \
         mock.patch.object(at.ba, "submit_order_with_retry", side_effect=_raise):
        with pytest.raises(OrderSubmitError, match="資金不足"):
            submit_paper_order("AAPL", 1000000, "buy", dry_run=False)


def test_limit_order_requires_price():
    with pytest.raises(ValueError, match="limit_price"):
        submit_paper_order("AAPL", 10, "buy", order_type="limit", dry_run=True)


def test_invalid_side_rejected():
    with pytest.raises(ValueError):
        submit_paper_order("AAPL", 10, "hold", dry_run=True)


def test_audit_log_written_on_dry_run(tmp_path, monkeypatch):
    """dry_run でも監査ログが追記される。"""
    monkeypatch.setattr(at, "_LOG_DIR", tmp_path)
    submit_paper_order("AAPL", 10, "buy", dry_run=True)
    logs = list(tmp_path.glob("alpaca_orders_*.log"))
    assert logs, "監査ログが書かれていない"
    content = logs[0].read_text(encoding="utf-8")
    assert "AAPL" in content and "dry_run" in content
