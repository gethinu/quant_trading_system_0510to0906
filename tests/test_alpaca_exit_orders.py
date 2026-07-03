"""common/alpaca_trading.py の exit wiring (Case C hybrid) の unit test.

抑えるべき仕様 (subscriber サービスイン基準):
    - system tag (client_order_id parsing / tracker hydration) が正しく効く
    - S1/S4: trailing stop + stop-loss protection の proposal を生成
    - S2/S3/S5/S6: max_holding_days 経過で time_based exit を生成
    - S7: SPY high >= max_70 で breakout exit を生成
    - protection は既存 client_order_id と重複しないよう dedup
    - dry_run=True で submit_paper_exit_order は実発注しない (audit log のみ)
    - side inversion: long -> sell close, short -> buy close
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from common.alpaca_trading import (
    ExitReasonCode,
    LiveAccountGuardError,
    PositionSnapshot,
    PreparedExit,
    build_exit_orders_from_positions,
    compute_holding_days,
    hydrate_system_tags,
    parse_entry_date_from_client_order_id,
    parse_system_from_client_order_id,
    submit_paper_exit_order,
)


# -------------------------------------------------------------------------
# client_order_id parsing
# -------------------------------------------------------------------------


class TestClientOrderIdParsing:
    def test_system_tag_extracted(self):
        assert parse_system_from_client_order_id("system1-AAPL-20260702") == "system1"
        assert parse_system_from_client_order_id("system7-SPY-20260702") == "system7"

    def test_entry_date_extracted(self):
        assert parse_entry_date_from_client_order_id("system1-AAPL-20260702") == "2026-07-02"

    def test_reject_protect_and_exit_prefix(self):
        # exit_check 側で生成した coid を entry と誤認しない
        assert parse_system_from_client_order_id("protect-system1-AAPL-20260702-protect-stop") is None
        assert parse_system_from_client_order_id("exit-system2-TSLA-20260702-exit-time") is None

    def test_reject_garbage(self):
        assert parse_system_from_client_order_id(None) is None
        assert parse_system_from_client_order_id("") is None
        assert parse_system_from_client_order_id("hoge") is None
        assert parse_entry_date_from_client_order_id("system1-AAPL") is None


# -------------------------------------------------------------------------
# hydrate_system_tags
# -------------------------------------------------------------------------


class TestHydrateSystemTags:
    def test_entry_orders_index_wins_over_tracker(self):
        snap = PositionSnapshot(symbol="AAPL", qty=10, side="long", avg_entry_price=100.0)
        hydrate_system_tags(
            [snap],
            tracker={"AAPL": {"system": "system3", "entry_date": "2026-06-01"}},
            entry_orders_index={"AAPL": {"system": "system1", "entry_date": "2026-07-01"}},
        )
        assert snap.system == "system1"
        assert snap.entry_date == "2026-07-01"

    def test_tracker_fallback_when_index_empty(self):
        snap = PositionSnapshot(symbol="MSFT", qty=5, side="long", avg_entry_price=400.0)
        hydrate_system_tags(
            [snap],
            tracker={"MSFT": {"system": "system4", "entry_date": "2026-06-15"}},
            entry_orders_index={},
        )
        assert snap.system == "system4"
        assert snap.entry_date == "2026-06-15"

    def test_no_source_leaves_none(self):
        snap = PositionSnapshot(symbol="XXX", qty=1, side="long", avg_entry_price=1.0)
        hydrate_system_tags([snap])
        assert snap.system is None
        assert snap.entry_date is None


# -------------------------------------------------------------------------
# compute_holding_days
# -------------------------------------------------------------------------


class TestHoldingDays:
    def test_basic(self):
        assert compute_holding_days("2026-07-01", "2026-07-04") == 3

    def test_same_day(self):
        assert compute_holding_days("2026-07-01", "2026-07-01") == 0

    def test_none_input(self):
        assert compute_holding_days(None, "2026-07-01") is None


# -------------------------------------------------------------------------
# build_exit_orders_from_positions (system-by-system)
# -------------------------------------------------------------------------


def _snap(symbol, system, side, qty, entry_price, entry_date) -> PositionSnapshot:
    signed_qty = qty if side == "long" else -qty
    return PositionSnapshot(
        symbol=symbol, qty=signed_qty, side=side,
        avg_entry_price=entry_price,
        system=system, entry_date=entry_date,
    )


class TestBuildExitOrders:
    def test_system2_time_based_triggers_at_max_holding_days(self):
        # S2: max_holding_days=2 → holding=2 で成行 close (sell)
        snap = _snap("TSLA", "system2", "short", 8, 250.0, "2026-06-30")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"TSLA": {10: 5.0}},
        )
        time_exits = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert len(time_exits) == 1
        te = time_exits[0]
        assert te.system == "system2"
        assert te.side == "buy"  # short close = buy
        assert te.order_type == "market"
        assert te.qty == 8
        assert te.holding_days == 2
        assert te.max_holding_days == 2
        assert te.client_order_id and te.client_order_id.startswith("exit-system2-TSLA-")

    def test_system2_time_based_not_triggered_before_max(self):
        snap = _snap("TSLA", "system2", "short", 8, 250.0, "2026-07-01")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"TSLA": {10: 5.0}},
        )
        time_exits = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert time_exits == []

    def test_system3_time_based_at_3_days(self):
        snap = _snap("MSFT", "system3", "long", 5, 420.0, "2026-06-29")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"MSFT": {10: 4.0}},
        )
        assert any(e.reason == ExitReasonCode.TIME and e.side == "sell" for e in exits)

    def test_system5_time_based_at_6_days(self):
        snap = _snap("NVDA", "system5", "long", 3, 120.0, "2026-06-26")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"NVDA": {10: 2.0}},
        )
        te = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert len(te) == 1
        assert te[0].max_holding_days == 6

    def test_system6_time_based_at_3_days_short(self):
        snap = _snap("GME", "system6", "short", 4, 30.0, "2026-06-29")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"GME": {10: 1.0}},
        )
        te = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert len(te) == 1
        assert te[0].side == "buy"  # short close

    def test_system7_spy_breakout_triggers_close(self):
        # S7: SPY short hedge, breakout (high >= max_70) → 成行 close (buy = short cover)
        snap = _snap("SPY", "system7", "short", 3, 540.0, "2026-06-15")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            spy_high=560.0, spy_max70=555.0,
        )
        breakouts = [e for e in exits if e.reason == ExitReasonCode.BREAKOUT]
        assert len(breakouts) == 1
        assert breakouts[0].side == "buy"
        assert breakouts[0].order_type == "market"

    def test_system7_no_breakout_when_below_max70(self):
        snap = _snap("SPY", "system7", "short", 3, 540.0, "2026-06-15")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            spy_high=550.0, spy_max70=555.0,
        )
        assert not any(e.reason == ExitReasonCode.BREAKOUT for e in exits)

    def test_system1_generates_trailing_and_stop_protection(self):
        # S1 long: trailing 25% + stop 5×ATR20 protection
        snap = _snap("AAPL", "system1", "long", 10, 195.0, "2026-07-01")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"AAPL": {20: 3.0}},
        )
        # 1) trailing_stop
        trail = [e for e in exits if e.reason == ExitReasonCode.PROTECT_TRAIL]
        assert len(trail) == 1
        assert trail[0].order_type == "trailing_stop"
        assert trail[0].side == "sell"
        assert trail[0].trail_percent == pytest.approx(25.0)
        # 2) stop-loss
        stop = [e for e in exits if e.reason == ExitReasonCode.PROTECT_STOP]
        assert len(stop) == 1
        # stop = 195 - 5×3 = 180
        assert stop[0].stop_price == pytest.approx(180.0)
        assert stop[0].side == "sell"

    def test_system4_generates_trailing_20_pct(self):
        snap = _snap("JPM", "system4", "long", 5, 200.0, "2026-07-01")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"JPM": {40: 2.0}},
        )
        trail = [e for e in exits if e.reason == ExitReasonCode.PROTECT_TRAIL]
        assert len(trail) == 1
        assert trail[0].trail_percent == pytest.approx(20.0)

    def test_system2_generates_target_and_stop_protection(self):
        # S2 short entry_price=250, target=250/(1.04)≈240.38, stop=250+3×5=265
        snap = _snap("TSLA", "system2", "short", 8, 250.0, "2026-07-02")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"TSLA": {10: 5.0}},
        )
        target = [e for e in exits if e.reason == ExitReasonCode.PROTECT_TARGET]
        stop = [e for e in exits if e.reason == ExitReasonCode.PROTECT_STOP]
        assert len(target) == 1
        assert target[0].limit_price == pytest.approx(250.0 / 1.04, rel=1e-3)
        assert target[0].side == "buy"  # short cover
        assert len(stop) == 1
        assert stop[0].stop_price == pytest.approx(265.0)

    def test_system5_atr_based_target(self):
        # S5 long entry=120, ATR10=2 → target = 120 + 1×2 = 122
        snap = _snap("NVDA", "system5", "long", 3, 120.0, "2026-07-02")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"NVDA": {10: 2.0}},
        )
        target = [e for e in exits if e.reason == ExitReasonCode.PROTECT_TARGET]
        assert len(target) == 1
        assert target[0].limit_price == pytest.approx(122.0)

    def test_protection_dedup_via_existing_coids(self):
        snap = _snap("AAPL", "system1", "long", 10, 195.0, "2026-07-01")
        existing = {
            "protect-system1-AAPL-20260701-protect-trail",
            "protect-system1-AAPL-20260701-protect-stop",
        }
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"AAPL": {20: 3.0}},
            existing_protect_coids=existing,
        )
        # 既存 protect coid と重複するものは skip される
        assert exits == []

    def test_time_exit_beats_protection_generation(self):
        # time-based が発火した日は protection は追加発注しない (優先順位テスト)
        snap = _snap("TSLA", "system2", "short", 8, 250.0, "2026-06-30")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02",
            atr_by_symbol={"TSLA": {10: 5.0}},
        )
        assert any(e.reason == ExitReasonCode.TIME for e in exits)
        assert not any(e.reason.startswith("protect_") for e in exits)

    def test_position_without_system_tag_is_skipped(self):
        snap = PositionSnapshot(symbol="XXX", qty=1, side="long", avg_entry_price=1.0)
        exits = build_exit_orders_from_positions([snap], today="2026-07-02")
        assert exits == []

    def test_zero_qty_is_skipped(self):
        snap = _snap("AAPL", "system1", "long", 0, 195.0, "2026-07-01")
        exits = build_exit_orders_from_positions([snap], today="2026-07-02")
        assert exits == []


# -------------------------------------------------------------------------
# submit_paper_exit_order (dry_run + paper enforce)
# -------------------------------------------------------------------------


class TestSubmitPaperExitOrder:
    def _sample(self) -> PreparedExit:
        return PreparedExit(
            symbol="AAPL", system="system1", qty=10, side="sell",
            order_type="market", reason=ExitReasonCode.TIME,
            client_order_id="exit-system1-AAPL-20260702-exit-time",
        )

    def test_dry_run_does_not_submit(self):
        po = self._sample()
        result = submit_paper_exit_order(po, dry_run=True)
        assert result.dry_run is True
        assert result.order_id is None
        assert result.status is None

    def test_dry_run_respects_zero_qty_guard(self):
        po = self._sample()
        po.qty = 0
        with pytest.raises(ValueError):
            submit_paper_exit_order(po, dry_run=True)

    def test_dry_run_respects_bad_side(self):
        po = self._sample()
        po.side = "hold"  # 意図的に invalid
        with pytest.raises(ValueError):
            submit_paper_exit_order(po, dry_run=True)

    def test_live_submit_requires_paper_env(self, monkeypatch):
        # ALPACA_PAPER=false でも live には行かせない (LiveAccountGuardError)
        monkeypatch.setenv("ALPACA_PAPER", "false")
        po = self._sample()
        with pytest.raises(LiveAccountGuardError):
            submit_paper_exit_order(po, dry_run=False, client=object())

    def test_live_submit_calls_broker(self, monkeypatch):
        monkeypatch.setenv("ALPACA_PAPER", "true")
        monkeypatch.setenv("ALPACA_API_BASE_URL", "")  # host guard スキップ

        calls: list[dict] = []

        def fake_submit(client, symbol, qty, **kwargs):
            calls.append({"symbol": symbol, "qty": qty, **kwargs})
            return SimpleNamespace(id="ORD-EXIT-1", status="accepted")

        # broker_alpaca.submit_order_with_retry を差し替え
        import common.alpaca_trading as at
        monkeypatch.setattr(at.ba, "submit_order_with_retry", fake_submit)

        po = self._sample()
        result = submit_paper_exit_order(po, dry_run=False, client=object())
        assert result.order_id == "ORD-EXIT-1"
        assert result.status == "accepted"
        assert len(calls) == 1
        assert calls[0]["side"] == "sell"
        assert calls[0]["order_type"] == "market"
        assert calls[0]["client_order_id"] == "exit-system1-AAPL-20260702-exit-time"
