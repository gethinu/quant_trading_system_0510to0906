"""drawdown サーキットブレーカ (common/drawdown_breaker) の判定 + 誤発火ガード検証。

config 無効 (threshold<=0) が最優先で no-op、履歴が薄い/equity 欠損では絶対に
would_flatten=True にしないことを固定化する (paper-only 安全弁の要)。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.drawdown_breaker import (  # noqa: E402
    assess,
    load_equity_history,
    resolve_peak_equity,
)


# --- disabled by default (最重要) -----------------------------------------
def test_disabled_when_threshold_zero():
    a = assess(80_000, 100_000, 0.0, n_history_points=20)
    assert a.armed is False
    assert a.would_flatten is False
    assert a.reason == "disabled(threshold<=0)"


def test_disabled_when_threshold_none():
    a = assess(80_000, 100_000, None, n_history_points=20)
    assert a.armed is False
    assert a.would_flatten is False


# --- fire path -------------------------------------------------------------
def test_would_flatten_when_breached_and_healthy():
    a = assess(80_000, 100_000, 0.15, n_history_points=20)
    assert a.armed is True
    assert a.breached is True
    assert a.would_flatten is True
    assert a.drawdown_pct == 0.20


def test_within_threshold_no_flatten():
    a = assess(90_000, 100_000, 0.15, n_history_points=20)
    assert a.breached is False
    assert a.would_flatten is False
    assert a.reason == "within_threshold"


# --- misfire guards --------------------------------------------------------
def test_thin_history_blocks_flatten():
    a = assess(80_000, 100_000, 0.15, n_history_points=3, min_history_points=5)
    assert a.breached is True  # 生判定は breach
    assert a.would_flatten is False  # だがガードで抑止
    assert any("thin_history" in g for g in a.guard_blocks)


def test_no_equity_blocks_flatten():
    a = assess(None, 100_000, 0.15, n_history_points=20)
    assert a.would_flatten is False
    assert "no_equity" in a.guard_blocks


def test_no_peak_blocks_flatten():
    a = assess(80_000, 0, 0.15, n_history_points=20)
    assert a.would_flatten is False
    assert "no_peak" in a.guard_blocks


def test_abs_usd_guard_blocks_small_drawdown():
    # 20% だが絶対額は $200 → min_abs_drawdown_usd=$1000 で抑止
    a = assess(800, 1000, 0.15, n_history_points=20, min_abs_drawdown_usd=1000)
    assert a.breached is True
    assert a.would_flatten is False
    assert any("below_abs_usd" in g for g in a.guard_blocks)


def test_abs_usd_guard_allows_large_drawdown():
    a = assess(80_000, 100_000, 0.15, n_history_points=20, min_abs_drawdown_usd=1000)
    assert a.would_flatten is True  # $20k > $1k ガード通過


# --- peak resolution -------------------------------------------------------
def test_resolve_peak_uses_history_and_current():
    hist = [
        {"t": "2026-07-07", "equity": 100_000},
        {"t": "2026-07-10", "equity": 106_000},
    ]
    peak, n = resolve_peak_equity(hist, 90_000)
    assert peak == 106_000  # 履歴の最大 (現 equity 90k は下回る)
    assert n == 2  # 現 equity を足す前の履歴点数


def test_resolve_peak_current_is_new_high():
    hist = [{"t": "2026-07-07", "equity": 100_000}]
    peak, n = resolve_peak_equity(hist, 120_000)
    assert peak == 120_000  # 新高値は current から
    assert n == 1


def test_resolve_peak_ignores_garbage_rows():
    hist = [{"t": "x", "equity": "bad"}, {"equity": 0}, {"equity": 105_000}]
    peak, n = resolve_peak_equity(hist, None)
    assert peak == 105_000
    assert n == 1  # 有効点は 105k のみ


def test_new_high_never_flattens_end_to_end():
    # 現 equity が新高値 → drawdown 0 → armed でも flatten しない
    hist = [{"t": "a", "equity": 100_000}]
    peak, n = resolve_peak_equity(hist, 130_000)
    a = assess(130_000, peak, 0.15, n_history_points=n, min_history_points=1)
    assert a.would_flatten is False


def test_load_equity_history_missing_file(tmp_path: Path):
    assert load_equity_history(tmp_path / "nope.json") == []


def test_load_equity_history_reads_list(tmp_path: Path):
    p = tmp_path / "hist.json"
    p.write_text(
        json.dumps([{"t": "a", "equity": 1}, "junk", {"t": "b"}]), encoding="utf-8"
    )
    out = load_equity_history(p)
    assert out == [{"t": "a", "equity": 1}, {"t": "b"}]  # dict 以外は除去
