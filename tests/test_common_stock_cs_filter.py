"""Unit tests for the 2026-07-13 Polygon-`type=CS` common-stock filter.

Covers common.symbol_universe.filter_to_common_stock / get_common_stock_set.
All tests are network-free: the authoritative CS set is injected explicitly
(cs_set=...) or the fetch is monkeypatched, so CI never calls Polygon.
"""

from __future__ import annotations

import json

import pytest


class TestFilterToCommonStock:
    def test_keeps_cs_drops_non_cs(self):
        from common.symbol_universe import filter_to_common_stock

        cs = {"AAPL", "MSFT", "F"}
        raw = ["AAPL", "BABA", "MSFT", "QQQ", "F", "ABLVW"]
        # BABA(ADR), QQQ(ETF), ABLVW(warrant) not in CS -> dropped
        assert filter_to_common_stock(raw, cs_set=cs) == ["AAPL", "MSFT", "F"]

    def test_always_keeps_spy_even_if_not_cs(self):
        from common.symbol_universe import filter_to_common_stock

        cs = {"AAPL"}
        # SPY is an ETF (not in cs) but must survive for System7
        out = filter_to_common_stock(["AAPL", "SPY", "QQQ"], cs_set=cs)
        assert out == ["AAPL", "SPY"]

    def test_custom_always_keep(self):
        from common.symbol_universe import filter_to_common_stock

        cs = {"AAPL"}
        out = filter_to_common_stock(
            ["AAPL", "QQQ", "IWM"], cs_set=cs, always_keep=("QQQ", "IWM")
        )
        assert out == ["AAPL", "QQQ", "IWM"]

    def test_order_preserved_and_deduped_upper(self):
        from common.symbol_universe import filter_to_common_stock

        cs = {"AAPL", "MSFT"}
        out = filter_to_common_stock(
            ["msft", "AAPL", "MSFT", "aapl"], cs_set=cs, always_keep=()
        )
        assert out == ["MSFT", "AAPL"]

    def test_empty_input(self):
        from common.symbol_universe import filter_to_common_stock

        assert filter_to_common_stock([], cs_set={"AAPL"}) == []

    def test_fallback_to_pattern_when_cs_set_unavailable(self, monkeypatch):
        """cs_set empty -> pattern filter (is_common_stock_symbol) is used."""
        import common.symbol_universe as su

        monkeypatch.setattr(su, "get_common_stock_set", lambda **kw: set())
        # '.W'/'.U' dotted suffixes are what the pattern filter DOES catch
        raw = ["AAPL", "FOO.W", "BAR.U", "MSFT", "SPY"]
        out = su.filter_to_common_stock(raw, cs_set=None)
        assert out == ["AAPL", "MSFT", "SPY"]


class TestGetCommonStockSet:
    def test_disable_env_returns_empty(self, monkeypatch):
        import common.symbol_universe as su

        monkeypatch.setenv("POLYGON_CS_FILTER_DISABLE", "1")
        assert su.get_common_stock_set() == set()

    def test_reads_fresh_disk_cache_without_network(self, monkeypatch, tmp_path):
        import datetime as dt

        import common.symbol_universe as su

        cache = tmp_path / "polygon_common_stocks.json"
        cache.write_text(
            json.dumps(
                {
                    "as_of": dt.date.today().isoformat(),
                    "count": 3,
                    "tickers": ["AAPL", "MSFT", "F"],
                }
            ),
            encoding="utf-8",
        )
        monkeypatch.setattr(su, "_polygon_cs_cache_path", lambda settings=None: cache)

        def _boom(**kw):  # network must NOT be called for a fresh cache
            raise AssertionError("network fetch should not run for fresh cache")

        monkeypatch.setattr(su, "fetch_polygon_common_stock_set", _boom)
        monkeypatch.delenv("POLYGON_CS_FILTER_DISABLE", raising=False)
        assert su.get_common_stock_set() == {"AAPL", "MSFT", "F"}

    def test_stale_disk_cache_triggers_refetch(self, monkeypatch, tmp_path):
        import common.symbol_universe as su

        cache = tmp_path / "polygon_common_stocks.json"
        cache.write_text(
            json.dumps({"as_of": "2000-01-01", "count": 1, "tickers": ["OLD"]}),
            encoding="utf-8",
        )
        monkeypatch.setattr(su, "_polygon_cs_cache_path", lambda settings=None: cache)
        monkeypatch.setattr(
            su, "fetch_polygon_common_stock_set", lambda **kw: {"NEW", "AAPL"}
        )
        monkeypatch.delenv("POLYGON_CS_FILTER_DISABLE", raising=False)
        got = su.get_common_stock_set(max_age_days=7)
        assert got == {"NEW", "AAPL"}
        # cache should have been rewritten with fresh data
        payload = json.loads(cache.read_text(encoding="utf-8"))
        assert set(payload["tickers"]) == {"AAPL", "NEW"}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
