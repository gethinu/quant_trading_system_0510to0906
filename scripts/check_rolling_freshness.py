"""Rolling-cache freshness guard (daily pipeline WARN step).

Would have caught the 2026-07-12..14 dashboard freeze: rolling cache stuck while
the pipeline reported success. Compares the newest date in the rolling cache
against its upstream (full_backup) and warns when rolling lags by more than a
tolerance in business days.

Exit codes: 0 = fresh, 2 = stale (WARN — pipeline keeps going but flags it),
1 = error (couldn't read caches).

Deliberately cheap: reads only the last line of each CSV (no full parse).
"""

from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys

ROOT_DIR = Path(__file__).resolve().parents[1]
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

from common.cache_freshness import (  # noqa: E402
    fraction_behind_upstream,
    lag_business_days,
    max_last_date,
    modal_last_date,
    symbols_behind_upstream,
)
from common.utils_spy import get_latest_nyse_trading_day  # noqa: E402


def _load_universe(path: Path | None) -> set[str] | None:
    if path is None or not path.is_file():
        return None
    syms: set[str] = set()
    try:
        for line in path.read_text(encoding="utf-8").splitlines():
            s = line.strip().upper()
            if s and not s.startswith("#"):
                syms.add(s)
    except OSError:
        return None
    return syms or None


def _last_date_of_csv(path: str) -> str | None:
    try:
        with open(path, "rb") as f:
            f.seek(0, os.SEEK_END)
            size = f.tell()
            if size == 0:
                return None
            block = min(size, 4096)
            f.seek(size - block)
            tail = f.read(block)
        lines = [ln for ln in tail.split(b"\n") if ln.strip()]
        if not lines:
            return None
        row = lines[-1].decode("utf-8", "replace")
        for field in row.split(",")[:2]:
            c = field.strip().strip('"')
            if len(c) >= 10 and c[4] == "-" and c[7] == "-":
                return c[:10]
    except OSError:
        return None
    return None


