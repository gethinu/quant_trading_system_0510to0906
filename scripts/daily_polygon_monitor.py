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
import json
import logging
import os
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

# スクリプトを直接 (python scripts/daily_polygon_monitor.py / .ps1 経由) 実行しても
# リポジトリ直下の `common` パッケージを解決できるようにする。
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

logger = logging.getLogger(__name__)

# --- System 別 gate 定義 (core/system*.py を grep で実測) ---------------
# 詳細は docs/HUMAN_TASK_polygon_daily_monitor_20260701.md §2.2 を参照。
SYSTEM_GATES: dict[str, dict[str, Any]] = {
    "sys1": {"min_price": 5.0, "dv_col": "DollarVolume20", "dv_min": 50_000_000, "warn_ratio": 0.05},
    "sys2": {"min_price": 5.0, "dv_col": "DollarVolume20", "dv_min": 25_000_000, "warn_ratio": 0.06},
    "sys3": {"min_price": 5.0, "dv_col": "DollarVolume20", "dv_min": 25_000_000, "warn_ratio": 0.06},
    "sys4": {"min_price": None, "dv_col": "DollarVolume50", "dv_min": 100_000_000, "warn_ratio": 0.04},
    "sys5": {"min_price": 5.0, "dv_col": None, "dv_min": None, "warn_ratio": 0.15},  # DV 閾値なし
    "sys6": {"min_price": 5.0, "dv_col": "DollarVolume50", "dv_min": 10_000_000, "warn_ratio": 0.10, "min_col": "Low"},
    "sys7": {"spy_only": True, "warn_ratio": 1.0},  # SPY 固定 (欠損時のみ FAIL)
}


@dataclass
class SystemSurvival:
    """1 system の生存率評価結果。"""

    system: str
    n_pass: int = 0
    n_total: int = 0
    ratio: float = 0.0
    warn_threshold: float = 0.0
    status: str = "pending"  # ok | warn | fail | pending
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


# --- 前営業日ロジック ---------------------------------------------------

def previous_business_day(anchor: date | None = None) -> date:
    """anchor (default: 今日) の直前平日を返す。US 祝日は考慮しない (Polygon 側で空応答 → warn)。"""
    d = anchor or date.today()
    d -= timedelta(days=1)
    while d.weekday() >= 5:  # Sat=5, Sun=6
        d -= timedelta(days=1)
    return d


# --- 核ロジック (skeleton) ---------------------------------------------

def fetch_grouped_daily(target_date: str) -> Any:
    """Polygon Grouped Daily を fetch する thin wrapper。

    Polygon key 未投入時は ValueError を common.polygon_data 側が raise するので
    そのまま propagate させる (fail-fast)。
    """
    # 遅延 import (POLYGON_API_KEY 未投入時に import だけで落ちないよう)
    from common.polygon_data import get_polygon_grouped_daily

    return get_polygon_grouped_daily(target_date)


def _load_dv_from_base_cache(cache_dir: Path) -> dict[str, dict[str, float]]:
    """``data_cache/base/*.feather`` から各銘柄の最新 DollarVolume20/50 を読む。"""
    out: dict[str, dict[str, float]] = {}
    base_dir = cache_dir / "base"
    if not base_dir.exists():
        return out
    import pandas as pd  # 遅延 import

    for fp in base_dir.glob("*.feather"):
        try:
            df = pd.read_feather(fp)
        except Exception:  # pragma: no cover - 壊れた feather はスキップ
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
            # pd.NA / None / 変換不能値は nan に落とす (float(pd.NA) は TypeError)。
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
    """Grouped Daily を過去 ``lookback_days`` 営業日分 fetch し DV20/50 を on-the-fly 計算。

    cache miss (base feather 未整備) 時のフォールバック。Close×Volume の
    直近 20/50 営業日平均を全銘柄まとめてベクトル計算する。
    """
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
    mat = mat.reindex(sorted(mat.columns), axis=1)  # 日付昇順
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
    """symbol -> {"DollarVolume20", "DollarVolume50"} の dict を返す。

    優先順位:
        1. ``data_cache/base/*.feather`` の最新 DollarVolume20/50 (cache_daily_polygon
           / cache_daily_data で整備済のもの)。
        2. cache coverage が薄い場合、``lookback_days`` > 0 なら Grouped Daily を
           その日数だけ fetch して Close×Volume の 20/50 日平均を on-the-fly 計算し
           cache を補完する (base 値を優先し、欠けている銘柄のみ埋める)。

    Parameters
    ----------
    target_date : str
        対象取引日 (YYYY-MM-DD)。on-the-fly 計算の終端。
    lookback_days : int
        on-the-fly 補完で fetch する営業日数 (0 = base cache のみ)。DV50 には 50 以上推奨。
    """
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
        # base を優先し、base に無い銘柄のみ on-the-fly で補完
        for sym, rec in otf.items():
            base.setdefault(sym, rec)
        logger.info("load_dv_cache: on-the-fly 補完後 合計 %d 銘柄", len(base))

    if not base:
        logger.warning("load_dv_cache: DV データ 0 件 (base cache 未整備 & lookback=0)。"
                       "cache_daily_polygon.py で backfill するか --dv-lookback を指定してください。")
    return base


