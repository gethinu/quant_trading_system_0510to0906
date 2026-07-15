"""Regression: pipeline_*.json funnel が「未計測」に silent 逆戻りしないことを固定する.

背景 (2026-07-15):
    coverage step (daily_polygon_monitor.build_pipeline_report) は Tgt/FILpass を
    「今日」の Polygon grouped-daily から実測する。だが定例 06:00 JST run では当日 EOD
    前で grouped が 403 → 空 DataFrame → measured={} となり Tgt/FILpass/STUpass が全て
    null (=ダッシュボード「未計測」)。一方 signals エンジンは today_signals.funnel に
    target/filter_pass/setup_pass を既に確定させている。この funnel を fallback ソース
    として採用し、grouped が空でも funnel 値で count を埋める。

    [[feedback-silent-cache-bug-lesson]]: データ欠損を silent に null 化せず、既に
    確定している値へ loud に fallback する。
"""

from __future__ import annotations

import json
from pathlib import Path

from scripts.daily_polygon_monitor import build_pipeline_report


def _write_signals(
    sig_dir: Path, date_compact: str, *, with_funnel: bool = True
) -> None:
    systems = {}
    # sys1: 通常ケース, sys6: STUpass=0, sys7: SPY 専用 (funnel target=universe)
    specs = {
        "sys1": dict(target=5223, filter_pass=1515, setup_pass=686, cand=10, entry=7),
        "sys6": dict(target=5223, filter_pass=1008, setup_pass=0, cand=0, entry=0),
        "sys7": dict(target=5223, filter_pass=1, setup_pass=0, cand=0, entry=0),
    }
    for name, s in specs.items():
        entry = {
            "signals": [],
            "n_candidates_input": s["cand"],
            "n_signals_output": s["entry"],
        }
        if with_funnel:
            entry["funnel"] = {
                "target": s["target"],
                "filter_pass": s["filter_pass"],
                "setup_pass": s["setup_pass"],
                "candidate_count": s["cand"],
                "entry_count": s["entry"],
                "exit_count": None,
            }
        systems[name] = entry
    payload = {"version": "1.0", "date": "2026-07-15", "systems": systems}
    (sig_dir / f"today_signals_{date_compact}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


def _phase(report: dict, sysname: str, phase_name: str) -> dict:
    return next(
        p for p in report["systems"][sysname]["phases"] if p["name"] == phase_name
    )


def test_funnel_fallback_when_grouped_empty(tmp_path: Path) -> None:
    """grouped 空 (403 相当) でも Tgt/FILpass/STUpass が funnel から埋まる (未計測回帰の核心)."""
    _write_signals(tmp_path, "20260715", with_funnel=True)

    report = build_pipeline_report(None, {}, "2026-07-15", signals_dir=tmp_path)

    tgt = _phase(report, "sys1", "Tgt")
    fil = _phase(report, "sys1", "FILpass")
    stu = _phase(report, "sys1", "STUpass")
    # count は funnel 由来で非 null (= ダッシュボードが数値表示する条件)
    assert tgt["count"] == 5223
    assert fil["count"] == 1515
    assert stu["count"] == 686
    # grouped 実測ではないので measured=False (provenance を偽らない)
    assert tgt["measured"] is False
    assert fil["measured"] is False
    # 比率も埋まる (未計測なら prev/univ が '—' 表示になる)
    assert fil["ratio_of_universe"] == round(1515 / 5223, 6)
    assert stu["ratio_of_prev"] == round(686 / 1515, 6)


def test_no_funnel_key_stays_null_graceful(tmp_path: Path) -> None:
    """today_signals に funnel が無い旧形式では Tgt/FILpass/STUpass は null のまま (例外を出さない)."""
    _write_signals(tmp_path, "20260715", with_funnel=False)

    report = build_pipeline_report(None, {}, "2026-07-15", signals_dir=tmp_path)

    assert _phase(report, "sys1", "Tgt")["count"] is None
    assert _phase(report, "sys1", "STUpass")["count"] is None
    # TRDlist/Entry は n_candidates_input/n_signals_output から従来通り埋まる
    assert _phase(report, "sys1", "TRDlist")["count"] == 10
    assert _phase(report, "sys1", "Entry")["count"] == 7


def test_grouped_measured_takes_priority_over_funnel(tmp_path: Path) -> None:
    """grouped 実測がある場合は measured=True でそちらを優先。STUpass は funnel fallback。"""
    import pandas as pd

    _write_signals(tmp_path, "20260715", with_funnel=True)
    # SPY + 高値高 DV の銘柄群を grouped に用意 (sys1 の min_price/DV gate を通す)
    grouped = pd.DataFrame(
        {
            "Open": [100.0, 200.0, 4.0],
            "High": [101.0, 201.0, 4.1],
            "Low": [99.0, 199.0, 3.9],
            "Close": [100.0, 200.0, 4.0],
            "Volume": [5_000_000, 5_000_000, 5_000_000],
        },
        index=pd.Index(["AAA", "BBB", "PENNY"], name="symbol"),
    )
    dv_cache = {
        "AAA": {"dollarvolume20": 80_000_000.0, "dollarvolume50": 80_000_000.0},
        "BBB": {"dollarvolume20": 80_000_000.0, "dollarvolume50": 80_000_000.0},
        "PENNY": {"dollarvolume20": 1_000.0, "dollarvolume50": 1_000.0},
    }

    report = build_pipeline_report(
        grouped, dv_cache, "2026-07-15", signals_dir=tmp_path
    )

    tgt = _phase(report, "sys1", "Tgt")
    stu = _phase(report, "sys1", "STUpass")
    # Tgt は grouped 実測 (measured=True) — funnel の 5223 ではなく grouped の銘柄数
    assert tgt["measured"] is True
    assert tgt["count"] != 5223
    # STUpass は grouped で実測不能 → funnel fallback (measured=False, count=686)
    assert stu["measured"] is False
    assert stu["count"] == 686


def test_exit_stays_null_when_signals_has_no_exit(tmp_path: Path) -> None:
    """funnel.exit_count が null なら Exit も null のまま (誤って埋めない = 誠実な未計測)."""
    _write_signals(tmp_path, "20260715", with_funnel=True)
    report = build_pipeline_report(None, {}, "2026-07-15", signals_dir=tmp_path)
    assert _phase(report, "sys1", "Exit")["count"] is None
