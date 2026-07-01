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
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

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


def load_dv_cache(target_date: str) -> Any:
    """既存 data_cache/*.feather から前日の DollarVolume20/50 を lookup。

    TODO(polygon-key-added-iter): cache_manager 経由で symbol -> {DV20, DV50, Close, Low}
    の dict を返す実装を追加。cache miss は Grouped Daily 過去 60 日を fetch で on-the-fly 補完。
    """
    logger.info("[skeleton] load_dv_cache: target_date=%s (implementation deferred)", target_date)
    return {}


def evaluate_survival(grouped_df: Any, dv_cache: dict[str, Any]) -> dict[str, SystemSurvival]:
    """sys1-7 それぞれの gate 生存率を計算する。

    TODO(polygon-key-added-iter): grouped_df / dv_cache を join し、SYSTEM_GATES の閾値を
    適用して n_pass / ratio / status を埋める。現状は空の SystemSurvival を返すだけ。
    """
    results: dict[str, SystemSurvival] = {}
    for sysname, cfg in SYSTEM_GATES.items():
        s = SystemSurvival(system=sysname, warn_threshold=cfg.get("warn_ratio", 0.0))
        results[sysname] = s
    return results


def compute_delta(current: CoverageReport, previous_path: Path | None) -> None:
    """前日 JSON との diff を current.delta_vs_previous / consecutive_drops に埋める。

    TODO(polygon-key-added-iter): previous_path が存在すれば JSON 読み込み、各 system で
    ratio 差分を計算。連続下落は過去 3 日分の履歴を walk して count。
    """
    if previous_path is None or not previous_path.exists():
        current.notes.append("no_previous_report (delta unavailable)")
        return
    logger.info("[skeleton] compute_delta: previous=%s (implementation deferred)", previous_path)


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
    previous_path = args.output_dir / f"polygon_daily_coverage_{previous_business_day(datetime.strptime(target_date, '%Y-%m-%d').date()).strftime('%Y%m%d')}.json"

    logger.info("target_date=%s  output=%s  dry_run=%s", target_date, output_path, args.dry_run)

    report = CoverageReport(date=target_date)

    if args.dry_run:
        report.notes.append("dry_run: skeleton only (fetch skipped)")
        output_path.write_text(report.to_json(), encoding="utf-8")
        logger.info("[dry-run] wrote skeleton report -> %s", output_path)
        return 0

    try:
        grouped = fetch_grouped_daily(target_date)
        report.n_candidates_total = int(getattr(grouped, "shape", [0])[0])
        dv_cache = load_dv_cache(target_date)
        survivals = evaluate_survival(grouped, dv_cache)
        report.survival_by_system = {k: v.as_dict() for k, v in survivals.items()}
        compute_delta(report, previous_path)
    except ValueError as exc:
        # POLYGON_API_KEY 未設定
        logger.error("fail-fast: %s", exc)
        return 1
    except Exception as exc:  # noqa: BLE001
        logger.exception("unexpected error: %s", exc)
        return 1

    output_path.write_text(report.to_json(), encoding="utf-8")
    logger.info("wrote %s (n_total=%d)", output_path, report.n_candidates_total)
    return notify_warnings(report)


if __name__ == "__main__":
    sys.exit(main())
