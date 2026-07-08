"""Polygon.io Grouped Daily を CacheManager production 経路へ backfill する。"""

from __future__ import annotations

import argparse
from datetime import date, datetime, timedelta
import logging
import os
from pathlib import Path
import sys
import time

import pandas as pd

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.cache_format import round_dataframe, safe_filename  # noqa: E402
from common.cache_manager import compute_base_indicators, save_base_cache  # noqa: E402
from common.indicators_common import add_indicators  # noqa: E402
from common.polygon_data import get_polygon_grouped_daily  # noqa: E402

logger = logging.getLogger(__name__)

_FREE_TIER_HISTORY_DAYS = int(os.getenv("POLYGON_FREE_TIER_HISTORY_DAYS", "730"))
_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


def iter_business_days(start: date, end: date):
    d = start
    while d <= end:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def fetch_grouped_panel(
    start: date,
    end: date,
    *,
    sleep_seconds: float,
    strict_history: bool,
    progress_every: int = 20,
) -> dict[str, pd.DataFrame]:
    panel: dict[str, pd.DataFrame] = {}
    days = list(iter_business_days(start, end))
    history_cutoff = date.today() - timedelta(days=_FREE_TIER_HISTORY_DAYS)
    empty_days: list[str] = []
    n = len(days)
    logger.info("Grouped Daily backfill: %d business days (%s..%s)", n, start, end)

    for i, d in enumerate(days, 1):
        ds = d.isoformat()
        df = get_polygon_grouped_daily(ds)
        if df is None or df.empty:
            empty_days.append(ds)
            if d < history_cutoff:
                msg = f"{ds}: Grouped Daily empty (possibly beyond free tier)"
                if strict_history:
                    raise RuntimeError(msg + " [--strict-history]")
                logger.warning(msg)
        else:
            panel[ds] = df
        if i % progress_every == 0 or i == n:
            logger.info(
                "  %d/%d days (fetched %d / empty %d)",
                i,
                n,
                len(panel),
                len(empty_days),
            )
        if sleep_seconds > 0 and i < n:
            time.sleep(sleep_seconds)

    if empty_days:
        logger.info(
            "empty %d days: %s%s",
            len(empty_days),
            ", ".join(empty_days[:10]),
            " ..." if len(empty_days) > 10 else "",
        )
    return panel


def pivot_to_symbol_frames(
    panel: dict[str, pd.DataFrame],
    symbols: set[str] | None = None,
) -> dict[str, pd.DataFrame]:
    rows: list[pd.DataFrame] = []
    for ds, df in panel.items():
        sub = df
        if symbols is not None:
            sub = sub[sub.index.isin(symbols)]
        if sub.empty:
            continue
        piece = sub[_OHLCV_COLS].copy()
        piece["Date"] = pd.Timestamp(ds)
        piece["symbol"] = sub.index.astype(str)
        rows.append(piece)

    if not rows:
        return {}

    allrows = pd.concat(rows, ignore_index=True)
    frames: dict[str, pd.DataFrame] = {}
    for sym, g in allrows.groupby("symbol"):
        f = g.drop(columns=["symbol"]).sort_values("Date")
        f = f.set_index("Date")
        f.index.name = "Date"
        f = f[~f.index.duplicated(keep="last")]
        f["AdjClose"] = f["Close"].astype("float64")
        f = f[["Open", "High", "Low", "Close", "AdjClose", "Volume"]]
        frames[str(sym)] = f
    return frames


