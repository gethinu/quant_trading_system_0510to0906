"""端株 (fractional share) exit の回帰テスト (2026-07-12 silent-drop バグ修正)。

背景:
    equity 連動サイジングは端株 (qty<1 / 小数株) を日常的に作る。旧実装は
    ``PositionSnapshot.abs_qty = int(abs(qty))`` で端株を 0 に切り捨て、
    ``build_exit_orders_from_positions`` の ``if snap.abs_qty <= 0`` により
    time / breakout / protection の **全 exit 種別から silent 除外** していた
    (system3 の満期 6 建玉が exit 未計画になった)。

抑えるべき仕様:
    - abs_qty は端株を保持 (切り捨てない)。exit_qty は整数株=int / 端株=float。
    - 端株の time / breakout / synthetic protection は必ず成行 DAY。
    - 端株 protection は native 不可 → synthetic (現値が stop/target を突破
      した時だけ成行 DAY 全数クローズを 1 件)。二重クローズしない。
    - 整数株 (whole) は従来どおり native stop/limit/trailing (gtc) を維持。
    - existing_exit_coids で同日再 run の二重発注を防ぐ。
    - submit_paper_exit_order は端株×非(成行DAY) を fail-fast する。
"""

from __future__ import annotations

import pytest

from common.alpaca_trading import (
    ExitReasonCode,
    PositionSnapshot,
    PreparedExit,
    build_exit_orders_from_positions,
    submit_paper_exit_order,
)

# -------------------------------------------------------------------------
# PositionSnapshot: abs_qty / is_fractional / current_price / exit_qty
# -------------------------------------------------------------------------


def _snap(
    symbol,
    system,
    side,
    qty,
    entry_price,
    entry_date,
    *,
    current_price=None,
) -> PositionSnapshot:
    """符号付き qty と (任意で) 現値から market_value を逆算した snapshot を作る。"""
    signed_qty = qty if side == "long" else -qty
    mv = None
    if current_price is not None:
        # long は +、short は - の market_value (Alpaca 準拠)。current_price は abs で復元。
        mv = current_price * signed_qty
    return PositionSnapshot(
        symbol=symbol,
        qty=signed_qty,
        side=side,
        avg_entry_price=entry_price,
        market_value=mv,
        system=system,
        entry_date=entry_date,
    )


class TestSnapshotQtyHelpers:
    def test_abs_qty_preserves_fraction(self):
        assert PositionSnapshot("X", 0.5, "long", 1.0).abs_qty == pytest.approx(0.5)
        assert PositionSnapshot("X", 3.7, "long", 1.0).abs_qty == pytest.approx(3.7)
        assert PositionSnapshot("X", -0.25, "short", 1.0).abs_qty == pytest.approx(0.25)
        assert PositionSnapshot("X", 5.0, "long", 1.0).abs_qty == pytest.approx(5.0)

    def test_is_fractional(self):
        assert PositionSnapshot("X", 0.5, "long", 1.0).is_fractional is True
        assert PositionSnapshot("X", 3.7, "long", 1.0).is_fractional is True
        assert PositionSnapshot("X", 5.0, "long", 1.0).is_fractional is False
        # float 表現誤差は整数株扱い
        assert PositionSnapshot("X", 5.0000000001, "long", 1.0).is_fractional is False

    def test_exit_qty_whole_is_int_fraction_is_float(self):
        whole = PositionSnapshot("X", 8.0, "long", 1.0).exit_qty()
        assert whole == 8 and isinstance(whole, int)
        frac = PositionSnapshot("X", 0.512345678, "long", 1.0).exit_qty()
        assert isinstance(frac, float)
        assert frac == pytest.approx(0.512345678)

    def test_current_price_roundtrip(self):
        s = _snap("X", "system3", "long", 0.5, 100.0, "2026-07-01", current_price=94.0)
        assert s.current_price == pytest.approx(94.0)
        # short: market_value は負でも abs で現値復元
        s2 = _snap("X", "system6", "short", 0.5, 30.0, "2026-07-01", current_price=32.0)
        assert s2.current_price == pytest.approx(32.0)

    def test_current_price_none_when_no_market_value(self):
        s = PositionSnapshot("X", 0.5, "long", 100.0, market_value=None)
        assert s.current_price is None


# -------------------------------------------------------------------------
# 端株の time-exit (system3 満期 6 建玉の再現) — 旧実装は silent drop
# -------------------------------------------------------------------------


