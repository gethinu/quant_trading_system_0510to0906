"""Polygon.io Grouped Daily を CacheManager production 経路へ backfill する。

`scripts/cache_daily_data.py` (EODHD/Alpaca 経由の per-symbol fetch) の
**Grouped Daily 版**。Polygon の decisive advantage である「1 call/日で全 US
銘柄」を使い、指定期間の日足パネルをまとめて取得して、既存 EODHD 経路と
**drop-in 互換**の base feather (指標付き) + full CSV を書き出す。

出力先 (既存 CacheManager と同一):
    - full : ``data_cache/full_backup/<SYM>.csv``   (add_indicators 済)
    - base : ``data_cache/base/<SYM>.feather``       (compute_base_indicators 済)

drop-in 互換の要点:
    - base feather columns : date/open/high/low/close/volume + 指標
      (DollarVolume20/50, SMA*, ATR*, RSI*, ROC200, HV50 …) を
      ``compute_base_indicators`` で EODHD 経路と同一計算式で付与。
    - Grouped Daily は unadjusted (raw close) のみ返すため AdjClose=Close。
      日次シグナル (SMA200/ROC200 = 200 日) には十分。長期 backtest の
      split/dividend 調整が要る場合のみ EODHD 履歴を併用する (verdict §運用注意)。

CLI:
    # 直近 250 営業日 (≒無料 tier 履歴上限の範囲内) を全銘柄 backfill
    python scripts/cache_daily_polygon.py --start 2024-07-01 --end 2026-06-30

    # 特定銘柄のみ (drop-in 検証用)
    python scripts/cache_daily_polygon.py --start 2026-04-01 --end 2026-06-30 --symbols AAPL,MSFT,SPY

Rate limit:
    Grouped Daily は 1 call/日。無料 tier 5 req/min のため既定 sleep=13s。
    250 日 backfill ≈ 250 call ÷ 5/min ≈ 50 分。``--sleep`` で調整可。
    429 は common/polygon_data.py::_request の指数バックオフが吸収する。

無料 tier 履歴上限 (≈2 年):
    範囲外を要求すると Grouped Daily が空を返す。平日で連続空応答を検知したら
    WARNING を出し (fail-soft)、``--strict-history`` 指定時は fail-fast する。
"""

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
from common.cache_manager import (  # noqa: E402
    compute_base_indicators,
    save_base_cache,
)
from common.indicators_common import add_indicators  # noqa: E402
from common.polygon_data import get_polygon_grouped_daily  # noqa: E402

logger = logging.getLogger(__name__)

# 無料 tier の履歴上限は約 2 年 (verdict §運用注意 1)。安全側で 730 日。
_FREE_TIER_HISTORY_DAYS = int(os.getenv("POLYGON_FREE_TIER_HISTORY_DAYS", "730"))

_OHLCV_COLS = ["Open", "High", "Low", "Close", "Volume"]


def iter_business_days(start: date, end: date):
    """start..end (両端含む) の平日を昇順で yield する。祝日は考慮しない。"""
    d = start
    while d <= end:
        if d.weekday() < 5:  # Mon-Fri
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
    """[start, end] の Grouped Daily を日次に fetch してパネルを返す。

    Returns
    -------
    dict[str, pd.DataFrame]
        キー=日付文字列 (YYYY-MM-DD)、値=index=symbol の OHLCV DataFrame。
        空応答 (祝日/週末/履歴外) の日は含めない。
    """
    panel: dict[str, pd.DataFrame] = {}
    days = list(iter_business_days(start, end))
    history_cutoff = date.today() - timedelta(days=_FREE_TIER_HISTORY_DAYS)
    empty_days: list[str] = []
    n = len(days)
    logger.info("Grouped Daily backfill: %d 営業日 (%s..%s)", n, start, end)

    for i, d in enumerate(days, 1):
        ds = d.isoformat()
        df = get_polygon_grouped_daily(ds)
        if df is None or df.empty:
            empty_days.append(ds)
            if d < history_cutoff:
                msg = (
                    f"{ds}: Grouped Daily が空 (無料 tier 履歴上限 ~"
                    f"{_FREE_TIER_HISTORY_DAYS}日 を超過している可能性)"
                )
                if strict_history:
                    raise RuntimeError(msg + " [--strict-history により中断]")
                logger.warning(msg)
        else:
            panel[ds] = df
        if i % progress_every == 0 or i == n:
            logger.info(
                "  進捗 %d/%d 日 (取得済 %d 日 / 空 %d 日)",
                i, n, len(panel), len(empty_days),
            )
        if sleep_seconds > 0 and i < n:
            time.sleep(sleep_seconds)

    if empty_days:
        logger.info("空応答 %d 日 (祝日/週末/履歴外): %s%s",
                    len(empty_days), ", ".join(empty_days[:10]),
                    " …" if len(empty_days) > 10 else "")
    return panel


