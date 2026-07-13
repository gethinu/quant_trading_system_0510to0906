"""Daily Polygon coverage monitor (skeleton).

sys1-7 の gate 生存率 (min-ADV / DollarVolume / MIN_PRICE) を Polygon.io
Grouped Daily (無料 tier / 1 call/日で全 US 銘柄) で日次モニタリングし、
閾値割れ検知 + 前日比 delta を JSON 出力する production パイプライン。

Status:
    - 骨格 (argparse / I/O / gate 定義) はここで実装済。
    - `common.polygon_data.get_polygon_grouped_daily` は既存実装済 (import OK)。
    - **fetch 実行 / dv20-dv50 lookup / delta 計算 / notification hook は
      POLYGON_API_KEY 投入後の別 iteration で肉付け**する。
    - 現状 --dry-run で骨格の import / argparse / 出力 path 生成のみを確認可能。

Runbook:
    docs/HUMAN_TASK_polygon_daily_monitor_20260701.md

Windows Task Scheduler:
    Task 名 `QuantTrading_PolygonDailyMonitor` から
    `scripts/daily_polygon_monitor.ps1` 経由で呼ばれる。

Exit codes:
    0 : 正常終了 (閾値割れなし)
    2 : 閾値割れ検知 (WARN log 済、後段 hook に emit)
    1 : 実行時エラー
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
import json
import logging
import os
from pathlib import Path
import sys
from typing import Any

# スクリプトを直接 (python scripts/daily_polygon_monitor.py / .ps1 経由) 実行しても
# リポジトリ直下の `common` パッケージを解決できるようにする。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger(__name__)

# --- System 別 gate 定義 (core/system*.py を grep で実測) ---------------
# 詳細は docs/HUMAN_TASK_polygon_daily_monitor_20260701.md §2.2 を参照。
SYSTEM_GATES: dict[str, dict[str, Any]] = {
    "sys1": {
        "min_price": 5.0,
        "dv_col": "DollarVolume20",
        "dv_min": 50_000_000,
        "warn_ratio": 0.05,
    },
    "sys2": {
        "min_price": 5.0,
        "dv_col": "DollarVolume20",
        "dv_min": 25_000_000,
        "warn_ratio": 0.06,
    },
    "sys3": {
        "min_price": 5.0,
        "dv_col": "DollarVolume20",
        "dv_min": 25_000_000,
        "warn_ratio": 0.06,
    },
    "sys4": {
        "min_price": None,
        "dv_col": "DollarVolume50",
        "dv_min": 100_000_000,
        "warn_ratio": 0.04,
    },
    "sys5": {"min_price": 5.0, "dv_col": None, "dv_min": None, "warn_ratio": 0.15},
    "sys6": {
        "min_price": 5.0,
        "dv_col": "DollarVolume50",
        "dv_min": 10_000_000,
        "warn_ratio": 0.10,
        "min_col": "Low",
    },
    "sys7": {"spy_only": True, "warn_ratio": 1.0},
}


@dataclass
class SystemSurvival:
    """1 system の生存率評価結果。"""

    system: str
    n_pass: int = 0
    n_total: int = 0
    ratio: float = 0.0
    warn_threshold: float = 0.0
    status: str = "pending"
    survived_tickers: list[str] = field(default_factory=list)
    rejected_tickers: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "n_pass": self.n_pass,
            "n_total": self.n_total,
            "ratio": round(self.ratio, 4),
            "warn_threshold": self.warn_threshold,
            "status": self.status,
        }


@dataclass
class CoverageReport:
    """日次 coverage report の schema。JSON 出力される。"""

    date: str
    provider: str = "polygon_grouped_daily"
    n_candidates_total: int = 0
    survival_by_system: dict[str, dict[str, Any]] = field(default_factory=dict)
    rejected_top10: list[dict[str, str]] = field(default_factory=list)
    delta_vs_previous: dict[str, float] = field(default_factory=dict)
    consecutive_drops: dict[str, int] = field(default_factory=dict)
    notes: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        return json.dumps(self.__dict__, ensure_ascii=False, indent=2, default=str)


def previous_business_day(anchor: date | None = None) -> date:
    """anchor (default: 今日) の直前平日を返す。"""
    d = anchor or date.today()
    d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d


def fetch_grouped_daily(target_date: str) -> Any:
    """Polygon Grouped Daily を fetch する thin wrapper。"""
    from common.polygon_data import get_polygon_grouped_daily

    return get_polygon_grouped_daily(target_date)


def apply_common_stock_filter(grouped_df: Any) -> Any:
    """grouped_df の index (symbol) を US 普通株 (Polygon type=CS) に絞り込む.

    2026-07-13: 従来の pattern filter (is_common_stock_symbol) は dotted-suffix
    (FOO.W) しか弾けず、concatenated-suffix の実データ (FOOW) をほぼ素通しに
    していたため ETF (~42%)/ADR/優先株/warrant がユニバースに残っていた。
    Polygon reference API (type=CS) を正とし、SPY (System7 ヘッジ) は温存する。
    CS セット取得不能時は従来の pattern filter にフォールバック。
    """
    if grouped_df is None or getattr(grouped_df, "empty", True):
        return grouped_df
    from common.symbol_universe import get_common_stock_set, is_common_stock_symbol

    try:
        raw_n = int(getattr(grouped_df, "shape", [0])[0])
        cs_set = get_common_stock_set()
        if cs_set:
            keep = cs_set | {"SPY"}
            mask = [str(sym).upper() in keep for sym in grouped_df.index]
            mode = "Polygon type=CS"
        else:
            mask = [is_common_stock_symbol(sym) for sym in grouped_df.index]
            mode = "pattern fallback"
        filtered = grouped_df[mask]
        kept = int(getattr(filtered, "shape", [0])[0])
        logger.info(
            "universe filter (%s): %d -> %d (%d dropped)",
            mode,
            raw_n,
            kept,
            raw_n - kept,
        )
        return filtered
    except Exception as exc:  # noqa: BLE001
        logger.warning("universe filter failed, using raw grouped_df: %s", exc)
        return grouped_df


def _load_dv_from_base_cache(cache_dir: Path) -> dict[str, dict[str, float]]:
    """``data_cache/base/*.feather`` から各銘柄の最新 DollarVolume20/50 を読む。"""
    out: dict[str, dict[str, float]] = {}
    base_dir = cache_dir / "base"
    if not base_dir.exists():
        return out
    import pandas as pd

    for fp in base_dir.glob("*.feather"):
        try:
            df = pd.read_feather(fp)
        except Exception:
            continue
        if df is None or df.empty:
            continue
        cols = {c.lower(): c for c in df.columns}
        dv20c, dv50c = cols.get("dollarvolume20"), cols.get("dollarvolume50")
        if not dv20c and not dv50c:
            continue
        last = df.iloc[-1]
        sym = fp.stem.upper()

        def _safe_float(v: object) -> float:
            try:
                if v is None or pd.isna(v):
                    return float("nan")
                return float(v)  # type: ignore[arg-type]
            except (TypeError, ValueError):
                return float("nan")

        rec: dict[str, float] = {}
        if dv20c is not None:
            rec["DollarVolume20"] = _safe_float(last.get(dv20c))
        if dv50c is not None:
            rec["DollarVolume50"] = _safe_float(last.get(dv50c))
        out[sym] = rec
    return out


def _compute_dv_from_grouped(
    target_date: str, lookback_days: int, sleep_seconds: float
) -> dict[str, dict[str, float]]:
    """Grouped Daily を過去 ``lookback_days`` 営業日分 fetch し DV20/50 を on-the-fly 計算。"""
    import time

    import pandas as pd

    from common.polygon_data import get_polygon_grouped_daily

    end = datetime.strptime(target_date, "%Y-%m-%d").date()
    days: list[date] = []
    d = end
    guard = 0
    while len(days) < lookback_days and guard < lookback_days * 3 + 10:
        if d.weekday() < 5:
            days.append(d)
        d -= timedelta(days=1)
        guard += 1
    days = sorted(days)

    dv_cols: list[Any] = []
    fetched = 0
    for i, dd in enumerate(days):
        g = get_polygon_grouped_daily(dd.isoformat())
        if g is not None and not g.empty:
            dv = g["Close"].astype("float64") * g["Volume"].astype("float64")
            dv.name = dd.isoformat()
            dv_cols.append(dv)
            fetched += 1
        if sleep_seconds > 0 and i < len(days) - 1:
            time.sleep(sleep_seconds)
    logger.info("on-the-fly DV: %d/%d 営業日を取得", fetched, len(days))
    if not dv_cols:
        return {}

    mat = pd.concat(dv_cols, axis=1)
    mat = mat.reindex(sorted(mat.columns), axis=1)
    dv20 = mat.iloc[:, -20:].mean(axis=1)
    dv50 = mat.iloc[:, -50:].mean(axis=1)
    out: dict[str, dict[str, float]] = {}
    for sym in mat.index:
        out[str(sym).upper()] = {
            "DollarVolume20": float(dv20.loc[sym]),
            "DollarVolume50": float(dv50.loc[sym]),
        }
    return out


def load_dv_cache(
    target_date: str,
    *,
    lookback_days: int = 0,
    sleep_seconds: float = 13.0,
    cache_dir: Path | None = None,
) -> dict[str, dict[str, float]]:
    """symbol -> {DollarVolume20, DollarVolume50} の dict を返す。"""
    if cache_dir is None:
        try:
            from config.settings import get_settings

            cache_dir = Path(get_settings(create_dirs=False).DATA_CACHE_DIR)
        except Exception:
            cache_dir = Path("data_cache")

    base = _load_dv_from_base_cache(cache_dir)
    logger.info("load_dv_cache: base cache から %d 銘柄", len(base))

    if lookback_days and lookback_days > 0:
        otf = _compute_dv_from_grouped(target_date, lookback_days, sleep_seconds)
        for sym, rec in otf.items():
            base.setdefault(sym, rec)
        logger.info("load_dv_cache: on-the-fly 補完後 合計 %d 銘柄", len(base))

    if not base:
        logger.warning("load_dv_cache: DV データ 0 件")
    return base


def evaluate_survival(
    grouped_df: Any, dv_cache: dict[str, dict[str, float]]
) -> dict[str, SystemSurvival]:
    """sys1-7 それぞれの gate 生存率を全 US 銘柄ユニバースで計算する。"""
    import math

    results: dict[str, SystemSurvival] = {}
    if grouped_df is None or getattr(grouped_df, "empty", True):
        for sysname, cfg in SYSTEM_GATES.items():
            results[sysname] = SystemSurvival(
                system=sysname, warn_threshold=cfg.get("warn_ratio", 0.0), status="fail"
            )
        return results

    close = grouped_df["Close"].astype("float64")
    low = grouped_df["Low"].astype("float64") if "Low" in grouped_df.columns else close
    symbols = [str(s).upper() for s in grouped_df.index]

    def _dv(sym: str, col: str) -> float:
        rec = dv_cache.get(sym)
        if not rec:
            return float("nan")
        return float(rec.get(col, float("nan")))

    for sysname, cfg in SYSTEM_GATES.items():
        s = SystemSurvival(system=sysname, warn_threshold=cfg.get("warn_ratio", 0.0))

        if cfg.get("spy_only"):
            has_spy = "SPY" in symbols
            s.n_total = 1
            s.n_pass = 1 if has_spy else 0
            s.ratio = 1.0 if has_spy else 0.0
            s.status = "ok" if has_spy else "fail"
            s.survived_tickers = ["SPY"] if has_spy else []
            results[sysname] = s
            continue

        min_price = cfg.get("min_price")
        min_col = cfg.get("min_col", "Close")
        price_series = low if min_col == "Low" else close
        dv_col = cfg.get("dv_col")
        dv_min = cfg.get("dv_min")

        n_total = 0
        survived: list[str] = []
        for i, sym in enumerate(symbols):
            px = float(price_series.iloc[i])
            if math.isnan(px):
                continue
            n_total += 1
            if min_price is not None and px < min_price:
                continue
            if dv_col is not None:
                dv = _dv(sym, dv_col)
                if math.isnan(dv) or dv <= float(dv_min):
                    continue
            survived.append(sym)

        s.n_total = n_total
        s.n_pass = len(survived)
        s.ratio = (s.n_pass / s.n_total) if s.n_total else 0.0
        s.status = "warn" if s.ratio < s.warn_threshold else "ok"
        s.survived_tickers = survived
        results[sysname] = s

    return results


def _measurable_counts_for_system(
    sysname: str,
    grouped_df: Any,
    dv_cache: dict[str, dict[str, float]],
) -> dict[str, int]:
    """grouped-daily から実測できる phase 名 -> 通過銘柄数 の dict を返す。"""
    import math

    cfg = SYSTEM_GATES.get(sysname, {})

    if cfg.get("spy_only"):
        symbols = {str(s).upper() for s in grouped_df.index}
        n = 1 if "SPY" in symbols else 0
        return {"Tgt": n, "FILpass": n}

    close = grouped_df["Close"].astype("float64")
    low = grouped_df["Low"].astype("float64") if "Low" in grouped_df.columns else close
    symbols = [str(s).upper() for s in grouped_df.index]

    min_price = cfg.get("min_price")
    min_col = cfg.get("min_col", "Close")
    price_series = low if min_col == "Low" else close
    dv_col = cfg.get("dv_col")
    dv_min = cfg.get("dv_min")

    universe = 0
    filpass = 0
    for i, sym in enumerate(symbols):
        px = float(price_series.iloc[i])
        if math.isnan(px):
            continue
        universe += 1
        if min_price is not None and px < min_price:
            continue
        if dv_col is not None:
            rec = dv_cache.get(sym) or {}
            dv = float(rec.get(dv_col, float("nan")))
            if math.isnan(dv) or dv <= float(dv_min):
                continue
        filpass += 1

    return {"Tgt": universe, "FILpass": filpass}


def build_pipeline_report(
    grouped_df: Any,
    dv_cache: dict[str, dict[str, float]],
    target_date: str,
    *,
    signals_dir: Path | None = None,
) -> dict[str, Any]:
    """sys1-7 の phase 別絞込フロー JSON を組み立てる (新 schema: signal_pipeline/v1)。"""
    from common.system_constants import SYSTEM_PIPELINE_PHASES

    empty = grouped_df is None or getattr(grouped_df, "empty", True)

    trdlist_counts: dict[str, int] = {}
    entry_counts: dict[str, int] = {}
    if signals_dir is not None:
        sig_path = signals_dir / f"today_signals_{target_date.replace('-', '')}.json"
        if sig_path.exists():
            try:
                sig = json.loads(sig_path.read_text(encoding="utf-8"))
                for sysname, entry in (sig.get("systems") or {}).items():
                    ci = entry.get("n_candidates_input")
                    so = entry.get("n_signals_output")
                    if isinstance(ci, (int, float)):
                        trdlist_counts[sysname] = int(ci)
                    if isinstance(so, (int, float)):
                        entry_counts[sysname] = int(so)
            except Exception as exc:  # pragma: no cover
                logger.warning("today_signals 読込失敗 (%s): %s", sig_path, exc)

    def _ratio(numer: int | None, denom: int | None) -> float | None:
        if numer is None or not denom:
            return None
        return round(numer / denom, 6)

    def _signal_fill(sysname: str, name: str) -> int | None:
        if name == "TRDlist":
            return trdlist_counts.get(sysname)
        if name == "Entry":
            return entry_counts.get(sysname)
        return None

    systems_out: dict[str, Any] = {}
    for sysname, phase_defs in SYSTEM_PIPELINE_PHASES.items():
        measured = (
            {}
            if empty
            else _measurable_counts_for_system(sysname, grouped_df, dv_cache)
        )
        universe_count = measured.get("Tgt", 0)

        phases_out: list[dict[str, Any]] = []
        prev_count: int | None = None
        for pdef in phase_defs:
            name = str(pdef["name"])
            count: int | None = measured.get(name)
            measured_flag = count is not None
            if count is None:
                count = _signal_fill(sysname, name)

            phases_out.append(
                {
                    "name": name,
                    "label": pdef.get("label", name),
                    "condition": pdef.get("condition", ""),
                    "count": count,
                    "measured": measured_flag,
                    "ratio_of_prev": _ratio(count, prev_count),
                    "ratio_of_universe": _ratio(count, universe_count),
                }
            )
            if count is not None:
                prev_count = count

        systems_out[sysname] = {
            "system_id": sysname,
            "phases": phases_out,
            "final_signals": entry_counts.get(sysname),
        }

    return {
        "date": target_date,
        "provider": "polygon_grouped_daily",
        "schema": "signal_pipeline/v1",
        "systems": systems_out,
        "notes": [
            "phases are reference counts, not evaluation criteria.",
            "monitor measures Tgt / FILpass only; STUpass/Exit are unmeasured (null).",
            "ratio_of_prev = count / previous measured phase; ratio_of_universe = count / Tgt.",
        ],
    }


def compute_delta(current: CoverageReport, previous_path: Path | None) -> None:
    """前日 JSON との diff を current.delta_vs_previous / consecutive_drops に埋める。"""
    for sysname in current.survival_by_system:
        current.delta_vs_previous.setdefault(sysname, 0.0)
        current.consecutive_drops.setdefault(sysname, 0)

    if previous_path is None or not previous_path.exists():
        current.notes.append("first_run=true (no_previous_report; delta=0)")
        return

    try:
        prev = json.loads(previous_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover
        logger.warning("前日 JSON の読み込みに失敗 (%s): %s", previous_path, exc)
        current.notes.append("first_run=true (previous_report_unreadable; delta=0)")
        return

    prev_survival: dict[str, Any] = prev.get("survival_by_system", {}) or {}
    prev_drops: dict[str, Any] = prev.get("consecutive_drops", {}) or {}

    for sysname, cur in current.survival_by_system.items():
        cur_ratio = float(cur.get("ratio", 0.0))
        prev_entry = prev_survival.get(sysname) or {}
        prev_ratio = float(prev_entry.get("ratio", cur_ratio))
        delta = round(cur_ratio - prev_ratio, 4)
        current.delta_vs_previous[sysname] = delta
        if delta < 0:
            current.consecutive_drops[sysname] = (
                int(prev_drops.get(sysname, 0) or 0) + 1
            )
        else:
            current.consecutive_drops[sysname] = 0

    dropping = {k: v for k, v in current.consecutive_drops.items() if v >= 3}
    if dropping:
        current.notes.append(f"consecutive_declines_3d: {dropping}")
    logger.info(
        "compute_delta: vs %s done (delta=%s)",
        previous_path.name,
        current.delta_vs_previous,
    )


def _build_rejected_top10(
    grouped_df: Any,
    dv_cache: dict[str, dict[str, float]],
    survivals: dict[str, SystemSurvival],
) -> list[dict[str, str]]:
    """当日出来高上位で sys1 (最も厳しい共通 gate) を通らない銘柄 top10 を理由付きで返す。"""
    if grouped_df is None or getattr(grouped_df, "empty", True):
        return []
    import math

    sys1_survived = set(
        survivals.get("sys1", SystemSurvival(system="sys1")).survived_tickers
    )
    close = grouped_df["Close"].astype("float64")
    vol = grouped_df["Volume"].astype("float64")
    dollar_vol = close * vol
    order = dollar_vol.sort_values(ascending=False)

    rejected: list[dict[str, str]] = []
    for sym_raw in order.index:
        sym = str(sym_raw).upper()
        if sym in sys1_survived:
            continue
        px = float(close.loc[sym_raw])
        rec = dv_cache.get(sym) or {}
        dv20 = rec.get("DollarVolume20", float("nan"))
        if not rec or (isinstance(dv20, float) and math.isnan(dv20)):
            reason = "no_dv20_cache"
        elif px < 5.0:
            reason = "price_below_5"
        else:
            reason = "dv20_below_50m"
        rejected.append({"symbol": sym, "reason": reason})
        if len(rejected) >= 10:
            break
    return rejected


def notify_warnings(report: CoverageReport) -> int:
    """閾値割れがあれば log + hook 呼び出し。exit-code 相当を返す (0=ok, 2=warn)。"""
    warns = [s for s in report.survival_by_system.values() if s.get("status") == "warn"]
    if not warns:
        logger.info("coverage OK (no warnings)")
        return 0
    for w in warns:
        logger.warning("coverage WARN: %s", w)
    return 2


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", type=str, default=None)
    p.add_argument("--output-dir", type=Path, default=Path("results_csv"))
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--dv-lookback", type=int, default=0)
    # 2026-07-02 hygiene: coverage の n_total は普通株のみに絞る
    p.add_argument(
        "--common-only",
        dest="common_only",
        action="store_true",
        default=True,
        help="preferred/warrant/unit/rights を除外し普通株のみで集計 (default)。",
    )
    p.add_argument(
        "--no-common-only",
        dest="common_only",
        action="store_false",
        help="pattern filter を無効化 (debug 用)。",
    )
    p.add_argument("--dv-sleep", type=float, default=13.0)
    p.add_argument("--log-level", default="INFO")
    return p


def _parse_target_date(raw: str | None) -> str:
    if raw:
        datetime.strptime(raw, "%Y-%m-%d")
        return raw
    return previous_business_day().isoformat()


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=args.log_level.upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    target_date = _parse_target_date(args.date)
    args.output_dir.mkdir(parents=True, exist_ok=True)

    output_path = (
        args.output_dir / f"polygon_daily_coverage_{target_date.replace('-', '')}.json"
    )
    pipeline_path = args.output_dir / f"pipeline_{target_date.replace('-', '')}.json"
    previous_path = (
        args.output_dir
        / f"polygon_daily_coverage_{previous_business_day(datetime.strptime(target_date, '%Y-%m-%d').date()).strftime('%Y%m%d')}.json"
    )

    logger.info(
        "target_date=%s  output=%s  pipeline=%s  dry_run=%s",
        target_date,
        output_path,
        pipeline_path,
        args.dry_run,
    )

    report = CoverageReport(date=target_date)

    if args.dry_run:
        report.notes.append("dry_run: skeleton only (fetch skipped)")
        output_path.write_text(report.to_json(), encoding="utf-8")
        pipeline = build_pipeline_report(
            None, {}, target_date, signals_dir=args.output_dir
        )
        pipeline["notes"].append("dry_run: skeleton only (counts unmeasured)")
        pipeline_path.write_text(
            json.dumps(pipeline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info(
            "[dry-run] wrote skeleton reports -> %s , %s", output_path, pipeline_path
        )
        return 0

    try:
        grouped = fetch_grouped_daily(target_date)
        # 2026-07-02 hygiene: 普通株のみに絞る (default True)。
        if getattr(args, "common_only", True):
            grouped = apply_common_stock_filter(grouped)
        report.n_candidates_total = int(getattr(grouped, "shape", [0])[0])
        dv_cache = load_dv_cache(
            target_date,
            lookback_days=args.dv_lookback,
            sleep_seconds=args.dv_sleep,
        )
        survivals = evaluate_survival(grouped, dv_cache)
        report.survival_by_system = {k: v.as_dict() for k, v in survivals.items()}
        report.rejected_top10 = _build_rejected_top10(grouped, dv_cache, survivals)
        compute_delta(report, previous_path)
        pipeline = build_pipeline_report(
            grouped, dv_cache, target_date, signals_dir=args.output_dir
        )
    except ValueError as exc:
        logger.error("fail-fast: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error: %s", exc)
        return 1

    output_path.write_text(report.to_json(), encoding="utf-8")
    pipeline_path.write_text(
        json.dumps(pipeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info(
        "wrote %s and %s (n_total=%d)",
        output_path,
        pipeline_path,
        report.n_candidates_total,
    )
    return notify_warnings(report)


if __name__ == "__main__":
    sys.exit(main())