class TestFractionalTimeExit:
    def test_fractional_long_time_exit_fires_as_market_day(self):
        # system3 long 端株 0.512、満期 (holding=3 >= max_holding_days=3)
        snap = _snap("AEHR", "system3", "long", 0.512, 100.0, "2026-06-29")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"AEHR": {10: 2.0}}
        )
        time_exits = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert len(time_exits) == 1, "端株の time-exit が silent drop されている"
        te = time_exits[0]
        assert te.side == "sell"
        assert te.order_type == "market"
        assert te.time_in_force == "day"  # 端株は成行 DAY 必須
        assert te.qty == pytest.approx(0.512)

    def test_fractional_position_is_not_dropped(self):
        # 旧バグの核: qty<1 の建玉が exit 案から丸ごと消える回帰ガード
        snap = _snap("AIP", "system3", "long", 0.3, 50.0, "2026-06-29")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"AIP": {10: 1.0}}
        )
        assert (
            exits
        ), "端株建玉が exit 案から丸ごと除外されている (旧 int() 切り捨てバグ)"

    def test_six_maturity_positions_all_planned(self):
        # system3 満期 6 建玉 (AEHR/AIP/FORM/MXL/UCTT/VECO) が全て time-exit される
        syms = ["AEHR", "AIP", "FORM", "MXL", "UCTT", "VECO"]
        snaps = [_snap(s, "system3", "long", 0.4, 40.0, "2026-06-29") for s in syms]
        atr = {s: {10: 1.0} for s in syms}
        exits = build_exit_orders_from_positions(
            snaps, today="2026-07-02", atr_by_symbol=atr
        )
        planned = {e.symbol for e in exits if e.reason == ExitReasonCode.TIME}
        assert planned == set(syms)
        assert all(e.order_type == "market" and e.time_in_force == "day" for e in exits)


# -------------------------------------------------------------------------
# 端株の synthetic protection (native stop/limit 不可)
# -------------------------------------------------------------------------


class TestFractionalSyntheticProtection:
    def test_synthetic_stop_fires_on_breach(self):
        # system3 long 端株、未満期。stop=100-2*2.5=95。現値94<=95 → synthetic stop
        snap = _snap(
            "MXL", "system3", "long", 0.5, 100.0, "2026-07-01", current_price=94.0
        )
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"MXL": {10: 2.0}}
        )
        stops = [e for e in exits if e.reason == ExitReasonCode.PROTECT_STOP]
        assert len(stops) == 1
        s = stops[0]
        assert s.order_type == "market"  # native "stop" ではなく成行
        assert s.time_in_force == "day"
        assert s.side == "sell"
        assert s.qty == pytest.approx(0.5)
        assert s.stop_price == pytest.approx(95.0)
        assert s.client_order_id and s.client_order_id.endswith("exit-synstop")

    def test_synthetic_stop_not_fired_when_price_above_stop(self):
        snap = _snap(
            "MXL", "system3", "long", 0.5, 100.0, "2026-07-01", current_price=96.0
        )
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"MXL": {10: 2.0}}
        )
        assert exits == []  # stop(95) 未突破 & target(104) 未突破 → 何も出さない

    def test_synthetic_target_fires_on_breach(self):
        # target=100*1.04=104。現値105 → synthetic target (stop95 は未突破)
        snap = _snap(
            "UCTT", "system3", "long", 0.5, 100.0, "2026-07-01", current_price=105.0
        )
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"UCTT": {10: 2.0}}
        )
        tgts = [e for e in exits if e.reason == ExitReasonCode.PROTECT_TARGET]
        assert len(tgts) == 1
        t = tgts[0]
        assert t.order_type == "market" and t.time_in_force == "day"
        assert t.limit_price == pytest.approx(104.0)
        assert t.client_order_id and t.client_order_id.endswith("exit-syntarget")

    def test_synthetic_short_stop_fires_on_breach(self):
        # system6 short 端株、未満期。stop=30+1*3=33。現値34>=33 → synthetic stop (buy)
        snap = _snap(
            "GME", "system6", "short", 0.5, 30.0, "2026-07-01", current_price=34.0
        )
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"GME": {10: 1.0}}
        )
        stops = [e for e in exits if e.reason == ExitReasonCode.PROTECT_STOP]
        assert len(stops) == 1
        assert stops[0].side == "buy"  # short cover
        assert stops[0].order_type == "market"

    def test_synthetic_skipped_when_current_price_unknown(self):
        # market_value も price_by_symbol も無い → synthetic は発注しない (safe fallback)
        snap = PositionSnapshot(
            "MXL",
            0.5,
            "long",
            100.0,
            market_value=None,
            system="system3",
            entry_date="2026-07-01",
        )
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"MXL": {10: 2.0}}
        )
        assert exits == []

    def test_synthetic_uses_price_by_symbol_fallback(self):
        # market_value 無しでも price_by_symbol が現値を供給すれば synthetic 判定できる
        snap = PositionSnapshot(
            "MXL",
            0.5,
            "long",
            100.0,
            market_value=None,
            system="system3",
            entry_date="2026-07-01",
        )
        exits = build_exit_orders_from_positions(
            [snap],
            today="2026-07-02",
            atr_by_symbol={"MXL": {10: 2.0}},
            price_by_symbol={"MXL": 94.0},
        )
        stops = [e for e in exits if e.reason == ExitReasonCode.PROTECT_STOP]
        assert len(stops) == 1
        assert stops[0].order_type == "market"


