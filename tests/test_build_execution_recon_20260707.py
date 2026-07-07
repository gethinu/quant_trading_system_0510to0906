"""build_execution_recon.build_recon の突合ロジック検証 (2026-07-07)。

signals → 生成 → entry 送信 → fill → exit を system×side で join し、
drop 内訳を集計することを確認する。入力欠損にも寛容であること。
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.build_execution_recon import build_recon  # noqa: E402


def _signals() -> dict:
    return {
        "date": "2026-07-08",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "AAPL", "side": "BUY"},
                    {"symbol": "MSFT", "side": "BUY"},
                ],
                "funnel": {
                    "target": 4000,
                    "filter_pass": 100,
                    "setup_pass": 20,
                    "candidate_count": 12,
                    "entry_count": 2,
                    "exit_count": 0,
                },
            },
            "sys2": {
                "signals": [{"symbol": "TSLA", "side": "SELL"}],
            },
        },
        "portfolio": {"total_signals": 3, "universe_target": 4000},
    }


def _paper_orders() -> dict:
    return {
        "orders": [
            # sys1 long: 1 submitted (filled), 1 skipped(min_notional)
            {"system": "system1", "side": "buy", "order_id": "o1", "status": "filled"},
            {"system": "system1", "side": "buy", "skip_reason": "skip:below_min_notional:$3<$5"},
            # sys2 short: 1 failed
            {"system": "system2", "side": "sell", "error": "insufficient buying power"},
        ]
    }


def _exit_orders() -> dict:
    return {
        "exits": [
            {"system": "system1", "side": "sell", "reason": "time_based", "order_id": "e1"},
            {"system": "system1", "side": "sell", "reason": "protect_stop", "order_id": "e2"},
            {"system": "system1", "side": "sell", "reason": "protect_trailing", "order_id": "e3"},
        ]
    }


def test_full_join():
    recon = build_recon(_signals(), _paper_orders(), _exit_orders(), account_equity=10120.0)
    p = recon["portfolio"]
    assert p["universe_target"] == 4000
    assert p["signals"] == 3
    assert p["long_signals"] == 2
    assert p["short_signals"] == 1
    assert p["orders_generated"] == 3
    assert p["entry_submitted"] == 1
    assert p["entry_filled"] == 1
    assert p["entry_skipped"] == 1
    assert p["entry_failed"] == 1
    assert p["exit_submitted"] == 3
    assert p["exit_close"] == 1
    assert p["exit_protect"] == 2
    assert p["account_equity"] == 10120.0
    # drop 内訳: min_notional 1, fail 1
    assert recon["portfolio"]["drop_breakdown"]["below_min_notional"] == 1
    assert recon["portfolio"]["drop_breakdown"]["fail"] == 1


def test_per_system_side_split():
    recon = build_recon(_signals(), _paper_orders(), _exit_orders())
    sys1 = recon["systems"]["system1"]
    assert sys1["long"]["signals"] == 2
    assert sys1["long"]["entry_submitted"] == 1
    assert sys1["long"]["skipped"] == 1
    assert sys1["funnel"]["setup_pass"] == 20
    assert sys1["exit"]["submitted"] == 3
    assert sys1["exit"]["protect"] == 2
    sys2 = recon["systems"]["system2"]
    assert sys2["short"]["failed"] == 1


def test_inputs_flags_and_missing_tolerance():
    recon = build_recon(_signals(), None, None)
    assert recon["inputs"] == {"signals": True, "paper_orders": False, "exit_orders": False}
    assert recon["portfolio"]["signals"] == 3
    assert recon["portfolio"]["entry_submitted"] == 0
    assert recon["portfolio"]["exit_submitted"] == 0


def test_empty_systems_pruned():
    recon = build_recon(_signals(), None, None)
    # sys3-7 は全 0 なので出力から落ちる
    assert set(recon["systems"].keys()) == {"system1", "system2"}
