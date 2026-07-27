"""portfolio cap の trim 理由が signals JSON に載ることの回帰テスト (2026-07-27)。

背景:
    2026-07-21..27 の 5 営業日、long 4 system (1/3/4/5) の signals が毎日 0 本
    だった。生成側の funnel は健全 (cand = 10/10/10/7) で、原因は
    ``held_long=51 > max_long_positions=40`` により ``allow_long=0`` になっていた
    こと。ところがこの事実は ``[PORTFOLIO_CAP]`` の INFO ログにしか出ておらず、
    signals JSON / ダッシュボードからは「system2 だけシグナルが出る」としか
    見えなかった (実ログ: logs/open_run_20260727/signals.log
    ``[PORTFOLIO_CAP] trimmed {'long_count': 30, 'total': 1} (held L51/S10)``)。

    本テストは cap の held/allow/trimmed が JSON の ``portfolio.caps`` に
    載ることを固定し、同じ「理由の見えない 0」が再発しないようにする。
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.signal_export import build_signals_json
from common.stage_metrics import StageMetricsStore

# 2026-07-27 の実測値 (logs/open_run_20260727/signals.log より)。
REAL_0727_CAPS = {
    "applied": True,
    "held": {"long": 51, "short": 10, "total": 61},
    "caps": {"max_total": 70, "max_long": 40, "max_short": 30},
    "allow": {"long": 0, "short": 20, "total": 9},
    "kept": {"long": 0, "short": 9, "total": 9},
    "trimmed": {"long_count": 30, "total": 1},
}


def test_store_roundtrips_portfolio_caps() -> None:
    store = StageMetricsStore()
    assert store.get_portfolio_caps() is None

    store.set_portfolio_caps(REAL_0727_CAPS)
    got = store.get_portfolio_caps()
    assert got is not None
    assert got["held"] == {"long": 51, "short": 10, "total": 61}
    assert got["allow"]["long"] == 0


def test_store_returns_a_copy_not_a_live_reference() -> None:
    """呼び出し側の変異が store 内部に伝播しないこと (観測値の汚染防止)。"""

    store = StageMetricsStore()
    store.set_portfolio_caps(REAL_0727_CAPS)

    got = store.get_portfolio_caps()
    assert got is not None
    got["held"] = {"long": 999}

    again = store.get_portfolio_caps()
    assert again is not None
    assert again["held"] == {"long": 51, "short": 10, "total": 61}


def test_reset_clears_portfolio_caps() -> None:
    store = StageMetricsStore()
    store.set_portfolio_caps(REAL_0727_CAPS)
    store.reset()
    assert store.get_portfolio_caps() is None


@pytest.mark.parametrize("bad", [None, "not-a-dict", 42, ["a"]])
def test_non_dict_reports_do_not_crash(bad: object) -> None:
    """観測性のための side-channel が allocation を壊さないこと。"""

    store = StageMetricsStore()
    store.set_portfolio_caps(bad)
    assert store.get_portfolio_caps() is None


def test_signals_json_carries_caps_so_zero_has_a_reason() -> None:
    """cand が健全でも signals=0 の理由 (allow_long=0) が JSON から読めること。"""

    payload = build_signals_json(
        pd.DataFrame(),
        {},
        date_str="2026-07-27",
        portfolio_caps=REAL_0727_CAPS,
    )

    caps = payload["portfolio"]["caps"]
    assert caps is not None
    # long が 0 本だった理由 = 既保有 51 が long cap 40 を超過 -> 新規枠 0。
    assert caps["held"]["long"] == 51
    assert caps["caps"]["max_long"] == 40
    assert caps["allow"]["long"] == 0
    assert caps["trimmed"]["long_count"] == 30
    # 「ちょうど 9 本」= 総枠 70 - 既保有 61 の残枠であって signal 生成数ではない。
    assert caps["allow"]["total"] == 9
    assert caps["kept"]["total"] == 9


def test_caps_absent_stays_none_and_does_not_break_schema() -> None:
    """cap 未適用 (旧経路 / 呼ばれなかった) 場合も従来どおり成立すること。"""

    payload = build_signals_json(pd.DataFrame(), {}, date_str="2026-07-27")

    assert payload["portfolio"]["caps"] is None
    # 既存キーは不変。
    for key in ("total_signals", "total_notional_usd", "hedge", "universe_target"):
        assert key in payload["portfolio"]


def test_apply_portfolio_caps_publishes_to_the_global_store() -> None:
    """allocation -> store の配線が実際に通ること (両端だけでなく結線を固定)。

    2026-07-27 の本番ログ
    ``[PORTFOLIO_CAP] trimmed {'long_count': 30, 'total': 1} (held L51/S10)``
    を合成入力から再現し、long 4 system が 0 本になった理由が
    ``allow.long == 0`` として観測できることを固定する。
    """

    from common.stage_metrics import GLOBAL_STAGE_METRICS
    from core.final_allocation import _apply_portfolio_caps

    GLOBAL_STAGE_METRICS.set_portfolio_caps(None)

    final_df = pd.DataFrame(
        [{"symbol": f"L{i}", "side": "long"} for i in range(30)]
        + [{"symbol": f"S{i}", "side": "short"} for i in range(10)]
    )

    class _Pos:
        def __init__(self, symbol: str) -> None:
            self.symbol = symbol
            self.qty = "1"
            self.side = "long"

    held = [_Pos(f"H{i}") for i in range(61)]
    symbol_system_map: dict[str, list[str]] = {f"H{i}": ["system1"] for i in range(51)}
    symbol_system_map.update({f"H{i}": ["system2"] for i in range(51, 61)})

    trimmed_df, report = _apply_portfolio_caps(
        final_df,
        caps={
            "max_total_positions": 70,
            "max_long_positions": 40,
            "max_short_positions": 30,
        },
        active_positions=held,
        symbol_system_map=symbol_system_map,
        long_systems=["system1", "system3", "system4", "system5"],
        short_systems=["system2", "system6", "system7"],
        equity=103684.01,
    )

    # 本番ログと同じ数字であること。
    assert report["held"] == {"long": 51, "short": 10, "total": 61}
    assert report["allow"]["long"] == 0  # 51 > 40 -> long は 1 本も通らない
    assert report["allow"]["total"] == 9  # 70 - 61 = 9 (= 「毎日ちょうど 9 本」の正体)
    assert report["trimmed"] == {"long_count": 30, "total": 1}
    assert len(trimmed_df) == 9
    assert set(trimmed_df["side"]) == {"short"}

    # 配線: allocation が store に publish していること。
    published = GLOBAL_STAGE_METRICS.get_portfolio_caps()
    assert published is not None
    assert published["allow"]["long"] == 0
    assert published["trimmed"]["long_count"] == 30


def test_caps_snapshot_is_detached_from_the_allocation_report() -> None:
    """JSON 化後に report が書き換わっても payload が汚れないこと。"""

    report = {"applied": True, "held": {"long": 51}}
    payload = build_signals_json(
        pd.DataFrame(), {}, date_str="2026-07-27", portfolio_caps=report
    )
    report["held"] = {"long": 0}

    assert payload["portfolio"]["caps"]["held"] == {"long": 51}
