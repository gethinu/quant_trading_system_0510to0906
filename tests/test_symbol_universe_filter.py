"""Unit + integration tests for the 2026-07-02 universe filter (item 4).

Coverage:
    - common.symbol_universe.is_common_stock_symbol / filter_common_stocks
      (pattern-based filter, no network)
    - scripts.daily_polygon_monitor.apply_common_stock_filter
      (grouped_df row filter)
    - scripts.cache_daily_polygon CLI contract: --common-only default True,
      --no-common-only present.
"""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest


@pytest.fixture(scope="module")
def _repo_root() -> Path:
    return Path(__file__).resolve().parents[1]


# ----- Layer 1: pure filter --------------------------------------------------


class TestIsCommonStockSymbol:
    def test_common_stock_pass(self):
        from common.symbol_universe import is_common_stock_symbol

        for sym in ("AAPL", "MSFT", "SPY", "BRK.A", "BRK.B", "GOOGL", "F"):
            assert is_common_stock_symbol(sym), f"{sym} should pass"

    def test_preferred_dollar_sign_rejected(self):
        from common.symbol_universe import is_common_stock_symbol

        for sym in ("AAB$P", "BAC$K", "$FOO"):
            assert not is_common_stock_symbol(sym), f"{sym} should be rejected"

    def test_warrants_rejected(self):
        from common.symbol_universe import is_common_stock_symbol

        for sym in ("AAC.W", "FOO.WS", "BAR.WI"):
            assert not is_common_stock_symbol(sym), f"{sym} should be rejected"

    def test_units_rejected(self):
        from common.symbol_universe import is_common_stock_symbol

        for sym in ("SPAC.U", "AAAA.UN"):
            assert not is_common_stock_symbol(sym), f"{sym} should be rejected"

    def test_rights_notes_rejected(self):
        from common.symbol_universe import is_common_stock_symbol

        for sym in ("FOO.R", "BAR.RT", "BAZ.N", "QUX.NT"):
            assert not is_common_stock_symbol(sym), f"{sym} should be rejected"

    def test_empty_and_none_rejected(self):
        from common.symbol_universe import is_common_stock_symbol

        for sym in (None, "", "   ", "$$$", "..."):
            assert not is_common_stock_symbol(sym), f"{sym!r} should be rejected"

    def test_case_insensitive(self):
        from common.symbol_universe import is_common_stock_symbol

        assert is_common_stock_symbol("aapl")
        assert not is_common_stock_symbol("foo.w")


class TestFilterCommonStocks:
    def test_filter_preserves_order_and_dedupes(self):
        from common.symbol_universe import filter_common_stocks

        raw = ["AAPL", "AAC.U", "MSFT", "BAC$K", "AAPL", "aapl", "SPY.W", "F"]
        assert filter_common_stocks(raw) == ["AAPL", "MSFT", "F"]

    def test_filter_empty_input(self):
        from common.symbol_universe import filter_common_stocks

        assert filter_common_stocks([]) == []


# ----- Layer 2: grouped_df filter (monitor script) ---------------------------


class TestApplyCommonStockFilterMonitor:
    def test_filters_out_non_common_from_grouped(self):
        from scripts.daily_polygon_monitor import apply_common_stock_filter

        # 8 銘柄中 5 が common, 3 が non-common
        idx = ["AAPL", "MSFT", "AAC.U", "BAR.W", "SPY", "F", "GOOGL", "BAC$K"]
        df = pd.DataFrame(
            {
                "Open": range(len(idx)),
                "High": range(len(idx)),
                "Low": range(len(idx)),
                "Close": range(len(idx)),
                "Volume": range(len(idx)),
            },
            index=pd.Index(idx, name="symbol"),
        )
        filtered = apply_common_stock_filter(df)
        assert list(filtered.index) == ["AAPL", "MSFT", "SPY", "F", "GOOGL"]
        assert filtered.shape[0] == 5

    def test_empty_grouped_passes_through(self):
        from scripts.daily_polygon_monitor import apply_common_stock_filter

        empty = pd.DataFrame()
        got = apply_common_stock_filter(empty)
        assert got is empty or got.empty

    def test_none_grouped_passes_through(self):
        from scripts.daily_polygon_monitor import apply_common_stock_filter

        assert apply_common_stock_filter(None) is None


# ----- Layer 3: CLI contract on the backfill script --------------------------


class TestCacheDailyPolygonCliContract:
    """Ensure --common-only flag exists and defaults to True."""

    def test_argparser_has_common_only_flag(self):
        import scripts.cache_daily_polygon as cdp

        parser = cdp.build_arg_parser()
        # parse a minimal valid invocation
        ns = parser.parse_args(["--start", "2026-07-01", "--end", "2026-07-01"])
        assert (
            getattr(ns, "common_only", None) is True
        ), "--common-only は default True (安全側)。この失敗は defaults 変更を示唆"

    def test_no_common_only_flag_flips(self):
        import scripts.cache_daily_polygon as cdp

        parser = cdp.build_arg_parser()
        ns = parser.parse_args(
            [
                "--start",
                "2026-07-01",
                "--end",
                "2026-07-01",
                "--no-common-only",
            ]
        )
        assert (
            ns.common_only is False
        ), "--no-common-only で pattern filter を無効化できないと debug 経路が塞がる"


class TestDailyPolygonMonitorCliContract:
    def test_argparser_has_common_only_flag(self):
        import scripts.daily_polygon_monitor as dpm

        parser = dpm.build_arg_parser()
        ns = parser.parse_args([])
        assert getattr(ns, "common_only", None) is True

    def test_no_common_only_flag_flips(self):
        import scripts.daily_polygon_monitor as dpm

        parser = dpm.build_arg_parser()
        ns = parser.parse_args(["--no-common-only"])
        assert ns.common_only is False
