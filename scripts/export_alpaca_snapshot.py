"""Read-only Alpaca snapshot wrapper with authoritative trailing protection data.

The pre-2026-09-10 exporter is preserved verbatim as
``export_alpaca_snapshot_legacy.py``. This layer changes only S1/S4 trailing
protection observation/presentation data. Broker OPEN orders win, then broker
HWM, then (for legacy fractional positions only) the intraday soft-monitor HWM.
It never fabricates a trailing threshold from entry ATR and never uses a $0.01
display floor.

All accounting, P&L, ledger, exposure and win-rate calculations remain delegated
to the preserved exporter unchanged.
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

_BOOTSTRAP_ROOT = Path(__file__).resolve().parents[1]
if str(_BOOTSTRAP_ROOT) not in sys.path:
    sys.path.insert(0, str(_BOOTSTRAP_ROOT))

from common.alpaca_trading import PositionSnapshot, protective_stop_price
from common.system5_live_exit import (
    SYSTEM5,
    SYSTEM5_TARGET_NEXT_OPEN,
    evaluate_system5_state,
)
from common.system5_live_exit import load_history as load_system5_history
from common.trade_management import SYSTEM_TRADE_RULES
from scripts import export_alpaca_snapshot_legacy as _legacy

_LEGACY_BUILD_SNAPSHOT = _legacy.build_snapshot
ROOT = _legacy.ROOT
PAPER_BASE = _legacy.PAPER_BASE
SCHEMA = _legacy.SCHEMA
PROVIDER = _legacy.PROVIDER
# Keep the canonical session-P&L helper explicitly visible through this wrapper.
# Existing source guards use this symbol to prove the bad equity-last_equity
# calculation has not replaced the preserved accounting implementation.
resolve_session_pnl = _legacy.resolve_session_pnl
_QTY_EPS = 1e-6
_SOFT_STATUS_MAX_AGE_SECONDS = 90 * 60


def __getattr__(name: str) -> Any:
    """Keep the legacy exporter helper surface compatible for callers/tests."""
    return getattr(_legacy, name)


def _f(value: Any) -> float | None:
    try:
        if value in (None, ""):
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def _enum_value(value: Any) -> str:
    raw = getattr(value, "value", value)
    return str(raw or "").strip().lower().split(".")[-1]


def _is_fractional(qty: Any) -> bool:
    q = _f(qty)
    return bool(q is not None and abs(abs(q) - round(abs(q))) > _QTY_EPS)


def _target_only(
    *, side: str, avg_entry: float, rules: Any, atr: dict[int, float]
) -> float | None:
    _legacy_stop, target = _legacy._estimate_stop_target(
        side=side, avg_entry=avg_entry, rules=rules, atr=atr
    )
    return target


def _estimate_stop_target(
    *,
    side: str,
    avg_entry: float,
    rules: Any,
    atr: dict[int, float],
    actual_stop: float | None = None,
    hwm: float | None = None,
) -> tuple[float | None, float | None]:
    """Resolve stop truth without allowing ATR to impersonate a trailing stop.

    Fixed-stop systems delegate to the preserved canonical execution math.
    Trailing systems resolve strictly as broker resting stop -> observed HWM
    times canonical trail width -> unknown.  ``atr`` is accepted for target
    compatibility only and is never used to fabricate a trailing threshold.
    """
    if not getattr(rules, "use_trailing_stop", False):
        return _legacy._estimate_stop_target(
            side=side, avg_entry=avg_entry, rules=rules, atr=atr
        )

    target = _target_only(side=side, avg_entry=avg_entry, rules=rules, atr=atr)
    stop = _f(actual_stop)
    if stop is not None and stop > 0:
        return stop, target

    observed_hwm = _f(hwm)
    trail_pct = _f(getattr(rules, "trailing_stop_pct", None))
    if observed_hwm is None or observed_hwm <= 0 or trail_pct is None:
        return None, target
    if not 0 < trail_pct < 1:
        return None, target

    side_n = str(side or "").strip().lower()
    if side_n == "long":
        return observed_hwm * (1.0 - trail_pct), target
    if side_n == "short":
        return observed_hwm * (1.0 + trail_pct), target
    return None, target


def _parse_aware_timestamp(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(timezone.utc)


def _load_fresh_soft_status(
    *, now: datetime | None = None
) -> dict[str, dict[str, Any]]:
    """Load the newest fresh soft-monitor status from the shared results dir.

    The clean runtime keeps mutable HWM state outside the git worktree.  The
    monitor already publishes a measured status JSON into ``results_csv`` (a
    junction to the PRIMARY runtime data), so the dashboard can consume that as
    evidence without dirtying the clean clone.  Status older than 90 minutes is
    rejected instead of being treated as current protection evidence.
    """
    stamp = now or datetime.now(timezone.utc)
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    stamp = stamp.astimezone(timezone.utc)
    status_dir = ROOT / "results_csv"
    try:
        candidates = sorted(
            status_dir.glob("soft_trailing_status_*.json"), reverse=True
        )
    except OSError:
        return {}

    for target in candidates[:3]:
        try:
            raw = json.loads(target.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if not isinstance(raw, dict):
            continue
        generated_at = _parse_aware_timestamp(raw.get("generated_at"))
        if generated_at is None:
            continue
        age_seconds = (stamp - generated_at).total_seconds()
        if age_seconds < -300 or age_seconds > _SOFT_STATUS_MAX_AGE_SECONDS:
            continue
        rows = raw.get("rows")
        if not isinstance(rows, list):
            continue
        out: dict[str, dict[str, Any]] = {}
        for row in rows:
            if not isinstance(row, dict):
                continue
            symbol = str(row.get("symbol") or "").upper()
            hwm = _f(row.get("hwm"))
            trail_pct = _f(row.get("trail_pct"))
            if not symbol or hwm is None or hwm <= 0:
                continue
            if trail_pct is None or not 0 < trail_pct < 1:
                continue
            out[symbol] = {
                "highest_price": hwm,
                "trailing_stop_pct": trail_pct,
                "last_update": generated_at.isoformat(),
                "source": "soft_trailing_status",
            }
        if out:
            return out
    return {}


def _load_soft_state(path: Path | None = None) -> dict[str, dict[str, Any]]:
    target = path or (ROOT / "data" / "trailing_stops.json")
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        raw = {}
    state = raw if isinstance(raw, dict) else {}
    if state or path is not None:
        return state
    return _load_fresh_soft_status()


def _iter_order_tree(order: Any):
    yield order
    for leg in getattr(order, "legs", None) or []:
        yield from _iter_order_tree(leg)


def get_open_order_protection(client: Any) -> dict[str, Any]:
    """Observe broker-resident OPEN protection orders, read-only.

    Failure is deliberately represented as ``measured=False`` rather than an
    empty order set: API failure must never be rendered as verified absence.
    """
    observed_at = datetime.now(timezone.utc).isoformat()
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        orders = client.get_orders(
            GetOrdersRequest(status=QueryOrderStatus.OPEN, nested=True, limit=500)
        )
    except Exception as exc:
        return {
            "measured": False,
            "observed_at": observed_at,
            "error": str(exc),
            "by_symbol": {},
        }

    by_symbol: dict[str, list[dict[str, Any]]] = {}
    for parent in orders or []:
        for order in _iter_order_tree(parent):
            symbol = str(getattr(order, "symbol", "") or "").upper()
            if not symbol:
                continue
            order_type = _enum_value(getattr(order, "type", None))
            order_class = _enum_value(getattr(order, "order_class", None))
            if order_type not in {
                "trailing_stop",
                "stop",
                "stop_limit",
            } and order_class not in {
                "bracket",
                "oco",
                "oto",
            }:
                continue
            trail_percent = _f(getattr(order, "trail_percent", None))
            trail_price = _f(getattr(order, "trail_price", None))
            hwm = _f(getattr(order, "hwm", None))
            stop_price = _f(getattr(order, "stop_price", None))
            if order_type == "trailing_stop":
                observed = "trailing"
            elif order_class in {"bracket", "oco", "oto"}:
                observed = "oco"
            else:
                observed = "stop"
            by_symbol.setdefault(symbol, []).append(
                {
                    "protection_observed": observed,
                    "order_type": order_type or None,
                    "order_class": order_class or None,
                    "resting_client_order_id": str(
                        getattr(order, "client_order_id", "") or ""
                    )
                    or None,
                    "trail_percent": trail_percent,
                    "trail_price": trail_price,
                    "hwm": hwm,
                    "broker_stop_price": stop_price,
                }
            )
    return {
        "measured": True,
        "observed_at": observed_at,
        "error": None,
        "by_symbol": by_symbol,
    }


def _canonical_trail_pct(position: dict[str, Any]) -> float | None:
    system = str(position.get("system") or "").lower()
    rules = SYSTEM_TRADE_RULES.get(system)
    value = _f(getattr(rules, "trailing_stop_pct", None)) if rules is not None else None
    if value is None:
        value = _f(position.get("trailing_stop_pct"))
    return value if value is not None and 0 < value < 1 else None


def _entry_day(position: dict[str, Any], today: str) -> bool:
    entry = str(position.get("entry_date") or "")[:10]
    return bool(entry and entry == today)


def _best_observed(rows: list[dict[str, Any]]) -> dict[str, Any] | None:
    if not rows:
        return None
    order = {"trailing": 0, "oco": 1, "stop": 2}
    return sorted(
        rows, key=lambda row: order.get(str(row.get("protection_observed")), 99)
    )[0]


def _apply_protection_truth(
    snapshot: dict[str, Any],
    *,
    observation: dict[str, Any],
    soft_state: dict[str, dict[str, Any]],
) -> dict[str, Any]:
    """Overlay trailing protection truth without touching accounting blocks."""
    measured = bool(observation.get("measured"))
    observed_at = observation.get("observed_at")
    by_symbol = observation.get("by_symbol") or {}
    today = str(snapshot.get("date") or "")[:10]

    for position in snapshot.get("positions") or []:
        if str(position.get("exit_type") or "") != "trailing":
            continue
        symbol = str(position.get("symbol") or "").upper()
        trail_pct = _canonical_trail_pct(position)
        position["trailing_stop_pct"] = trail_pct
        position["protection_observed_at"] = observed_at
        position["protection_observation_measured"] = measured
        position["protection_observation_error"] = (
            observation.get("error") if not measured else None
        )
        position["protection_verified"] = False
        position["resting_client_order_id"] = None
        position["broker_hwm"] = None
        position["broker_stop_price"] = None
        position["stop_price_source"] = None

        fractional = _is_fractional(position.get("qty"))
        observed = (
            _best_observed(list(by_symbol.get(symbol) or [])) if measured else None
        )
        if observed is not None:
            position["protection_observed"] = observed.get("protection_observed")
            position["resting_client_order_id"] = observed.get(
                "resting_client_order_id"
            )
            position["broker_hwm"] = observed.get("hwm")
            position["broker_stop_price"] = observed.get("broker_stop_price")
            broker_trail_pct = _f(observed.get("trail_percent"))
            if broker_trail_pct is not None:
                broker_trail_pct /= 100.0
            effective_pct = broker_trail_pct or trail_pct
            broker_stop = _f(observed.get("broker_stop_price")) or _f(
                observed.get("trail_price")
            )
            broker_hwm = _f(observed.get("hwm"))
            if broker_stop is not None:
                position["stop_price_est"] = broker_stop
                position["stop_price_source"] = "broker_order"
            elif broker_hwm is not None and effective_pct is not None:
                position["stop_price_est"] = broker_hwm * (1.0 - effective_pct)
                position["stop_price_source"] = "broker_hwm"
            else:
                position["stop_price_est"] = None
            position["protection_verified"] = True
            position["protection_state"] = "broker_verified"
            if broker_trail_pct is not None:
                position["trailing_stop_pct"] = broker_trail_pct
        elif fractional:
            soft = (
                soft_state.get(symbol)
                if isinstance(soft_state.get(symbol), dict)
                else {}
            )
            hwm = _f(soft.get("highest_price"))
            soft_pct = _f(soft.get("trailing_stop_pct")) or trail_pct
            if hwm is not None and soft_pct is not None:
                position["stop_price_est"] = hwm * (1.0 - soft_pct)
                position["stop_price_source"] = "soft_hwm"
                position["protection_observed"] = "soft"
                position["protection_state"] = "soft_monitor"
            else:
                position["stop_price_est"] = None
                position["protection_observed"] = "none" if measured else "unmeasured"
                position["protection_state"] = (
                    "soft_pending" if measured else "unmeasured"
                )
        elif not measured:
            position["stop_price_est"] = None
            position["protection_observed"] = "unmeasured"
            position["protection_state"] = "unmeasured"
        else:
            position["stop_price_est"] = None
            position["protection_observed"] = "none"
            position["protection_state"] = (
                "pending_arm" if _entry_day(position, today) else "missing_after_arm"
            )

        current = _f(position.get("current_price"))
        stop = _f(position.get("stop_price_est"))
        position["distance_to_stop_pct"] = (
            ((current / stop) - 1.0) * 100.0
            if current is not None and stop is not None and stop > 0
            else None
        )
    return snapshot


def _apply_system5_truth(snapshot: dict[str, Any]) -> dict[str, Any]:
    """Overlay only System5 timing/ATR fields with canonical strategy semantics."""
    today = str(snapshot.get("date") or "")[:10]
    rolling_dir = ROOT / "data_cache" / "rolling"
    rules = SYSTEM_TRADE_RULES[SYSTEM5]
    for position in snapshot.get("positions") or []:
        if str(position.get("system") or "").lower() != SYSTEM5:
            continue
        snap = PositionSnapshot(
            symbol=str(position.get("symbol") or "").upper(),
            qty=float(position.get("qty") or 0.0),
            side=str(position.get("side") or "long"),
            avg_entry_price=float(position.get("avg_entry_price") or 0.0),
            market_value=_f(position.get("market_value")),
            unrealized_pl=_f(position.get("unrealized_pl")),
            system=SYSTEM5,
            entry_date=str(position.get("entry_date") or "")[:10] or None,
        )
        history = load_system5_history(rolling_dir, snap.symbol)
        state = evaluate_system5_state(snap, today=today, history=history)

        if snap.entry_date:
            # Six full post-entry sessions are observations; the seventh open is the
            # timeout.  Therefore holding=6 means "あと1日", not "本日手仕舞い".
            position["days_remaining"] = (
                int(rules.max_holding_days) + 1 - state.holding_days
            )
            position["exit_date"] = state.timeout_exit_date

        position["target_price_est"] = (
            round(state.target_price, 4) if state.target_price is not None else None
        )
        position["system5_entry_atr10"] = state.entry_atr10
        position["system5_target_hit_date"] = state.target_hit_date

        stop = None
        if state.entry_atr10 is not None and snap.avg_entry_price > 0:
            stop = protective_stop_price(
                side=snap.side,
                avg_entry_price=snap.avg_entry_price,
                rules=rules,
                atr_value=state.entry_atr10,
                symbol=snap.symbol,
            )
        position["stop_price_est"] = round(stop, 4) if stop is not None else None
        current = _f(position.get("current_price"))
        position["distance_to_stop_pct"] = (
            round((stop - current) / current * 100.0, 3)
            if stop is not None and current is not None and current > 0
            else None
        )
        target = state.target_price
        position["distance_to_target_pct"] = (
            round((target - current) / current * 100.0, 3)
            if target is not None and current is not None and current > 0
            else None
        )

        if state.target_exit_due:
            position["exit_expected"] = SYSTEM5_TARGET_NEXT_OPEN
        elif state.timeout_exit_due:
            position["exit_expected"] = "time_based"
        else:
            position["exit_expected"] = None
            position["exit_execution_state"] = None
    return snapshot


def build_snapshot(*args: Any, **kwargs: Any) -> dict[str, Any]:
    """Delegate all legacy measurement, then replace only protection truth."""
    original_estimator = _legacy._estimate_stop_target
    _legacy._estimate_stop_target = _estimate_stop_target
    try:
        snapshot = _LEGACY_BUILD_SNAPSHOT(*args, **kwargs)
        snapshot = _apply_system5_truth(snapshot)
    finally:
        _legacy._estimate_stop_target = original_estimator

    client = kwargs.get("client")
    if client is None:
        # The legacy build currently accepts the trading client positionally first.
        client = args[0] if args else None
    observation = (
        get_open_order_protection(client)
        if client is not None
        else {
            "measured": False,
            "observed_at": None,
            "error": "client unavailable",
            "by_symbol": {},
        }
    )
    return _apply_protection_truth(
        snapshot,
        observation=observation,
        soft_state=_load_soft_state(),
    )


def main(argv: list[str] | None = None) -> int:
    """Run the preserved CLI with this wrapper's protection-aware builder."""
    original = _legacy.build_snapshot
    _legacy.build_snapshot = build_snapshot
    try:
        return _legacy.main(argv)
    finally:
        _legacy.build_snapshot = original


if __name__ == "__main__":
    raise SystemExit(main())