def scan_last_dates(cache_dir: Path) -> dict[str, dict]:
    manifest: dict[str, dict] = {}
    if not cache_dir.is_dir():
        return manifest
    with os.scandir(cache_dir) as it:
        for entry in it:
            if entry.is_file() and entry.name.endswith(".csv"):
                manifest[entry.name[:-4]] = {"last_date": _last_date_of_csv(entry.path)}
    return manifest


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description="Rolling cache freshness guard.")
    p.add_argument("--rolling-dir", default=None)
    p.add_argument("--upstream-dir", default=None, help="full_backup dir (reference)")
    p.add_argument(
        "--tolerance-bdays",
        type=int,
        default=1,
        help="max acceptable business-day lag (max-date, contextual only)",
    )
    p.add_argument(
        "--max-frac-behind",
        type=float,
        default=0.05,
        help="WARN if more than this fraction of universe symbols lag upstream",
    )
    p.add_argument(
        "--universe-file",
        default=None,
        help="signal universe (one symbol/line); scopes the freeze check "
        "so non-universe ETFs are not counted. default data/universe_auto.txt",
    )
    p.add_argument(
        "--max-abs-lag-bdays",
        type=int,
        default=2,
        help="WARN if upstream (full_backup) newest date lags the latest NYSE "
        "trading day by more than this many business days (total-freeze "
        "detector; catches rolling+upstream both frozen, which the relative "
        "check reads as fresh). Generous default avoids vendor EOD-lag noise.",
    )
    p.add_argument(
        "--today",
        default=None,
        help="reference 'today' (YYYY-MM-DD, ET) for the absolute-staleness "
        "check; default = now in America/New_York. Mainly for tests.",
    )
    args = p.parse_args(argv)

    try:
        from config.settings import get_settings

        s = get_settings(create_dirs=False)
        rolling_dir = Path(args.rolling_dir or s.cache.rolling_dir)
        upstream_dir = Path(args.upstream_dir or s.cache.full_dir)
    except Exception as exc:  # noqa: BLE001
        if args.rolling_dir and args.upstream_dir:
            rolling_dir = Path(args.rolling_dir)
            upstream_dir = Path(args.upstream_dir)
        else:
            print(f"[freshness] ERROR: cannot resolve cache dirs: {exc}")
            return 1

    rolling = scan_last_dates(rolling_dir)
    upstream = scan_last_dates(upstream_dir)
    if not rolling or not upstream:
        print(
            f"[freshness] ERROR: empty cache (rolling={len(rolling)} upstream={len(upstream)})"
        )
        return 1

    # Resolve signal universe (scopes out non-universe ETFs fetched into full_backup
    # but intentionally not rebuilt into rolling).
    uni_path = (
        Path(args.universe_file)
        if args.universe_file
        else (ROOT_DIR / "data" / "universe_auto.txt")
    )
    universe = _load_universe(uni_path)

    r_mode, u_mode = modal_last_date(rolling), modal_last_date(upstream)
    mode_lag = lag_business_days(r_mode, u_mode)

    print(
        f"[freshness] rolling modal={r_mode} (max={max_last_date(rolling)}, {len(rolling)} files) | "
        f"upstream modal={u_mode} (max={max_last_date(upstream)}, {len(upstream)} files)"
    )

    # --- absolute-staleness (total-freeze) check ---------------------------
    # 既存の rolling-vs-upstream 比較は「両方同時に凍結」を検知できない (lag=0 で
    # fresh に見える)。upstream(full_backup) 自体が NYSE カレンダーの直近取引日から
    # どれだけ遅れているかを絶対評価し、多日フリーズを surface する。
    # 2026-07-12..14 のダッシュ凍結 (cache step が確定日を取れず全体停滞) は、
    # この経路なら検知できた。exit 2 は soft WARN (pipeline は継続、$Failures に計上)。
    try:
        import pandas as pd  # local import: 起動コストを絶対チェック時のみに限定

        ref = (
            pd.Timestamp(args.today)
            if args.today
            else pd.Timestamp.now(tz="America/New_York").tz_localize(None)
        )
        latest_trading = get_latest_nyse_trading_day(ref)
        latest_iso = latest_trading.date().isoformat()
        u_max = max_last_date(upstream)
        abs_lag = lag_business_days(u_max, latest_iso)
        if abs_lag is not None and abs_lag > args.max_abs_lag_bdays:
            print(
                f"[freshness] WARN: upstream(full_backup) newest={u_max} は直近 NYSE "
                f"取引日 ({latest_iso}) から {abs_lag} 営業日遅れています "
                f"(> {args.max_abs_lag_bdays})。キャッシュ全体が凍結している疑い "
                "(cache step が確定日を取得できていない)。cache_daily_polygon.py の "
                "実行と Polygon 接続を確認してください。"
            )
            return 2
    except Exception as exc:  # noqa: BLE001
        # 絶対チェックは best-effort。失敗しても既存の相対チェックは実行する。
        print(f"[freshness] (absolute-staleness check skipped: {exc})")

    if universe:
        behind = symbols_behind_upstream(rolling, upstream, universe=universe)
        frac = fraction_behind_upstream(rolling, upstream, universe=universe)
        n_common = sum(1 for s in upstream if s in rolling and s in universe)
        print(
            f"[freshness] universe={uni_path.name} ({len(universe)}); "
            f"behind {len(behind)}/{n_common} ({frac:.2%}); sample={behind[:8]}"
        )
        if frac > args.max_frac_behind:
            print(
                f"[freshness] WARN: {frac:.1%} of universe symbols lag upstream "
                f"(> {args.max_frac_behind:.0%}). rolling cache は前進していません "
                f"(凍結の疑い)。build_rolling_with_indicators.py の実行を確認してください。"
            )
            return 2
        print("[freshness] OK: rolling universe is fresh relative to upstream.")
        return 0

    # No universe file: fall back to modal-date comparison (robust to noise).
    print("[freshness] (universe file absent; using modal-date comparison)")
    if mode_lag is None:
        print("[freshness] ERROR: unparseable modal dates")
        return 1
    if mode_lag > args.tolerance_bdays:
        print(
            f"[freshness] WARN: rolling modal date lags upstream by {mode_lag} "
            f"business days (凍結の疑い)。build_rolling_with_indicators.py を確認。"
        )
        return 2
    print("[freshness] OK: rolling modal date is fresh relative to upstream.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
