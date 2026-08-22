"""orphan を「帰属欠落」と「exit 発注不能」に分けることの検証。

2026-08-18 の run で CDTX / FOLD が ORPHAN/UNMANAGED として警告された。
「position_tracker/symbol_system_map/entry-coid のいずれにも無い」という同じ文言に
括られていたが、broker に問い合わせると両銘柄とも ``tradable=False`` (上場廃止) で、
**system 帰属を直しても保護注文は API では永久に出せない**。

「帰属が無い (直せば守れる)」と「そもそも exit を出せない (手動対応が要る)」を
同じ orphan として扱うと、前者だと誤読されて放置される。到達可能性で分類を分ける。
確認できない時は断定しない (誤って「対応不要」と見せない)。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import paper_exit_check as pec  # noqa: E402


def _rows():
    return [
        {"symbol": "CDTX", "market_value": 2213.8},
        {"symbol": "FOLD", "market_value": 2072.07},
        {"symbol": "LIVE", "market_value": 500.0},
    ]


def _patch(monkeypatch, mapping):
    monkeypatch.setattr(
        pec, "probe_asset_tradable", lambda sym, client=None: mapping.get(sym)
    )


def test_untradable_gets_its_own_classification(monkeypatch):
    _patch(monkeypatch, {"CDTX": False, "FOLD": False, "LIVE": True})
    rows = _rows()
    counts = pec.refine_orphan_classifications(rows)
    assert counts == {"untradable": 2, "tradable": 1, "unknown": 0}
    assert rows[0]["classification"] == "untradable_no_exit_possible"
    assert rows[1]["classification"] == "untradable_no_exit_possible"


def test_tradable_orphan_keeps_attribution_problem(monkeypatch):
    """取引可能な orphan は「帰属を直せば守れる」ので分類を書き換えない。"""
    _patch(monkeypatch, {"LIVE": True})
    rows = [{"symbol": "LIVE", "classification": "orphan_no_system_origin"}]
    pec.refine_orphan_classifications(rows)
    assert rows[0]["classification"] == "orphan_no_system_origin"
    assert rows[0]["tradable"] is True


def test_unknown_tradability_is_not_asserted(monkeypatch):
    """broker に届かない時は untradable と断定しない (誤って安心させない)。"""
    _patch(monkeypatch, {"X": None})
    rows = [{"symbol": "X"}]
    counts = pec.refine_orphan_classifications(rows)
    assert counts["unknown"] == 1
    assert rows[0]["classification"] == "orphan_no_system_origin_tradability_unknown"
    assert rows[0]["tradable"] is None


def test_empty_symbol_is_skipped(monkeypatch):
    _patch(monkeypatch, {})
    rows = [{"symbol": ""}]
    counts = pec.refine_orphan_classifications(rows)
    assert counts == {"untradable": 0, "tradable": 0, "unknown": 0}


def test_probe_never_places_orders(monkeypatch):
    """判定は read-only。発注 API を触らないこと。"""
    calls = []

    class FakeClient:
        def get_asset(self, symbol):
            calls.append(("get_asset", symbol))

            class A:
                tradable = False

            return A()

        def submit_order(self, *a, **k):  # pragma: no cover - 呼ばれたら失敗
            raise AssertionError("probe が発注 API を呼んだ")

    from common.alpaca_trading import probe_asset_tradable

    assert probe_asset_tradable("CDTX", FakeClient()) is False
    assert calls == [("get_asset", "CDTX")]


@pytest.mark.parametrize("exc", [RuntimeError("boom"), ValueError("bad")])
def test_probe_failure_returns_unknown_not_false(exc):
    """問い合わせ失敗を「取引不可」と混同しない。"""

    class Failing:
        def get_asset(self, symbol):
            raise exc

    from common.alpaca_trading import probe_asset_tradable

    assert probe_asset_tradable("X", Failing()) is None
