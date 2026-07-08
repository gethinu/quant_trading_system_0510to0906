# ============================================================================
# 🧠 Context Note
# このファイルは当日配分の最終段階。各システムの候補を統合し、slot/capital モードで割当を計算
#
# 前提条件：
#   - API 契約: finalize_allocation(per_system, strategies=?, positions=?, ...) は絶対変更禁止
#   - per_system: {"system1": df1, ...} 形式（必須）
#   - strategies パラメータなしは capital 配分が機能しない場合がある
#   - slot/capital モード共存。ユースケースに応じて選択
#   - スロット重複緩和: slot_dedup_enabled と slot_max_rank_depth で制御
#
# ロジック単位：
#   finalize_allocation()       → 統合候補→割当計算→最終リスト
#   _compute_capital_allocation() → capital モード計算
#   _compute_slot_allocation()    → slot モード計算
#
# Copilot へ：
#   → API 契約は変更するな（downstream で breaking change）
#   → strategies を渡さないと資本配分が未実装になる場合がある。厳格運用は ALLOCATION_REQUIRE_STRATEGIES=1
#   → slot モードで実株数が必要なら include_trade_management=True を指定
# ============================================================================

"""Final allocation stage utilities.

This module extracts the core logic for the *allocation & final list* stage
from ``scripts.run_all_systems_today`` so that it can be unit tested in
isolation.  The helpers here are intentionally side-effect free – all
I/O (Alpaca API calls, CSV writes, etc.) should be handled by the caller.

The key entry point is :func:`finalize_allocation` which combines the
per-system candidate tables into the final trade list using either the
slot-based or capital allocation mode.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
import logging
import math
import os
from typing import Any, TypeAlias, TypedDict, cast
import warnings

import pandas as pd

from common.symbol_map import (
    SymbolSystemMap,
    coerce_system_list,
    load_symbol_system_map,
    resolve_primary_system,
)
from common.trade_management import TradeManager
from config.environment import EnvironmentConfig, get_env_config

# Type aliases for better readability
PositionDict: TypeAlias = dict[str, Any]
SystemName: TypeAlias = str
Symbol: TypeAlias = str
StrategyMapping: TypeAlias = Mapping[str, object]


class AllocationConfig(TypedDict, total=False):
    """Type definition for allocation configuration."""

    long_allocations: dict[SystemName, float]
    short_allocations: dict[SystemName, float]
    max_positions: int
    risk_pct: float
    max_pct: float


# Configure logger
logger = logging.getLogger(__name__)


class AllocationConstants:
    """Centralized constants for allocation logic."""

    DEFAULT_RISK_PCT = 0.02
    DEFAULT_MAX_PCT = 0.10
    DEFAULT_MAX_POSITIONS = 10
    DEFAULT_CAPITAL = 100_000.0
    DEFAULT_LONG_RATIO = 0.5
    MAX_ITERATIONS = 10_000  # 無限ループ防止

    # エラーメッセージ
    MSG_INVALID_ALLOCATION = "Invalid allocation weights: {}"
    MSG_POSITION_CALC_ERROR = "Error calculating position size for {}: {}"
    MSG_EMPTY_DATA = "No data available for system {}"


DEFAULT_LONG_ALLOCATIONS: dict[str, float] = {
    "system1": 0.25,
    "system3": 0.25,
    "system4": 0.25,
    "system5": 0.25,
}

DEFAULT_SHORT_ALLOCATIONS: dict[str, float] = {
    "system2": 0.40,
    "system6": 0.40,
    "system7": 0.20,
}


def _load_allocations_from_settings() -> tuple[dict[str, float], dict[str, float]]:
    """Load allocation settings from configuration.

    Returns:
        Tuple of (long_allocations, short_allocations) dictionaries.
        Falls back to DEFAULT_*_ALLOCATIONS if settings are unavailable.
    """
    try:
        from config.settings import get_settings

        settings = get_settings()

        # UIセクションから配分設定を取得
        long_alloc = getattr(settings.ui, "long_allocations", {}) or {}
        short_alloc = getattr(settings.ui, "short_allocations", {}) or {}

        # 設定がある場合はそれを使用、無い場合はデフォルトを使用
        if long_alloc:
            long_result = {}
            for k, v in long_alloc.items():
                try:
                    fv = float(v)
                    if fv > 0:
                        long_result[str(k)] = fv
                except (TypeError, ValueError):
                    continue
        else:
            long_result = DEFAULT_LONG_ALLOCATIONS.copy()

        if short_alloc:
            short_result = {}
            for k, v in short_alloc.items():
                try:
                    fv = float(v)
                    if fv > 0:
                        short_result[str(k)] = fv
                except (TypeError, ValueError):
                    continue
        else:
            short_result = DEFAULT_SHORT_ALLOCATIONS.copy()
        return long_result, short_result

    except Exception:
        # 設定読み込みに失敗した場合はデフォルトを返す
        return DEFAULT_LONG_ALLOCATIONS.copy(), DEFAULT_SHORT_ALLOCATIONS.copy()


def _safe_positive_float(value: Any, *, allow_zero: bool = False) -> float | None:
    """Attempt to convert value to a positive float with improved error handling.

    Args:
        value: Value to convert
        allow_zero: Whether to allow zero values

    Returns:
        Converted float value or None if invalid

    Raises:
        Never raises - returns None for all errors
    """
    if value in (None, ""):
        logger.debug("Empty value provided for float conversion")
        return None

    try:
        f = float(value)
    except (TypeError, ValueError) as e:
        logger.debug("Failed to convert %r to float: %s", value, e)
        return None

    # Validation checks
    if f < 0:
        logger.debug("Negative value rejected: %s", f)
        return None
    if not allow_zero and f == 0:
        logger.debug("Zero value rejected (allow_zero=False)")
        return None
    if not (0 <= f < float("inf")):
        logger.debug("Invalid numeric value: %s", f)
        return None

    return f


@dataclass(slots=True)
class AllocationSummary:
    """Summary of the final allocation step.

    Attributes:
        mode: Allocation mode ('slot' or 'capital')
        long_allocations: Weight distribution for long systems
        short_allocations: Weight distribution for short systems
        active_positions: Current position count by system
        available_slots: Available slots for new positions by system
        final_counts: Final allocated positions by system
        slot_allocation: Slot distribution by system (slot mode only)
        slot_candidates: Available candidates by system (slot mode only)
        budgets: Budget allocation by system (capital mode only)
        budget_remaining: Remaining budget by system (capital mode only)
        capital_long: Total long-side capital (capital mode only)
        capital_short: Total short-side capital (capital mode only)
    """

    mode: str
    long_allocations: dict[str, float]
    short_allocations: dict[str, float]
    active_positions: dict[str, int]
    available_slots: dict[str, int]
    final_counts: dict[str, int]
    slot_allocation: dict[str, int] | None = None
    slot_candidates: dict[str, int] | None = None
    budgets: dict[str, float] | None = None
    budget_remaining: dict[str, float] | None = None
    capital_long: float | None = None
    capital_short: float | None = None
    system_diagnostics: dict[str, Any] | None = None
    # Optional final metrics (added for diagnostics/reporting)
    final_count: int = 0
    final_symbols: list[str] | None = None
    final_long_count: int = 0
    final_short_count: int = 0

    def __post_init__(self) -> None:
        """Validate allocation summary after initialization."""
        if self.mode not in ("slot", "capital"):
            logger.warning("Unknown allocation mode: %s", self.mode)

        # Validate that slot mode has slot-specific data
        if self.mode == "slot" and self.slot_allocation is None:
            logger.warning("Slot mode summary missing slot_allocation data")

        # Validate that capital mode has capital-specific data
        if self.mode == "capital" and self.budgets is None:
            logger.warning("Capital mode summary missing budgets data")


def _get_position_attr(obj: object, name: str) -> Any:
    """Get attribute from position object safely.

    Args:
        obj: Position object (could be object with attributes or dict)
        name: Attribute name to retrieve

    Returns:
        Attribute value or None if not found
    """
    try:
        if hasattr(obj, name):
            return getattr(obj, name)
        if isinstance(obj, Mapping):
            return obj.get(name)
    except Exception as e:
        logger.debug("Error accessing attribute %s: %s", name, e)
    return None


def count_active_positions_by_system(
    positions: Sequence[object] | None,
    symbol_system_map: Mapping[str, Any] | None,
) -> dict[str, int]:
    """Return a mapping of system names to active position counts.

    The helper tolerates legacy ``symbol -> system`` strings as well as the new
    ``symbol -> list[str]`` format produced by :mod:`common.symbol_map`.
    Only the primary system (list先頭) is counted to preserve existing
    allocation semantics.
    """

    if positions is None:
        positions = []

    normalized: SymbolSystemMap = {}
    if symbol_system_map:
        for key, value in symbol_system_map.items():
            try:
                symbol = str(key).strip().upper()
            except Exception:
                continue
            if not symbol:
                continue
            systems = coerce_system_list(value, ensure_all=False)
            if systems:
                normalized[symbol] = systems

    counts: dict[str, int] = {}
    for i, pos in enumerate(positions):
        try:
            symbol_raw = _get_position_attr(pos, "symbol")
            if symbol_raw is None:
                logger.debug("Position %d missing symbol", i)
                continue

            sym = str(symbol_raw).strip().upper()
            if not sym:
                logger.debug("Position %d has empty symbol", i)
                continue

            qty_raw = _get_position_attr(pos, "qty")
            try:
                qty_val = abs(float(qty_raw)) if qty_raw is not None else 0.0
            except (TypeError, ValueError) as exc:
                logger.debug("Position %d invalid quantity %r: %s", i, qty_raw, exc)
                qty_val = 0.0

            if qty_val <= 0:
                logger.debug("Position %d has zero/negative quantity: %s", i, qty_val)
                continue

            side_raw = _get_position_attr(pos, "side")
            side = str(side_raw).strip().lower() if side_raw is not None else ""

            primary_system = resolve_primary_system(normalized.get(sym))
            if not primary_system:
                if sym == "SPY" and side == "short":
                    primary_system = "system7"
                else:
                    logger.debug("No system mapping found for symbol: %s", sym)
                    continue

            if logger.isEnabledFor(logging.DEBUG):
                systems_for_symbol = normalized.get(sym)
                if systems_for_symbol and len(systems_for_symbol) > 1:
                    logger.debug(
                        "Multiple systems mapped to %s -> %s (primary=%s)",
                        sym,
                        systems_for_symbol,
                        primary_system,
                    )

            counts[primary_system] = counts.get(primary_system, 0) + 1

        except Exception as exc:  # noqa: BLE001 - defensive logging
            logger.warning("Error processing position %d: %s", i, exc)
            continue

    logger.debug("Counted active positions: %s", dict(counts))
    return counts


def _normalize_allocations(
    weights: Mapping[str, float] | None,
    defaults: Mapping[str, float],
) -> dict[str, float]:
    """Normalize allocation weights to sum to 1.0.

    Args:
        weights: Raw allocation weights by system
        defaults: Default weights to use if weights is empty/invalid

    Returns:
        Normalized weights that sum to 1.0

    Notes:
        - Invalid weights (negative, zero, non-numeric) are filtered out
        - If all weights are invalid, defaults are used
        - If defaults are also invalid, equal weights are assigned
    """
    filtered: dict[str, float] = {}

    if weights:
        for key, value in weights.items():
            try:
                numeric = float(value)
                if numeric > 0:  # Only positive weights allowed
                    filtered[str(key).strip().lower()] = numeric
                else:
                    logger.debug("Skipping non-positive weight: %s=%s", key, value)
            except (TypeError, ValueError) as e:
                logger.debug("Skipping invalid weight %s=%r: %s", key, value, e)
                continue

    # Fall back to defaults if no valid weights provided
    if not filtered:
        logger.debug("No valid weights provided, using defaults")
        try:
            filtered = {k: float(v) for k, v in defaults.items() if float(v) > 0}
        except (TypeError, ValueError) as e:
            logger.error("Invalid default weights: %s", e)
            filtered = {}

    # Calculate total for normalization
    total = sum(filtered.values())
    if total <= 0:
        # Final fallback: equal weights
        logger.warning("All weights are zero/negative, using equal weights")
        n = len(defaults)
        if n == 0:
            logger.error("No systems available for equal weight allocation")
            return {}
        return {k: 1.0 / n for k in defaults}

    # Normalize to sum to 1.0
    normalized = {k: v / total for k, v in filtered.items()}
    logger.debug("Normalized allocations: %s", normalized)
    return normalized


def _candidate_count(df: pd.DataFrame | None) -> int:
    if df is None or getattr(df, "empty", True):
        return 0
    return int(len(df))


def _normalize_symbol_key(value: Any) -> str | None:
    if value is None:
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None
    return text.upper()


def _extract_symbol(row: Any) -> str | None:
    if row is None:
        return None
    keys = ("symbol", "Symbol", "ticker", "Ticker")
    for key in keys:
        try:
            if isinstance(row, Mapping):
                value = row.get(key)
            else:
                value = getattr(row, key, None)
        except Exception:
            continue
        sym = _normalize_symbol_key(value)
        if sym:
            return sym
    return None


def _safe_int_value(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, (int,)):
        return int(value)
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        try:
            text = str(value).strip()
        except Exception:
            return None
        if not text:
            return None
        try:
            numeric = float(text)
        except (TypeError, ValueError):
            return None
    if not math.isfinite(numeric):
        return None
    try:
        return int(numeric)
    except (TypeError, ValueError):
        return None


def _stable_system_order(
    names: Sequence[str], per_system: Mapping[str, pd.DataFrame | None]
) -> list[str]:
    order: list[str] = []
    seen: set[str] = set()
    for name in names:
        key = str(name).strip().lower()
        if not key or key in seen:
            continue
        seen.add(key)
        if key not in per_system:
            continue
        order.append(key)
    return order


def _standardize_system_key_map(weights: Mapping[str, Any]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in weights.items():
        try:
            name = str(key).strip().lower()
        except Exception:
            continue
        if not name:
            continue
        result[name] = value
    return result


def _ensure_system_columns(df: pd.DataFrame, system: str, side: str) -> pd.DataFrame:
    out = df.copy()
    if "system" not in out.columns:
        out.insert(0, "system", system)
    if "side" not in out.columns:
        out["side"] = side
    return out


def _distribute_slots(
    weights: Mapping[str, float],
    total_slots: int,
    available_counts: Mapping[str, int],
) -> dict[str, int]:
    if total_slots <= 0:
        return {k: 0 for k in weights}

    base: dict[str, int] = {}
    for key, weight in weights.items():
        slots = int(total_slots * weight)
        cand = int(available_counts.get(key, 0))
        if cand <= 0:
            slots = 0
        elif slots == 0:
            slots = 1
        base[key] = min(slots, cand)

    used = sum(base.values())
    remain = max(0, total_slots - used)
    if remain and weights:
        order = sorted(
            weights.keys(),
            key=lambda k: (available_counts.get(k, 0), weights.get(k, 0.0)),
            reverse=True,
        )
        idx = 0
        while remain > 0 and order:
            name = order[idx % len(order)]
            if available_counts.get(name, 0) > base.get(name, 0):
                base[name] += 1
                remain -= 1
            idx += 1
            if idx > 10000:
                break

    for key in list(base.keys()):
        base[key] = min(base[key], int(available_counts.get(key, 0)))
    return base


@dataclass(slots=True)
class SlotAllocationResult:
    frame: pd.DataFrame
    distribution: dict[str, int]
    candidate_counts: dict[str, int]
    dedup_stats: dict[str, Any] | None = None


@dataclass(slots=True)
class SlotDedupResult:
    frames: dict[str, pd.DataFrame]
    consensus_groups: dict[str, list[dict[str, Any]]]
    stats: dict[str, Any]
    applied: bool
    side: str
    max_depth: int | None


def _collect_consensus_groups(
    data_map: Mapping[str, pd.DataFrame], *, max_depth: int | None
) -> dict[str, list[dict[str, Any]]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for system, df in data_map.items():
        if df is None or getattr(df, "empty", True):
            continue
        try:
            limit = len(df)
        except Exception:
            continue
        if max_depth is not None and max_depth > 0:
            limit = min(limit, max_depth)
        if limit <= 0:
            continue
        subset = df.head(limit)
        for pos, row in subset.iterrows():
            symbol = _extract_symbol(row)
            if not symbol:
                continue
            key = symbol.upper()
            entry: dict[str, Any] = {
                "system": system,
            }
            position_val = _safe_int_value(pos)
            if position_val is not None:
                entry["position"] = position_val + 1
            try:
                rank_val = row.get("rank") if isinstance(row, Mapping) else None
            except Exception:
                rank_val = None
            rank_int = _safe_int_value(rank_val)
            if rank_int is not None:
                entry["rank"] = rank_int
            try:
                score_val = row.get("score") if isinstance(row, Mapping) else None
            except Exception:
                score_val = None
            if score_val is not None:
                try:
                    score_num = float(score_val)
                except (TypeError, ValueError):
                    score_num = None
                if score_num is not None and math.isfinite(score_num):
                    entry["score"] = score_num
            try:
                side_val = row.get("side") if isinstance(row, Mapping) else None
            except Exception:
                side_val = None
            if side_val:
                entry["side"] = str(side_val)
            groups.setdefault(key, []).append(entry)
    return {symbol: members for symbol, members in groups.items() if len(members) >= 2}


def _apply_slot_round_robin_dedup(
    per_system: Mapping[str, pd.DataFrame | None],
    system_names: Sequence[str],
    *,
    max_depth: int | None,
    side: str,
) -> SlotDedupResult:
    system_order = _stable_system_order(system_names, per_system)
    before_counts: dict[str, int] = {}
    data_map: dict[str, pd.DataFrame] = {}
    for name in system_order:
        df = per_system.get(name)
        before_counts[name] = _candidate_count(df)
        if df is None or getattr(df, "empty", True):
            continue
        try:
            data_map[name] = df.reset_index(drop=True).copy()
        except Exception:
            data_map[name] = df.copy()

    depth_limit: int | None
    if max_depth is None:
        depth_limit = None
    else:
        try:
            depth_value = int(max_depth)
        except (TypeError, ValueError):
            depth_limit = None
        else:
            depth_limit = None if depth_value <= 0 else depth_value

    consensus = _collect_consensus_groups(data_map, max_depth=depth_limit)

    if not data_map:
        stats_payload = cast(
            dict[str, Any],
            {
                "per_system": {
                    name: {
                        "before": before_counts.get(name, 0),
                        "after": before_counts.get(name, 0),
                        "removed": 0,
                    }
                    for name in system_order
                },
                "owners": {},
                "requested_depth": max_depth,
                "effective_depth": depth_limit,
            },
        )
        return SlotDedupResult(
            frames={},
            consensus_groups=consensus,
            stats=stats_payload,
            applied=False,
            side=side,
            max_depth=depth_limit,
        )

    pointers: dict[str, int] = {name: 0 for name in data_map}
    accepted_indices: dict[str, list[int]] = {name: [] for name in data_map}
    owner_map: dict[str, str] = {}
    seen_symbols: set[str] = set()

    def _run_round(limit_map: Mapping[str, int]) -> bool:
        progress = False
        for name in system_order:
            df = data_map.get(name)
            if df is None or getattr(df, "empty", True):
                continue
            limit = int(limit_map.get(name, 0))
            pointer = pointers.get(name, 0)
            while pointer < limit:
                try:
                    row = df.iloc[pointer]
                except Exception:
                    pointer += 1
                    pointers[name] = pointer
                    continue
                pointer += 1
                pointers[name] = pointer
                symbol = _extract_symbol(row)
                if not symbol:
                    continue
                key = symbol.upper()
                if key in seen_symbols:
                    continue
                accepted_indices[name].append(pointer - 1)
                seen_symbols.add(key)
                owner_map[key] = name
                progress = True
                break
        return progress

    initial_limits: dict[str, int] = {}
    for name, df in data_map.items():
        try:
            length = len(df)
        except Exception:
            length = 0
        if depth_limit is None:
            initial_limits[name] = length
        else:
            initial_limits[name] = min(length, depth_limit)

    while _run_round(initial_limits):
        continue

    if depth_limit is not None:
        tail_limits: dict[str, int] = {}
        for name, df in data_map.items():
            try:
                tail_limits[name] = len(df)
            except Exception:
                tail_limits[name] = initial_limits.get(name, 0)
        while _run_round(tail_limits):
            continue

    frames: dict[str, pd.DataFrame] = {}
    per_system_stats: dict[str, dict[str, int]] = {}
    for name in system_order:
        before = before_counts.get(name, 0)
        df_original = data_map.get(name)
        if df_original is None:
            frames[name] = pd.DataFrame()
            per_system_stats[name] = {
                "before": before,
                "after": before,
                "removed": 0,
            }
            continue
        indices = accepted_indices.get(name, [])
        if indices:
            trimmed = df_original.iloc[indices].copy().reset_index(drop=True)
        else:
            trimmed = df_original.iloc[0:0].copy().reset_index(drop=True)
        frames[name] = trimmed
        after = _candidate_count(trimmed)
        per_system_stats[name] = {
            "before": before,
            "after": after,
            "removed": max(0, before - after),
        }

    stats_payload = cast(
        dict[str, Any],
        {
            "per_system": per_system_stats,
            "owners": owner_map.copy(),
            "requested_depth": max_depth,
            "effective_depth": depth_limit,
        },
    )

    return SlotDedupResult(
        frames=frames,
        consensus_groups=consensus,
        stats=stats_payload,
        applied=True,
        side=side,
        max_depth=depth_limit,
    )


def _emit_slot_consensus_logs(
    results: Sequence[SlotDedupResult], env: "EnvironmentConfig"
) -> None:
    if not results:
        return
    for result in results:
        if not result.applied or not result.consensus_groups:
            continue
        emoji = "" if env.no_emoji else "🤝 "
        side_label = result.side.upper()
        depth_hint = "" if result.max_depth is None else f" depth≤{result.max_depth}"
        for symbol, entries in sorted(result.consensus_groups.items()):
            parts: list[str] = []
            best_rank: int | None = None
            for entry in entries:
                system = entry.get("system", "?")
                position = entry.get("position")
                rank_val = entry.get("rank")
                if rank_val is not None:
                    best_rank = (
                        rank_val if best_rank is None else min(best_rank, rank_val)
                    )
                label = f"{system}"
                if position is not None:
                    label += f"#{position}"
                if rank_val is not None and rank_val != position:
                    label += f"(r{rank_val})"
                parts.append(label)
            rank_hint = ""
            if best_rank is not None:
                rank_hint = f" best_rank={best_rank}"
            systems_str = ", ".join(parts)
            message = (
                f"{emoji}consensus {side_label} {symbol}: "
                f"{len(entries)} systems align ({systems_str}).{rank_hint}{depth_hint}"
            )
            logger.info(message)


def _allocate_by_slots(
    per_system: Mapping[str, pd.DataFrame],
    *,
    long_alloc: Mapping[str, float],
    short_alloc: Mapping[str, float],
    available_slots: Mapping[str, int],
    slots_long: int,
    slots_short: int,
) -> SlotAllocationResult:
    env = get_env_config()
    long_weights = _standardize_system_key_map(long_alloc)
    short_weights = _standardize_system_key_map(short_alloc)

    per_system_local: dict[str, pd.DataFrame | None] = {}
    for key, df in per_system.items():
        try:
            name = str(key).strip().lower()
        except Exception:
            continue
        per_system_local[name] = df

    all_names = (
        set(per_system_local.keys())
        | set(long_weights.keys())
        | set(short_weights.keys())
    )
    raw_counts: dict[str, int] = {
        name: _candidate_count(per_system_local.get(name)) for name in all_names
    }

    dedup_enabled = bool(getattr(env, "slot_dedup_enabled", False))
    depth_setting = getattr(env, "slot_max_rank_depth", None)

    dedup_results: list[SlotDedupResult] = []
    consensus_results: list[SlotDedupResult] = []

    if dedup_enabled:
        if long_weights:
            dedup_results.append(
                _apply_slot_round_robin_dedup(
                    per_system_local,
                    tuple(long_weights.keys()),
                    max_depth=depth_setting,
                    side="long",
                )
            )
        if short_weights:
            dedup_results.append(
                _apply_slot_round_robin_dedup(
                    per_system_local,
                    tuple(short_weights.keys()),
                    max_depth=depth_setting,
                    side="short",
                )
            )
        for result in dedup_results:
            if not result.applied:
                continue
            for system_name, frame in result.frames.items():
                per_system_local[system_name] = frame
        consensus_results = [res for res in dedup_results if res.consensus_groups]
    else:
        temp_results: list[SlotDedupResult] = []
        if long_weights:
            temp_results.append(
                _apply_slot_round_robin_dedup(
                    per_system_local,
                    tuple(long_weights.keys()),
                    max_depth=depth_setting,
                    side="long",
                )
            )
        if short_weights:
            temp_results.append(
                _apply_slot_round_robin_dedup(
                    per_system_local,
                    tuple(short_weights.keys()),
                    max_depth=depth_setting,
                    side="short",
                )
            )
        consensus_results = [res for res in temp_results if res.consensus_groups]

    if consensus_results:
        _emit_slot_consensus_logs(consensus_results, env)

    all_names = (
        set(per_system_local.keys())
        | set(long_weights.keys())
        | set(short_weights.keys())
    )
    candidate_counts: dict[str, int] = {
        name: _candidate_count(per_system_local.get(name)) for name in all_names
    }

    long_available: dict[str, int] = {}
    for name in long_weights:
        cand_cnt = candidate_counts.get(name, 0)
        long_available[name] = min(cand_cnt, int(available_slots.get(name, 0)))

    short_available: dict[str, int] = {}
    for name in short_weights:
        cand_cnt = candidate_counts.get(name, 0)
        short_available[name] = min(cand_cnt, int(available_slots.get(name, 0)))

    long_slots = _distribute_slots(long_weights, slots_long, long_available)
    short_slots = _distribute_slots(short_weights, slots_short, short_available)

    frames: list[pd.DataFrame] = []
    distribution: dict[str, int] = {}
    for name, slot in {**long_slots, **short_slots}.items():
        if slot <= 0:
            continue
        df = per_system_local.get(name)
        if df is None or getattr(df, "empty", True):
            continue
        free_slots = int(available_slots.get(name, 0))
        take = min(int(slot), free_slots)
        if take <= 0:
            continue
        side = "long" if name in long_weights else "short"
        subset = _ensure_system_columns(df.head(take), name, side).copy()
        try:
            subset["side"] = side
        except Exception:
            pass
        try:
            if name in long_weights:
                subset["alloc_weight"] = long_weights.get(name)
            else:
                subset["alloc_weight"] = short_weights.get(name, 0.0)
        except Exception:
            subset["alloc_weight"] = 0.0
        frames.append(subset)
        distribution[name] = take

    if frames:
        frame = _concat_nonempty(frames)
    else:
        frame = pd.DataFrame()

    dedup_stats: dict[str, Any] | None = None
    if dedup_enabled:
        dedup_stats = {
            "enabled": True,
            "counts_before": dict(raw_counts),
            "counts_after": dict(candidate_counts),
            "sides": {},
        }
        for result in dedup_results:
            side_key = result.side
            dedup_stats["sides"][side_key] = {
                "stats": result.stats,
                "consensus": result.consensus_groups,
            }

    return SlotAllocationResult(
        frame=frame,
        distribution=distribution,
        candidate_counts=candidate_counts,
        dedup_stats=dedup_stats,
    )


@dataclass(slots=True)
class _StrategyAllocationMeta:
    calc_fn: Any
    risk_pct: float
    max_pct: float
    max_positions: int


@dataclass(slots=True)
class CapitalAllocationResult:
    frame: pd.DataFrame
    budgets: dict[str, float]
    remaining: dict[str, float]
    counts: dict[str, int]


def _concat_nonempty(frames: list[pd.DataFrame]) -> pd.DataFrame:
    """Safely concat DataFrames while avoiding pandas FutureWarning.

    - Skip None, empty, and all-NA frames
    - Suppress dtype-inference FutureWarning during concat
    """
    valid: list[pd.DataFrame] = []
    for _df in frames or []:
        try:
            if _df is None or getattr(_df, "empty", True):
                continue
            if bool(_df.isna().all().all()):
                continue
            # 1 行以上あることを厳格に確認（念のため）
            if getattr(_df, "shape", None) and _df.shape[0] <= 0:
                continue
            valid.append(_df)
        except Exception:
            continue

    if not valid:
        return pd.DataFrame()

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", category=FutureWarning)
        return pd.concat(valid, ignore_index=True)


def _allocate_by_capital(
    per_system: Mapping[str, pd.DataFrame],
    *,
    strategies: Mapping[str, object],
    total_budget: float,
    weights: Mapping[str, float],
    side: str,
    active_positions: Mapping[str, int],
) -> CapitalAllocationResult:
    env = get_env_config()
    debug_mode = env.allocation_debug
    # デバッグ時は簡潔に呼び出し元の欠落コンテキストを警告するが、
    # 具体的なパラメータの存在チェックは外側の呼び出し元で行う。

    budgets: dict[str, float] = {}
    for name in weights:
        try:
            budgets[name] = float(total_budget) * float(weights.get(name, 0.0))
        except Exception:
            budgets[name] = 0.0
    remaining = budgets.copy()

    if debug_mode:
        logger.debug(
            "[ALLOC_DEBUG] Starting capital allocation: side=%s, total_budget=$%s",
            side,
            f"{total_budget:,.0f}",
        )
        logger.debug("[ALLOC_DEBUG] System weights: %s", dict(weights))
        logger.debug("[ALLOC_DEBUG] System budgets: %s", dict(budgets))
        logger.debug("[ALLOC_DEBUG] Active positions: %s", dict(active_positions))

    # stable ordering
    order = [f"system{i}" for i in range(1, 8)]
    ordered_names = [name for name in order if name in weights]

    meta_map: dict[str, _StrategyAllocationMeta] = {}
    candidates: dict[str, list[dict[str, Any]]] = {}
    index_map: dict[str, int] = {}

    for name in ordered_names:
        stg = strategies.get(name)
        config = getattr(stg, "config", {}) if stg is not None else {}
        calc_fn = getattr(stg, "calculate_position_size", None)
        if not callable(calc_fn):
            calc_fn = None
            if debug_mode:
                logger.warning(
                    "[ALLOC_DEBUG] %s: calculate_position_size not found",
                    name,
                )

        # リスク設定の読み込み
        try:
            risk_pct = float(config.get("risk_pct", 0.02))
        except (TypeError, ValueError):
            risk_pct = 0.02
        try:
            max_pct = float(config.get("max_pct", 0.10))
        except (TypeError, ValueError):
            max_pct = 0.10
        # Guard against non-positive or non-finite settings coming from config
        try:
            import math as _math

            if (not _math.isfinite(risk_pct)) or risk_pct <= 0:
                if debug_mode:
                    logger.info(
                        (
                            "[ALLOC_DEBUG] %s: risk_pct=%s invalid -> "
                            "fallback to default 0.020"
                        ),
                        name,
                        risk_pct,
                    )
                risk_pct = 0.02
            if (not _math.isfinite(max_pct)) or max_pct <= 0:
                if debug_mode:
                    logger.info(
                        (
                            "[ALLOC_DEBUG] %s: max_pct=%s invalid -> "
                            "fallback to default 0.100"
                        ),
                        name,
                        max_pct,
                    )
                max_pct = 0.10
        except Exception:
            # On any unexpected error, keep already assigned values
            pass
        try:
            max_positions = int(config.get("max_positions", 10))
        except (TypeError, ValueError):
            max_positions = 10
        meta_map[name] = _StrategyAllocationMeta(
            calc_fn=calc_fn,
            risk_pct=risk_pct,
            max_pct=max_pct,
            max_positions=max_positions,
        )

        # 候補データの準備
        df = per_system.get(name)
        if df is None or getattr(df, "empty", True):
            candidates[name] = []
            if debug_mode:
                logger.debug(f"[ALLOC_DEBUG] {name}: No candidates")
        else:
            records = df.to_dict("records")
            # 正規化: dict[Hashable, Any] -> dict[str, Any]
            norm_records: list[dict[str, Any]] = []
            for rec in records:
                try:
                    norm_records.append({str(k): v for k, v in rec.items()})
                except Exception:
                    norm_records.append({str(k): rec.get(k) for k in rec})
            candidates[name] = norm_records
            if debug_mode:
                logger.debug(
                    "[ALLOC_DEBUG] %s: %s candidates loaded",
                    name,
                    len(norm_records),
                )
        index_map[name] = 0

    counts = {name: 0 for name in ordered_names}
    max_pos_map = {}
    for name in ordered_names:
        try:
            max_pos = meta_map[name].max_positions
            taken = int(active_positions.get(name, 0))
            max_pos_map[name] = max(0, max_pos - taken)
        except Exception:
            max_pos_map[name] = 0

    if debug_mode:
        logger.debug(f"[ALLOC_DEBUG] Max positions available: {dict(max_pos_map)}")

    chosen: list[dict[str, Any]] = []
    chosen_symbols: set[str] = set()

    def _normalize_shares(value: Any) -> int:
        try:
            return int(float(value))
        except (TypeError, ValueError):
            return 0

    # システム別の処理統計
    # Note: per-system processing stats were considered but are currently
    # unused in the final output. Keep logic minimal to avoid lint noise.

    still = True
    round_num = 0
    while still:
        still = False
        round_num += 1

        if debug_mode and round_num <= 3:  # 最初の3ラウンドのみログ
            logger.debug(f"[ALLOC_DEBUG] === Round {round_num} ===")

        for name in ordered_names:
            rows = candidates.get(name, [])
            if (
                not rows
                or remaining.get(name, 0.0) <= 0.0
                or counts.get(name, 0) >= max_pos_map.get(name, 0)
                or index_map.get(name, 0) >= len(rows)
            ):
                # スキップ理由の診断
                if debug_mode and round_num <= 3:
                    skip_reasons = []
                    if not rows:
                        skip_reasons.append("no_rows")
                    if remaining.get(name, 0.0) <= 0.0:
                        rem = remaining.get(name, 0.0)
                        skip_reasons.append(f"no_budget({rem:.0f})")
                    if counts.get(name, 0) >= max_pos_map.get(name, 0):
                        curr = counts.get(name, 0)
                        mp = max_pos_map.get(name, 0)
                        skip_reasons.append(f"max_pos({curr}/{mp})")
                    if index_map.get(name, 0) >= len(rows):
                        exhausted = index_map.get(name, 0)
                        total_rows = len(rows)
                        skip_reasons.append(f"exhausted({exhausted}/{total_rows})")
                    if skip_reasons:
                        logger.debug(
                            "[ALLOC_DEBUG] %s skipped: %s",
                            name,
                            ",".join(skip_reasons),
                        )
                continue

            meta = meta_map[name]
            idx = index_map[name]

            while idx < len(rows):
                row = rows[idx]
                idx += 1
                index_map[name] = idx

                sym = str(row.get("symbol", "")).upper()
                if not sym or sym in chosen_symbols:
                    if debug_mode and round_num <= 2:
                        if not sym:
                            logger.debug(
                                "[ALLOC_DEBUG] %s: Skipping row with missing symbol",
                                name,
                            )
                        else:
                            logger.debug(
                                "[ALLOC_DEBUG] %s %s: Already selected",
                                name,
                                sym,
                            )
                    continue

                # entry_price/stop_price の取得 - 存在しない場合は Close/ATR から計算
                entry = _safe_positive_float(row.get("entry_price"), allow_zero=False)
                stop = _safe_positive_float(row.get("stop_price"), allow_zero=False)

                # Fallback: entry_price が存在しない場合は Close を使用
                if entry is None or entry <= 0:
                    entry = _safe_positive_float(row.get("Close"), allow_zero=False)
                    if debug_mode and round_num <= 2:
                        logger.debug(
                            "[ALLOC_DEBUG] %s %s: Using Close as entry_price=%s",
                            name,
                            sym,
                            entry,
                        )

                # Fallback: stop_price が存在しない場合は Close ± ATR から計算
                if stop is None or stop <= 0:
                    # システム別優先順: S2/3/5/6=atr10, S1=atr20, S4=atr40, S7=atr50
                    atr = _safe_positive_float(row.get("atr10"), allow_zero=False)
                    if atr is None:
                        atr = _safe_positive_float(row.get("ATR10"), allow_zero=False)
                    if atr is None:
                        atr = _safe_positive_float(row.get("atr20"), allow_zero=False)
                    if atr is None:
                        atr = _safe_positive_float(row.get("atr40"), allow_zero=False)
                    if atr is None:
                        atr = _safe_positive_float(row.get("atr50"), allow_zero=False)

                    if entry and entry > 0 and atr and atr > 0:
                        if side == "long":
                            stop = entry - (atr * 2.0)  # ロング: エントリー - ATR*2
                        else:
                            stop = entry + (atr * 2.0)  # ショート: エントリー + ATR*2

                        if debug_mode and round_num <= 2:
                            logger.debug(
                                (
                                    "[ALLOC_DEBUG] %s %s: Calculated stop_price=%s "
                                    "from entry=%s atr=%s"
                                ),
                                name,
                                sym,
                                stop,
                                entry,
                                atr,
                            )

                if entry is None or stop is None or entry <= 0:
                    if debug_mode and round_num <= 2:
                        logger.debug(
                            ("[ALLOC_DEBUG] %s %s: Invalid prices entry=%s stop=%s"),
                            name,
                            sym,
                            entry,
                            stop,
                        )
                    continue

                desired_shares = 0
                if meta.calc_fn is not None:
                    try:
                        # 正しい引数で calculate_position_size を呼び出し
                        entry_price = entry
                        stop_price = stop

                        if entry_price and stop_price and entry_price > 0:
                            desired_shares = _normalize_shares(
                                meta.calc_fn(
                                    remaining[name],  # available capital
                                    entry_price,  # entry_price
                                    stop_price,  # stop_price
                                    risk_pct=meta.risk_pct,
                                    max_pct=meta.max_pct,
                                )
                            )
                            # 詳細診断: リスク/資金上限ベースの概算株数も併記
                            if debug_mode and round_num <= 3:
                                try:
                                    rem_cap = float(remaining[name])
                                    e = float(entry_price)
                                    s = float(stop_price)
                                    # compute intermediate values in steps
                                    # to avoid long lines
                                    if e is not None and s is not None:
                                        rps = abs(e - s)
                                    else:
                                        rps = 0.0
                                    allow_risk = rem_cap * float(meta.risk_pct)
                                    if rps > 0:
                                        shares_by_risk = int(allow_risk // rps)
                                    else:
                                        shares_by_risk = 0
                                    cap_limit_dollars = rem_cap * float(meta.max_pct)
                                    if e > 0:
                                        try:
                                            abs_e = abs(e)
                                            tmp_cap = cap_limit_dollars // abs_e
                                            shares_by_capital = int(tmp_cap)
                                        except Exception:
                                            shares_by_capital = 0
                                        try:
                                            abs_e2 = abs(e)
                                            shares_by_cash = int(rem_cap // abs_e2)
                                        except Exception:
                                            shares_by_cash = 0
                                    else:
                                        shares_by_capital = 0
                                        shares_by_cash = 0
                                    # Log shares calculation details
                                    try:
                                        logger.debug(
                                            "[ALLOC_DEBUG] %s %s: shares_calc rem=$%s "
                                            "entry=%s stop=%s "
                                            "risk_pct=%.4f max_pct=%.4f "
                                            "rps=%s shr_risk=%s "
                                            "shr_cap=%s shr_cash=%s desired=%s",
                                            name,
                                            sym,
                                            f"{rem_cap:.0f}",
                                            e,
                                            s,
                                            float(meta.risk_pct),
                                            float(meta.max_pct),
                                            (f"{rps:.4f}" if rps else "0"),
                                            shares_by_risk,
                                            shares_by_capital,
                                            shares_by_cash,
                                            desired_shares,
                                        )
                                    except Exception:
                                        # 診断ログは失敗しても処理継続
                                        pass
                                except Exception:
                                    # 診断ログは失敗しても処理継続
                                    pass
                        else:
                            if debug_mode and round_num <= 2:
                                logger.debug(
                                    (
                                        "[ALLOC_DEBUG] %s %s: Invalid entry/stop "
                                        "entry_price=%s stop_price=%s"
                                    ),
                                    name,
                                    sym,
                                    entry_price,
                                    stop_price,
                                )
                    except Exception as e:
                        desired_shares = 0
                        if debug_mode and round_num <= 2:
                            logger.debug(
                                "[ALLOC_DEBUG] %s %s: calc_fn error %s",
                                name,
                                sym,
                                e,
                            )
                else:
                    if debug_mode and round_num <= 2:
                        logger.debug(
                            "[ALLOC_DEBUG] %s %s: No calc_fn available",
                            name,
                            sym,
                        )

                if desired_shares <= 0:
                    if debug_mode and round_num <= 2:
                        logger.debug(
                            "[ALLOC_DEBUG] %s %s: Invalid shares %s",
                            name,
                            sym,
                            desired_shares,
                        )
                    continue

                max_by_cash = int(remaining[name] // abs(entry)) if entry else 0
                shares = min(desired_shares, max_by_cash)
                if debug_mode and round_num <= 3:
                    try:
                        logger.debug(
                            (
                                "[ALLOC_DEBUG] %s %s: shares_decision desired=%s "
                                "max_by_cash=%s -> shares=%s"
                            ),
                            name,
                            sym,
                            desired_shares,
                            max_by_cash,
                            shares,
                        )
                    except Exception:
                        pass
                if shares <= 0:
                    if debug_mode and round_num <= 2:
                        logger.debug(
                            (
                                "[ALLOC_DEBUG] %s %s: No cash shares=%s "
                                "(desired=%s, max_cash=%s, remaining=$%s)"
                            ),
                            name,
                            sym,
                            shares,
                            desired_shares,
                            max_by_cash,
                            f"{remaining[name]:.0f}",
                        )
                    continue

                position_value = shares * abs(entry)
                if position_value <= 0:
                    if debug_mode and round_num <= 2:
                        logger.debug(
                            "[ALLOC_DEBUG] %s %s: Invalid position_value %s",
                            name,
                            sym,
                            position_value,
                        )
                    continue

                # 成功 - ポジション追加
                record = dict(row)
                record["shares"] = int(shares)
                record["position_value"] = float(round(position_value, 2))
                record["system"] = record.get("system", name)
                record["side"] = record.get("side", side)
                record["system_budget"] = float(round(remaining[name], 2))

                # フォールバック計算された entry_price と stop_price を設定
                record["entry_price"] = entry
                record["stop_price"] = stop
                remaining_after = remaining[name] - position_value
                record["remaining_after"] = float(round(remaining_after, 2))
                chosen.append(record)
                chosen_symbols.add(sym)
                remaining[name] -= position_value
                counts[name] = counts.get(name, 0) + 1
                still = True

                if debug_mode:
                    logger.debug(
                        (
                            "[ALLOC_DEBUG] %s %s: ALLOCATED shares=%s "
                            "value=$%s remaining=$%s"
                        ),
                        name,
                        sym,
                        shares,
                        f"{position_value:.0f}",
                        f"{remaining_after:.0f}",
                    )
                break

    if chosen:
        frame = pd.DataFrame(chosen)
    else:
        frame = pd.DataFrame()

    # デバッグサマリー
    if debug_mode:
        total_allocated = len(chosen)
        total_budget_used = 0.0
        for name in budgets:
            try:
                total_budget_used += budgets[name] - remaining.get(name, 0)
            except Exception:
                continue
        logger.info(f"[ALLOC_DEBUG] === ALLOCATION SUMMARY ({side}) ===")
        logger.info(f"[ALLOC_DEBUG] Total allocated: {total_allocated} positions")
        logger.info(
            "[ALLOC_DEBUG] Budget used: $%s / $%s",
            f"{total_budget_used:,.0f}",
            f"{total_budget:,.0f}",
        )
        logger.info(f"[ALLOC_DEBUG] System counts: {dict(counts)}")
        logger.info(f"[ALLOC_DEBUG] Remaining budgets: {dict(remaining)}")

        if total_allocated == 0:
            # サイドを明示して誤解を避ける（例: ショート側のみ候補なし）
            logger.warning("[ALLOC_DEBUG] ⚠️ NO POSITIONS ALLOCATED! Check (%s):", side)
            for name in ordered_names:
                cand_count = len(candidates.get(name, []))
                calc_fn_available = meta_map[name].calc_fn is not None
                logger.warning(
                    f"[ALLOC_DEBUG]   {name}: candidates={cand_count}, "
                    f"calc_fn={calc_fn_available}, "
                    f"budget=${remaining.get(name, 0):.0f}, "
                    f"max_pos={max_pos_map.get(name, 0)}"
                )

    return CapitalAllocationResult(
        frame=frame,
        budgets=budgets,
        remaining=remaining,
        counts=counts,
    )


def _sort_final_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or df.empty or "system" not in df.columns:
        return df.copy() if df is not None else df

    tmp = df.copy()
    try:
        tmp["system"] = tmp["system"].astype(str)
    except Exception:
        tmp["system"] = tmp["system"]
    try:
        tmp["_system_no"] = tmp["system"].str.extract(r"(\d+)").fillna(0).astype(int)
    except Exception:
        tmp["_system_no"] = 0

    sort_cols = [c for c in ["side", "_system_no"] if c in tmp.columns]
    if sort_cols:
        tmp = tmp.sort_values(sort_cols, kind="stable")

    try:
        parts: list[pd.DataFrame] = []
        for name, group in tmp.groupby("system", sort=False):
            if "score" not in group.columns:
                parts.append(group)
                continue
            asc = False
            try:
                if isinstance(name, str) and name.lower() == "system4":
                    asc = True
            except Exception:
                asc = False
            parts.append(
                group.sort_values(
                    "score",
                    ascending=asc,
                    kind="stable",
                    na_position="last",
                )
            )
        tmp = _concat_nonempty(parts)
    except Exception:
        pass

    # Drop _system_no column if it exists
    if "_system_no" in tmp.columns:
        tmp = tmp.drop(columns=["_system_no"])

    # Add sequential numbering
    try:
        tmp.insert(0, "no", range(1, len(tmp) + 1))
    except Exception:
        tmp["no"] = range(1, len(tmp) + 1)
    return tmp.reset_index(drop=True)


def _resolve_max_positions(
    strategies: Mapping[str, object] | None,
    systems: Sequence[str],
    default_max_positions: int,
) -> dict[str, int]:
    result: dict[str, int] = {name: default_max_positions for name in systems}
    if not strategies:
        return result
    for name, obj in strategies.items():
        key = str(name).strip().lower()
        if not key:
            continue
        config = getattr(obj, "config", {})
        try:
            value = int(config.get("max_positions", default_max_positions))
        except (TypeError, ValueError, AttributeError):
            value = default_max_positions
        result[key] = max(0, value)
    return result


def _load_portfolio_caps() -> dict[str, float]:
    """portfolio-level 上限を settings から読む (欠損時は no-op default)。

    Phase 5 (2026-07-07)。default は既存ルール由来の no-op。
    詳細: docs/POSITION_MANAGEMENT_PHASE5_20260707.md。
    """
    defaults: dict[str, float] = {
        "max_total_positions": 70,
        "max_long_positions": 40,
        "max_short_positions": 30,
        "max_gross_exposure_pct": 1.0,
        "max_net_exposure_pct": 1.0,
    }
    try:
        from config.settings import get_settings

        pf = get_settings().risk.portfolio
        return {
            "max_total_positions": int(getattr(pf, "max_total_positions", 70)),
            "max_long_positions": int(getattr(pf, "max_long_positions", 40)),
            "max_short_positions": int(getattr(pf, "max_short_positions", 30)),
            "max_gross_exposure_pct": float(getattr(pf, "max_gross_exposure_pct", 1.0)),
            "max_net_exposure_pct": float(getattr(pf, "max_net_exposure_pct", 1.0)),
        }
    except Exception:
        return defaults


def _apply_portfolio_caps(
    final_df: pd.DataFrame,
    *,
    caps: Mapping[str, float],
    active_positions: Sequence[object] | None,
    symbol_system_map: Mapping[str, Any] | None,
    long_systems: Sequence[str],
    short_systems: Sequence[str],
    equity: float,
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """優先度順 final_df に portfolio 上限 (件数 total/long/short + gross/net exposure)
    を適用し、超過分を末尾から trim する。

    - 既保有 (active_positions) + 新規 が上限を超えないよう新規側を削る。
    - exposure は ``position_value`` 列がある時のみ (新規分の gross/net を equity×pct と比較)。
    - default caps は no-op (現状 allocation は上限を超えない) なので通常は無 trim。
    戻り値: (trimmed_df, report)。report は観測性のため summary に載せる。
    """
    report: dict[str, Any] = {"applied": False}
    if final_df is None or getattr(final_df, "empty", True):
        return final_df, report

    active_by_system = count_active_positions_by_system(
        active_positions, symbol_system_map
    )
    long_set = {str(s).strip().lower() for s in long_systems}
    short_set = {str(s).strip().lower() for s in short_systems}
    held_long = sum(v for k, v in active_by_system.items() if k in long_set)
    held_short = sum(v for k, v in active_by_system.items() if k in short_set)
    held_total = sum(active_by_system.values())

    max_total = int(caps.get("max_total_positions", 70))
    max_long = int(caps.get("max_long_positions", 40))
    max_short = int(caps.get("max_short_positions", 30))
    gross_cap = float(equity) * float(caps.get("max_gross_exposure_pct", 1.0))
    net_cap = float(equity) * float(caps.get("max_net_exposure_pct", 1.0))

    allow_long = max(0, max_long - held_long)
    allow_short = max(0, max_short - held_short)
    allow_total = max(0, max_total - held_total)

    has_pv = "position_value" in final_df.columns
    kept_idx: list[Any] = []
    n_long = n_short = n_total = 0
    long_usd = short_usd = 0.0
    trims: dict[str, int] = {}

    for idx, row in final_df.iterrows():
        side = str(row.get("side", "")).strip().lower()
        if n_total >= allow_total:
            trims["total"] = trims.get("total", 0) + 1
            continue
        if side == "long" and n_long >= allow_long:
            trims["long_count"] = trims.get("long_count", 0) + 1
            continue
        if side == "short" and n_short >= allow_short:
            trims["short_count"] = trims.get("short_count", 0) + 1
            continue
        if has_pv:
            try:
                pv = float(row.get("position_value") or 0.0)
            except (TypeError, ValueError):
                pv = 0.0
            new_long = long_usd + (pv if side == "long" else 0.0)
            new_short = short_usd + (pv if side == "short" else 0.0)
            if gross_cap > 0 and (new_long + new_short) > gross_cap:
                trims["gross_exposure"] = trims.get("gross_exposure", 0) + 1
                continue
            if net_cap > 0 and abs(new_long - new_short) > net_cap:
                trims["net_exposure"] = trims.get("net_exposure", 0) + 1
                continue
            long_usd, short_usd = new_long, new_short
        kept_idx.append(idx)
        n_total += 1
        if side == "long":
            n_long += 1
        elif side == "short":
            n_short += 1

    if len(kept_idx) < len(final_df):
        trimmed_df = final_df.loc[kept_idx].copy().reset_index(drop=True)
    else:
        trimmed_df = final_df

    report = {
        "applied": True,
        "held": {"long": held_long, "short": held_short, "total": held_total},
        "caps": {
            "max_total": max_total,
            "max_long": max_long,
            "max_short": max_short,
            "gross_cap_usd": round(gross_cap, 2),
            "net_cap_usd": round(net_cap, 2),
        },
        "allow": {"long": allow_long, "short": allow_short, "total": allow_total},
        "kept": {"long": n_long, "short": n_short, "total": n_total},
        "trimmed": trims,
        "new_long_usd": round(long_usd, 2),
        "new_short_usd": round(short_usd, 2),
    }
    if trims:
        logger.info(
            "[PORTFOLIO_CAP] trimmed %s (held L%d/S%d)", trims, held_long, held_short
        )
    return trimmed_df, report


def finalize_allocation(
    per_system: Mapping[str, pd.DataFrame],
    *,
    strategies: Mapping[str, object] | None = None,
    positions: Sequence[object] | None = None,
    symbol_system_map: Mapping[str, Any] | None = None,
    long_allocations: Mapping[str, float] | None = None,
    short_allocations: Mapping[str, float] | None = None,
    slots_long: int | None = None,
    slots_short: int | None = None,
    capital_long: float | None = None,
    capital_short: float | None = None,
    default_capital: float = 100000.0,
    default_long_ratio: float = 0.5,
    default_max_positions: int = 10,
    system_diagnostics: Mapping[str, Any] | None = None,
    market_data_dict: Mapping[str, pd.DataFrame] | None = None,
    signal_date: Any | None = None,
    include_trade_management: bool = False,
) -> tuple[pd.DataFrame, AllocationSummary]:
    """Combine per-system candidates into the final trade list.

    Parameters mirror the behaviour of ``scripts.run_all_systems_today`` but
    are greatly simplified so that tests can provide deterministic inputs.
    This function also includes trade management field calculations.
    """
    # 入力候補データのデバッグ情報を出力
    env = get_env_config()
    debug_mode = env.allocation_debug
    if debug_mode:
        logger.info("[ALLOC_DEBUG] === INPUT CANDIDATES DEBUG ===")
        for sys_name, df in per_system.items():
            if not df.empty:
                cols_str = f"{list(df.columns)}"
                msg = f"[ALLOC_DEBUG] {sys_name}: {len(df)} rows, columns: {cols_str}"
                logger.info(msg)
                # サンプル行の詳細を出力
                if len(df) > 0:
                    sample_row = df.iloc[0]
                    sample_dict = dict(sample_row)
                    logger.info(f"[ALLOC_DEBUG] {sys_name} sample row: {sample_dict}")
            else:
                logger.info(f"[ALLOC_DEBUG] {sys_name}: EMPTY DataFrame")

    # 非破壊の簡潔な診断ログ: 呼び出し元が strategies/symbol_system_map を
    # 渡していない場合、資本配分や現有ポジションのカウントに影響するため
    # デバッグ時のみ警告を出す。
    if debug_mode:
        if strategies is None:
            logger.warning(
                "[ALLOC_DEBUG] finalize_allocation called without strategies"
            )
        if symbol_system_map is None:
            logger.warning(
                "[ALLOC_DEBUG] called without symbol_system_map; "
                "active counts may be incomplete"
            )

    per_system_norm: dict[str, pd.DataFrame] = {}
    for name, df in per_system.items():
        try:
            per_system_norm[str(name).strip().lower()] = df
        except Exception:
            continue

    # 配分設定が提供されていない場合は、設定ファイルから読み込む
    if long_allocations is None and short_allocations is None:
        config_long_alloc, config_short_alloc = _load_allocations_from_settings()
        long_alloc = _normalize_allocations(config_long_alloc, DEFAULT_LONG_ALLOCATIONS)
        short_alloc = _normalize_allocations(
            config_short_alloc,
            DEFAULT_SHORT_ALLOCATIONS,
        )
    else:
        long_alloc = _normalize_allocations(long_allocations, DEFAULT_LONG_ALLOCATIONS)
        short_alloc = _normalize_allocations(
            short_allocations,
            DEFAULT_SHORT_ALLOCATIONS,
        )

    systems = sorted({*per_system_norm.keys(), *long_alloc.keys(), *short_alloc.keys()})
    max_pos_map = _resolve_max_positions(strategies, systems, default_max_positions)

    active_positions = count_active_positions_by_system(positions, symbol_system_map)
    available_slots: dict[str, int] = {}
    for name in systems:
        taken = int(active_positions.get(name, 0))
        limit = int(max_pos_map.get(name, default_max_positions))
        available_slots[name] = max(0, limit - taken)

    candidate_counts: dict[str, int] = {}
    for name in systems:
        candidate_counts[name] = _candidate_count(per_system_norm.get(name))

    # ✅ 診断情報の整合性検証（デバッグモード時）
    if debug_mode and system_diagnostics:
        for system_name in systems:
            diag = system_diagnostics.get(system_name, {})
            setup_count = diag.get("setup_predicate_count", 0)
            ranked_count = diag.get("ranked_top_n_count", 0)
            actual_count = candidate_counts.get(system_name, 0)

            if ranked_count > setup_count:
                logger.error(
                    "[ALLOC_DEBUG] %s: Logic error - ranked_count(%d) > "
                    "setup_count(%d). Actual candidates: %d",
                    system_name,
                    ranked_count,
                    setup_count,
                    actual_count,
                )

            if actual_count != ranked_count:
                logger.warning(
                    "[ALLOC_DEBUG] %s: Mismatch - diagnostics says %d, "
                    "but DataFrame has %d rows",
                    system_name,
                    ranked_count,
                    actual_count,
                )

    # Remember whether strategies was explicitly provided by the caller
    original_strategies_provided = strategies is not None

    # Structured diagnostics to assist root-cause analysis when allocations
    # produce zero final entries. Populated and attached to AllocationSummary
    # before return. This includes whether callers provided strategies/symbol map
    # and per-system candidacy/slot/budget info.
    diagnostics: dict[str, Any] = {}
    diagnostics["callers"] = {
        # strategies_provided_initial reflects whether the caller passed
        # strategies explicitly. strategies_provided reflects the final
        # state after any fallback construction.
        "strategies_provided_initial": bool(original_strategies_provided),
        "strategies_provided": bool(strategies),
        "symbol_system_map_provided": bool(symbol_system_map),
        "include_trade_management": bool(include_trade_management),
    }
    diagnostics["candidate_counts"] = dict(candidate_counts)
    diagnostics["active_positions"] = dict(active_positions)
    diagnostics["available_slots"] = dict(available_slots)
    diagnostics["max_pos_map"] = dict(max_pos_map)

    # Fallbacks: if callers didn't provide strategies or symbol_system_map,
    # attempt to build safe defaults. This reduces the chance of getting
    # zero entries when callers simply omitted these optional arguments.
    if not strategies:
        try:
            # Import known strategy classes and build mapping similar to other
            # tools/scripts that construct strategies. This is a best-effort
            # fallback and will be skipped on import errors.
            from strategies.system1_strategy import System1Strategy
            from strategies.system2_strategy import System2Strategy
            from strategies.system3_strategy import System3Strategy
            from strategies.system4_strategy import System4Strategy
            from strategies.system5_strategy import System5Strategy
            from strategies.system6_strategy import System6Strategy
            from strategies.system7_strategy import System7Strategy

            objs = [
                System1Strategy(),
                System2Strategy(),
                System3Strategy(),
                System4Strategy(),
                System5Strategy(),
                System6Strategy(),
                System7Strategy(),
            ]
            strategies = {getattr(s, "SYSTEM_NAME", "").lower(): s for s in objs}
            diagnostics["callers"]["strategies_provided"] = bool(strategies)
            logger.debug("[ALLOC_DEBUG] built fallback strategies mapping")
        except Exception:
            # Leave strategies as-is (None) if build fails
            logger.debug("[ALLOC_DEBUG] could not build fallback strategies mapping")

    if not symbol_system_map:
        try:
            # Use the module helper to load persisted mapping if available
            symbol_system_map = load_symbol_system_map()
            diagnostics["callers"]["symbol_system_map_provided"] = bool(
                symbol_system_map
            )
            logger.debug("[ALLOC_DEBUG] loaded symbol_system_map fallback")
        except Exception:
            symbol_system_map = {}
            logger.debug("[ALLOC_DEBUG] could not load symbol_system_map fallback")

    # Production safety: optionally require strategies to be explicitly provided.
    # Controlled by env var ALLOCATION_REQUIRE_STRATEGIES. Default is permissive
    # (do not raise) to avoid breaking existing tests/tools. When enabled, this
    # forces callers to pass strategies and helps catch omission errors early.
    try:
        require_strat = os.environ.get("ALLOCATION_REQUIRE_STRATEGIES", "0") == "1"
    except Exception:
        require_strat = False

    if require_strat and not strategies:
        # In strict mode, require that strategies are available (either
        # explicitly provided by the caller or constructed via fallback).
        # This avoids surprising test failures when callers omit the arg
        # but fallback succeeded.
        raise RuntimeError("finalize_allocation: strategies required")

    # Determine allocation mode.
    mode = "slot"
    if capital_long is not None or capital_short is not None:
        mode = "capital"

    if mode == "slot":
        if slots_long is None:
            slots_long = sum(available_slots.get(name, 0) for name in long_alloc)
        if slots_short is None:
            slots_short = sum(available_slots.get(name, 0) for name in short_alloc)
        slot_result = _allocate_by_slots(
            per_system_norm,
            long_alloc=long_alloc,
            short_alloc=short_alloc,
            available_slots=available_slots,
            slots_long=int(slots_long or 0),
            slots_short=int(slots_short or 0),
        )
        final_df = slot_result.frame
        summary = AllocationSummary(
            mode="slot",
            long_allocations=dict(long_alloc),
            short_allocations=dict(short_alloc),
            active_positions=dict(active_positions),
            available_slots=dict(available_slots),
            final_counts={},
            slot_allocation=dict(slot_result.distribution),
            slot_candidates=dict(slot_result.candidate_counts),
        )

        # Add slot-mode diagnostics
        try:
            diagnostics.setdefault("slot", {})
            diagnostics["slot"]["distribution"] = dict(slot_result.distribution)
            diagnostics["slot"]["candidate_counts"] = dict(slot_result.candidate_counts)
            if slot_result.dedup_stats:
                diagnostics["slot"]["dedup"] = slot_result.dedup_stats
        except Exception:
            pass

        # Slot モードでも実株数が必要な場合（UI/TradeManager 連携）、
        # 選抜済み候補に対して資本配分ロジックでサイズ決定を実施する。
        # 条件: include_trade_management=True（既定の呼び出しパス）
        try:
            if include_trade_management and not final_df.empty:
                # 総資本/比率の解決（既定値で安全に）
                ratio_conv = _safe_positive_float(default_long_ratio, allow_zero=True)
                ratio = ratio_conv if ratio_conv is not None else 0.5
                cap_conv = _safe_positive_float(default_capital, allow_zero=True)
                total_cap = cap_conv if cap_conv is not None else 100000.0
                long_cap = total_cap * ratio
                short_cap = total_cap * (1.0 - ratio)

                # 選抜済みスロットから side 別の per_system マップを作る
                selected_per_system_long: dict[str, pd.DataFrame] = {}
                selected_per_system_short: dict[str, pd.DataFrame] = {}
                if "system" in final_df.columns:
                    for pair in final_df.groupby("system", sort=False):
                        try:
                            sys_name = str(pair[0])
                            group = pair[1]
                        except Exception:
                            continue
                        key = str(sys_name).strip().lower()
                        try:
                            # そのまま side 列を尊重（slot 結果には付与済み）
                            is_short = False
                            if "side" in group.columns:
                                first_side = str(group["side"].iloc[0]).strip().lower()
                                is_short = first_side == "short"

                            grp = group.reset_index(drop=True)
                            if is_short:
                                selected_per_system_short[key] = grp
                            else:
                                selected_per_system_long[key] = grp
                        except Exception:
                            # 何かあれば long 側へ
                            selected_per_system_long[key] = group.reset_index(drop=True)

                # strategies 正規化 (slot-mode local variable)
                strategies_norm_slot: dict[str, object] = {}
                try:
                    for name, obj in (strategies or {}).items():
                        try:
                            strategies_norm_slot[str(name).strip().lower()] = obj
                        except Exception:
                            continue
                except Exception:
                    strategies_norm_slot = {}

                # 選抜済み候補に対し、各 side で資本配分により shares を算出
                long_sized = _allocate_by_capital(
                    selected_per_system_long,
                    strategies=strategies_norm_slot,
                    total_budget=long_cap,
                    weights=long_alloc,
                    side="long",
                    active_positions=active_positions,
                )
                short_sized = _allocate_by_capital(
                    selected_per_system_short,
                    strategies=strategies_norm_slot,
                    total_budget=short_cap,
                    weights=short_alloc,
                    side="short",
                    active_positions=active_positions,
                )

                # 非空のみ連結（FutureWarning 回避のため all-NA は除外）
                def _is_effectively_empty(_df: pd.DataFrame) -> bool:
                    try:
                        if _df is None or getattr(_df, "empty", True):
                            return True
                        return bool(_df.isna().all().all())
                    except Exception:
                        return True

                frames_cap = []
                for df in [long_sized.frame, short_sized.frame]:
                    try:
                        if df is not None and not _is_effectively_empty(df):
                            frames_cap.append(df)
                    except Exception:
                        continue
                if frames_cap:
                    final_df = _concat_nonempty(frames_cap)
                else:
                    # sizing に失敗した場合は従来の slot 結果を返す（後段で shares=0 警告になる）
                    final_df = slot_result.frame
        except Exception:
            # sizing での例外は致命的ではないため、slot 結果をそのまま継続
            final_df = slot_result.frame
    else:
        # Capital mode replicates ``run_all_systems_today`` defaults, with
        # stricter Optional handling for mypy friendliness.
        ratio_conv = _safe_positive_float(default_long_ratio, allow_zero=True)
        ratio = ratio_conv if ratio_conv is not None else 0.5
        cap_conv = _safe_positive_float(default_capital, allow_zero=True)
        default_cap_float = cap_conv if cap_conv is not None else 100000.0

        long_cap_opt = _safe_positive_float(capital_long)
        short_cap_opt = _safe_positive_float(capital_short)

        # Derive missing sides if one or both unspecified / non-positive.
        if long_cap_opt is None and short_cap_opt is None:
            total: float = default_cap_float
            long_cap_val: float = total * ratio
            short_cap_val: float = total * (1.0 - ratio)
        elif long_cap_opt is None and short_cap_opt is not None:
            total = short_cap_opt  # short_cap_opt is float (not None) in this branch
            long_cap_val = total * ratio
            short_cap_val = total * (1.0 - ratio)
        elif short_cap_opt is None and long_cap_opt is not None:
            total = long_cap_opt  # long_cap_opt is float (not None) here
            long_cap_val = total * ratio
            short_cap_val = total * (1.0 - ratio)
        else:
            # both provided (and positive) -> enforce non-None with assert for mypy
            assert long_cap_opt is not None and short_cap_opt is not None
            long_cap_val = long_cap_opt
            short_cap_val = short_cap_opt

        long_cap = long_cap_val
        short_cap = short_cap_val

        strategies_norm: dict[str, object] = {
            str(name).strip().lower(): obj for name, obj in (strategies or {}).items()
        }

        long_result = _allocate_by_capital(
            per_system_norm,
            strategies=strategies_norm,
            total_budget=long_cap,
            weights=long_alloc,
            side="long",
            active_positions=active_positions,
        )
        short_result = _allocate_by_capital(
            per_system_norm,
            strategies=strategies_norm,
            total_budget=short_cap,
            weights=short_alloc,
            side="short",
            active_positions=active_positions,
        )
        # Concatenate non-empty and non-all-NA frames only to avoid
        # pandas FutureWarning about dtype inference with empty/all-NA entries

        def _is_effectively_empty(_df: pd.DataFrame) -> bool:
            try:
                if _df is None or getattr(_df, "empty", True):
                    return True
                # all cells are NA -> treat as empty for concat purposes
                return bool(_df.isna().all().all())
            except Exception:
                return True

        frames = []
        for df in [long_result.frame, short_result.frame]:
            try:
                if df is not None and not _is_effectively_empty(df):
                    frames.append(df)
            except Exception:
                continue
        if frames:
            final_df = _concat_nonempty(frames)
        else:
            final_df = pd.DataFrame()

        budgets_combined: dict[str, float] = {}
        budgets_combined.update(long_result.budgets)
        budgets_combined.update(short_result.budgets)
        remaining_combined: dict[str, float] = {}
        remaining_combined.update(long_result.remaining)
        remaining_combined.update(short_result.remaining)

        summary = AllocationSummary(
            mode="capital",
            long_allocations=dict(long_alloc),
            short_allocations=dict(short_alloc),
            active_positions=dict(active_positions),
            available_slots=dict(available_slots),
            final_counts={},
            budgets=budgets_combined,
            budget_remaining=remaining_combined,
            capital_long=long_cap,
            capital_short=short_cap,
        )

        # Add capital-mode diagnostics from allocation results
        try:
            diagnostics.setdefault("capital", {})
            diagnostics["capital"]["long"] = {
                "budgets": dict(long_result.budgets),
                "remaining": dict(long_result.remaining),
                "counts": dict(long_result.counts),
            }
            diagnostics["capital"]["short"] = {
                "budgets": dict(short_result.budgets),
                "remaining": dict(short_result.remaining),
                "counts": dict(short_result.counts),
            }
        except Exception:
            pass

    if system_diagnostics:
        try:
            # Merge user-provided diagnostics with auto-collected diagnostics
            merged = dict(diagnostics or {})
            try:
                merged.update({str(k): v for k, v in system_diagnostics.items()})
            except Exception:
                # fallback: attach as-is
                merged["user"] = system_diagnostics
            summary.system_diagnostics = merged
        except Exception:
            try:
                summary.system_diagnostics = dict(system_diagnostics)
            except Exception:
                summary.system_diagnostics = None
    else:
        # Attach our collected diagnostics when caller didn't provide any
        try:
            summary.system_diagnostics = dict(diagnostics)
        except Exception:
            summary.system_diagnostics = diagnostics or None

    if not final_df.empty:
        final_df = _sort_final_frame(final_df)
    else:
        final_df = final_df.copy()

    # portfolio-level caps (Phase 5, 2026-07-07): 優先度順 (sort 済) の final_df に
    # 件数 (total/long/short) + gross/net exposure 上限を適用。default は no-op。
    # 既保有 + 新規 が上限を超えないよう新規を末尾から trim する。
    try:
        portfolio_caps = _load_portfolio_caps()
        equity_base = _safe_positive_float(default_capital, allow_zero=True) or 100000.0
        final_df, caps_report = _apply_portfolio_caps(
            final_df,
            caps=portfolio_caps,
            active_positions=positions,
            symbol_system_map=symbol_system_map,
            long_systems=list(long_alloc.keys()),
            short_systems=list(short_alloc.keys()),
            equity=float(equity_base),
        )
        try:
            existing_diag = summary.system_diagnostics or {}
            if not isinstance(existing_diag, dict):
                existing_diag = {}
            existing_diag = dict(existing_diag)
            existing_diag["portfolio_caps"] = caps_report
            summary.system_diagnostics = existing_diag
        except Exception:
            pass
    except (
        Exception
    ) as exc:  # noqa: BLE001 - caps は best-effort、失敗しても allocation 継続
        logger.warning("[PORTFOLIO_CAP] 適用に失敗、trim なしで継続: %s", exc)

    if "system" in final_df.columns:
        try:
            series = final_df["system"].astype(str)
            series = series.str.strip().str.lower()
            counts_series = series.value_counts()
            summary.final_counts = {str(k): int(v) for k, v in counts_series.items()}
        except Exception:
            summary.final_counts = {}
    else:
        summary.final_counts = {}

    # Add candidate counts for capital mode (slot mode already has them)
    if summary.slot_candidates is None:
        summary.slot_candidates = candidate_counts

    if include_trade_management:
        if market_data_dict is None or signal_date is None:
            raise ValueError(
                "market_data_dict and signal_date must be provided "
                "when include_trade_management is True."
            )
        # Convert cached frames so TradeManager receives a datetime index
        # Rolling cache commonly stores an integer index
        prepared_market_data = {}
        for sym, df in market_data_dict.items():
            if df is None or df.empty:
                continue
            # Check if 'date' column exists and index is not already datetime
            if "date" in df.columns and not isinstance(df.index, pd.DatetimeIndex):
                df_copy = df.copy()
                df_copy["date"] = pd.to_datetime(df_copy["date"])
                df_copy = df_copy.set_index("date", drop=False)
                prepared_market_data[sym] = df_copy
            else:
                prepared_market_data[sym] = df

        # TradeManager API: instantiate without args and enhance allocation
        tm = TradeManager()
        final_df = tm.enhance_allocation_with_trade_management(
            final_df, prepared_market_data, signal_date
        )

    # Final summary diagnostics
    if system_diagnostics:
        summary.system_diagnostics = {str(k): v for k, v in system_diagnostics.items()}

    # final_count
    try:
        summary.final_count = int(len(final_df))
    except Exception:
        summary.final_count = 0

    # final_symbols
    try:
        if "symbol" in final_df.columns:
            summary.final_symbols = [str(s) for s in final_df["symbol"].tolist()]
        else:
            summary.final_symbols = []
    except Exception:
        summary.final_symbols = []

    # Compute long/short counts. Prefer 'side' column if available, otherwise
    # fall back to numeric 'long'/'short' columns when present.
    try:
        if "side" in final_df.columns:
            side_series = final_df["side"].astype(str).str.lower()
            summary.final_long_count = int((side_series == "long").sum())
            summary.final_short_count = int((side_series == "short").sum())
        else:
            # Fallback to numeric columns named 'long'/'short'
            try:
                if "long" in final_df.columns:
                    long_series = pd.to_numeric(
                        final_df["long"], errors="coerce"
                    ).fillna(0)
                    summary.final_long_count = int(long_series.sum())
                else:
                    summary.final_long_count = 0
            except Exception:
                summary.final_long_count = 0

            try:
                if "short" in final_df.columns:
                    short_series = pd.to_numeric(
                        final_df["short"], errors="coerce"
                    ).fillna(0)
                    summary.final_short_count = int(short_series.sum())
                else:
                    summary.final_short_count = 0
            except Exception:
                summary.final_short_count = 0
    except Exception:
        summary.final_long_count = 0
        summary.final_short_count = 0

    return final_df, summary


def to_allocation_summary_dict(summary: AllocationSummary | Any) -> dict[str, Any]:
    """AllocationSummary もしくは類似オブジェクトを dict へ安全変換。

    - 既知フィールドを優先的に収集
    - 失敗しても空 dict
    - 追加フィールドにある程度耐性
    """
    try:
        fields = [
            "mode",
            "long_allocations",
            "short_allocations",
            "active_positions",
            "available_slots",
            "final_counts",
            "slot_allocation",
            "slot_candidates",
            "budgets",
            "budget_remaining",
            "capital_long",
            "capital_short",
            "system_diagnostics",
        ]
        out: dict[str, Any] = {}
        for f in fields:
            if hasattr(summary, f):
                try:
                    out[f] = getattr(summary, f)
                except Exception:
                    pass
        # 追加で *_allocations など緩く拾う（既存キー除外）
        try:
            for name in dir(summary):
                if name.startswith("_") or name in out:
                    continue
                if callable(getattr(summary, name, None)):
                    continue
                if name.endswith("_allocations"):
                    try:
                        out[name] = getattr(summary, name)
                    except Exception:
                        pass
        except Exception:
            pass
        return out
    except Exception:
        return {}


__all__ = [
    "AllocationSummary",
    "count_active_positions_by_system",
    "finalize_allocation",
    "load_symbol_system_map",
    "to_allocation_summary_dict",
]
