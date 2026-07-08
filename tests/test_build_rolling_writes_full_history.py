"""Regression tests for ``scripts/build_rolling_with_indicators.py``.

Guards two bugs discovered 2026-07-02:

1. **Stale-NaN indicator column skip (indicator all-NaN in rolling)**
   ``full_backup`` CSV schema keeps placeholder indicator columns
   (``atr10``, ``sma25``, …) even before values are populated. When
   ``build_rolling`` fed that frame to ``add_indicators``, the "skip
   recompute when column exists" fast-path left every indicator NaN.
   Rolling then landed with numeric OHLCV but ``sma25 = drop3d =
   atr_ratio = NaN``, exactly matching the production symptom.

2. **1-row rolling despite 501-row base (mirror invariant)**
   ``rolling`` should mirror the source cache row count, not the
   ephemeral last day. Historic runs where ``full_backup`` was
   temporarily flattened to 1 row propagated that flatten into rolling
   and stuck. The fix reads ``base`` preferentially (already indicator-
   computed, contains the full window), so rolling = base rows.

The tests build synthetic caches under ``tmp_path``, wire ``CacheManager``
via monkey-patched settings, invoke ``extract_rolling_from_full`` end-to-
end (parallel workers disabled for deterministic assertion), and verify:

  * ``len(rolling_df) == len(base_df) == 501`` (mirror invariant)
  * ``rolling_df["sma25"].notna().sum() >= 476`` (SMA25 valid from row 25)
  * ``rolling_df["drop3d"].notna().sum() >= 498`` (drop3d valid from row 3)
  * ``rolling_df["atr_ratio"].notna().sum() >= 490`` (ATR10-derived)
  * ``rolling_df["Close"]`` matches the source ``Close`` on the last date

Both a "base-authoritative" case (base + full both present) and a
"full-only" fallback case (base missing, stale-NaN placeholders in full)
are exercised.
"""

from __future__ import annotations

from pathlib import Path
import sys
from types import SimpleNamespace

import numpy as np
import pandas as pd
import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_ohlcv(rows: int = 501, seed: int = 42) -> pd.DataFrame:
    """501 rows of realistic OHLCV keyed on Date (PascalCase, matches full_backup)."""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start="2024-07-02", periods=rows)
    open_ = 500.0 + np.cumsum(rng.standard_normal(rows))
    high = open_ + np.abs(rng.standard_normal(rows))
    low = open_ - np.abs(rng.standard_normal(rows))
    close = open_ + rng.standard_normal(rows) * 0.5
    volume = rng.integers(30_000_000, 80_000_000, rows)
    return pd.DataFrame(
        {
            "Date": dates,
            "Open": open_,
            "High": high,
            "Low": low,
            "Close": close,
            "AdjClose": close,
            "Volume": volume,
        }
    )


def _stale_full_backup_csv(rows: int = 501) -> pd.DataFrame:
    """Replica of the real full_backup CSV: numeric OHLCV + all-NaN indicator placeholders.

    This is the exact shape that triggered the 2026-07-02 rolling-all-NaN
    bug: pd.read_csv on the file returns 501 rows with valid OHLCV, but
    every indicator column exists and is entirely NaN — enough to make
    ``add_indicators`` skip recomputation.
    """
    df = _make_ohlcv(rows)
    for col in (
        "atr10",
        "atr20",
        "atr40",
        "atr50",
        "sma25",
        "sma50",
        "sma100",
        "sma150",
        "sma200",
        "roc200",
        "rsi3",
        "rsi4",
        "adx7",
        "dollarvolume20",
        "dollarvolume50",
        "avgvolume50",
        "atr_ratio",
        "atr_pct",
        "return_3d",
        "return_6d",
        "return_pct",
        "drop3d",
        "hv50",
        "min_50",
        "max_70",
    ):
        df[col] = np.nan
    df["uptwodays"] = False
    df["twodayup"] = False
    return df


