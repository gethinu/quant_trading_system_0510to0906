"""Phase 5 (2026-07-07) portfolio_guard の off-by-default + 発火検証。"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.portfolio_guard import (  # noqa: E402
    evaluate_drawdown_flatten,
    filter_by_sector_cap,
)


# --- drawdown flatten -----------------------------------------------------
def test_drawdown_disabled_by_default():
    d = evaluate_drawdown_flatten(equity=70, peak_equity=100, threshold_pct=0.0)
    assert d.flatten is False
    assert d.reason == "disabled"


def test_drawdown_fires_when_exceeded():
    d = evaluate_drawdown_flatten(equity=69, peak_equity=100, threshold_pct=0.30)
    assert d.flatten is True
    assert d.drawdown_pct == 0.31


def test_drawdown_within_threshold_no_flatten():
    d = evaluate_drawdown_flatten(equity=80, peak_equity=100, threshold_pct=0.30)
    assert d.flatten is False
    assert d.reason == "within_threshold"


def test_drawdown_invalid_input():
    d = evaluate_drawdown_flatten(equity=None, peak_equity=100, threshold_pct=0.3)
    assert d.flatten is False


# --- sector cap -----------------------------------------------------------
def _sector_of(row):
    return row.get("sector")


def test_sector_cap_disabled_by_default():
    rows = [{"symbol": f"A{i}", "sector": "tech"} for i in range(10)]
    kept, dropped = filter_by_sector_cap(rows, _sector_of, cap=0)
    assert len(kept) == 10
    assert dropped == {}


def test_sector_cap_limits_per_sector():
    rows = [{"symbol": f"T{i}", "sector": "tech"} for i in range(5)]
    rows += [{"symbol": f"E{i}", "sector": "energy"} for i in range(2)]
    kept, dropped = filter_by_sector_cap(rows, _sector_of, cap=3)
    tech_kept = [r for r in kept if r["sector"] == "tech"]
    assert len(tech_kept) == 3  # tech は 3 で頭打ち
    assert dropped.get("tech") == 2
    assert (
        len([r for r in kept if r["sector"] == "energy"]) == 2
    )  # energy は 2 <= 3 全通過


def test_sector_none_always_passes():
    rows = [{"symbol": f"X{i}", "sector": None} for i in range(10)]
    kept, dropped = filter_by_sector_cap(rows, _sector_of, cap=2)
    assert len(kept) == 10  # sector 不明は cap 対象外
    assert dropped == {}