def _merge_with_existing_full_csv(
    new_df: pd.DataFrame,
    full_csv_path: Path,
) -> pd.DataFrame:
    if not full_csv_path.exists():
        return new_df
    try:
        existing = pd.read_csv(full_csv_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("read failure %s: %s. new only.", full_csv_path, exc)
        return new_df
    if existing is None or existing.empty:
        return new_df
    for df_ in (existing, new_df):
        if "Date" not in df_.columns and "date" in df_.columns:
            df_.rename(columns={"date": "Date"}, inplace=True)
        try:
            df_["Date"] = pd.to_datetime(df_["Date"], errors="coerce")
        except Exception:
            pass
    combined = pd.concat([existing, new_df], ignore_index=True, sort=False)
    combined = combined.dropna(subset=["Date"])
    combined = combined.drop_duplicates(subset=["Date"], keep="last")
    combined = combined.sort_values("Date").reset_index(drop=True)
    return combined


def write_symbol_to_cache(
    symbol: str,
    df: pd.DataFrame,
    *,
    full_dir: Path,
    round_decimals: int | None,
    settings: object | None = None,
) -> bool:
    try:
        safe = safe_filename(symbol)
        full_dir.mkdir(parents=True, exist_ok=True)
        full_csv_path = full_dir / f"{safe}.csv"

        new_full = add_indicators(df.copy()).reset_index()
        merged_full = _merge_with_existing_full_csv(new_full, full_csv_path)

        try:
            work = merged_full.copy()
            if "Date" in work.columns:
                work["Date"] = pd.to_datetime(work["Date"], errors="coerce")
                work = work.dropna(subset=["Date"]).sort_values("Date")
                work = work.set_index("Date")
            ohlcv_cols = [
                c
                for c in ("Open", "High", "Low", "Close", "AdjClose", "Volume")
                if c in work.columns
            ]
            if ohlcv_cols:
                recomputed = add_indicators(work[ohlcv_cols].copy())
                merged_full = recomputed.reset_index()
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: indicator retry fail: %s", symbol, exc)

        merged_full = round_dataframe(merged_full, round_decimals)
        tmp_csv = full_csv_path.with_suffix(".csv.tmp")
        merged_full.to_csv(tmp_csv, index=False)
        tmp_csv.replace(full_csv_path)

        try:
            base_source = merged_full.copy()
            if "Date" in base_source.columns:
                base_source["Date"] = pd.to_datetime(
                    base_source["Date"], errors="coerce"
                )
                base_source = base_source.dropna(subset=["Date"]).sort_values("Date")
                base_source = base_source.set_index("Date")
            base_df = compute_base_indicators(base_source)
        except Exception as exc:  # noqa: BLE001
            logger.debug("%s: base indicator fail: %s", symbol, exc)
            base_df = None
        if base_df is not None and not base_df.empty:
            save_base_cache(symbol, base_df)
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("%s: cache write failed - %s", symbol, exc)
        return False


def run_backfill(
    start: date,
    end: date,
    *,
    symbols: set[str] | None,
    max_symbols: int | None,
    sleep_seconds: float,
    strict_history: bool,
    dry_run: bool,
    common_only: bool = True,
) -> dict[str, int]:
    from config.settings import get_settings

    settings = get_settings(create_dirs=True)
    full_dir = Path(settings.cache.full_dir)
    round_decimals = getattr(settings.cache, "round_decimals", None)

    panel = fetch_grouped_panel(
        start, end, sleep_seconds=sleep_seconds, strict_history=strict_history
    )
    if not panel:
        logger.error("no business days fetched")
        return {"days": 0, "symbols": 0, "written": 0, "failed": 0}

    frames = pivot_to_symbol_frames(panel, symbols)
    all_syms = sorted(frames)
    # 2026-07-02 hygiene: 普通株のみに絞る (default True)
    if common_only:
        from common.symbol_universe import is_common_stock_symbol

        pre = len(all_syms)
        all_syms = [s for s in all_syms if is_common_stock_symbol(s)]
        dropped = pre - len(all_syms)
        if dropped:
            logger.info(
                "universe filter: %d -> %d (%d dropped: non-common)",
                pre,
                len(all_syms),
                dropped,
            )
    if max_symbols is not None:
        all_syms = all_syms[:max_symbols]
    logger.info("pivoted: %d days -> %d symbols", len(panel), len(all_syms))

    if dry_run:
        logger.info("[dry-run] skip write. first 5=%s", all_syms[:5])
        return {"days": len(panel), "symbols": len(all_syms), "written": 0, "failed": 0}

    written = failed = 0
    for i, sym in enumerate(all_syms, 1):
        ok = write_symbol_to_cache(
            sym,
            frames[sym],
            full_dir=full_dir,
            round_decimals=round_decimals,
            settings=settings,
        )
        written += int(ok)
        failed += int(not ok)
        if i % 500 == 0 or i == len(all_syms):
            logger.info(
                "  wrote %d/%d (ok %d / fail %d)", i, len(all_syms), written, failed
            )

    return {
        "days": len(panel),
        "symbols": len(all_syms),
        "written": written,
        "failed": failed,
    }


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Polygon Grouped Daily backfill.")
    p.add_argument("--start", required=True, type=_parse_date, help="start YYYY-MM-DD")
    p.add_argument("--end", required=True, type=_parse_date, help="end YYYY-MM-DD")
    p.add_argument("--symbols", default=None, help="comma-separated symbol filter")
    p.add_argument(
        "--max-symbols", type=int, default=None, help="upper bound for symbols"
    )
    p.add_argument(
        "--sleep", type=float, default=13.0, help="sleep between Grouped Daily calls"
    )
    p.add_argument(
        "--strict-history", action="store_true", help="fail-fast on empty weekdays"
    )
    p.add_argument(
        "--dry-run", action="store_true", help="fetch/pivot only, skip writes"
    )
    # 2026-07-02 hygiene: 普通株以外 (preferred/warrant/unit/rights/notes) を除外
    p.add_argument(
        "--common-only",
        dest="common_only",
        action="store_true",
        default=True,
        help="restrict to US common stocks (default; ~44% faster).",
    )
    p.add_argument(
        "--no-common-only",
        dest="common_only",
        action="store_false",
        help="disable pattern filter (keep full Polygon universe).",
    )
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.end < args.start:
        logger.error("--end (%s) < --start (%s)", args.end, args.start)
        return 1

    symbols = None
    if args.symbols:
        symbols = {
            s.strip().upper().replace(".US", "")
            for s in args.symbols.split(",")
            if s.strip()
        }

    try:
        stats = run_backfill(
            args.start,
            args.end,
            symbols=symbols,
            max_symbols=args.max_symbols,
            sleep_seconds=args.sleep,
            strict_history=args.strict_history,
            dry_run=args.dry_run,
            common_only=getattr(args, "common_only", True),
        )
    except RuntimeError as exc:
        logger.error("fail-fast: %s", exc)
        return 1
    except ValueError as exc:
        logger.error("fail-fast: %s", exc)
        return 1

    logger.info(
        "done: days=%d symbols=%d written=%d failed=%d",
        stats["days"],
        stats["symbols"],
        stats["written"],
        stats["failed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