def pivot_to_symbol_frames(
    panel: dict[str, pd.DataFrame],
    symbols: set[str] | None = None,
) -> dict[str, pd.DataFrame]:
    """日次パネルを symbol -> (Date-indexed OHLCV DataFrame) に転置する。

    返す DataFrame は EODHD/Alpaca provider と同一スキーマ:
        index=DatetimeIndex(name="Date", 昇順), columns=OHLCV(+AdjClose=Close)。
    """
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
        # Grouped Daily は unadjusted → AdjClose = raw Close
        f["AdjClose"] = f["Close"].astype("float64")
        f = f[["Open", "High", "Low", "Close", "AdjClose", "Volume"]]
        frames[str(sym)] = f
    return frames


def write_symbol_to_cache(
    symbol: str,
    df: pd.DataFrame,
    *,
    full_dir: Path,
    round_decimals: int | None,
) -> bool:
    """1 銘柄を EODHD 経路と同一の production 経路で書き出す。

    full CSV (add_indicators 済) + base feather (compute_base_indicators 済)。
    """
    try:
        safe = safe_filename(symbol)
        # --- full CSV (add_indicators) ---
        full_df = add_indicators(df.copy())
        df_reset = full_df.reset_index()
        df_reset = round_dataframe(df_reset, round_decimals)
        full_dir.mkdir(parents=True, exist_ok=True)
        df_reset.to_csv(full_dir / f"{safe}.csv", index=False)
        # --- base feather (compute_base_indicators → DollarVolume20/50 等) ---
        base_df = compute_base_indicators(df)
        if base_df is not None and not base_df.empty:
            save_base_cache(symbol, base_df)
        return True
    except Exception as exc:  # noqa: BLE001 - 1 銘柄失敗は継続
        logger.warning("%s: cache 書き込み失敗 - %s", symbol, exc)
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
) -> dict[str, int]:
    from config.settings import get_settings

    settings = get_settings(create_dirs=True)
    full_dir = Path(settings.cache.full_dir)
    round_decimals = getattr(settings.cache, "round_decimals", None)

    panel = fetch_grouped_panel(
        start, end, sleep_seconds=sleep_seconds, strict_history=strict_history
    )
    if not panel:
        logger.error("取得できた営業日が 0 日。範囲/履歴上限/rate limit を確認してください。")
        return {"days": 0, "symbols": 0, "written": 0, "failed": 0}

    frames = pivot_to_symbol_frames(panel, symbols)
    all_syms = sorted(frames)
    if max_symbols is not None:
        all_syms = all_syms[:max_symbols]
    logger.info("転置完了: %d 営業日 → %d 銘柄", len(panel), len(all_syms))

    if dry_run:
        logger.info("[dry-run] 書き込みスキップ。先頭 5 銘柄=%s", all_syms[:5])
        return {"days": len(panel), "symbols": len(all_syms), "written": 0, "failed": 0}

    written = failed = 0
    for i, sym in enumerate(all_syms, 1):
        ok = write_symbol_to_cache(
            sym, frames[sym], full_dir=full_dir, round_decimals=round_decimals
        )
        written += int(ok)
        failed += int(not ok)
        if i % 500 == 0 or i == len(all_syms):
            logger.info("  cache 書込 %d/%d (成功 %d / 失敗 %d)", i, len(all_syms), written, failed)

    return {"days": len(panel), "symbols": len(all_syms), "written": written, "failed": failed}


def _parse_date(s: str) -> date:
    return datetime.strptime(s, "%Y-%m-%d").date()


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--start", required=True, type=_parse_date, help="開始日 YYYY-MM-DD")
    p.add_argument("--end", required=True, type=_parse_date, help="終了日 YYYY-MM-DD")
    p.add_argument("--symbols", default=None,
                   help="カンマ区切りの銘柄フィルタ (例: AAPL,MSFT,SPY)。未指定なら全銘柄。")
    p.add_argument("--max-symbols", type=int, default=None,
                   help="書き込む銘柄数の上限 (テスト/部分 backfill 用)。")
    p.add_argument("--sleep", type=float, default=13.0,
                   help="Grouped Daily call 間の sleep 秒 (既定 13s = 無料 tier 5req/min)。")
    p.add_argument("--strict-history", action="store_true",
                   help="履歴上限超過 (平日で空応答) を fail-fast する。")
    p.add_argument("--dry-run", action="store_true",
                   help="fetch/転置のみ行い cache 書き込みをスキップ。")
    p.add_argument("--log-level", default="INFO")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    if args.end < args.start:
        logger.error("--end (%s) が --start (%s) より前です。", args.end, args.start)
        return 1

    symbols = None
    if args.symbols:
        symbols = {s.strip().upper().replace(".US", "") for s in args.symbols.split(",") if s.strip()}

    try:
        stats = run_backfill(
            args.start, args.end,
            symbols=symbols, max_symbols=args.max_symbols,
            sleep_seconds=args.sleep, strict_history=args.strict_history,
            dry_run=args.dry_run,
        )
    except RuntimeError as exc:
        logger.error("fail-fast: %s", exc)
        return 1
    except ValueError as exc:
        logger.error("fail-fast: %s", exc)  # API key 未設定など
        return 1

    logger.info(
        "完了: days=%d symbols=%d written=%d failed=%d",
        stats["days"], stats["symbols"], stats["written"], stats["failed"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