def evaluate_survival(
    grouped_df: Any, dv_cache: dict[str, dict[str, float]]
) -> dict[str, SystemSurvival]:
    """sys1-7 それぞれの gate 生存率を全 US 銘柄ユニバースで計算する。

    grouped_df (当日 Grouped Daily / index=symbol) に dv_cache (前日 DV20/50) を
    join し、SYSTEM_GATES の閾値を適用。ratio = n_pass / n_total (n_total = 当日
    価格のある全銘柄) で production coverage metric を算出する。
    """
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
            n_total += 1  # 当日価格のある全銘柄が母数
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


# --- Signal pipeline (絞込フロー) ---------------------------------------
# user 指摘: 単一 "survival rate" は評価軸ではない。universe → filter → setup →
# ... → final の phase 別絞込を「参考数値」として可視化するため、grouped-daily で
# 実測できる phase (universe / price / DV) の通過銘柄数を計測し JSON 出力する。
# setup 以降 (指標依存) は monitor では計測不能なので count=None を返す。


def _measurable_counts_for_system(
    sysname: str,
    grouped_df: Any,
    dv_cache: dict[str, dict[str, float]],
) -> dict[str, int]:
    """grouped-daily から実測できる phase 名 -> 通過銘柄数 の dict を返す。

    key は SYSTEM_PIPELINE_PHASES の phase ``name`` に一致させる:
    ``Tgt`` (ユニバース) と ``FILpass`` (Phase2 事前フィルター通過 = price + DV)。
    STUpass 以降は指標依存で monitor では計測できない。
    """
    import math

    cfg = SYSTEM_GATES.get(sysname, {})

    # sys7: SPY 固定。Tgt/FILpass = SPY があれば 1。
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
    """sys1-7 の phase 別絞込フロー JSON を組み立てる (新 schema: signal_pipeline/v1)。

    各 system は SYSTEM_PIPELINE_PHASES の phase 定義を辿り、grouped-daily で
    実測できる phase には count / ratio_of_prev / ratio_of_universe を、実測不能な
    phase (setup 以降) には count=None を入れる。今日の signals JSON があれば
    ``final`` phase を n_signals_output で補完する。

    NOTE: ratio は「絞込透明性のための参考数値」であり評価軸ではない。
    """
    from common.system_constants import SYSTEM_PIPELINE_PHASES

    empty = grouped_df is None or getattr(grouped_df, "empty", True)

    # 今日の today_signals (あれば) から TRDlist / Entry phase を補完:
    #   TRDlist ≈ n_candidates_input (ランキング抽出後の候補数)
    #   Entry   ≈ n_signals_output   (allocation 後の最終エントリ数)
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

    # phase name -> 当日 today_signals からの補完値 (monitor では未計測な phase 用)
    def _signal_fill(sysname: str, name: str) -> int | None:
        if name == "TRDlist":
            return trdlist_counts.get(sysname)
        if name == "Entry":
            return entry_counts.get(sysname)
        return None

    systems_out: dict[str, Any] = {}
    for sysname, phase_defs in SYSTEM_PIPELINE_PHASES.items():
        measured = {} if empty else _measurable_counts_for_system(sysname, grouped_df, dv_cache)
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
                    # measured=True は grouped-daily monitor の実測のみ (補完値は False)
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
            "phases (Tgt/FILpass/STUpass/TRDlist/Entry/Exit) は絞込透明性のための"
            "参考数値 (evaluation ではない)。",
            "monitor が実測するのは Tgt / FILpass のみ。STUpass/Exit は未計測 (null)、"
            "TRDlist/Entry は当日 today_signals があれば補完。",
            "ratio_of_prev = count / 直前計測 phase; ratio_of_universe = count / Tgt。",
        ],
    }


def compute_delta(current: CoverageReport, previous_path: Path | None) -> None:
    """前日 JSON との diff を current.delta_vs_previous / consecutive_drops に埋める。

    - delta_vs_previous[sys] = 当日 ratio - 前日 ratio (小数 4 桁)。
    - consecutive_drops[sys] = ratio が前日より下落していれば前日 count + 1、
      非下落なら 0 にリセット。3 以上で「連続 3 日下落」フラグ相当。
    - 前日 JSON が無ければ全 delta=0 / first_run=true フラグを notes に付与。
    """
    # まず全 system を 0 初期化 (前日欠損時のデフォルト)
    for sysname in current.survival_by_system:
        current.delta_vs_previous.setdefault(sysname, 0.0)
        current.consecutive_drops.setdefault(sysname, 0)

    if previous_path is None or not previous_path.exists():
        current.notes.append("first_run=true (no_previous_report; delta=0)")
        return

    try:
        prev = json.loads(previous_path.read_text(encoding="utf-8"))
    except Exception as exc:  # pragma: no cover - 壊れた JSON は first_run 扱い
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
            current.consecutive_drops[sysname] = int(prev_drops.get(sysname, 0) or 0) + 1
        else:
            current.consecutive_drops[sysname] = 0

    dropping = {k: v for k, v in current.consecutive_drops.items() if v >= 3}
    if dropping:
        current.notes.append(f"consecutive_declines_3d: {dropping}")
    logger.info("compute_delta: vs %s 完了 (delta=%s)",
                previous_path.name, current.delta_vs_previous)


