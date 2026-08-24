"""cap_equity — portfolio cap の equity 基準を実口座 equity に連動させる (flag-gated)。

背景 (2026-08-20):
    ``core/final_allocation.py::_apply_portfolio_caps`` の gross/net exposure cap は
    docs (``docs/POSITION_MANAGEMENT_PHASE5_20260707.md`` §2 / §3.1) で
    「``gross / equity``」「``|net| / equity``」= **equity 比**と定義されている。
    しかし実装は ``equity_base = default_capital`` を使い、本番の呼び出し
    (``scripts/run_all_systems_today.py`` の ``finalize_allocation(...)``) が
    ``default_capital`` を渡さないため signature 既定の **100000.0 に落ちていた**。
    結果 net cap は常に ``100000 * 0.5 = $50,000`` 固定で、実 equity と乖離する。

本 module は「実 equity をどこから取るか」だけを担う。**既定は完全 OFF** で、
OFF の間は ``(None, "disabled")`` を返し、呼び出し側は従来どおり
``default_capital`` を使う (= 現行挙動と bit 一致)。

    - ``CAP_USE_REAL_EQUITY``  master flag。既定 OFF。
    - ``CAP_EQUITY_USD``       明示上書き (replay / test 用)。flag ON のときのみ有効。

解決順 (flag ON 時):
    1. ``CAP_EQUITY_USD`` 環境変数
    2. Alpaca paper 口座の equity (**read-only GET**。発注は一切しない)
    3. ``results_csv/alpaca_snapshot_<date>.json`` の ``account.equity`` (最新)
    4. 解決不能 → ``(None, "unresolved")`` → 呼び出し側が従来値へ退避

**paper のみ / ライブ発注なし**: 本 module は口座情報の読み取りしか行わない。
"""

from __future__ import annotations

import json
import logging
import os
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

FLAG_ENV = "CAP_USE_REAL_EQUITY"
OVERRIDE_ENV = "CAP_EQUITY_USD"

_TRUTHY = {"1", "true", "yes", "on", "y", "t"}


def cap_real_equity_enabled(env: dict[str, str] | None = None) -> bool:
    """``CAP_USE_REAL_EQUITY`` が truthy なら True。未設定/空は **False (既定 OFF)**。"""
    src = os.environ if env is None else env
    return str(src.get(FLAG_ENV, "") or "").strip().lower() in _TRUTHY


def _positive_float(value: Any) -> float | None:
    try:
        v = float(value)
    except (TypeError, ValueError):
        return None
    if v != v or v in (float("inf"), float("-inf")):  # NaN / inf
        return None
    return v if v > 0 else None


def _equity_from_env() -> float | None:
    raw = os.environ.get(OVERRIDE_ENV, "")
    if not str(raw).strip():
        return None
    return _positive_float(raw)


def _equity_from_alpaca() -> float | None:
    """Alpaca paper 口座 equity の read-only 取得。失敗は None (例外を上げない)。"""
    try:
        from common.alpaca_trading import fetch_account_equity

        return _positive_float(fetch_account_equity())
    except Exception as exc:  # noqa: BLE001 - equity 解決失敗で allocation を壊さない
        logger.warning("[CAP_EQUITY] Alpaca equity 取得失敗: %s", exc)
        return None


def _snapshot_dir(results_dir: Path | str | None) -> Path:
    if results_dir is not None:
        return Path(results_dir)
    try:
        from config.settings import get_settings

        return Path(get_settings(create_dirs=False).outputs.results_csv_dir)
    except Exception:  # noqa: BLE001
        return Path("results_csv")


def _equity_from_snapshot(
    results_dir: Path | str | None = None,
) -> tuple[float | None, str]:
    """最新の ``alpaca_snapshot_*.json`` から ``account.equity`` を読む。"""
    try:
        base = _snapshot_dir(results_dir)
        files = sorted(base.glob("alpaca_snapshot_*.json"), reverse=True)
    except Exception as exc:  # noqa: BLE001
        logger.warning("[CAP_EQUITY] snapshot 探索に失敗: %s", exc)
        return None, ""
    for fp in files:
        try:
            with fp.open(encoding="utf-8") as fh:
                payload = json.load(fh)
            eq = _positive_float((payload.get("account") or {}).get("equity"))
            if eq is not None:
                return eq, fp.stem.replace("alpaca_snapshot_", "")
        except Exception:  # noqa: BLE001 - 壊れた snapshot は skip して次を見る
            continue
    return None, ""


def resolve_cap_equity(
    *,
    allow_fetch: bool | None = None,
    results_dir: Path | str | None = None,
) -> tuple[float | None, str]:
    """portfolio cap の equity 基準を解決する。

    Returns:
        ``(equity, source)``。flag OFF なら ``(None, "disabled")``、
        解決できなければ ``(None, "unresolved")``。呼び出し側は ``None`` のとき
        **従来どおり ``default_capital`` を使うこと** (後方互換)。

    ``allow_fetch=None`` のときは ``TEST_MODE`` が設定されていれば Alpaca を
    叩かない (テストを決定論的に保つ。``resolve_sizing_equity`` と同じ流儀)。
    """
    if not cap_real_equity_enabled():
        return None, "disabled"

    eq = _equity_from_env()
    if eq is not None:
        return eq, f"env:{OVERRIDE_ENV}"

    if allow_fetch is None:
        allow_fetch = not os.environ.get("TEST_MODE")
    if allow_fetch:
        eq = _equity_from_alpaca()
        if eq is not None:
            return eq, "alpaca"

    eq, stamp = _equity_from_snapshot(results_dir)
    if eq is not None:
        return eq, f"snapshot:{stamp}" if stamp else "snapshot"

    logger.warning(
        "[CAP_EQUITY] 実 equity を解決できず default_capital へ退避 (flag は ON)"
    )
    return None, "unresolved"


__all__ = [
    "FLAG_ENV",
    "OVERRIDE_ENV",
    "cap_real_equity_enabled",
    "resolve_cap_equity",
]
