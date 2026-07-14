"""Cache freshness / advancement guards.

Encodes the failure mode behind the 2026-07-12..14 dashboard freeze: the daily
pipeline kept reporting success (exit 0) while the rolling cache never advanced —
signals recomputed the same stale snapshot every day (a *silent no-op*), and no
guard flagged it.

These pure helpers operate on lightweight "manifests" — ``{symbol: {"last_date":
"YYYY-MM-DD", "n_rows": int, ...}}`` — so they are cheap to run over a whole cache
dir and trivial to unit-test with synthetic data (no network, no real cache).

Intended uses:
  * post-fetch / post-rolling-rebuild: assert something actually advanced
    (``is_silent_noop``) and nothing regressed (``detect_regressions``);
  * daily freshness monitor: compare rolling vs its upstream (full_backup/base)
    and warn when rolling lags (``lag_business_days``).
"""

from __future__ import annotations

from datetime import date, datetime


def _to_date(s: str | None) -> date | None:
    if not s:
        return None
    try:
        return datetime.strptime(str(s)[:10], "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None


def max_last_date(manifest: dict[str, dict]) -> str | None:
    """Newest ``last_date`` across all symbols, or None if the manifest is empty."""
    dates = [v.get("last_date") for v in manifest.values() if v and v.get("last_date")]
    return max(dates) if dates else None


def modal_last_date(manifest: dict[str, dict]) -> str | None:
    """Most common ``last_date`` (ties broken toward the newer date).

    Robust to a single fresh file (which fools ``max_last_date``) and to a long
    tail of delisted names: the actively-traded bulk dominates the mode.
    """
    from collections import Counter

    c = Counter(
        v.get("last_date") for v in manifest.values() if v and v.get("last_date")
    )
    if not c:
        return None
    return max(c.items(), key=lambda kv: (kv[1], kv[0] or ""))[0]


def detect_regressions(before: dict[str, dict], after: dict[str, dict]) -> list[str]:
    """Symbols whose last_date moved BACKWARD or whose row count shrank.

    Either signals that a write destroyed past bars — the opposite of the
    "monotonic non-decreasing per file" contract the cache must uphold.
    """
    bad: list[str] = []
    for sym, a in after.items():
        b = before.get(sym)
        if not b:
            continue
        bd, ad = _to_date(b.get("last_date")), _to_date(a.get("last_date"))
        if bd and ad and ad < bd:
            bad.append(sym)
            continue
        b_rows, a_rows = b.get("n_rows"), a.get("n_rows")
        if (
            isinstance(b_rows, int)
            and isinstance(a_rows, int)
            and b_rows > 0
            and a_rows < b_rows
        ):
            bad.append(sym)
    return bad


def count_advanced(before: dict[str, dict], after: dict[str, dict]) -> int:
    """How many symbols advanced their last_date (or are newly present with data)."""
    advanced = 0
    for sym, a in after.items():
        ad = _to_date(a.get("last_date"))
        if ad is None:
            continue
        b = before.get(sym)
        if b is None:
            advanced += 1
            continue
        bd = _to_date(b.get("last_date"))
        if bd is None or ad > bd:
            advanced += 1
    return advanced


def is_silent_noop(
    before: dict[str, dict], after: dict[str, dict], *, min_advanced: int = 1
) -> bool:
    """True when an "update" ran but (almost) nothing advanced.

    A healthy fetch/rebuild on a business day advances at least ``min_advanced``
    symbols. Zero advancement while claiming success is the silent-freeze bug.
    """
    return count_advanced(before, after) < max(1, int(min_advanced))


def symbols_behind_upstream(
    derived: dict[str, dict],
    upstream: dict[str, dict],
    *,
    universe: set[str] | None = None,
) -> list[str]:
    """Symbols whose derived (rolling) last_date is OLDER than their upstream's.

    This is the robust freeze signal: comparing max-date-vs-max-date is fooled by a
    single fresh file, and comparing medians is dragged down by delisted names that
    legitimately stopped trading. Per-symbol lag counts only symbols that fell behind
    *their own* upstream — delisted names (equally old upstream) are not counted.

    ``universe`` (optional) restricts the comparison to the signal universe, so
    symbols that are fetched into full_backup but intentionally NOT rebuilt into
    rolling (e.g. ETFs / non-common names outside the trading universe) do not
    masquerade as a freeze.
    """
    behind: list[str] = []
    for sym, u in upstream.items():
        if universe is not None and sym not in universe:
            continue
        d = derived.get(sym)
        if not d:
            continue
        du, dd = _to_date(u.get("last_date")), _to_date(d.get("last_date"))
        if du and dd and dd < du:
            behind.append(sym)
    return behind


def fraction_behind_upstream(
    derived: dict[str, dict],
    upstream: dict[str, dict],
    *,
    universe: set[str] | None = None,
) -> float:
    """Fraction of shared symbols whose derived cache lags its upstream (0.0..1.0)."""
    common = [
        s for s in upstream if s in derived and (universe is None or s in universe)
    ]
    if not common:
        return 0.0
    return len(symbols_behind_upstream(derived, upstream, universe=universe)) / len(
        common
    )


def lag_business_days(newest: str | None, reference: str | None) -> int | None:
    """Business-day gap between ``reference`` (expected newest) and ``newest``.

    Positive => the cache lags the reference by that many weekdays (freeze signal).
    ``None`` if either date is unparseable.
    """
    n, r = _to_date(newest), _to_date(reference)
    if n is None or r is None:
        return None
    if r <= n:
        return 0
    days = 0
    cur = n
    from datetime import timedelta

    while cur < r:
        cur += timedelta(days=1)
        if cur.weekday() < 5:  # Mon-Fri
            days += 1
    return days