def _build_rejected_top10(
    grouped_df: Any,
    dv_cache: dict[str, dict[str, float]],
    survivals: dict[str, SystemSurvival],
) -> list[dict[str, str]]:
    """当日出来高上位で sys1 (最も厳しい共通 gate) を通らない銘柄 top10 を理由付きで返す。"""
    if grouped_df is None or getattr(grouped_df, "empty", True):
        return []
    import math

    sys1_survived = set(survivals.get("sys1", SystemSurvival(system="sys1")).survived_tickers)
    close = grouped_df["Close"].astype("float64")
    vol = grouped_df["Volume"].astype("float64")
    dollar_vol = (close * vol)
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
    """閾値割れがあれば log + hook 呼び出し。exit-code 相当を返す (0=ok, 2=warn)。

    TODO(polygon-key-added-iter): Discord webhook / Windows toast を .env の
    MONITOR_NOTIFY_WEBHOOK に応じて発火。まずは log WARN のみで足りる。
    """
    warns = [s for s in report.survival_by_system.values() if s.get("status") == "warn"]
    if not warns:
        logger.info("coverage OK (no warnings)")
        return 0
    for w in warns:
        logger.warning("coverage WARN: %s", w)
    return 2


# --- entry point --------------------------------------------------------

def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--date",
        type=str,
        default=None,
        help="対象取引日 (YYYY-MM-DD)。未指定なら前営業日。",
    )
    p.add_argument(
        "--output-dir",
        type=Path,
        default=Path("results_csv"),
        help="JSON 出力先ディレクトリ (default: results_csv/)。",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Polygon fetch をスキップし、骨格 / 出力 path のみ確認する。",
    )
    p.add_argument(
        "--dv-lookback",
        type=int,
        default=0,
        help="base cache が薄い場合に Grouped Daily を過去 N 営業日 fetch して "
        "DV20/50 を on-the-fly 計算する (0=cache のみ, DV50 には 50 以上推奨)。",
    )
    p.add_argument(
        "--dv-sleep",
        type=float,
        default=13.0,
        help="on-the-fly DV 計算時の Grouped Daily call 間 sleep 秒 (既定 13s)。",
    )
    p.add_argument("--log-level", default="INFO", help="ログレベル (default: INFO)。")
    return p


def _parse_target_date(raw: str | None) -> str:
    if raw:
        # 妥当性チェック (fail-fast)
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

    output_path = args.output_dir / f"polygon_daily_coverage_{target_date.replace('-', '')}.json"
    # 新 schema (絞込フロー) の出力先。旧 coverage と並行して書き出す (Pack4 移行期)。
    pipeline_path = args.output_dir / f"pipeline_{target_date.replace('-', '')}.json"
    previous_path = args.output_dir / f"polygon_daily_coverage_{previous_business_day(datetime.strptime(target_date, '%Y-%m-%d').date()).strftime('%Y%m%d')}.json"

    logger.info("target_date=%s  output=%s  pipeline=%s  dry_run=%s",
                target_date, output_path, pipeline_path, args.dry_run)

    report = CoverageReport(date=target_date)

    if args.dry_run:
        report.notes.append("dry_run: skeleton only (fetch skipped)")
        output_path.write_text(report.to_json(), encoding="utf-8")
        # 新 schema も skeleton (grouped 空) で書き出し、下流の存在チェックを通す。
        pipeline = build_pipeline_report(None, {}, target_date, signals_dir=args.output_dir)
        pipeline["notes"].append("dry_run: skeleton only (counts unmeasured)")
        pipeline_path.write_text(
            json.dumps(pipeline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.info("[dry-run] wrote skeleton reports -> %s , %s", output_path, pipeline_path)
        return 0

    try:
        grouped = fetch_grouped_daily(target_date)
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
        # POLYGON_API_KEY / MASSIVE_API_KEY 未設定 (共に common.polygon_data で判定)
        logger.error("fail-fast: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error: %s", exc)
        return 1

    output_path.write_text(report.to_json(), encoding="utf-8")
    pipeline_path.write_text(
        json.dumps(pipeline, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    logger.info("wrote %s and %s (n_total=%d)", output_path, pipeline_path, report.n_candidates_total)
    return notify_warnings(report)


if __name__ == "__main__":
    sys.exit(main())
