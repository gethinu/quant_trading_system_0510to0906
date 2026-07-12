"""drawdown circuit breaker — equity ドローダウンで全 flatten する安全弁 (paper 専用)。

``portfolio_guard.evaluate_drawdown_flatten`` (副作用なしの純判定) の上に薄く重ねて

    1. equity 履歴 (results_csv/alpaca_equity_history.json) からの **peak 解決**
    2. **誤発火防止ガード** (config 無効 / equity 欠損 / 履歴が薄い / 絶対額が小さい)
    3. paper 口座の **flatten 実行** (close_all_positions, cancel_orders=True)

を提供する。**default は完全に無効** (config ``risk.portfolio.drawdown_flatten_pct``
が 0 の間は ``armed=False`` で必ず no-op)。有効化は user が config に閾値を入れた
ときのみ。live 口座には一切触れない (実行系は呼び出し側で ``assert_paper_env`` 済み前提)。

設計方針 (docs/POSITION_MANAGEMENT_PHASE5_20260707.md §3.2 の延長):
    - 判定は純関数 (assess) に閉じ込め、単体テストで全ガードを検証できるようにする。
    - flatten は「1) config 有効 かつ 2) 閾値超え かつ 3) 全ガード通過 かつ
      4) 呼び出し側が --confirm を明示」の 4 条件が揃ったときだけ。
    - 単一の壊れた equity 値で全決済しないよう、履歴点数と絶対ドローダウン額でガード。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
from pathlib import Path
from typing import Any

from common.portfolio_guard import FlattenDecision, evaluate_drawdown_flatten

logger = logging.getLogger(__name__)

# 誤発火防止のデフォルト。値を締めたいときは script 側の flag / config で上書き。
DEFAULT_MIN_HISTORY_POINTS = 5
DEFAULT_MIN_ABS_DRAWDOWN_USD = 0.0  # 0 = 無効 (絶対額ガードを掛けない)

# config の drawdown_flatten_pct を有効化するときの **保守的な提案値**。
# config 自体は 0.0 (無効) のままにしておき、user が opt-in するときの目安に使う。
SUGGESTED_THRESHOLD_PCT = 0.15


@dataclass
class BreakerAssessment:
    """circuit breaker の総合判定 (実行はしない・純データ)。"""

    armed: bool  # config で有効化されているか (threshold > 0)
    breached: bool  # 閾値を超えたか (ガード前の生判定)
    would_flatten: bool  # breached かつ 全ガード通過 → flatten 対象
    equity: float | None
    peak_equity: float | None
    drawdown_pct: float
    threshold_pct: float
    n_history_points: int
    reason: str
    guard_blocks: list[str] = field(default_factory=list)
    decision: FlattenDecision | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "armed": self.armed,
            "breached": self.breached,
            "would_flatten": self.would_flatten,
            "equity": self.equity,
            "peak_equity": self.peak_equity,
            "drawdown_pct": self.drawdown_pct,
            "threshold_pct": self.threshold_pct,
            "n_history_points": self.n_history_points,
            "reason": self.reason,
            "guard_blocks": self.guard_blocks,
        }


def load_equity_history(path: Path | str) -> list[dict[str, Any]]:
    """alpaca_equity_history.json ([{t, equity}, ...]) を読む。壊れてても空 list。"""
    p = Path(path)
    if not p.exists():
        return []
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("equity 履歴の読込に失敗 (無視して空扱い): %s", exc)
        return []
    if not isinstance(data, list):
        return []
    return [row for row in data if isinstance(row, dict)]


def resolve_peak_equity(
    history: list[dict[str, Any]], current_equity: float | None
) -> tuple[float | None, int]:
    """履歴 + 現 equity の最大値を peak とする。履歴の有効点数も返す。

    - 履歴点数 (n) は **現 equity を足す前** の有効点数 = 履歴の厚さ判定に使う。
    - peak には現 equity も含める (新高値なら drawdown=0 になり breach しない)。
    """
    values: list[float] = []
    for row in history:
        try:
            e = float(row.get("equity"))
        except (TypeError, ValueError):
            continue
        if e > 0:
            values.append(e)
    n_points = len(values)
    if current_equity is not None:
        try:
            cur = float(current_equity)
            if cur > 0:
                values.append(cur)
        except (TypeError, ValueError):
            pass
    peak = max(values) if values else None
    return peak, n_points


def assess(
    equity: float | None,
    peak_equity: float | None,
    threshold_pct: float | None,
    *,
    n_history_points: int,
    min_history_points: int = DEFAULT_MIN_HISTORY_POINTS,
    min_abs_drawdown_usd: float = DEFAULT_MIN_ABS_DRAWDOWN_USD,
) -> BreakerAssessment:
    """flatten すべきか総合判定する (副作用なし)。

    誤発火防止:
      - ``threshold_pct <= 0``: config 無効 → armed=False で即 no-op (default)。
      - ``equity`` / ``peak_equity`` が欠損/非正 → guard_block (flatten しない)。
      - 履歴点数が ``min_history_points`` 未満 → 薄い履歴で peak が不確か → guard_block。
      - ``min_abs_drawdown_usd > 0`` かつ 絶対ドローダウン額がそれ未満 → guard_block。
    ``would_flatten`` は「閾値超え かつ ガードが 1 つも掛からない」ときだけ True。
    """
    decision = evaluate_drawdown_flatten(equity, peak_equity, threshold_pct)
    dd = decision.drawdown_pct
    try:
        th = float(threshold_pct) if threshold_pct is not None else 0.0
    except (TypeError, ValueError):
        th = 0.0

    armed = th > 0
    if not armed:
        return BreakerAssessment(
            armed=False,
            breached=False,
            would_flatten=False,
            equity=equity,
            peak_equity=peak_equity,
            drawdown_pct=dd,
            threshold_pct=th,
            n_history_points=n_history_points,
            reason="disabled(threshold<=0)",
            guard_blocks=[],
            decision=decision,
        )

    guard_blocks: list[str] = []
    if equity is None or not (float(equity) > 0):
        guard_blocks.append("no_equity")
    if peak_equity is None or not (float(peak_equity) > 0):
        guard_blocks.append("no_peak")
    if n_history_points < min_history_points:
        guard_blocks.append(f"thin_history({n_history_points}<{min_history_points})")

    breached = bool(decision.flatten)
    if (
        breached
        and min_abs_drawdown_usd > 0
        and peak_equity is not None
        and equity is not None
    ):
        abs_dd = float(peak_equity) - float(equity)
        if abs_dd < min_abs_drawdown_usd:
            guard_blocks.append(
                f"below_abs_usd({abs_dd:.0f}<{min_abs_drawdown_usd:.0f})"
            )

    would_flatten = breached and not guard_blocks
    if not breached:
        reason = decision.reason  # within_threshold / no_peak / disabled 等
    elif guard_blocks:
        reason = "breached_but_guarded:" + ",".join(guard_blocks)
    else:
        reason = decision.reason  # drawdown_XX%>=threshold_YY%

    return BreakerAssessment(
        armed=True,
        breached=breached,
        would_flatten=would_flatten,
        equity=equity,
        peak_equity=peak_equity,
        drawdown_pct=dd,
        threshold_pct=th,
        n_history_points=n_history_points,
        reason=reason,
        guard_blocks=guard_blocks,
        decision=decision,
    )


def flatten_all_paper(client: Any) -> dict[str, Any]:
    """paper 口座の全ポジションを成行 close + open order cancel する。

    **前提**: 呼び出し側で ``assert_paper_env`` 済み。Alpaca ネイティブの
    ``close_all_positions(cancel_orders=True)`` を使い、long/short・端株を broker 側で
    正しく処理させる (side/qty 計算の自作バグを避ける)。open_auto_run の flatten-all と
    同じ実装パターン。戻り値は監視/durable ログ用の要約 dict。
    """
    rows: list[dict[str, Any]] = []
    order_ids: list[str] = []
    ok = 0
    failed = 0
    try:
        resps = client.close_all_positions(cancel_orders=True)
    except Exception as exc:  # noqa: BLE001
        logger.error("close_all_positions 失敗: %s", exc)
        return {"ok": 0, "failed": 0, "error": str(exc), "order_ids": [], "rows": []}

    for r in resps or []:
        sym = getattr(r, "symbol", None)
        st = getattr(r, "status", None)
        raw_oid = getattr(r, "order_id", None)
        oid = str(raw_oid) if raw_oid else None
        if st == 200 and oid:
            ok += 1
            order_ids.append(oid)
        else:
            failed += 1
        rows.append(
            {
                "symbol": sym,
                "order_id": oid,
                "http_status": st,
                "reason": "drawdown_flatten",
            }
        )
    return {"ok": ok, "failed": failed, "order_ids": order_ids, "rows": rows}


__all__ = [
    "BreakerAssessment",
    "DEFAULT_MIN_ABS_DRAWDOWN_USD",
    "DEFAULT_MIN_HISTORY_POINTS",
    "SUGGESTED_THRESHOLD_PCT",
    "assess",
    "flatten_all_paper",
    "load_equity_history",
    "resolve_peak_equity",
]
