"""System3 / System5 / System6 の live 指値エントリーを **ドキュメント spec** に戻した回帰テスト。

`tests/test_system2_live_spec_20260822.py` (バグ B) の S3/S5/S6 版。S2 の fix が
「S3/S5/S6 の emitter が limit_price を出していないので既存の documented fallback
(market) に落ちる。将来 emitter が spec 指値を出すようになれば、そこだけ直せば
自動的に指値になる」と残していた課題 (docs/SYSTEM2_LIVE_SPEC_FIX_20260822.md §2
「既知の残課題」) を閉じる。

対象 spec (すべて repo 内の既存記述。ここで新しい数字は作らない):

  docs/systems/システム3.txt:19  仕掛け: 「前日の終値の7%下に指値注文を入れる。」
  docs/systems/システム5.txt:20  仕掛け: 「前日の終値の3%下に指値をして買う。」
  docs/systems/システム6.txt:33  仕掛け: 「前日の終値を5%上回る位置に指値を置いて売る。」

  config/config.yaml
      system3.entry_price_ratio_vs_prev_close: 0.93
      system5.entry_price_ratio_vs_prev_close: 0.97
      system6.entry_price_ratio_vs_prev_close: 1.05

  common/trade_management.py (SYSTEM_TRADE_RULES)
      system3: entry_type=LIMIT / entry_price_offset_pct=-7.0 / reference="close"
      system5: entry_type=LIMIT / entry_price_offset_pct=-3.0 / reference="close"
      system6: entry_type=LIMIT / entry_price_offset_pct=+5.0 / reference="close"

  common/alpaca_trading.py (docs-alignment コメント / _DEFAULT_SYSTEM_ORDER_TYPE)
      「S3 = 前日終値-7% 指値買 (LIMIT)」「S5 = 前日終値-3% 指値買 (LIMIT)」
      「S6 = 前日終値+5% 指値売 (LIMIT)」

修正したバグ:
  live 経路 (``common/today_signals._compute_entry_stop``) は entry_date の bar が
  df に無い (= 翌日の注文を今日作る) ため ``strategy.compute_entry`` が必ず None を
  返し、entry_price が **オフセットなしの前日終値** (ratio=1.0000 /
  entry_source=prev_close_fallback) になっていた。limit_price が空なので
  ``signals_json_to_orders`` は documented fallback で market を出していた。
  2026-08-20 の実 artifact でも sys3 8 件 / sys5 10 件すべての entry_price が
  素の Close と完全一致 (ratio 1.0000)、limit_price は null。

バックテストは無変更 (``compute_entry`` に触れていない)。本ファイルの
``TestBacktestPathUntouched`` が live 指値と backtest 指値の一致を固定する。
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest
import yaml

from common.alpaca_trading import (
    EXEC_QTY,
    _DEFAULT_SYSTEM_ORDER_TYPE,
    _flatten_json_signals,
    plan_order_execution,
    signals_json_to_orders,
)
from common.today_signals import _compute_entry_stop
from common.trade_management import SYSTEM_TRADE_RULES
from strategies.system3_strategy import System3Strategy
from strategies.system5_strategy import System5Strategy
from strategies.system6_strategy import System6Strategy

_REPO = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------
# spec 表 — この 3 つの数字だけが「新しく足した値」ではないことを、下の
# test_spec_values_exist_in_repo が 4 つの独立した出所すべてに突き合わせる。
# ---------------------------------------------------------------------
SPECS = {
    "system3": {
        "cls": System3Strategy,
        "side": "long",
        "signal_side": "BUY",
        "ratio": 0.93,
        "offset_pct": -7.0,
        "stop_mult": 2.5,
        "doc": "docs/systems/システム3.txt",
        "doc_phrase": "前日の終値の7%下に指値注文を入れる。",
    },
    "system5": {
        "cls": System5Strategy,
        "side": "long",
        "signal_side": "BUY",
        "ratio": 0.97,
        "offset_pct": -3.0,
        "stop_mult": 3.0,
        "doc": "docs/systems/システム5.txt",
        "doc_phrase": "前日の終値の3%下に指値をして買う。",
    },
    "system6": {
        "cls": System6Strategy,
        "side": "short",
        "signal_side": "SELL",
        "ratio": 1.05,
        "offset_pct": 5.0,
        "stop_mult": 3.0,
        "doc": "docs/systems/システム6.txt",
        "doc_phrase": "前日の終値を5%上回る位置に指値を置いて売る。",
    },
}

SYSTEMS = sorted(SPECS)


# =====================================================================
# invented number の検出器
# =====================================================================


@pytest.mark.parametrize("system", SYSTEMS)
def test_spec_values_exist_in_repo(system):
    """使った ratio が **4 つの独立した既存 spec すべて** から出ることを固定する。

    どれか 1 つでも欠けたら (= どこにも書かれていない数字を使ったら) 落ちる。
    """
    spec = SPECS[system]

    # (1) 機械可読 spec: common/trade_management.py
    rules = SYSTEM_TRADE_RULES[system]
    assert rules.entry_type.value.lower() == "limit", system
    assert rules.entry_reference == "close", system
    assert rules.entry_price_offset_pct == pytest.approx(spec["offset_pct"]), system
    # common/trade_management.py:567/599 と同じ式で ratio へ変換する
    derived = 1.0 + (rules.entry_price_offset_pct / 100.0)
    assert derived == pytest.approx(spec["ratio"]), system

    # (2) 運用 config: config/config.yaml
    cfg = yaml.safe_load((_REPO / "config" / "config.yaml").read_text(encoding="utf-8"))
    yaml_ratio = cfg["strategies"][system]["entry_price_ratio_vs_prev_close"]
    assert float(yaml_ratio) == pytest.approx(spec["ratio"]), system

    # (3) canonical spec doc の原文
    doc = (_REPO / spec["doc"]).read_text(encoding="utf-8")
    assert spec["doc_phrase"] in doc, f"{system}: {spec['doc']} に仕掛けの原文が無い"

    # (4) 注文種別の single source of truth
    assert _DEFAULT_SYSTEM_ORDER_TYPE[system] == "limit", system

    # 損切り倍率も spec 由来であること (指値からの stop 距離に効く)
    assert rules.stop_atr_multiplier == pytest.approx(spec["stop_mult"]), system
    assert rules.stop_atr_period == 10, system
    assert float(cfg["strategies"][system]["stop_atr_multiple"]) == pytest.approx(
        spec["stop_mult"]
    ), system


@pytest.mark.parametrize("system", SYSTEMS)
def test_side_direction_matches_the_spec(system):
    """long は前日終値より **下**、short は **上** に指値を置く。"""
    spec = SPECS[system]
    if spec["side"] == "long":
        assert spec["ratio"] < 1.0, system
        assert spec["cls"]().compute_entry_limit_price(100.0) < 100.0
    else:
        assert spec["ratio"] > 1.0, system
        assert spec["cls"]().compute_entry_limit_price(100.0) > 100.0


# =====================================================================
# spec 指値エントリー
# =====================================================================


def _frame(prev_close: float = 10.0, atr10: float = 0.30) -> pd.DataFrame:
    idx = pd.date_range("2026-08-03", periods=15, freq="B")
    return pd.DataFrame(
        {
            "Open": [prev_close] * len(idx),
            "High": [prev_close * 1.02] * len(idx),
            "Low": [prev_close * 0.98] * len(idx),
            "Close": [prev_close] * len(idx),
            "ATR10": [atr10] * len(idx),
        },
        index=idx,
    )


def _next_bday(df: pd.DataFrame) -> pd.Timestamp:
    return df.index[-1] + pd.tseries.offsets.BDay(1)


class TestLimitEntry:
    @pytest.mark.parametrize("system", SYSTEMS)
    def test_strategy_exposes_the_documented_limit_price(self, system):
        spec = SPECS[system]
        s = spec["cls"]()
        assert s.compute_entry_limit_price(10.0) == pytest.approx(
            round(10.0 * spec["ratio"], 2)
        )
        # 不正入力は指値を作らない (成行フォールバックへ落とす)
        assert s.compute_entry_limit_price(0) is None
        assert s.compute_entry_limit_price(-1.0) is None
        assert s.compute_entry_limit_price(None) is None
        assert s.compute_entry_limit_price("abc") is None

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_live_signal_uses_the_spec_ratio(self, system):
        """当日シグナル (翌日 bar が未到来) で spec 指値が出る。"""
        spec = SPECS[system]
        df = _frame(prev_close=10.0, atr10=0.30)
        debug: dict = {}
        got = _compute_entry_stop(
            spec["cls"](),
            df,
            {"symbol": "TEST", "entry_date": _next_bday(df)},
            spec["side"],
            debug=debug,
        )
        assert got is not None
        entry, stop = got
        assert entry == pytest.approx(round(10.0 * spec["ratio"], 2))
        # 損切りは long なら買値の下、short なら売値の上に stop_mult x ATR10
        if spec["side"] == "long":
            assert stop == pytest.approx(entry - spec["stop_mult"] * 0.30)
        else:
            assert stop == pytest.approx(entry + spec["stop_mult"] * 0.30)
        assert debug["details"]["entry_source"] == "spec_limit_price"
        assert debug["details"]["entry_prev_close"] == pytest.approx(10.0)

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_ratio_comes_from_config_not_a_hardcoded_constant(self, system):
        """config の ratio を差し替えたら指値も追随する (数字を焼き付けていない)。"""
        s = SPECS[system]["cls"]()
        s.config["entry_price_ratio_vs_prev_close"] = 0.5
        assert s.compute_entry_limit_price(10.0) == pytest.approx(5.0)

    @pytest.mark.parametrize(
        "system_module,side,atr_col",
        [
            ("strategies.system1_strategy:System1Strategy", "long", "ATR20"),
            ("strategies.system4_strategy:System4Strategy", "long", "ATR40"),
        ],
    )
    def test_market_systems_live_entry_is_unchanged(self, system_module, side, atr_col):
        """成行 system (S1/S4) は従来どおり offset なしの前日終値のまま。"""
        import importlib

        mod_name, cls_name = system_module.split(":")
        cls = getattr(importlib.import_module(mod_name), cls_name)
        df = _frame(prev_close=10.0)
        df[atr_col] = 0.30
        debug: dict = {}
        got = _compute_entry_stop(
            cls(), df, {"symbol": "TEST", "entry_date": _next_bday(df)}, side, debug=debug
        )
        assert got is not None
        entry, _stop = got
        assert entry == pytest.approx(10.0)
        assert debug["details"]["entry_source"] == "prev_close_fallback"

    def test_system2_limit_entry_is_untouched(self):
        """先行の S2 fix (+4% 指値売) を壊していない。"""
        from strategies.system2_strategy import System2Strategy

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
        assert got[0] == pytest.approx(10.40)
        assert debug["details"]["entry_source"] == "spec_limit_price"


def _reach_limit(df: pd.DataFrame, system: str) -> pd.DataFrame:
    """最終バーを **指値に到達する** レンジへ広げる。

    main の f8fbba4 (`fix(backtest): System3/5/6 の指値エントリーを到達判定つきに`)
    以降、backtest の ``compute_entry`` は当日バーが指値へ届いたかを
    ``StrategyBase._limit_entry_filled`` で判定する (long は ``Low <= limit``、
    short は ``High >= limit``)。``_frame`` の素のバー (±2%) は S3 の -7% / S5 の
    -3% / S6 の +5% のいずれにも届かないので、指値の **値** を比べたいテストでは
    到達するバーを作る必要がある。届かないバーが None になること自体は
    ``test_backtest_entry_is_none_when_the_bar_never_reaches_the_limit`` が固定する。
    """
    spec = SPECS[system]
    limit = round(float(df["Close"].iloc[-2]) * spec["ratio"], 2)
    out = df.copy()
    last = out.index[-1]
    if spec["side"] == "long":
        out.loc[last, "Low"] = limit - 0.05
    else:
        out.loc[last, "High"] = limit + 0.05
    return out


class TestBacktestPathUntouched:
    """``compute_entry`` (バックテスト経路) は無変更で、live 指値と同じ値を出す。"""

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_live_limit_equals_the_backtest_limit(self, system):
        spec = SPECS[system]
        df = _reach_limit(_frame(prev_close=10.0, atr10=0.30), system)
        strat = spec["cls"]()
        # entry_date の bar が df に **ある** = backtest 経路
        entry_date = df.index[-1]
        comp = strat.compute_entry(df, {"symbol": "TEST", "entry_date": entry_date}, 0.0)
        assert comp is not None, f"{system}: compute_entry が None"
        bt_entry, _bt_stop = comp
        live_limit = strat.compute_entry_limit_price(float(df["Close"].iloc[-2]))
        assert live_limit == pytest.approx(bt_entry), (
            f"{system}: live 指値 {live_limit} が backtest 指値 {bt_entry} と不一致"
        )

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_backtest_entry_is_none_when_the_bar_never_reaches_the_limit(self, system):
        """到達しないバーは建玉にしない (main f8fbba4 の約定判定を固定する)。

        live 指値の復元 (738834b) と backtest の到達判定 (f8fbba4) は別ブランチで
        並行に入ったので、統合後に **両方** 効いていることをここで固定する。
        """
        df = _frame(prev_close=10.0, atr10=0.30)  # ±2% レンジ = どの指値にも未到達
        strat = SPECS[system]["cls"]()
        comp = strat.compute_entry(
            df, {"symbol": "TEST", "entry_date": df.index[-1]}, 0.0
        )
        assert comp is None, f"{system}: 未到達バーで建玉が作られている"


# =====================================================================
# 注文の emit
# =====================================================================


def _json(with_limits=("sys2", "sys3", "sys5", "sys6")):
    """7 system ぶんの signals JSON。limit_price は with_limits の系統だけに載せる。"""
    rows = {
        "sys1": ("AAA", "BUY", 50.0, None),
        "sys2": ("BBB", "SELL", 10.4, 10.4),
        "sys3": ("CCC", "BUY", 18.6, 18.6),
        "sys4": ("DDD", "BUY", 30.0, None),
        "sys5": ("EEE", "BUY", 38.8, 38.8),
        "sys6": ("FFF", "SELL", 63.0, 63.0),
        "sys7": ("SPY", "SELL", 500.0, None),
    }
    systems = {}
    for key, (sym, side, entry, limit) in rows.items():
        sig = {"symbol": sym, "side": side, "entry_price": entry, "weight": 0.15}
        if limit is not None and key in with_limits:
            sig["limit_price"] = limit
        systems[key] = {"signals": [sig]}
    return {"date": "2026-08-22", "systems": systems}


def _orders(js):
    return {
        o.system: o
        for o in signals_json_to_orders(
            js, tier="medium", dry_run=True, account_equity=100_000.0
        )
    }


class TestOrderEmission:
    def test_limit_price_survives_the_json_flattener(self):
        rows = {r["system"]: r for r in _flatten_json_signals(_json())}
        for system in SYSTEMS + ["system2"]:
            assert rows[system]["limit_price"] is not None, system
        for market_sys in ("system1", "system4", "system7"):
            assert rows[market_sys]["limit_price"] is None, market_sys

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_limit_system_emits_a_day_limit_order(self, system):
        orders = _orders(_json())
        po = orders[system]
        assert po.order_type == "limit", system
        assert po.limit_price is not None, system
        # 指値が翌セッションに残ると当日のシグナルでない建玉を持つので DAY。
        assert po.time_in_force == "day", system
        expected_side = "buy" if SPECS[system]["side"] == "long" else "sell"
        assert po.side == expected_side, system

    def test_system2_still_emits_its_limit_order(self):
        po = _orders(_json())["system2"]
        assert po.order_type == "limit"
        assert po.limit_price == pytest.approx(10.4)
        assert po.time_in_force == "day"
        assert po.side == "sell"

    def test_market_systems_still_emit_market_orders(self):
        orders = _orders(_json())
        for other in ("system1", "system4", "system7"):
            assert orders[other].order_type == "market", other
            assert orders[other].limit_price is None, other
            assert orders[other].time_in_force == "day", other

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_limit_system_without_a_price_falls_back_to_market(self, system):
        """limit_price が無い行は成行へ (誤発注防止)。既存の documented fallback。"""
        keep = tuple(k for k in ("sys2", "sys3", "sys5", "sys6") if k != f"sys{system[-1]}")
        orders = _orders(_json(with_limits=keep))
        assert orders[system].order_type == "market", system
        assert orders[system].limit_price is None, system

    @pytest.mark.parametrize("system", SYSTEMS)
    def test_end_to_end_todaysignal_to_order(self, system):
        """TodaySignal -> signal_export JSON -> flattener -> PreparedOrder。

        emitter が付けた ``limit_price`` が 1 度も落ちずに注文まで届くこと。
        """
        from common.signal_export import build_signals_json
        from common.today_signals import TodaySignal

        spec = SPECS[system]
        limit = round(10.0 * spec["ratio"], 2)
        signal_type = "buy" if spec["side"] == "long" else "sell"
        ts = TodaySignal(
            symbol="ZZZ",
            system=system,
            side=spec["side"],
            signal_type=signal_type,
            entry_date=pd.Timestamp("2026-08-24"),
            entry_price=limit,
            limit_price=limit,
            stop_price=(limit - 1.0 if spec["side"] == "long" else limit + 1.0),
        )
        row = {f: getattr(ts, f) for f in ts.__dataclass_fields__}
        frame = pd.DataFrame([row])
        js = build_signals_json(frame, {system: frame}, date_str="2026-08-24")

        exported = js["systems"][f"sys{system[-1]}"]["signals"][0]
        assert exported["limit_price"] == pytest.approx(limit), system

        po = _orders(js)[system]
        assert po.order_type == "limit", system
        assert po.limit_price == pytest.approx(limit), system
        assert po.time_in_force == "day", system
        assert po.side == signal_type, system

    def test_long_limit_orders_take_the_whole_share_path(self):
        """指値は notional (成行専用) 経路に落とせない。

        S3/S5 は long なので、成行だった頃は fractionable 銘柄で notional 経路に
        乗っていた。指値になった今は必ず整数株 (EXEC_QTY) 側へ寄る
        (``signals_json_to_orders`` の ``prefer_fractional and
        po.order_type != "limit"`` ガード)。落とすと long の指値が黙って成行に化ける。
        """
        mode, qty, _notional, reason = plan_order_execution(
            side="buy",
            notional_usd=1_000.0,
            price=18.6,
            fractionable=True,
            prefer_fractional=False,
        )
        assert mode == EXEC_QTY
        assert qty == int(1_000.0 // 18.6)
        assert "prefer_qty" in reason