# -------------------------------------------------------------------------
# 二重クローズ防止
# -------------------------------------------------------------------------


class TestNoDoubleClose:
    def test_time_exit_beats_synthetic_single_close(self):
        # 満期 かつ 現値がストップ突破でも、close は time-exit 1 件のみ
        snap = _snap(
            "AEHR", "system3", "long", 0.5, 100.0, "2026-06-29", current_price=90.0
        )
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"AEHR": {10: 2.0}}
        )
        assert len(exits) == 1
        assert exits[0].reason == ExitReasonCode.TIME

    def test_existing_exit_coid_dedups_time_exit(self):
        snap = _snap("AEHR", "system3", "long", 0.5, 100.0, "2026-06-29")
        coid = "exit-system3-AEHR-20260702-exit-time"
        exits = build_exit_orders_from_positions(
            [snap],
            today="2026-07-02",
            atr_by_symbol={"AEHR": {10: 2.0}},
            existing_exit_coids={coid},
        )
        assert exits == []  # 既に open なので二重発注しない

    def test_existing_exit_coid_dedups_synthetic(self):
        snap = _snap(
            "MXL", "system3", "long", 0.5, 100.0, "2026-07-01", current_price=94.0
        )
        coid = "exit-system3-MXL-20260702-exit-synstop"
        exits = build_exit_orders_from_positions(
            [snap],
            today="2026-07-02",
            atr_by_symbol={"MXL": {10: 2.0}},
            existing_exit_coids={coid},
        )
        assert exits == []


# -------------------------------------------------------------------------
# 整数株 (whole) 回帰: native protection を維持
# -------------------------------------------------------------------------


class TestWholeShareRegression:
    def test_whole_long_keeps_native_gtc_protection(self):
        # system1 整数株 long → native trailing_stop + stop (gtc)。synthetic に落ちない
        snap = _snap("AAPL", "system1", "long", 10, 195.0, "2026-07-01")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"AAPL": {20: 3.0}}
        )
        by_reason = {e.reason: e for e in exits}
        assert ExitReasonCode.PROTECT_TRAIL in by_reason
        assert ExitReasonCode.PROTECT_STOP in by_reason
        assert by_reason[ExitReasonCode.PROTECT_TRAIL].order_type == "trailing_stop"
        assert by_reason[ExitReasonCode.PROTECT_STOP].order_type == "stop"
        # 整数株は gtc の resting order (成行 DAY ではない)
        assert all(e.time_in_force == "gtc" for e in exits)
        # qty は int 型
        assert all(isinstance(e.qty, int) for e in exits)

    def test_whole_time_exit_qty_is_int(self):
        snap = _snap("TSLA", "system2", "short", 8, 250.0, "2026-06-30")
        exits = build_exit_orders_from_positions(
            [snap], today="2026-07-02", atr_by_symbol={"TSLA": {10: 5.0}}
        )
        te = [e for e in exits if e.reason == ExitReasonCode.TIME]
        assert len(te) == 1
        assert te[0].qty == 8 and isinstance(te[0].qty, int)


# -------------------------------------------------------------------------
# submit ガード (端株 × 非成行DAY は fail-fast)
# -------------------------------------------------------------------------


class TestSubmitFractionalGuards:
    def _frac_market(self) -> PreparedExit:
        return PreparedExit(
            symbol="MXL",
            system="system3",
            qty=0.5,
            side="sell",
            order_type="market",
            reason=ExitReasonCode.TIME,
            time_in_force="day",
            client_order_id="exit-system3-MXL-20260702-exit-time",
        )

    def test_fractional_market_day_dry_run_ok(self):
        po = self._frac_market()
        result = submit_paper_exit_order(po, dry_run=True)
        assert result.dry_run is True
        assert result.order_id is None

    def test_fractional_stop_order_type_rejected(self):
        po = self._frac_market()
        po.order_type = "stop"
        po.stop_price = 95.0
        with pytest.raises(ValueError):
            submit_paper_exit_order(po, dry_run=True)

    def test_fractional_gtc_rejected(self):
        po = self._frac_market()
        po.time_in_force = "gtc"
        with pytest.raises(ValueError):
            submit_paper_exit_order(po, dry_run=True)

    def test_zero_qty_still_rejected(self):
        po = self._frac_market()
        po.qty = 0
        with pytest.raises(ValueError):
            submit_paper_exit_order(po, dry_run=True)

    def test_whole_share_stop_still_allowed(self):
        # 整数株の native stop は従来どおり許可 (端株ガードに引っかからない)
        po = PreparedExit(
            symbol="AAPL",
            system="system1",
            qty=10,
            side="sell",
            order_type="stop",
            reason=ExitReasonCode.PROTECT_STOP,
            stop_price=180.0,
            time_in_force="gtc",
            client_order_id="protect-system1-AAPL-20260701-protect-stop",
        )
        result = submit_paper_exit_order(po, dry_run=True)
        assert result.dry_run is True
