"""portfolio_guard — off-by-default の portfolio 防衛機構 (Phase 5, 2026-07-07)。

drawdown flatten と sector cap を **純関数** として提供する。config 値が偽
(0 / <=0) の間は必ず no-op を返す。発火は user が config を有効化したときのみ。

方針 (docs/POSITION_MANAGEMENT_PHASE5_20260707.md §3.2):
    - 本 module は判定ロジックのみ。実際の flatten 発注や候補削減の *wiring* は
      呼び出し側 (exit orchestration / allocation) が opt-in で行う。
    - **paper のみ / ライブ発注なし**: 本 module は Alpaca に一切触れない。
    - default は無効。勝手にリスクを締めない。

- evaluate_drawdown_flatten(equity, peak_equity, threshold_pct)
    peak からの drawdown が閾値以上なら FlattenDecision(flatten=True)。
- filter_by_sector_cap(rows, sector_of, cap)
    1 sector あたり cap 件を超える候補を優先度順に落とす。
"""

from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True)
class FlattenDecision:
    """drawdown flatten の判定結果。"""

    flatten: bool
    drawdown_pct: float
    threshold_pct: float
    reason: str


def evaluate_drawdown_flatten(
    equity: Any, peak_equity: Any, threshold_pct: Any
) -> FlattenDecision:
    """peak equity からの drawdown が ``threshold_pct`` 以上なら flatten=True。

    ``threshold_pct <= 0`` は無効 (常に flatten=False = off-by-default)。
    drawdown = max(0, (peak - equity) / peak)。
    """
    try:
        eq = float(equity)
        pk = float(peak_equity)
        th = float(threshold_pct)
    except (TypeError, ValueError):
        return FlattenDecision(False, 0.0, 0.0, "invalid_input")

    if th <= 0:
        return FlattenDecision(False, 0.0, th, "disabled")
    if pk <= 0:
        return FlattenDecision(False, 0.0, th, "no_peak")

    dd = max(0.0, (pk - eq) / pk)
    if dd >= th:
        return FlattenDecision(
            True, round(dd, 4), th, f"drawdown_{dd:.2%}>=threshold_{th:.2%}"
        )
    return FlattenDecision(False, round(dd, 4), th, "within_threshold")


def filter_by_sector_cap(
    rows: Iterable[Any],
    sector_of: Callable[[Any], str | None],
    cap: int,
) -> tuple[list[Any], dict[str, int]]:
    """1 sector あたり ``cap`` 件を超える候補を優先度順に落とす。

    ``cap <= 0`` は無効 (全通過 = off-by-default)。``rows`` は優先度降順で渡す前提
    (先頭が高優先)。``sector_of(row)`` が None を返す行は cap 対象外 (常に通過)。

    戻り値: (kept_rows, dropped_by_sector)。
    """
    row_list = list(rows)
    if cap is None or cap <= 0:
        return row_list, {}

    kept: list[Any] = []
    counts: dict[str, int] = {}
    dropped: dict[str, int] = {}
    for row in row_list:
        try:
            sector = sector_of(row)
        except Exception:
            sector = None
        if not sector:
            kept.append(row)
            continue
        if counts.get(sector, 0) >= cap:
            dropped[sector] = dropped.get(sector, 0) + 1
            continue
        counts[sector] = counts.get(sector, 0) + 1
        kept.append(row)
    return kept, dropped


def load_guard_config() -> dict[str, float]:
    """settings.risk.portfolio から drawdown / sector の設定を読む (欠損は無効値)。"""
    try:
        from config.settings import get_settings

        pf = get_settings().risk.portfolio
        return {
            "drawdown_flatten_pct": float(getattr(pf, "drawdown_flatten_pct", 0.0)),
            "max_positions_per_sector": int(getattr(pf, "max_positions_per_sector", 0)),
        }
    except Exception:
        return {"drawdown_flatten_pct": 0.0, "max_positions_per_sector": 0}


__all__ = [
    "FlattenDecision",
    "evaluate_drawdown_flatten",
    "filter_by_sector_cap",
    "load_guard_config",
]
