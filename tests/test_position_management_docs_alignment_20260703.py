"""Position management / capital allocation / risk override の docs 準拠 regression test.

対象 docs:
  - docs/systems/システム1.txt 〜 システム7.txt (per-system risk / max_pct / max_pos /
    holding / trailing / profit target / stop / entry)
  - docs/systems/INDEX.md (long/short bucket 配分)
  - docs/today_signal_scan/6. 配分・最終リスト生成フェーズ.md (bucket split, default_capital)
  - config/config.yaml (long_allocations, short_allocations, risk.max_positions)

方針: docs = single source of truth。impl が docs から drift した時に test で検出する。
docs に記述の無い項目 (portfolio 総 cap, cross-system dedup, drawdown flatten, sector cap)
は本 test で assert しない (Phase 5 report の future consideration 扱い)。

由来: 2026-07-03 docs-alignment audit dispatch。
参考: docs/POSITION_MANAGEMENT_AUDIT_20260703.md
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.alpaca_trading import _DEFAULT_SYSTEM_ORDER_TYPE, signals_to_orders
from common.trade_management import OrderType, SYSTEM_TRADE_RULES
from core.final_allocation import (
    DEFAULT_LONG_ALLOCATIONS,
    DEFAULT_SHORT_ALLOCATIONS,
)


# =========================================================================
# Cluster A: capital allocation (docs/systems/INDEX.md)
# =========================================================================


class TestCapitalAllocationDocsAlignment:
    """docs/systems/INDEX.md 明記の long/short bucket 配分と impl 一致確認。"""

    def test_long_allocations_match_docs(self):
        # docs/systems/INDEX.md:
        #   System1 = 25%, System3 = 25%, System4 = 25%, System5 = 25%
        assert DEFAULT_LONG_ALLOCATIONS == {
            "system1": 0.25,
            "system3": 0.25,
            "system4": 0.25,
            "system5": 0.25,
        }
        assert sum(DEFAULT_LONG_ALLOCATIONS.values()) == pytest.approx(1.0)

    def test_short_allocations_match_docs(self):
        # docs/systems/INDEX.md:
        #   System2 = 40%, System6 = 40%, System7 = 20%
        assert DEFAULT_SHORT_ALLOCATIONS == {
            "system2": 0.40,
            "system6": 0.40,
            "system7": 0.20,
        }
        assert sum(DEFAULT_SHORT_ALLOCATIONS.values()) == pytest.approx(1.0)


# =========================================================================
# Cluster B: per-system trade rules (docs/systems/システム{N}.txt)
# =========================================================================


class TestPerSystemTradeRulesDocsAlignment:
    """SYSTEM_TRADE_RULES の各 field が docs の spec と一致することを確認。"""

    def test_system1_matches_docs(self):
        # docs/systems/システム1.txt:
        #   仕掛け: 翌日寄付成行 (MARKET/open)
        #   損切: 買値-20日5ATR
        #   利益保護: 25% トレーリング
        #   利食い: 目標なし
        #   ポジション: risk 2%, size max 10%, 最大 10 ポジション
        rule = SYSTEM_TRADE_RULES["system1"]
        assert rule.side == "long"
        assert rule.entry_type == OrderType.MARKET
        assert rule.entry_reference == "open"
        assert rule.stop_atr_period == 20
        assert rule.stop_atr_multiplier == 5.0
        assert rule.use_trailing_stop is True
        assert rule.trailing_stop_pct == 0.25
        assert rule.profit_target_type == "none"
        assert rule.risk_pct == 0.02
        assert rule.max_pct == 0.10
        # docs: max_holding_days 記述なし = 時間 exit 無し
        assert rule.max_holding_days == 0

    def test_system2_matches_docs(self):
        # docs/systems/システム2.txt:
        #   仕掛け: 前日終値+4%以上の指値売 (LIMIT +4%)
        #   損切: 売値+10日3ATR
        #   利益保護: 使わない
        #   利食い: 4%の利益で翌日大引け、2日後まで到達しなければ手仕舞い
        #   ポジション: risk 2%, size max 10%, 最大 10 ポジション
        rule = SYSTEM_TRADE_RULES["system2"]
        assert rule.side == "short"
        assert rule.entry_type == OrderType.LIMIT
        assert rule.entry_price_offset_pct == 4.0
        assert rule.entry_reference == "close"
        assert rule.stop_atr_period == 10
        assert rule.stop_atr_multiplier == 3.0
        assert rule.use_trailing_stop is False
        assert rule.profit_target_type == "percentage"
        assert rule.profit_target_value == 4.0
        assert rule.max_holding_days == 2
        assert rule.risk_pct == 0.02
        assert rule.max_pct == 0.10

    def test_system3_matches_docs(self):
        # docs/systems/システム3.txt:
        #   仕掛け: 前日終値-7% 指値買 (LIMIT -7%)
        #   損切: 買値-10日2.5ATR
        #   利益保護: 使わない
        #   利食い: 4%の利益で翌日大引け、3日超えなら手仕舞い
        #   ポジション: risk 2%, size max 10%
        rule = SYSTEM_TRADE_RULES["system3"]
        assert rule.side == "long"
        assert rule.entry_type == OrderType.LIMIT
        assert rule.entry_price_offset_pct == -7.0
        assert rule.stop_atr_period == 10
        assert rule.stop_atr_multiplier == 2.5
        assert rule.use_trailing_stop is False
        assert rule.profit_target_type == "percentage"
        assert rule.profit_target_value == 4.0
        assert rule.max_holding_days == 3
        assert rule.risk_pct == 0.02
        assert rule.max_pct == 0.10

    def test_system4_matches_docs(self):
        # docs/systems/システム4.txt:
        #   仕掛け: 寄付成行 (MARKET/open)
        #   損切: 買値-40日1.5ATR
        #   利益保護: 20% トレーリング
        #   利食い: 使わない
        #   ポジション: risk 2%, size max 10%
        rule = SYSTEM_TRADE_RULES["system4"]
        assert rule.side == "long"
        assert rule.entry_type == OrderType.MARKET
        assert rule.entry_reference == "open"
        assert rule.stop_atr_period == 40
        assert rule.stop_atr_multiplier == 1.5
        assert rule.use_trailing_stop is True
        assert rule.trailing_stop_pct == 0.20
        assert rule.profit_target_type == "none"
        assert rule.max_holding_days == 0
        assert rule.risk_pct == 0.02
        assert rule.max_pct == 0.10

    def test_system5_matches_docs(self):
        # docs/systems/システム5.txt:
        #   仕掛け: 前日終値-3% 指値買 (LIMIT -3%)
        #   損切: 買値-10日3ATR
        #   利益保護: 使わない
        #   利食い: 過去10日1ATR 目標で翌日寄付、6日後まで未達なら翌日寄付手仕舞い
        #   ポジション: risk 2%, size max 10%
        rule = SYSTEM_TRADE_RULES["system5"]
        assert rule.side == "long"
        assert rule.entry_type == OrderType.LIMIT
        assert rule.entry_price_offset_pct == -3.0
        assert rule.stop_atr_period == 10
        assert rule.stop_atr_multiplier == 3.0
        assert rule.use_trailing_stop is False
        assert rule.profit_target_type == "atr"
        assert rule.profit_target_value == 1.0
        assert rule.profit_target_atr_period == 10
        assert rule.max_holding_days == 6
        assert rule.risk_pct == 0.02
        assert rule.max_pct == 0.10

    def test_system6_matches_docs(self):
        # docs/systems/システム6.txt:
        #   仕掛け: 前日終値+5% 指値売 (LIMIT +5%)
        #   損切: 売値+10日3ATR
        #   利益保護: 使わない
        #   利食い: 5%の利益で翌日大引け、3日後には大引け手仕舞い
        #   ポジション: risk 2%, size max 10%
        rule = SYSTEM_TRADE_RULES["system6"]
        assert rule.side == "short"
        assert rule.entry_type == OrderType.LIMIT
        assert rule.entry_price_offset_pct == 5.0
        assert rule.stop_atr_period == 10
        assert rule.stop_atr_multiplier == 3.0
        assert rule.use_trailing_stop is False
        assert rule.profit_target_type == "percentage"
        assert rule.profit_target_value == 5.0
        assert rule.max_holding_days == 3
        assert rule.risk_pct == 0.02
        assert rule.max_pct == 0.10

    def test_system7_is_absent_from_trade_rules(self):
        # docs/systems/システム7.txt: SPY 固定 catastrophe hedge。stop = 過去50日3ATR、
        # 70日高値 breakout で翌寄手仕舞い。この rule は strategies/system7_strategy.py
        # 側で持つ (独自 compute_exit)。SYSTEM_TRADE_RULES.get("system7") は None が期待値
        # (audit-remediation 2026-07-02 の finding Part4 で stub 削除済)。
        assert SYSTEM_TRADE_RULES.get("system7") is None


# =========================================================================
# Cluster C: entry order type mapping (docs vs alpaca_trading)
# =========================================================================


class TestEntryOrderTypeMapDocsAlignment:
    """`_DEFAULT_SYSTEM_ORDER_TYPE` が docs の「仕掛け」節と一致することを確認。

    2026-07-03 の docs-alignment audit で S3=market/S5=market/S7=limit の 3 件
    ミスマッチを是正 (S3/S5→limit, S7→market)。regression 予防。
    """

    def test_system1_market(self):
        # docs/systems/システム1.txt: 翌日の寄り付きで成り行きで仕掛ける。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system1"] == "market"

    def test_system2_limit(self):
        # docs/systems/システム2.txt: 翌日、前日の終値を4%以上上回る価格で売る。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system2"] == "limit"

    def test_system3_limit(self):
        # docs/systems/システム3.txt: 前日の終値の7%下に指値注文を入れる。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system3"] == "limit"

    def test_system4_market(self):
        # docs/systems/システム4.txt: 寄り付きで成り行きで仕掛ける。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system4"] == "market"

    def test_system5_limit(self):
        # docs/systems/システム5.txt: 前日の終値の3%下に指値をして買う。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system5"] == "limit"

    def test_system6_limit(self):
        # docs/systems/システム6.txt: 前日の終値を5%上回る位置に指値を置いて売る。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system6"] == "limit"

    def test_system7_market(self):
        # docs/systems/システム7.txt: 翌日の寄り付きで成り行きで仕掛ける。
        assert _DEFAULT_SYSTEM_ORDER_TYPE["system7"] == "market"


# =========================================================================
# Cluster D: docs-driven end-to-end signals_to_orders behavior
# =========================================================================


class TestSignalsToOrdersDocsAlignment:
    """docs 通りの entry_price 指定なら期待される limit/market が発行される。"""

    def test_system3_creates_limit_order(self):
        # docs: S3 = 前日終値-7% 指値買。final_allocation が entry_price を計算した状態を模擬。
        df = pd.DataFrame(
            [
                {
                    "symbol": "AMD",
                    "system": "system3",
                    "side": "long",
                    "shares": 5,
                    "entry_price": 140.0,
                    "entry_date": "2026-07-03",
                }
            ]
        )
        orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
        assert len(orders) == 1
        assert orders[0].order_type == "limit"
        assert orders[0].limit_price == 140.0
        assert orders[0].side == "buy"

    def test_system5_creates_limit_order(self):
        df = pd.DataFrame(
            [
                {
                    "symbol": "NVDA",
                    "system": "system5",
                    "side": "long",
                    "shares": 4,
                    "entry_price": 120.0,
                    "entry_date": "2026-07-03",
                }
            ]
        )
        orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
        assert len(orders) == 1
        assert orders[0].order_type == "limit"
        assert orders[0].limit_price == 120.0

    def test_system7_creates_market_order(self):
        # docs: S7 = 翌日寄付成行 (MARKET)。限定注文だと 50 日安値割れの瞬間を逃す。
        df = pd.DataFrame(
            [
                {
                    "symbol": "SPY",
                    "system": "system7",
                    "side": "short",
                    "shares": 3,
                    "entry_price": 545.0,
                    "entry_date": "2026-07-03",
                }
            ]
        )
        orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
        assert len(orders) == 1
        assert orders[0].order_type == "market"
        # market order でも entry_price 参照は残るが、limit_price は None
        assert orders[0].limit_price is None
        assert orders[0].side == "sell"


# =========================================================================
# Cluster E: cross-system same-symbol dedup (impl behavior; docs 未明記)
# =========================================================================


class TestCrossSystemDedupBehavior:
    """cross-system で同一 symbol が候補になった時の impl 振る舞いを固定化。

    NOTE: docs は cross-system dedup を明記しない。impl は `chosen_symbols` set
    (final_allocation._allocate_by_capital) と `seen` set (signals_to_orders) で
    独自 dedup を実装している。この test はその振る舞いを "現行仕様" として
    lock in するもの (docs 追記の議論は Phase 5 report 参照)。
    """

    def test_signals_to_orders_dedups_same_system_symbol_date(self):
        # (symbol, system, entry_date) tuple が重複したら 1 注文に統合。
        df = pd.DataFrame(
            [
                {
                    "symbol": "AAPL",
                    "system": "system1",
                    "side": "long",
                    "shares": 10,
                    "entry_price": 195.0,
                    "entry_date": "2026-07-03",
                },
                {
                    "symbol": "AAPL",
                    "system": "system1",
                    "side": "long",
                    "shares": 10,
                    "entry_price": 195.0,
                    "entry_date": "2026-07-03",
                },
            ]
        )
        orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
        assert len(orders) == 1

    def test_signals_to_orders_allows_same_symbol_different_system(self):
        # (symbol, system, entry_date) tuple が違えば別注文で通す (現行仕様)。
        # AAPL が S1 の buy candidate と S3 の buy candidate 両方に上がった場合、
        # signals_to_orders はそのまま 2 注文を通す (最終 dedup は final_allocation
        # の chosen_symbols で担保される)。
        df = pd.DataFrame(
            [
                {
                    "symbol": "AAPL",
                    "system": "system1",
                    "side": "long",
                    "shares": 5,
                    "entry_price": 195.0,
                    "entry_date": "2026-07-03",
                },
                {
                    "symbol": "AAPL",
                    "system": "system3",
                    "side": "long",
                    "shares": 4,
                    "entry_price": 180.0,
                    "entry_date": "2026-07-03",
                },
            ]
        )
        orders = signals_to_orders(df, account_equity=100000.0, dry_run=True)
        assert len(orders) == 2
        systems = {o.system for o in orders}
        assert systems == {"system1", "system3"}
