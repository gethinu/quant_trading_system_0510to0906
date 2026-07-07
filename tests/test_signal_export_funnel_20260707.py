"""build_signals_json に stage_metrics funnel を per-system serialize する検証。

背景 (2026-07-07 observability fix):
    phase count (Tgt/FIL/STU/TRD/Entry/Exit) は従来 in-memory の
    ``GLOBAL_STAGE_METRICS`` にしか無く、``today_signals_*.json`` へ書かれて
    いなかったため Vercel dashboard の SIGNAL PIPELINE funnel が全 '未計測'
    だった。build_signals_json に ``stage_metrics`` を渡すと per-system の
    ``funnel`` と portfolio.universe_target が JSON に載る。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.signal_export import build_signals_json  # noqa: E402
from common.stage_metrics import StageSnapshot  # noqa: E402


def _final_df() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {"symbol": "AAPL", "system": "system1", "side": "long", "entry_price": 100.0},
            {"symbol": "MSFT", "system": "system1", "side": "long", "entry_price": 200.0},
            {"symbol": "TSLA", "system": "system2", "side": "short", "entry_price": 250.0},
        ]
    )


def _stage_metrics() -> dict[str, StageSnapshot]:
    return {
        "system1": StageSnapshot(
            progress=100,
            target=4000,
            filter_pass=120,
            setup_pass=30,
            candidate_count=12,
            entry_count=2,
            exit_count=1,
        ),
        "system2": StageSnapshot(
            progress=100,
            target=4000,
            filter_pass=80,
            setup_pass=20,
            candidate_count=9,
            entry_count=1,
            exit_count=0,
        ),
        # system7 は SPY 固定なので target=1 (universe_target の max に影響しない)
        "system7": StageSnapshot(progress=100, target=1, candidate_count=1),
    }


def test_funnel_absent_when_no_stage_metrics():
    """後方互換: stage_metrics を渡さない従来呼び出しは funnel を付けない。"""
    payload = build_signals_json(_final_df(), None, date_str="2026-07-07")
    for cfg in payload["systems"].values():
        assert "funnel" not in cfg
    assert payload["portfolio"]["universe_target"] is None


def test_funnel_serialized_per_system():
    payload = build_signals_json(
        _final_df(), None, date_str="2026-07-07", stage_metrics=_stage_metrics()
    )
    sys1 = payload["systems"]["sys1"]["funnel"]
    assert sys1["target"] == 4000
    assert sys1["filter_pass"] == 120
    assert sys1["setup_pass"] == 30
    assert sys1["candidate_count"] == 12
    assert sys1["entry_count"] == 2
    assert sys1["exit_count"] == 1


def test_universe_target_uses_max_not_system7():
    """universe_target は system7(target=1) に引きずられず shared universe を出す。"""
    payload = build_signals_json(
        _final_df(), None, date_str="2026-07-07", stage_metrics=_stage_metrics()
    )
    assert payload["portfolio"]["universe_target"] == 4000


def test_funnel_none_phase_preserved_as_unmeasured():
    """一部 phase が None (未計測) でも funnel dict は出す (dashboard が '-' 表示)。"""
    metrics = {"system3": StageSnapshot(progress=100, target=4000, setup_pass=None)}
    df = pd.DataFrame(
        [{"symbol": "AMD", "system": "system3", "side": "long", "entry_price": 90.0}]
    )
    payload = build_signals_json(
        df, None, date_str="2026-07-07", stage_metrics=metrics
    )
    funnel = payload["systems"]["sys3"]["funnel"]
    assert funnel["target"] == 4000
    assert funnel["setup_pass"] is None


def test_entry_count_backfilled_from_signals_when_null():
    """headless では entry_count が None になりがち → n_signals_output で補完する。"""
    # snapshot に entry_count を入れない (=None)。final_df に system1 の 2 signals。
    metrics = {"system1": StageSnapshot(progress=100, target=4000, candidate_count=12)}
    df = pd.DataFrame([
        {"symbol": "AAPL", "system": "system1", "side": "long", "entry_price": 100.0},
        {"symbol": "MSFT", "system": "system1", "side": "long", "entry_price": 200.0},
    ])
    payload = build_signals_json(df, None, date_str="2026-07-07", stage_metrics=metrics)
    funnel = payload["systems"]["sys1"]["funnel"]
    assert funnel["entry_count"] == 2  # n_signals_output で補完


def test_entry_count_from_snapshot_not_overwritten():
    """snapshot に entry_count があればそれを優先 (補完で上書きしない)。"""
    metrics = {"system1": StageSnapshot(progress=100, target=4000, entry_count=7)}
    df = pd.DataFrame([
        {"symbol": "AAPL", "system": "system1", "side": "long", "entry_price": 100.0},
    ])
    payload = build_signals_json(df, None, date_str="2026-07-07", stage_metrics=metrics)
    assert payload["systems"]["sys1"]["funnel"]["entry_count"] == 7
