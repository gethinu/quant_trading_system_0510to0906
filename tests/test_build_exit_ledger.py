"""build_exit_ledger の read-only 契約と純関数の regression test (offline)。

broker には一切繋がない。Alpaca 接続部は :mod:`tests.test_exit_ledger` が守る
pure module 側で検証済みなので、ここでは
  - 立会日の進行状態判定 (寄り前の偽陽性を出さないこと)
  - 過去 exit 理由の復元 (推測しないこと)
  - 全 page 取得の silent truncation 防止
  - 発注系 API を触っていないこと
を見る。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.exit_ledger import (  # noqa: E402
    SESSION_BEFORE_OPEN,
    SESSION_CLOSED,
    SESSION_OPEN,
    SESSION_UNKNOWN,
    ExitLedgerError,
    parse_fills,
    reconstruct_round_trips,
)
from scripts import build_exit_ledger as bl  # noqa: E402


class _Clock:
    def __init__(self, timestamp, is_open, next_open):
        self.timestamp = pd.Timestamp(timestamp)
        self.is_open = is_open
        self.next_open = pd.Timestamp(next_open)


# ---------------------------------------------------------------------------
# safety contract
# ---------------------------------------------------------------------------


def test_ledger_builder_is_read_only():
    with open(bl.__file__, encoding="utf-8") as fh:
        text = fh.read()
    for banned in (
        "submit_order",
        "MarketOrderRequest",
        "cancel_order",
        "close_position",
    ):
        assert banned not in text, f"read-only な台帳生成に発注系 {banned} が混入"


def test_no_alpaca_mode_returns_zero_without_connecting():
    assert bl.main(["--no-alpaca"]) == bl.EXIT_OK


def test_default_date_is_local_not_utc():
    """exit_orders_YYYYMMDD.json は pipeline のローカル日付で命名される。

    UTC 日付にすると JST 早朝 (06:00) 実行で前日の意図ファイルを読み、
    「exit 予定が全部未約定」の偽陽性になる。
    """
    from datetime import datetime

    assert bl._today_str() == datetime.now().strftime("%Y-%m-%d")


# ---------------------------------------------------------------------------
# 立会の進行状態
# ---------------------------------------------------------------------------


def test_session_state_before_open_when_target_is_a_future_session():
    """JST 昼 = ET 前日深夜。対象 (ET 当日) の寄りはまだ来ていない。"""
    clock = _Clock("2026-07-21T23:32:00-04:00", False, "2026-07-22T09:30:00-04:00")
    assert bl.resolve_session_state("2026-07-22", clock) == SESSION_BEFORE_OPEN


def test_session_state_open_during_regular_hours():
    clock = _Clock("2026-07-22T10:00:00-04:00", True, "2026-07-23T09:30:00-04:00")
    assert bl.resolve_session_state("2026-07-22", clock) == SESSION_OPEN


def test_session_state_closed_after_the_bell():
    clock = _Clock("2026-07-22T17:00:00-04:00", False, "2026-07-23T09:30:00-04:00")
    assert bl.resolve_session_state("2026-07-22", clock) == SESSION_CLOSED


def test_session_state_before_open_in_the_premarket_of_the_same_day():
    clock = _Clock("2026-07-22T07:00:00-04:00", False, "2026-07-22T09:30:00-04:00")
    assert bl.resolve_session_state("2026-07-22", clock) == SESSION_BEFORE_OPEN


def test_session_state_closed_for_a_past_session():
    clock = _Clock("2026-07-22T10:00:00-04:00", True, "2026-07-23T09:30:00-04:00")
    assert bl.resolve_session_state("2026-07-17", clock) == SESSION_CLOSED


def test_session_state_unknown_when_clock_is_unavailable():
    assert bl.resolve_session_state("2026-07-22", None) == SESSION_UNKNOWN

    class _Broken:
        timestamp = "not-a-timestamp"

    assert bl.resolve_session_state("2026-07-22", _Broken()) == SESSION_UNKNOWN


# ---------------------------------------------------------------------------
# exit 理由の復元 (推測しない)
# ---------------------------------------------------------------------------


def _trades():
    fills = parse_fills(
        [
            {
                "symbol": "AAA",
                "side": "buy",
                "qty": "1",
                "price": "10",
                "transaction_time": "2026-07-01T14:00:00Z",
                "id": "1",
            },
            {
                "symbol": "AAA",
                "side": "sell",
                "qty": "1",
                "price": "12",
                "transaction_time": "2026-07-06T14:00:00Z",
                "id": "2",
            },
            {
                "symbol": "BBB",
                "side": "buy",
                "qty": "1",
                "price": "10",
                "transaction_time": "2026-07-01T14:00:00Z",
                "id": "3",
            },
            {
                "symbol": "BBB",
                "side": "sell",
                "qty": "1",
                "price": "9",
                "transaction_time": "2026-07-08T14:00:00Z",
                "id": "4",
            },
        ]
    )
    return reconstruct_round_trips(fills).closed_trades


def test_historical_exit_reasons_are_restored_by_session_and_symbol(tmp_path):
    (tmp_path / "exit_orders_20260706.json").write_text(
        json.dumps({"exits": [{"symbol": "AAA", "reason": "time_based"}]}),
        encoding="utf-8",
    )
    trades = _trades()
    tagged = bl.attach_historical_exit_reasons(trades, tmp_path)

    assert tagged == 1
    by_symbol = {t.symbol: t.exit_reason for t in trades}
    assert by_symbol["AAA"] == "time_based"
    # 記録が無い分は推測せず None のまま (dashboard 側で「記録なし」と出す)
    assert by_symbol["BBB"] is None


def test_historical_exit_reasons_do_not_leak_across_sessions(tmp_path):
    """別の日の同一 symbol の理由を流用しない。"""
    (tmp_path / "exit_orders_20260715.json").write_text(
        json.dumps({"exits": [{"symbol": "AAA", "reason": "protect_stop"}]}),
        encoding="utf-8",
    )
    trades = _trades()
    assert bl.attach_historical_exit_reasons(trades, tmp_path) == 0
    assert all(t.exit_reason is None for t in trades)


def test_historical_exit_reasons_ignore_broken_files(tmp_path):
    (tmp_path / "exit_orders_20260706.json").write_text("{ broken", encoding="utf-8")
    (tmp_path / "exit_orders_notadate.json").write_text("{}", encoding="utf-8")
    assert bl.attach_historical_exit_reasons(_trades(), tmp_path) == 0


def test_load_exit_intents_missing_file_is_empty(tmp_path):
    assert bl.load_exit_intents(tmp_path, "20260722") == []


# ---------------------------------------------------------------------------
# 取得の silent truncation 防止
# ---------------------------------------------------------------------------


class _PagingClient:
    """page_size ぴったりを返し続ける client (= 無限 page)。"""

    def __init__(self, pages):
        self.pages = pages
        self.calls = 0

    def get(self, path, params=None):
        assert path == "/account/activities/FILL"
        self.calls += 1
        size = (params or {}).get("page_size", 100)
        return [{"id": f"{self.calls}-{i}"} for i in range(size)]


def test_fetch_all_fills_raises_instead_of_returning_a_short_list():
    """page 上限に当たったら黙って短い list を返さない (計測漏れの温床)。"""
    client = _PagingClient(pages=None)
    with pytest.raises(ExitLedgerError):
        bl.fetch_all_fills(client, page_size=2, max_pages=3)
    assert client.calls == 3


def test_fetch_all_fills_stops_on_a_short_page():
    class _Short:
        def __init__(self):
            self.calls = 0

        def get(self, path, params=None):
            self.calls += 1
            return [{"id": "a"}, {"id": "b"}] if self.calls == 1 else [{"id": "c"}]

    out = bl.fetch_all_fills(_Short(), page_size=2, max_pages=5)
    assert [r["id"] for r in out] == ["a", "b", "c"]


def test_fetch_broker_positions_skips_unparsable_qty():
    class _P:
        def __init__(self, symbol, qty):
            self.symbol = symbol
            self.qty = qty

    class _C:
        def get_all_positions(self):
            return [_P("aapl", "10"), _P("BAD", "n/a"), _P("SHRT", "-5")]

    assert bl.fetch_broker_positions(_C()) == {"AAPL": 10.0, "SHRT": -5.0}
