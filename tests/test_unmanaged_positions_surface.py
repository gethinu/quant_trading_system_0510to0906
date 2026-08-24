"""FIX-1 回帰テスト: system タグ未解決ポジの silent-drop を可視化する。

背景 (2026-07-31 診断):
    Alpaca paper に MF/FOLD/CDTX の 3 建玉が system="unknown"/entry_date 未解決で存在し、
    ``build_exit_orders_from_positions`` の ``if not snap.system: continue`` で time も
    protection も生成されないまま黙って落ちていた (旧 debug ログのみ)。同じ未解決ポジは
    overdue カウンタからも外れ、「無管理なのに不可視」だった。

本テストが固定する契約 (1 は 2026-08-19 に更新):
    1. 未解決 (system=None) ポジは **time/close exit を生まない** (既定 max_hold を
       当てない = 捏造しない) が、``unassigned_out`` に必ず載る。
       2026-08-19 追加: 「無管理」と「無保護」は別問題なので、下方保護の
       protective stop だけは既定値で張る (ORPHAN_DEFAULT_PROTECTION、既定 ON)。
       FOLD/CDTX が丸裸で放置されていた穴を塞ぐための変更。
    2. タグ付きポジの exit 生成挙動は不変 (回帰なし)。
    3. ``unassigned_out`` 未指定でも従来どおり動く (後方互換)。
    4. build_e2e_ledger の overdue_unassigned は未解決ポジを別枠で数え、
       rule-based overdue_exits は一切変えない (未解決に既定 max_hold を当てない＝捏造しない)。
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import pytest

from common.alpaca_trading import PositionSnapshot, build_exit_orders_from_positions

ROOT = Path(__file__).resolve().parents[1]


def _mk(symbol, system, *, qty=100.0, side="long"):
    return PositionSnapshot(
        symbol=symbol,
        qty=qty,
        side=side,
        avg_entry_price=10.0,
        market_value=abs(qty) * 11.0,
        unrealized_pl=abs(qty) * 1.0,
        system=system,
        entry_date=None if system is None else "2026-07-28",
    )


def test_unmanaged_position_surfaced_and_only_default_stop():
    unmanaged = _mk("MF", None)
    out: list[dict] = []
    exits = build_exit_orders_from_positions(
        [unmanaged], today="2026-07-30", unassigned_out=out
    )
    # 未解決ポジは time/close exit を生まない (捏造しない)
    assert all(e.reason != "time_based" for e in exits)
    assert all(e.order_type != "market" for e in exits)
    # 下方保護の stop だけは張る (2026-08-19)
    assert [e.reason for e in exits] == ["protect_stop"]
    # ただし必ず surface される
    assert len(out) == 1
    assert out[0]["symbol"] == "MF"
    assert out[0]["side"] == "long"
    assert out[0]["qty"] == 100.0


def test_unmanaged_position_no_exit_when_protection_disabled(monkeypatch):
    """可逆性: 既定保護を切れば従来どおり exit 0 件。"""
    monkeypatch.setenv("ORPHAN_DEFAULT_PROTECTION", "0")
    out: list[dict] = []
    exits = build_exit_orders_from_positions(
        [_mk("MF", None)], today="2026-07-30", unassigned_out=out
    )
    assert exits == []
    assert len(out) == 1


def test_unassigned_out_optional_backward_compat():
    # unassigned_out 未指定でも従来どおり (例外なく list を返す)
    exits = build_exit_orders_from_positions([_mk("FOLD", None)], today="2026-07-30")
    assert isinstance(exits, list)
    # 生成されるのは既定の protective stop だけ (close は作らない)
    assert all(e.reason == "protect_stop" for e in exits)


def test_tagged_position_behavior_unchanged():
    # system タグ付きは従来どおり exit 生成経路に入る (unassigned に落ちない)
    out: list[dict] = []
    build_exit_orders_from_positions(
        [_mk("AAPL", "system1")], today="2026-07-30", unassigned_out=out
    )
    assert out == []  # タグ付きは surface されない


def test_warning_logged_for_unmanaged(caplog):
    import logging

    with caplog.at_level(logging.WARNING):
        build_exit_orders_from_positions([_mk("CDTX", None)], today="2026-07-30")
    assert any("UNMANAGED" in r.message for r in caplog.records)


def _load_ledger():
    spec = importlib.util.spec_from_file_location(
        "build_e2e_ledger", ROOT / "scripts" / "build_e2e_ledger.py"
    )
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


def test_ledger_overdue_unassigned_separate_from_overdue():
    m = _load_ledger()
    snap = {
        "positions": [
            {
                "symbol": "MF",
                "system": "unknown",
                "days_remaining": None,
                "max_holding_days": 0,
                "exit_expected": None,
            },
            {
                "symbol": "FOLD",
                "system": None,
                "days_remaining": None,
                "max_holding_days": 0,
                "exit_expected": None,
            },
            {
                "symbol": "AAPL",
                "system": "system1",
                "days_remaining": None,
                "max_holding_days": 0,
                "exit_expected": None,
            },
        ]
    }
    # 未解決 2 件を別枠で数える
    assert m._unassigned_positions(snap) == 2
    # rule-based overdue は未解決を混ぜない = 0 のまま (捏造しない)
    assert m._overdue_exits(snap) == 0


def test_ledger_counters_handle_empty_snapshot():
    m = _load_ledger()
    assert m._unassigned_positions(None) is None
    assert m._overdue_exits(None) is None


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
