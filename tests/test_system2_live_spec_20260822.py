"""System2 の live 実行を **ドキュメント spec** に戻した 3 件の回帰テスト。

対象 spec (すべて repo 内の既存記述。ここで新しい数字は作らない):

  docs/systems/システム2.txt
      仕掛け  : 「翌日、前日の終値を4%以上上回る価格で売る。」
      損切り  : 「売値の上に、過去 10日の3ATR の位置に損切り注文を置く。」
      利食い  : 「大引けで4%以上の利益が出ているときは翌日の大引けで…手仕舞う」
                 「2日後に利益目標に到達しないときは、翌日に…注文を入れる」
  config/config.yaml (system2)
      entry_min_gap_pct: 0.04 / profit_take_pct: 0.04 / max_hold_days: 2
      stop_atr_multiple: 3.0
  common/trade_management.py (SYSTEM_TRADE_RULES["system2"])
      entry_type=LIMIT / entry_price_offset_pct=4.0 / entry_reference="close"
      profit_target_type="percentage" / profit_target_value=4.0
      max_holding_days=2 / stop_atr_multiplier=3.0
  common/alpaca_trading.py (docs-alignment コメント)
      「S2 = 翌日 前日終値+4% 以上の指値売 (LIMIT)」
  strategies/system2_strategy.py compute_exit docstring
      「未達: 2営業日待っても利確に届かない場合は3日目の大引けで決済」

修正した 3 バグ:
  (A) +4% 利確が live で一度も常駐していなかった。Alpaca は 1 注文が qty を
      全量予約するので stop が枠を握り、target は常に抑止される。OCO
      (PROTECT_USE_OCO=1) が唯一の同時常駐手段だが、単発 stop が既に resting
      だと already_open で短絡して OCO 分岐に到達しなかった。
  (B) +4% の指値エントリーが live 経路に存在しなかった。signals_json_to_orders
      が order_type="market" 固定で、entry_price も offset なしの前日終値。
  (C) _holding_days が暦日で数え、立会日 spec (max_holding_days=2) と単位が
      食い違っていた (金曜エントリーが月曜 = 立会 1 日で time exit した)。
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.alpaca_trading import (
    ExitReasonCode,
    PositionSnapshot,
    _flatten_json_signals,
    build_exit_orders_from_positions,
    build_stop_rearm_after_failed_oco,
    compute_holding_days,
    signals_json_to_orders,
)
from common.today_signals import _compute_entry_stop
from common.trade_management import SYSTEM_TRADE_RULES
from strategies.system2_strategy import System2Strategy


# =====================================================================
# spec 定数そのものが repo にあることの固定 (invented number の検出器)
# =====================================================================


def test_spec_values_exist_in_repo():
    """+4% entry / +4% target / 2 日保有 が spec 側に実在する。"""
    rules = SYSTEM_TRADE_RULES["system2"]
    assert rules.entry_type.value.lower() == "limit"
    assert rules.entry_price_offset_pct == pytest.approx(4.0)
    assert rules.entry_reference == "close"
    assert rules.profit_target_type == "percentage"
    assert rules.profit_target_value == pytest.approx(4.0)
    assert rules.max_holding_days == 2
    assert rules.stop_atr_multiplier == pytest.approx(3.0)
    assert rules.stop_atr_period == 10


# =====================================================================
# (A) +4% 利確が常駐する
# =====================================================================


def _s2_short(entry=100.0, entry_date="2026-08-19"):
    return PositionSnapshot(
        symbol="ESTC",
        qty=-100.0,
        side="short",
        avg_entry_price=entry,
        market_value=100.0 * entry,
        system="system2",
        entry_date=entry_date,
    )


def _oco_coid(sym="ESTC", date="20260819"):
    return f"protect-system2-{sym}-{date}-protect-oco"


def _stop_coid(sym="ESTC", date="20260819"):
    return f"protect-system2-{sym}-{date}-protect-stop"


class TestTargetRests:
    """spec の +4% 利確 (entry x 0.96 = ショートの利確指値) が常駐すること。"""

    def test_fresh_position_oco_carries_stop_and_target(self, monkeypatch):
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        exits = build_exit_orders_from_positions(
            [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        assert len(exits) == 1
        oco = exits[0]
        assert oco.order_type == "oco"
        assert oco.reason == ExitReasonCode.PROTECT_OCO
        assert oco.skip_reason is None
        # stop = 売値 + 3 * ATR10 (docs 損切り / stop_atr_multiplier=3.0)
        assert oco.stop_price == pytest.approx(106.0)
        # target = 売値 / 1.04 (既存 _target_price_for のショート式。
        # 4% は profit_target_value=4.0 由来で、ここでは変更していない)
        assert oco.limit_price == pytest.approx(96.15)

    def test_standalone_stop_is_upgraded_so_the_target_can_rest(self, monkeypatch):
        """既存建玉に単発 stop が resting でも +4% 利確が常駐に昇格する。

        これが無いと flag を立てても既存建玉には一生 target が張られない
        (2026-08-22 の paper 実測で ON/OFF 差分 0 行だった原因)。
        """
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        exits = build_exit_orders_from_positions(
            [_s2_short()],
            today="2026-08-19",
            atr_by_symbol={"ESTC": {10: 2.0}},
            existing_protect_coids={_stop_coid()},
        )
        assert len(exits) == 1
        oco = exits[0]
        assert oco.order_type == "oco"
        assert oco.skip_reason is None
        assert oco.stop_price == pytest.approx(106.0)  # stop は張られたまま
        assert oco.limit_price == pytest.approx(96.15)  # target が常駐する
        # 単発 stop は qty を全量予約しているので先に外す必要がある
        assert oco.cancel_client_order_ids == [_stop_coid()]

    def test_resting_oco_is_not_resubmitted(self, monkeypatch):
        """OCO が既に resting なら再送しない (毎日 422 duplicate を作らない)。"""
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        exits = build_exit_orders_from_positions(
            [_s2_short()],
            today="2026-08-19",
            atr_by_symbol={"ESTC": {10: 2.0}},
            existing_protect_coids={_oco_coid()},
        )
        assert exits == []

    def test_flag_off_keeps_old_behaviour(self, monkeypatch):
        """既定 (flag なし) は従来どおり stop 1 本 + target 抑止のまま。"""
        monkeypatch.delenv("PROTECT_USE_OCO", raising=False)
        exits = build_exit_orders_from_positions(
            [_s2_short()],
            today="2026-08-19",
            atr_by_symbol={"ESTC": {10: 2.0}},
            existing_protect_coids={_stop_coid()},
        )
        tgt = next(e for e in exits if e.reason == ExitReasonCode.PROTECT_TARGET)
        assert tgt.skip_reason == "qty_reserved:stop_order_already_open"
        assert not getattr(tgt, "cancel_client_order_ids", [])

    def test_failed_upgrade_rearms_the_stop(self, monkeypatch):
        """昇格 OCO が拒否されても建玉を無保護にしない。"""
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        oco = build_exit_orders_from_positions(
            [_s2_short()],
            today="2026-08-19",
            atr_by_symbol={"ESTC": {10: 2.0}},
            existing_protect_coids={_stop_coid()},
        )[0]
        rearm = build_stop_rearm_after_failed_oco(oco)
        assert rearm is not None
        assert rearm.order_type == "stop"
        assert rearm.reason == ExitReasonCode.PROTECT_STOP
        assert rearm.stop_price == pytest.approx(106.0)  # 元と同じ stop 価格
        assert rearm.qty == oco.qty
        assert rearm.side == oco.side
        # coid は使い回せない (Alpaca) ので rearm 専用 suffix
        assert rearm.client_order_id.endswith("protect-stop-rearm")
        assert rearm.client_order_id != _stop_coid()

    def test_rearmed_stop_blocks_a_second_upgrade_attempt(self, monkeypatch):
        """一度失敗した建玉は毎日 cancel->拒否 を繰り返さない。"""
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        rearm_coid = "protect-system2-ESTC-20260819-protect-stop-rearm"
        exits = build_exit_orders_from_positions(
            [_s2_short()],
            today="2026-08-19",
            atr_by_symbol={"ESTC": {10: 2.0}},
            existing_protect_coids={rearm_coid},
        )
        assert all(e.order_type != "oco" for e in exits)
        tgt = next(e for e in exits if e.reason == ExitReasonCode.PROTECT_TARGET)
        assert tgt.skip_reason == "qty_reserved:stop_order_already_open"

    def test_rearm_is_not_built_for_a_fresh_oco(self, monkeypatch):
        """昇格でない (cancel を伴わない) OCO は張り直し対象ではない。"""
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        fresh = build_exit_orders_from_positions(
            [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
        )[0]
        assert fresh.cancel_client_order_ids == []
        assert build_stop_rearm_after_failed_oco(fresh) is None

    def test_trailing_holder_is_never_upgraded(self, monkeypatch):
        """S1/S4 の trailing が握る建玉は OCO 化しない (対象外)。"""
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        snap = PositionSnapshot(
            symbol="ADVB",
            qty=100.0,
            side="long",
            avg_entry_price=100.0,
            market_value=10_000.0,
            system="system1",
            entry_date="2026-08-18",
        )
        exits = build_exit_orders_from_positions(
            [snap],
            today="2026-08-19",
            atr_by_symbol={"ADVB": {20: 2.0}},
            existing_protect_coids={"protect-system1-ADVB-20260818-protect-trail"},
        )
        assert all(e.order_type != "oco" for e in exits)


# =====================================================================
# (B) +4% 指値エントリー
# =====================================================================


def _frame(prev_close: float = 10.0, atr10: float = 0.30) -> pd.DataFrame:
    idx = pd.date_range("2026-08-03", periods=15, freq="B")
    df = pd.DataFrame(
        {
            "Open": [prev_close] * len(idx),
            "High": [prev_close * 1.02] * len(idx),
            "Low": [prev_close * 0.98] * len(idx),
            "Close": [prev_close] * len(idx),
            "ATR10": [atr10] * len(idx),
        },
        index=idx,
    )
    return df


def _next_bday(df: pd.DataFrame) -> pd.Timestamp:
    return df.index[-1] + pd.tseries.offsets.BDay(1)


class TestLimitEntry:
    def test_strategy_exposes_the_documented_limit_price(self):
        s = System2Strategy()
        # docs/systems/システム2.txt 仕掛け: 前日終値を 4% 上回る価格で売る
        assert s.compute_entry_limit_price(10.0) == pytest.approx(10.40)
        assert s.compute_entry_limit_price(0) is None
        assert s.compute_entry_limit_price(None) is None

    def test_live_signal_uses_prev_close_times_1_04(self):
        """当日シグナル (翌日 bar が未到来) で +4% 指値が出る。"""
        df = _frame(prev_close=10.0, atr10=0.30)
        debug: dict = {}
        got = _compute_entry_stop(
            System2Strategy(),
            df,
            {"symbol": "TEST", "entry_date": _next_bday(df)},
            "short",
            debug=debug,
        )
        assert got is not None
        entry, stop = got
        assert entry == pytest.approx(10.40)  # 10.00 x 1.04
        # 損切りは **売値の上** に 3 x ATR10 (docs 損切り)
        assert stop == pytest.approx(10.40 + 3.0 * 0.30)
        assert debug["details"]["entry_source"] == "spec_limit_price"

    @pytest.mark.parametrize(
        "system_module,side,atr_col",
        [
            ("strategies.system1_strategy:System1Strategy", "long", "ATR20"),
            ("strategies.system4_strategy:System4Strategy", "long", "ATR40"),
        ],
    )
    def test_other_systems_live_entry_is_unchanged(self, system_module, side, atr_col):
        """成行 system (S1/S4) は従来どおり offset なしの前日終値のまま。

        NOTE (2026-08-22, S3/S5/S6 fix): S3/S5/S6 はこの本 fix の時点では
        ``prev_close_fallback`` のままで、当テストもそれを固定していた。同日の
        後続 fix で 3 系統にも spec 指値 (S3 x0.93 / S5 x0.97 / S6 x1.05) を
        生やしたため、ここでは成行 spec の S1/S4 だけを残す。3 系統の新しい
        期待値は ``tests/test_system356_live_spec_20260822.py`` が固定する。
        """
        import importlib

        mod_name, cls_name = system_module.split(":")
        cls = getattr(importlib.import_module(mod_name), cls_name)
        df = _frame(prev_close=10.0)
        df[atr_col] = 0.30
        debug: dict = {}
        got = _compute_entry_stop(
            cls(),
            df,
            {"symbol": "TEST", "entry_date": _next_bday(df)},
            side,
            debug=debug,
        )
        assert got is not None
        entry, _stop = got
        assert entry == pytest.approx(10.0)
        assert debug["details"]["entry_source"] == "prev_close_fallback"


class TestOrderEmission:
    def _json(self):
        return {
            "date": "2026-08-22",
            "systems": {
                "sys1": {
                    "signals": [
                        {
                            "symbol": "AAA",
                            "side": "BUY",
                            "entry_price": 50.0,
                            "weight": 0.15,
                        }
                    ]
                },
                "sys2": {
                    "signals": [
                        {
                            "symbol": "BBB",
                            "side": "SELL",
                            "entry_price": 10.4,
                            "limit_price": 10.4,
                            "weight": 0.15,
                        }
                    ]
                },
                "sys3": {
                    "signals": [
                        {
                            "symbol": "CCC",
                            "side": "BUY",
                            "entry_price": 20.0,
                            "weight": 0.15,
                        }
                    ]
                },
                "sys4": {
                    "signals": [
                        {
                            "symbol": "DDD",
                            "side": "BUY",
                            "entry_price": 30.0,
                            "weight": 0.15,
                        }
                    ]
                },
                "sys5": {
                    "signals": [
                        {
                            "symbol": "EEE",
                            "side": "BUY",
                            "entry_price": 40.0,
                            "weight": 0.15,
                        }
                    ]
                },
                "sys6": {
                    "signals": [
                        {
                            "symbol": "FFF",
                            "side": "SELL",
                            "entry_price": 60.0,
                            "weight": 0.15,
                        }
                    ]
                },
                "sys7": {
                    "signals": [
                        {
                            "symbol": "SPY",
                            "side": "SELL",
                            "entry_price": 500.0,
                            "weight": 0.10,
                        }
                    ]
                },
            },
        }

    def test_limit_price_survives_the_json_flattener(self):
        rows = {r["system"]: r for r in _flatten_json_signals(self._json())}
        assert rows["system2"]["limit_price"] == pytest.approx(10.4)
        for other in ("system1", "system3", "system4", "system5", "system6", "system7"):
            assert rows[other]["limit_price"] is None

    def test_system2_emits_a_day_limit_order(self):
        orders = {
            o.system: o
            for o in signals_json_to_orders(
                self._json(), tier="medium", dry_run=True, account_equity=100_000.0
            )
        }
        s2 = orders["system2"]
        assert s2.order_type == "limit"
        assert s2.limit_price == pytest.approx(10.4)
        # 売り指値が翌セッションに残ると当日のシグナルでない建玉を持つので DAY。
        assert s2.time_in_force == "day"
        assert s2.side == "sell"

    def test_other_systems_still_emit_market_orders(self):
        """この JSON は sys2 にしか ``limit_price`` を載せていない。

        S3/S5/S6 は ``_DEFAULT_SYSTEM_ORDER_TYPE`` 上は limit だが、行に指値が
        無いので documented fallback (market) に落ちる — それをここで固定する。
        指値が **載っている** ときの期待値は
        ``tests/test_system356_live_spec_20260822.py`` 側。
        """
        orders = {
            o.system: o
            for o in signals_json_to_orders(
                self._json(), tier="medium", dry_run=True, account_equity=100_000.0
            )
        }
        for other in ("system1", "system3", "system4", "system5", "system6", "system7"):
            assert orders[other].order_type == "market", other
            assert orders[other].limit_price is None, other
            assert orders[other].time_in_force == "day", other

    def test_limit_system_without_a_price_falls_back_to_market(self):
        """limit_price が無い行は成行へ (誤発注防止)。既存の documented fallback。"""
        js = self._json()
        js["systems"]["sys2"]["signals"][0].pop("limit_price")
        orders = {
            o.system: o
            for o in signals_json_to_orders(
                js, tier="medium", dry_run=True, account_equity=100_000.0
            )
        }
        assert orders["system2"].order_type == "market"
        assert orders["system2"].limit_price is None


# =====================================================================
# (C) 保有日数は立会日で数える
# =====================================================================


class TestHoldingDaysAreTradingDays:
    def test_friday_entry_is_one_trading_day_on_monday(self):
        # 2026-08-21 = 金, 2026-08-24 = 月
        assert compute_holding_days("2026-08-21", "2026-08-24") == 1

    def test_friday_entry_reaches_the_two_day_limit_on_tuesday(self):
        assert compute_holding_days("2026-08-21", "2026-08-25") == 2

    def test_weekday_run_is_unchanged(self):
        # 2026-08-17 = 月, 2026-08-19 = 水
        assert compute_holding_days("2026-08-17", "2026-08-19") == 2

    def test_nyse_holiday_is_not_counted(self):
        # 2026-11-26 = Thanksgiving (休場)。11-25(水) -> 11-30(月) は
        # 11-27(金) と 11-30(月) の 2 立会日。暦日なら 5。
        assert compute_holding_days("2026-11-25", "2026-11-30") == 2

    def test_same_day_and_bad_input(self):
        assert compute_holding_days("2026-08-21", "2026-08-21") == 0
        assert compute_holding_days(None, "2026-08-21") is None
        assert compute_holding_days("not-a-date", "2026-08-21") is None

    def test_time_exit_does_not_fire_one_trading_day_early(self):
        """金曜エントリーの System2 は月曜には手仕舞わない (立会 1 日)。"""
        snap = _s2_short(entry_date="2026-08-21")  # 金
        monday = build_exit_orders_from_positions(
            [snap], today="2026-08-24", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        assert all(e.reason != ExitReasonCode.TIME for e in monday)

    def test_time_exit_fires_on_the_second_trading_day(self):
        snap = _s2_short(entry_date="2026-08-21")  # 金
        tuesday = build_exit_orders_from_positions(
            [snap], today="2026-08-25", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        time_exit = next(e for e in tuesday if e.reason == ExitReasonCode.TIME)
        assert time_exit.holding_days == 2
        assert time_exit.max_holding_days == 2
        assert time_exit.order_type == "market"
        assert time_exit.side == "buy"  # short のクローズ
