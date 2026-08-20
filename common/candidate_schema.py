"""Shared normalisation for backtest candidate payloads.

Both backtest engines (:mod:`common.backtest_utils` and
:mod:`common.integrated_backtest`) consume ``candidates_by_date`` produced by the
``core/systemN.generate_candidates_*`` functions, but those functions do not all
emit the same shape:

``{date: {symbol: payload}}``
    System2 / System4 / System5 / System6 / System7 latest-only. The engines
    injected ``entry_date`` for this form only.

``{date: [ {symbol, entry_date, ...}, ... ]}``
    System1 full scan, System7 full scan.

``{date: [ {symbol, date, ...}, ... ]}``
    **System3 full scan.** The signal date lives under ``date`` and there is no
    ``entry_date``. Before 2026-08-21 the engines looked up
    ``candidate["entry_date"]`` inside a bare ``except Exception: continue``, so
    every System3 candidate was dropped without a single log line and System3
    booked zero trades in every backtest and every validation report.

This module centralises the three shapes and - crucially - turns an
*unrecognised* shape into a loud :class:`CandidateSchemaError` instead of a
silent drop.

Live trading does not import this module: it is only reachable from the backtest
engines.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

import pandas as pd

__all__ = [
    "CANDIDATE_DATE_KEYS",
    "CandidateSchemaError",
    "normalize_candidates_for_date",
    "resolve_entry_bar",
]

#: Keys a candidate may use to say when the trade happens, most specific first.
CANDIDATE_DATE_KEYS = ("entry_date", "date")


class CandidateSchemaError(ValueError):
    """Raised when a backtest candidate cannot be mapped onto an entry bar.

    This is deliberately fatal: a candidate the engine cannot interpret means the
    system silently books nothing, which is indistinguishable from "the strategy
    had no signals" in every downstream report.
    """


def _describe(system: str | None) -> str:
    return f"{system}: " if system else ""


def _coerce_timestamp(value: Any) -> pd.Timestamp | None:
    if value is None:
        return None
    try:
        ts = pd.Timestamp(value)
    except Exception:
        return None
    try:
        if pd.isna(ts):
            return None
    except Exception:
        return None
    return ts


def normalize_candidates_for_date(
    candidates: Any,
    date: Any,
    *,
    system: str | None = None,
) -> list[dict[str, Any]]:
    """Return ``candidates`` for one signal date as a list of candidate dicts.

    ``{symbol: payload}`` mappings gain ``entry_date = date`` (unchanged
    behaviour). List/tuple payloads are passed through after validation.

    Raises:
        CandidateSchemaError: the container is neither a mapping nor a sequence,
            an element is not a mapping, or an element carries none of
            :data:`CANDIDATE_DATE_KEYS`.
    """
    if candidates is None:
        return []

    if isinstance(candidates, Mapping):
        out: list[dict[str, Any]] = []
        for sym, payload in candidates.items():
            if not isinstance(sym, str) or not sym:
                continue
            if payload is not None and not isinstance(payload, Mapping):
                raise CandidateSchemaError(
                    f"{_describe(system)}candidate payload for {sym!r} on "
                    f"{date!r} must be a mapping, got {type(payload).__name__}"
                )
            out.append(
                {
                    "symbol": str(sym),
                    "entry_date": pd.Timestamp(date),
                    **(dict(payload) if payload else {}),
                }
            )
        return out

    if isinstance(candidates, (str, bytes)) or not isinstance(candidates, Sequence):
        raise CandidateSchemaError(
            f"{_describe(system)}candidates for {date!r} must be a mapping or a "
            f"sequence of mappings, got {type(candidates).__name__}"
        )

    normalized: list[dict[str, Any]] = []
    for position, item in enumerate(candidates):
        if not isinstance(item, Mapping):
            raise CandidateSchemaError(
                f"{_describe(system)}candidate #{position} for {date!r} must be "
                f"a mapping, got {type(item).__name__}"
            )
        usable = any(
            _coerce_timestamp(item.get(key)) is not None
            for key in CANDIDATE_DATE_KEYS
        )
        if not usable:
            keys = sorted(str(k) for k in item.keys())
            raise CandidateSchemaError(
                f"{_describe(system)}candidate #{position} for {date!r} "
                f"(symbol={item.get('symbol')!r}) carries no usable entry date: "
                f"expected one of {list(CANDIDATE_DATE_KEYS)}, got keys={keys}"
            )
        normalized.append(dict(item))
    return normalized


def _positional(loc: Any) -> int | None:
    """Coerce an ``Index.get_loc`` result to a positional int, else ``None``."""
    if isinstance(loc, bool):
        return None
    if isinstance(loc, int):
        return int(loc)
    if isinstance(loc, slice):
        return None
    item = getattr(loc, "item", None)
    if callable(item):
        try:
            value = item()
        except Exception:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return int(value)
    return None


def _next_bar_after(df: pd.DataFrame, signal_ts: pd.Timestamp) -> int | None:
    """Positional index of the first bar strictly after ``signal_ts``."""
    try:
        index = pd.DatetimeIndex(df.index)
    except Exception:
        return None
    if len(index) == 0:
        return None
    try:
        probe = index.normalize()
        target = pd.Timestamp(signal_ts).normalize()
    except Exception:
        probe, target = index, signal_ts
    try:
        if probe.is_monotonic_increasing:
            position = int(probe.searchsorted(target, side="right"))
        else:
            later = [i for i, value in enumerate(probe) if value > target]
            position = later[0] if later else len(probe)
    except Exception:
        return None
    if position >= len(index):
        return None
    return position


def resolve_entry_bar(
    df: pd.DataFrame,
    candidate: Mapping[str, Any],
    *,
    system: str | None = None,
) -> tuple[int, pd.Timestamp] | None:
    """Resolve the bar a candidate would actually be entered on.

    Returns ``(positional_index, entry_timestamp)`` where ``entry_timestamp`` is
    the value taken straight out of ``df.index`` (so
    ``df.index.get_loc(entry_timestamp)`` succeeds for strategy hooks), or
    ``None`` when the bar simply is not in this frame - a data condition, not a
    schema problem (e.g. the signal fired on the last available bar).

    ``entry_date`` wins when it is present *and* present in the frame. Otherwise
    the signal ``date`` is advanced to the next bar in ``df`` - that bar is the
    one the strategies price off (``compute_entry`` reads ``entry_idx - 1`` for
    the previous close/ATR), so the frame's own calendar is authoritative.

    Raises:
        CandidateSchemaError: the candidate has no usable date key at all.
    """
    if df is None or getattr(df, "empty", True):
        return None

    def _bar_timestamp(position: int) -> pd.Timestamp:
        value: Any = df.index[position]
        return pd.Timestamp(value)

    entry_ts = _coerce_timestamp(candidate.get("entry_date"))
    signal_ts = _coerce_timestamp(candidate.get("date"))
    if entry_ts is None and signal_ts is None:
        keys = sorted(str(k) for k in candidate.keys())
        raise CandidateSchemaError(
            f"{_describe(system)}candidate (symbol={candidate.get('symbol')!r}) "
            f"carries no usable entry date: expected one of "
            f"{list(CANDIDATE_DATE_KEYS)}, got keys={keys}"
        )

    if entry_ts is not None:
        try:
            position = _positional(df.index.get_loc(entry_ts))
        except Exception:
            position = None
        if position is not None:
            return position, _bar_timestamp(position)

    if signal_ts is not None:
        position = _next_bar_after(df, signal_ts)
        if position is not None:
            return position, _bar_timestamp(position)

    return None
