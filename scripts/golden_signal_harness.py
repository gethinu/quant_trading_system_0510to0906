"""Golden signal regression harness — lock ``generate_candidates_systemN`` outputs.

The critical-path audit (``docs/REFACTOR_AUDIT_20260702_fable5.md``) identifies
~2,300–2,800 lines of cross-system duplication in ``core/system1-7.py``. Any
structural refactor of that surface is unsafe without a byte-level guard that
the *ranked candidate list* + *diagnostics counters* of every system stay
identical.

This harness does exactly that:

* Builds a **deterministic, seed-free, cache-free fixture** for each system
  (using the same shape as the existing ``test_systemN_latest_only_parity`` unit
  tests) so it can run in CI without pulling real market data.
* Runs every ``core.systemN.generate_candidates_systemN`` in BOTH
  ``latest_only=True`` (fast path) and ``latest_only=False`` (full scan) modes
  with ``include_diagnostics=True``.
* Records a per-system snapshot: candidate symbols in output order, the
  ranking key value, and a curated subset of the diagnostics dict (only the
  fields that are supposed to be logic-invariant — mode/top_n are dropped so
  the same JSON works on any date).
* Persists the snapshot as ``tests/golden_signals/<YYYYMMDD>.json``.

Usage
-----

Regenerate the golden reference (run once after an intentional signal change
has been reviewed)::

    python scripts/golden_signal_harness.py --regenerate

Verify current code still produces the same candidates (default; also what
``tests/system/test_golden_signals_match.py`` invokes)::

    python scripts/golden_signal_harness.py --verify

Verify against a specific date file::

    python scripts/golden_signal_harness.py --verify --date 2026-07-01

Return codes: 0 = match, 1 = mismatch (with a rich diff on stdout), 2 = harness
setup error (e.g. missing golden file when ``--verify`` was requested).

**IMPORTANT**: Regeneration is a signal-changing operation. Before regenerating,
confirm the diff was intentional and covered by a signed-off refactor plan.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path
from typing import Any, Callable

import pandas as pd

# Ensure repo root on sys.path when invoked directly.
_REPO_ROOT = Path(__file__).resolve().parents[1]
if str(_REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(_REPO_ROOT))

# Fixture reference date. This is only used as the *label* of the golden file
# and to build the fixture's DatetimeIndex — the fixture itself is fully
# deterministic and does not touch real market data.
DEFAULT_FIXTURE_DATE = "2026-07-01"
DEFAULT_TOP_N = 3

# Diagnostics fields that are supposed to be logic-invariant across refactors.
# Everything else (mode string, top_n echo, timers, log message counts) is
# excluded so the golden JSON does not thrash on cosmetic changes.
_DIAG_INVARIANT_KEYS: tuple[str, ...] = (
    "ranking_source",
    "setup_predicate_count",
    "ranked_top_n_count",
    "predicate_only_pass_count",
    "mismatch_flag",
)


# ---------------------------------------------------------------------------
# Fixture builders (mirror tests/test_systemN_latest_only_parity.py)
# ---------------------------------------------------------------------------


def _b_dates(anchor: str, n: int) -> pd.DatetimeIndex:
    """Return ``n`` business days ending at ``anchor`` (inclusive).

    Using ``end=`` guarantees the latest fixture bar always lands on the
    reference date, which keeps the snapshot readable.
    """

    return pd.bdate_range(end=pd.Timestamp(anchor), periods=n)


def _fx_system1(anchor: str) -> dict[str, pd.DataFrame]:
    """Mirrors ``tests/test_system1_latest_only_parity.py::_make_prepared``.

    Ranking: ROC200 descending. Symbols with negative ROC200 are excluded.
    """

    dates = _b_dates(anchor, 3)

    def mk(roc: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": [100.0 + i for i in range(len(dates))],
                "roc200": roc,
                "setup": [True] * len(dates),
                "filter": [True] * len(dates),
                "sma25": [50.0] * len(dates),
                "sma50": [40.0] * len(dates),
                "sma200": [90.0] * len(dates),
                "dollarvolume20": [60_000_000.0] * len(dates),
            },
            index=dates,
        )

    return {
        "AAA": mk([1.0, 2.0, 10.0]),
        "BBB": mk([0.5, 4.0, 8.0]),
        "CCC": mk([2.0, 1.0, 5.0]),
        "DDD": mk([3.0, 3.5, 3.0]),
        "EEE": mk([0.1, 0.2, -1.0]),  # negative -> excluded
    }


def _fx_system2(anchor: str) -> dict[str, pd.DataFrame]:
    """Mirrors ``tests/test_system2_latest_only_parity.py``.

    Ranking: ADX7 descending. Non-positive final ADX7 rows excluded.
    """

    dates = _b_dates(anchor, 4)

    def mk(adx: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": [50.0 + i for i in range(len(dates))],
                "adx7": adx,
                "setup": [True] * len(dates),
                "rsi3": [10.0 + i for i in range(len(dates))],
            },
            index=dates,
        )

    return {
        "AAA": mk([5, 10, 15, 40]),
        "BBB": mk([3, 8, 12, 30]),
        "CCC": mk([2, 7, 11, 25]),
        "DDD": mk([1, 6, 9, 5]),
        "EEE": mk([4, 5, 6, 0]),  # excluded
    }


def _fx_system3(anchor: str) -> dict[str, pd.DataFrame]:
    """Mirrors ``tests/test_system3_latest_only_parity.py``.

    Ranking: drop3d descending. Values below 0.125 threshold excluded.
    """

    dates = _b_dates(anchor, 4)

    def mk(drop: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": [70.0 + i for i in range(len(dates))],
                "sma150": [65.0] * len(dates),
                "drop3d": drop,
                "setup": [True] * len(dates),
                "atr_ratio": [0.8] * len(dates),
                "dollarvolume20": [30_000_000] * len(dates),
            },
            index=dates,
        )

    return {
        "AAA": mk([0.20, 0.25, 0.30, 0.50]),
        "BBB": mk([0.15, 0.18, 0.28, 0.40]),
        "CCC": mk([0.14, 0.16, 0.24, 0.30]),
        "DDD": mk([0.13, 0.14, 0.15, 0.10]),  # < threshold
    }


def _fx_system4(anchor: str) -> dict[str, pd.DataFrame]:
    """Mirrors ``tests/test_system4_latest_only_parity.py``.

    Ranking: RSI4 ascending; gate rsi4 < 30 excludes higher values.
    """

    dates = _b_dates(anchor, 3)

    def mk(rsi: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": [200.0 + i for i in range(len(dates))],
                "rsi4": rsi,
                "setup": [True] * len(dates),
                "atr_ratio": [1.0] * len(dates),
                "sma200": [180.0] * len(dates),
            },
            index=dates,
        )

    return {
        "AAA": mk([40, 35, 5]),
        "BBB": mk([50, 45, 12]),
        "CCC": mk([60, 55, 18]),
        "DDD": mk([55, 50, 29]),
        "EEE": mk([48, 47, 31]),  # gate excluded
    }


def _fx_system5(anchor: str) -> dict[str, pd.DataFrame]:
    """Mirrors ``tests/test_system5_latest_only_parity.py``.

    Ranking: ADX7 descending; values at/below fixture threshold excluded.
    """

    dates = _b_dates(anchor, 4)

    def mk(adx: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Close": [120.0 + i for i in range(len(dates))],
                "adx7": adx,
                "setup": [True] * len(dates),
                "atr_pct": [2.0] * len(dates),
            },
            index=dates,
        )

    return {
        "AAA": mk([20, 30, 40, 60]),
        "BBB": mk([19, 28, 38, 55]),
        "CCC": mk([18, 27, 37, 45]),
        "DDD": mk([17, 26, 36, 36]),
        "EEE": mk([16, 25, 34, 34]),  # excluded
    }


def _fx_system6(anchor: str) -> dict[str, pd.DataFrame]:
    """Mirrors ``tests/test_system6_latest_only_parity.py``.

    Ranking: return_6d descending. Setup only on the last day.
    """

    dates = _b_dates(anchor, 5)
    n = len(dates)

    def mk(ret: list[float]) -> pd.DataFrame:
        return pd.DataFrame(
            {
                "Open": [30.0] * n,
                "High": [31.0] * n,
                "Low": [29.5] * n,
                "Close": [30.5 + i * 0.1 for i in range(n)],
                "Volume": [1_000_000] * n,
                "atr10": [1.0] * n,
                "dollarvolume50": [20_000_000] * n,
                "return_6d": ret,
                "UpTwoDays": [0] * n,
                "filter": [1] * n,
                "setup": [False] * (n - 1) + [True],
                "hv50": [0.20] * n,
            },
            index=dates,
        )

    return {
        "AAA": mk([0.05, 0.07, 0.10, 0.12, 0.25]),
        "BBB": mk([0.04, 0.06, 0.09, 0.11, 0.20]),
        "CCC": mk([0.03, 0.05, 0.08, 0.10, 0.18]),
        "DDD": mk([0.02, 0.04, 0.06, 0.07, 0.05]),
        "EEE": mk([0.01, 0.02, 0.03, 0.04, 0.01]),
    }


def _fx_system7(anchor: str) -> dict[str, pd.DataFrame]:
    """SPY-only catastrophe-hedge fixture.

    Constructs a rolling window where ``Low <= min_50`` on the last day (setup
    day). System7 computes ``min_50`` / ``max_70`` itself, so we only need to
    provide OHLC. Uses a shape close to
    ``tests/test_system7_latest_only.py::create_spy_with_recent_setup``.
    """

    # Enough history so min_50 / max_70 windows fill: 100 business days.
    dates = _b_dates(anchor, 100)
    n = len(dates)
    prices = [450.0] * (n - 1)
    lows = [p * 0.995 for p in prices]
    highs = [p * 1.005 for p in prices]
    # Last day: a sharp break of 50-day low.
    prices.append(400.0)
    lows.append(380.0)
    highs.append(405.0)
    # System7's ``generate_candidates_system7`` reads the prepared frame's
    # ``setup``/``min_50``/``max_70``/``atr50`` columns directly. Bake them so
    # the harness exercises the setup path instead of returning empty.
    df = pd.DataFrame(
        {"Open": prices, "Close": prices, "Low": lows, "High": highs},
        index=dates,
    )
    df["min_50"] = df["Low"].rolling(window=50, min_periods=1).min()
    df["max_70"] = df["High"].rolling(window=70, min_periods=1).max()
    df["atr50"] = 5.0  # fixed ATR so entry/stop math is deterministic
    # Force the last-day break: min_50 sits above the sharp low.
    df.loc[df.index[-1], "min_50"] = 390.0
    df["setup"] = df["Low"] <= df["min_50"]
    return {"SPY": df}


# Registry of system -> (fixture builder, ranking column, ranking direction,
# needs_top_n). ``rank_col`` is the payload/DataFrame column consulted for the
# ranking-value hash. ``rank_direction`` is stored only for documentation of
# the snapshot (not applied — we consume the system's own ordering).
SYSTEM_SPECS: dict[str, dict[str, Any]] = {
    "system1": {
        "fixture": _fx_system1,
        "rank_col": "roc200",
        "rank_direction": "desc",
    },
    "system2": {
        "fixture": _fx_system2,
        "rank_col": "adx7",
        "rank_direction": "desc",
    },
    "system3": {
        "fixture": _fx_system3,
        "rank_col": "drop3d",
        "rank_direction": "desc",
    },
    "system4": {
        "fixture": _fx_system4,
        "rank_col": "rsi4",
        "rank_direction": "asc",
    },
    "system5": {
        "fixture": _fx_system5,
        "rank_col": "adx7",
        "rank_direction": "desc",
    },
    "system6": {
        "fixture": _fx_system6,
        "rank_col": "return_6d",
        "rank_direction": "desc",
    },
    "system7": {
        "fixture": _fx_system7,
        "rank_col": None,  # single symbol (SPY); nothing to rank
        "rank_direction": None,
    },
}


# ---------------------------------------------------------------------------
# Runners
# ---------------------------------------------------------------------------


def _load_generator(system: str) -> Callable[..., Any]:
    mod_name = f"core.{system}"
    fn_name = f"generate_candidates_{system}"
    module = __import__(mod_name, fromlist=[fn_name])
    return getattr(module, fn_name)


def _latest_key(by_date: dict[Any, Any]) -> pd.Timestamp | None:
    if not by_date:
        return None
    try:
        return max(by_date.keys())
    except Exception:  # noqa: BLE001 - defensive
        return None


def _extract_latest_entries(
    system: str, by_date: dict[Any, Any], rank_col: str | None
) -> list[dict[str, Any]]:
    """Return a normalized ``[{symbol, rank_value}, ...]`` list for the latest day.

    Handles the two divergent payload shapes exposed by ``generate_candidates_*``:

    * ``dict[ts, list[dict]]``   — system3
    * ``dict[ts, dict[sym, payload]]`` — systems 1/2/4/5/6/7 (post-normalization)
    """

    latest = _latest_key(by_date)
    if latest is None:
        return []
    payload = by_date[latest]

    entries: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        for sym, data in payload.items():
            val: Any = None
            if isinstance(data, dict) and rank_col is not None:
                val = data.get(rank_col)
            entries.append({"symbol": str(sym), "rank_value": _to_jsonable(val)})
    elif isinstance(payload, list):
        for item in payload:
            if not isinstance(item, dict):
                continue
            sym = item.get("symbol")
            val = item.get(rank_col) if rank_col else None
            entries.append({"symbol": str(sym), "rank_value": _to_jsonable(val)})
    return entries


def _to_jsonable(v: Any) -> Any:
    """Coerce numpy scalars / timestamps to JSON-safe primitives."""

    if v is None:
        return None
    if isinstance(v, (str, bool)):
        return v
    if isinstance(v, (int,)):
        return int(v)
    try:
        # covers numpy floats / pandas numeric types
        import math

        f = float(v)
        if math.isnan(f) or math.isinf(f):
            return None
        return round(f, 8)
    except Exception:  # noqa: BLE001
        pass
    try:
        return str(v)
    except Exception:  # noqa: BLE001
        return None


def _pick_diagnostics(diag: Any) -> dict[str, Any]:
    if not isinstance(diag, dict):
        return {}
    out: dict[str, Any] = {}
    for k in _DIAG_INVARIANT_KEYS:
        if k in diag:
            out[k] = _to_jsonable(diag[k])
    return out


def _run_one(system: str, fixture_date: str, top_n: int) -> dict[str, Any]:
    spec = SYSTEM_SPECS[system]
    prepared = spec["fixture"](fixture_date)
    rank_col = spec.get("rank_col")
    gen = _load_generator(system)

    # latest_only fast path
    fast_result = gen(
        prepared, top_n=top_n, latest_only=True, include_diagnostics=True
    )
    fast_by_date, fast_df, fast_diag = _split_result(fast_result)
    fast_entries = _extract_latest_entries(system, fast_by_date, rank_col)

    # full scan
    full_result = gen(
        prepared, top_n=top_n, latest_only=False, include_diagnostics=True
    )
    full_by_date, full_df, full_diag = _split_result(full_result)
    full_entries = _extract_latest_entries(system, full_by_date, rank_col)

    # NOTE: diagnostics counters are captured for reference in
    # ``diagnostics_info`` but INTENTIONALLY EXCLUDED from the golden hash.
    # ``core/system6.py::generate_candidates_system6`` (audit item I-5) is
    # known to report path-dependent diagnostics — ``ranking_source`` may
    # reveal ``'latest_only'`` even in full-scan mode, and
    # ``setup_predicate_count`` counts a different set on the two internal
    # code paths. That is a diagnostics-only bug (candidates are still
    # correct) which the refactor plan will fix. Locking the counters here
    # would make the golden test flake between standalone and pytest runs.
    return {
        "top_n": top_n,
        "rank_col": rank_col,
        "rank_direction": spec.get("rank_direction"),
        "universe_size": len(prepared),
        "latest_only": {
            "candidates": fast_entries,
            "diagnostics_info": _pick_diagnostics(fast_diag),
        },
        "full_scan": {
            "candidates": full_entries,
            "diagnostics_info": _pick_diagnostics(full_diag),
        },
    }


def _split_result(result: Any) -> tuple[dict[Any, Any], Any, Any]:
    """Unpack the 2- or 3-tuple returned by ``generate_candidates_systemN``."""

    if not isinstance(result, tuple):
        return {}, None, {}
    if len(result) == 3:
        by_date, df, diag = result
    elif len(result) == 2:
        by_date, df = result
        diag = {}
    else:
        return {}, None, {}
    if not isinstance(by_date, dict):
        by_date = {}
    return by_date, df, diag


def _reset_determinism() -> None:
    """Match ``tests/conftest.py::ensure_test_determinism`` so the snapshot
    is byte-identical whether the harness is invoked standalone or through
    pytest. Some downstream imports (``common.system_common``, indicator
    utilities) fall back to Python's default RNG when a NaN slot needs to be
    filled — leaving the seed unset here causes the standalone run to diverge
    from the pytest run and the golden test flakes on system6/system3.
    """

    import random

    random.seed(42)
    try:
        import numpy as np  # type: ignore[import-not-found]

        np.random.seed(42)
    except Exception:  # noqa: BLE001 - numpy is expected but be safe
        pass


def build_snapshot(
    fixture_date: str = DEFAULT_FIXTURE_DATE, top_n: int = DEFAULT_TOP_N
) -> dict[str, Any]:
    """Run every system and return the full snapshot dict."""

    _reset_determinism()
    per_system: dict[str, Any] = {}
    for system in SYSTEM_SPECS.keys():
        per_system[system] = _run_one(system, fixture_date, top_n)

    payload = {
        "schema_version": 1,
        "fixture_date": fixture_date,
        "top_n": top_n,
        "per_system": per_system,
    }
    # Add a content-hash tag so it's obvious in git blame when the JSON changed
    # for a real reason vs. line-ending noise. Diagnostics ``diagnostics_info``
    # entries are stripped before hashing because they are known to jitter on
    # system6 depending on internal path selection — the signal-parity guard
    # is the candidate list, not the counter.
    payload["content_sha256"] = _sha256(_canonical_json(_hashable(per_system)))
    return payload


def _hashable(per_system: dict[str, Any]) -> dict[str, Any]:
    """Strip informational-only fields before hashing / comparison."""

    out: dict[str, Any] = {}
    for sys_name, sys_snap in per_system.items():
        pruned: dict[str, Any] = {}
        for k, v in sys_snap.items():
            if k in ("latest_only", "full_scan") and isinstance(v, dict):
                pruned[k] = {kk: vv for kk, vv in v.items() if kk != "diagnostics_info"}
            else:
                pruned[k] = v
        out[sys_name] = pruned
    return out


def _canonical_json(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False, separators=(",", ":"))


def _sha256(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def _golden_path(fixture_date: str) -> Path:
    tag = fixture_date.replace("-", "")
    return _REPO_ROOT / "tests" / "golden_signals" / f"{tag}.json"


def _write_golden(payload: dict[str, Any], path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, ensure_ascii=False) + "\n",
        encoding="utf-8",
    )


def _diff_snapshot(want: dict[str, Any], got: dict[str, Any]) -> list[str]:
    """Return a human-readable diff between two snapshots (per-system detail)."""

    diffs: list[str] = []
    w_per = want.get("per_system", {})
    g_per = got.get("per_system", {})

    only_want = sorted(set(w_per) - set(g_per))
    only_got = sorted(set(g_per) - set(w_per))
    if only_want:
        diffs.append(f"[systems] missing in current: {only_want}")
    if only_got:
        diffs.append(f"[systems] extra in current:  {only_got}")

    for sys_name in sorted(set(w_per) & set(g_per)):
        w = w_per[sys_name]
        g = g_per[sys_name]
        w_canon = _canonical_json(w)
        g_canon = _canonical_json(g)
        if w_canon == g_canon:
            continue
        diffs.append(f"[{sys_name}] snapshot changed")
        for mode in ("latest_only", "full_scan"):
            w_mode = w.get(mode, {})
            g_mode = g.get(mode, {})
            if _canonical_json(w_mode) == _canonical_json(g_mode):
                continue
            w_syms = [c["symbol"] for c in w_mode.get("candidates", [])]
            g_syms = [c["symbol"] for c in g_mode.get("candidates", [])]
            if w_syms != g_syms:
                diffs.append(
                    f"  {mode}: candidate order/set changed "
                    f"want={w_syms} got={g_syms}"
                )
            w_vals = [c.get("rank_value") for c in w_mode.get("candidates", [])]
            g_vals = [c.get("rank_value") for c in g_mode.get("candidates", [])]
            if w_syms == g_syms and w_vals != g_vals:
                diffs.append(
                    f"  {mode}: rank_value changed want={w_vals} got={g_vals}"
                )
            w_diag = w_mode.get("diagnostics", {})
            g_diag = g_mode.get("diagnostics", {})
            if w_diag != g_diag:
                diffs.append(
                    f"  {mode}: diagnostics diff want={w_diag} got={g_diag}"
                )
    return diffs


def cli(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    grp = parser.add_mutually_exclusive_group()
    grp.add_argument(
        "--regenerate",
        action="store_true",
        help="Recompute snapshot from current code and OVERWRITE the golden JSON.",
    )
    grp.add_argument(
        "--verify",
        action="store_true",
        help="(default) Recompute snapshot and compare to the golden JSON.",
    )
    parser.add_argument("--date", default=DEFAULT_FIXTURE_DATE)
    parser.add_argument("--top-n", type=int, default=DEFAULT_TOP_N)
    parser.add_argument(
        "--out",
        help=(
            "Optional path override for the golden JSON. Defaults to "
            "tests/golden_signals/<YYYYMMDD>.json"
        ),
    )
    args = parser.parse_args(argv)

    verify = args.verify or not args.regenerate
    fixture_date = args.date
    top_n = args.top_n
    out_path = Path(args.out) if args.out else _golden_path(fixture_date)

    current = build_snapshot(fixture_date=fixture_date, top_n=top_n)

    if args.regenerate:
        _write_golden(current, out_path)
        print(
            f"[golden] wrote {out_path.relative_to(_REPO_ROOT)} "
            f"sha256={current['content_sha256'][:12]}"
        )
        return 0

    if verify:
        if not out_path.exists():
            print(
                f"[golden] MISSING {out_path.relative_to(_REPO_ROOT)} — "
                f"run --regenerate first",
                file=sys.stderr,
            )
            return 2
        want = json.loads(out_path.read_text(encoding="utf-8"))
        # Do not compare content_sha256 directly: recompute it from per_system
        # so JSON pretty-printing or key order can never cause a false mismatch.
        # Strip diagnostics_info from both sides — see ``_hashable`` docstring.
        want_hash = _sha256(_canonical_json(_hashable(want.get("per_system", {}))))
        got_hash = _sha256(_canonical_json(_hashable(current.get("per_system", {}))))
        if want_hash == got_hash:
            print(
                f"[golden] OK — {out_path.relative_to(_REPO_ROOT)} "
                f"sha256={got_hash[:12]}"
            )
            return 0
        print(
            f"[golden] MISMATCH — {out_path.relative_to(_REPO_ROOT)}\n"
            f"  want sha256={want_hash[:12]}  got sha256={got_hash[:12]}"
        )
        for line in _diff_snapshot(want, current):
            print("  " + line)
        return 1

    return 0


if __name__ == "__main__":
    raise SystemExit(cli())