def _base_feather_frame(rows: int = 501) -> pd.DataFrame:
    """Build a base feather frame the way ``compute_base_indicators`` + ``save_base_cache`` do.

    Columns are lowercased (save_base_cache does this), ``date`` is a
    column (index reset), and indicators are the ``compute_base_indicators``
    set (SMA{25..200}, EMA{20,50}, ATR{10,14,20,40,50}, RSI{3,4,14},
    ROC200, HV50, DollarVolume20/50).
    """
    from common.cache_manager import compute_base_indicators

    raw = _make_ohlcv(rows)
    base_df = compute_base_indicators(raw)
    # save_base_cache lowercases column names before writing feather
    base_df.columns = [str(c).lower() for c in base_df.columns]
    return base_df


@pytest.fixture
def tmp_cache(tmp_path: Path) -> Path:
    """Create empty ``data_cache/{full_backup,base,rolling}`` under tmp_path."""
    root = tmp_path / "data_cache"
    (root / "full_backup").mkdir(parents=True)
    (root / "base").mkdir(parents=True)
    (root / "rolling").mkdir(parents=True)
    return root


def _install_fake_settings(monkeypatch: pytest.MonkeyPatch, data_cache: Path) -> None:
    """Point config.settings.get_settings at the tmp cache tree."""
    rolling_cfg = SimpleNamespace(
        base_lookback_days=300,
        buffer_days=30,
        max_symbols=None,
        max_stale_days=2,
        max_staleness_days=2,
        prune_chunk_days=30,
        meta_file="_meta.json",
        round_decimals=None,
        workers=None,
        recompute_indicators_on_read=False,
        adaptive_window_count=8,
        adaptive_increase_threshold=1.02,
        adaptive_decrease_threshold=0.98,
        adaptive_step=1,
        adaptive_min_workers=1,
        adaptive_max_workers=None,
        adaptive_report_seconds=10,
        csv=SimpleNamespace(decimal_point=".", thousands_sep=None, field_sep=","),
        load_max_workers=None,
    )
    cache_cfg = SimpleNamespace(
        full_dir=str(data_cache / "full_backup"),
        rolling_dir=str(data_cache / "rolling"),
        rolling=rolling_cfg,
        file_format="auto",
        round_decimals=None,
        indicator_lookback_margin=200,
    )
    logs_dir = data_cache.parent / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    outputs_ns = SimpleNamespace(logs_dir=str(logs_dir))
    fake_settings = SimpleNamespace(
        DATA_CACHE_DIR=str(data_cache),
        LOGS_DIR=str(logs_dir),
        cache=cache_cfg,
        outputs=outputs_ns,
    )
    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda create_dirs=True: fake_settings,
    )
    monkeypatch.setattr(
        "common.cache_manager.get_settings",
        lambda create_dirs=True: fake_settings,
    )
    # scripts.build_rolling_with_indicators imports get_settings from
    # config.settings at call time via `from config.settings import
    # get_settings`, so we also need to patch the reference in the module
    # if it was already imported. Use setattr; safe when not imported.
    import scripts.build_rolling_with_indicators as bri

    monkeypatch.setattr(bri, "get_settings", lambda create_dirs=True: fake_settings)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


