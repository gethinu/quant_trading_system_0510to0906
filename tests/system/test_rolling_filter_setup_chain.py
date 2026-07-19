"""Integration: rolling → indicators → filter → setup → candidate の連鎖が
実データ形状の frame で seam を跨いで機能すること (silent 3-stage break の回帰)。

背景 (2026-07-19 audit — gap ii):
    既存の signal test は stage 単位、または事前計算済みの setup/filter 列を
    fixture に焼き込んでおり、indicator→filter や filter→setup の列名/契約が
    割れて candidates が 0 件に silent 落ちする seam を end-to-end で検知して
    いなかった。本 test は raw OHLCV から実パイプライン関数
    (add_indicators / filter_system1 / prepare_data_vectorized_system1 /
    generate_candidates_system1) を通し、各 seam が生きていることを確認する。

    System1 (ROC200 momentum long) を代表に、明確な上昇トレンドを合成して
    filter (Close>=5 & DV20>=50M) と setup (SMA25>SMA50 & ROC200>0) を両方
    満たすようにし、chain が候補を生む (= seam が全部生きている) ことを assert する。
"""

from __future__ import annotations

from pathlib import Path
import sys

import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


def _uptrend_ohlcv(n: int = 260) -> pd.DataFrame:
    """DV20>=50M & 明確な上昇トレンドの合成 OHLCV (System1 の filter+setup 通過用)。"""
    idx = pd.bdate_range("2025-01-01", periods=n)
    close = np.linspace(50.0, 150.0, n)
    df = pd.DataFrame(
        {
            "Open": close * 0.995,
            "High": close * 1.01,
            "Low": close * 0.985,
            "Close": close,
            "Volume": np.full(n, 1_500_000.0),
        },
        index=idx,
    )
    df.index.name = "Date"
    return df


def test_rolling_filter_setup_chain_system1_produces_candidate():
    from common.indicators_common import add_indicators
    from common.today_filters import filter_system1
    from core.system1 import (
        generate_candidates_system1,
        prepare_data_vectorized_system1,
    )

    sym = "UPTR"
    raw = _uptrend_ohlcv()

    # --- seam 1: rolling frame -> indicators (numeric, not all-NaN) ----------
    ind = add_indicators(raw)
    for col in ("sma25", "sma50", "roc200"):
        assert col in ind.columns, f"indicator '{col}' missing (indicator stage broke)"
        assert ind[col].notna().any(), f"'{col}' all-NaN (indicator stage broke)"

    # --- seam 2: indicators -> filter ---------------------------------------
    stats: dict = {}
    passed = filter_system1([sym], {sym: ind}, stats)
    assert stats.get("total") == 1
    assert stats.get("dv_pass") == 1, "engineered uptrend should pass DV20>=50M"
    assert (
        sym in passed
    ), "filter dropped a qualifying symbol (indicator→filter seam broke)"

    # --- seam 3: prepare (filter+setup) -> setup True on qualifying row ------
    # 実パイプラインの rolling cache は指標を precompute 済 → prepare は reuse する。
    # ここでは seam 1 の add_indicators 出力 (=rolling cache 相当) を食わせる。
    prepared = prepare_data_vectorized_system1(
        {sym: ind}, symbols=[sym], reuse_indicators=True
    )
    assert sym in prepared and not prepared[sym].empty
    pdf = prepared[sym]
    assert "setup" in pdf.columns, "setup column missing (filter→setup seam broke)"
    assert (
        bool(pdf["setup"].iloc[-1]) is True
    ), "qualifying uptrend last row should be setup=True (setup logic broke)"

    # --- seam 4: setup -> candidate generation (latest_only = daily path) ----
    res = generate_candidates_system1(
        prepared, top_n=10, latest_only=True, include_diagnostics=True
    )
    candidates = res[0]
    assert candidates, (
        "chain yielded 0 candidates for a strong uptrend "
        "(setup→candidate seam broke — this is the silent-3-stage failure mode)"
    )