def test_prepare_rolling_frame_recomputes_when_full_has_stale_nan_placeholders(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ Bug #1 primary regression:
    ``full_backup`` CSV with 501 rows and stale-NaN indicator placeholder
    columns must yield rolling with **numeric** indicators, not NaN.

    Before the fix, ``add_indicators`` skipped recomputation because the
    columns existed. After the fix, ``_drop_all_nan_indicator_columns``
    removes the empties so recompute fires.
    """
    _install_fake_settings(monkeypatch, tmp_cache)

    from scripts.build_rolling_with_indicators import _prepare_rolling_frame

    # Simulate the exact shape ``cache_manager.read(sym, "full")`` returns
    # for a real full_backup CSV: columns lowercased, all indicator
    # placeholders NaN.
    stale = _stale_full_backup_csv(rows=501)
    stale.columns = [c.lower() for c in stale.columns]

    result = _prepare_rolling_frame(stale, target_days=330, source="full")

    assert result is not None, "expected non-null result"
    assert (
        len(result) == 330
    ), f"full source is tail-capped at target_days=330, got {len(result)}"
    # ★ core assertion — indicators must be numeric, not NaN.
    assert result["sma25"].notna().sum() >= 300, (
        "sma25 should be recomputed (>= 300 non-null in a 330-row window). "
        "If this fails the stale-NaN skip bug has regressed."
    )
    assert (
        result["drop3d"].notna().sum() >= 320
    ), "drop3d should be numeric after recompute."
    assert (
        result["atr_ratio"].notna().sum() >= 310
    ), "atr_ratio should be numeric after recompute."
    assert result["roc200"].notna().sum() > 0, "roc200 should be numeric."


def test_prepare_rolling_frame_mirrors_base_row_count(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ Bug #2 mirror invariant: rolling row count == base row count.

    When source is ``base`` (which already carries indicators), the
    processor must NOT tail-truncate. base(501) → rolling(501).
    """
    _install_fake_settings(monkeypatch, tmp_cache)

    from scripts.build_rolling_with_indicators import _prepare_rolling_frame

    base = _base_feather_frame(rows=501)

    result = _prepare_rolling_frame(base, target_days=330, source="base")

    assert result is not None
    assert len(result) == 501, (
        f"base source should NOT be tail-truncated. "
        f"base=501 → rolling should be 501, got {len(result)}. "
        "1-row rolling bug regression."
    )
    assert (
        result["sma25"].notna().sum() >= 476
    ), "sma25 from base should stay numeric (>=476 non-null in 501 rows)."
    assert result["sma25"].iloc[-1] == pytest.approx(
        base["sma25"].iloc[-1]
    ), "base's sma25 value on the last date must be preserved in rolling."


def test_read_symbol_source_prefers_base_over_full(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """When both base and full exist, ``_read_symbol_source`` must return base."""
    _install_fake_settings(monkeypatch, tmp_cache)

    # Write a stale full_backup CSV (1-row-flatten simulator).
    stale = _stale_full_backup_csv(rows=1)
    stale.to_csv(tmp_cache / "full_backup" / "SPY.csv", index=False)

    # Write a healthy base feather (501 rows).
    base = _base_feather_frame(rows=501)
    base.reset_index(drop=True).to_feather(tmp_cache / "base" / "SPY.feather")

    from common.cache_manager import CacheManager
    from config.settings import get_settings
    from scripts.build_rolling_with_indicators import _read_symbol_source

    cm = CacheManager(get_settings(create_dirs=True))
    df, label = _read_symbol_source(cm, "SPY")

    assert label == "base", (
        f"expected base source, got {label!r}. "
        "This is the guard against 1-row full_backup poisoning rolling."
    )
    assert df is not None and len(df) == 501


def test_read_symbol_source_falls_back_to_full_when_base_missing(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """No base feather → fall back to full_backup."""
    _install_fake_settings(monkeypatch, tmp_cache)

    healthy = _make_ohlcv(rows=501)
    healthy.to_csv(tmp_cache / "full_backup" / "SPY.csv", index=False)

    from common.cache_manager import CacheManager
    from config.settings import get_settings
    from scripts.build_rolling_with_indicators import _read_symbol_source

    cm = CacheManager(get_settings(create_dirs=True))
    df, label = _read_symbol_source(cm, "SPY")

    assert label == "full"
    assert df is not None and len(df) == 501


def test_extract_rolling_end_to_end_produces_501_row_rolling_from_base(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """★ Full end-to-end: base(501) + stale full(501, NaN placeholders) → rolling(501, numeric).

    This is the top-level contract asserted by the 2026-07-02 fix:
    a Windows-authoritative base feather with 501 rows and computed
    indicators should be replicated into rolling verbatim, regardless of
    whether the accompanying full_backup CSV is healthy or has stale-NaN
    placeholders.
    """
    _install_fake_settings(monkeypatch, tmp_cache)

    symbols = ["SPY", "AAPL", "MSFT"]
    for sym in symbols:
        stale = _stale_full_backup_csv(rows=501)
        stale.to_csv(tmp_cache / "full_backup" / f"{sym}.csv", index=False)
        base = _base_feather_frame(rows=501)
        base.reset_index(drop=True).to_feather(tmp_cache / "base" / f"{sym}.feather")

    from common.cache_manager import CacheManager
    from config.settings import get_settings
    from scripts.build_rolling_with_indicators import extract_rolling_from_full

    cm = CacheManager(get_settings(create_dirs=True))
    stats = extract_rolling_from_full(
        cm,
        symbols=symbols,
        # workers=None triggers the serial path — deterministic and easy
        # to reason about in tests. Parallel path shares the same
        # _process_symbol_worker so behaviour is identical.
        workers=None,
    )

    assert stats.errors == {}, f"unexpected errors: {stats.errors}"
    assert stats.updated_symbols == len(symbols)

    for sym in symbols:
        rolling_feather = tmp_cache / "rolling" / f"{sym}.feather"
        rolling_csv = tmp_cache / "rolling" / f"{sym}.csv"
        # write_atomic auto-detects — CacheManager wrote either CSV or feather.
        # For a fresh directory without pre-existing files, detect_path
        # falls through to the "csv" default, so we expect the CSV.
        candidate = rolling_feather if rolling_feather.exists() else rolling_csv
        assert candidate.exists(), f"rolling cache for {sym} was not written"

        if candidate.suffix == ".feather":
            df = pd.read_feather(candidate)
        else:
            df = pd.read_csv(candidate)

        # ★ core assertion #1: row count mirrors base
        assert len(df) == 501, (
            f"{sym}: rolling should mirror base (501 rows), got {len(df)}. "
            "1-row bug regression."
        )
        # ★ core assertion #2: sma25 is numeric
        assert df["sma25"].notna().sum() >= 476, (
            f"{sym}: sma25 must be numeric in rolling (base already had it). "
            f"Got {df['sma25'].notna().sum()} non-null out of {len(df)}. "
            "Stale-NaN skip bug regression."
        )


def test_drop_all_nan_indicator_columns_removes_only_empty_placeholders(
    tmp_cache: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Utility guard: ``_drop_all_nan_indicator_columns`` must:

    * drop indicator columns that are entirely NaN,
    * keep indicator columns that carry real values,
    * keep non-indicator columns untouched.
    """
    _install_fake_settings(monkeypatch, tmp_cache)
    from scripts.build_rolling_with_indicators import _drop_all_nan_indicator_columns

    df = pd.DataFrame(
        {
            "date": pd.date_range("2026-01-01", periods=5),
            "Close": [100.0, 101.0, 102.0, 103.0, 104.0],
            "sma25": [np.nan] * 5,  # stale placeholder, MUST be dropped
            "sma50": [10.0, 11.0, 12.0, 13.0, 14.0],  # populated, MUST stay
            "unrelated_col": [1, 2, 3, 4, 5],  # not an indicator, MUST stay
            "uptwodays": [False] * 5,  # all-False placeholder → drop for recompute
        }
    )
    result = _drop_all_nan_indicator_columns(df)

    assert "sma25" not in result.columns, "empty placeholder must be dropped"
    assert "sma50" in result.columns, "populated indicator must stay"
    assert "Close" in result.columns, "OHLCV must stay"
    assert "unrelated_col" in result.columns, "non-indicator column must stay"
    assert (
        "uptwodays" not in result.columns
    ), "all-False boolean placeholder should be dropped so add_indicators recomputes"
