"""C1 (2026-07-07): 配分の現保有ポジション突合の配線を検証する。

docs today_signal_scan/6 (配分フェーズ) の「現保有と突合して空き枠算出」を
_resolve_positions_for_allocation() が env / creds gating 付きで解決すること、
finalize_allocation が渡された positions を available_slots に反映することを確認。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.run_all_systems_today as rast  # noqa: E402
from core.final_allocation import count_active_positions_by_system  # noqa: E402


class _Pos:
    def __init__(self, symbol, qty, side="long"):
        self.symbol = symbol
        self.qty = qty
        self.side = side


def test_reconcile_disabled_by_env(monkeypatch):
    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "0")
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    positions, _ = rast._resolve_positions_for_allocation()
    assert positions is None  # env 無効なら突合しない (従来挙動)


def test_reconcile_skipped_without_creds(monkeypatch):
    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "1")
    monkeypatch.delenv("APCA_API_KEY_ID", raising=False)
    monkeypatch.delenv("APCA_API_SECRET_KEY", raising=False)
    positions, _ = rast._resolve_positions_for_allocation()
    assert positions is None  # creds 無しなら Alpaca に触れない


def test_reconcile_fetches_when_enabled(monkeypatch):
    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "1")
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    fake_positions = [_Pos("AAPL", 10)]
    monkeypatch.setattr(
        rast,
        "_fetch_positions_and_symbol_map",
        lambda: (fake_positions, {"AAPL": "system1"}),
    )
    positions, sym_map = rast._resolve_positions_for_allocation()
    assert positions == fake_positions


def test_reconcile_fetch_failure_falls_back_to_none(monkeypatch):
    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "1")
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")

    def _boom():
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(rast, "_fetch_positions_and_symbol_map", _boom)
    positions, _ = rast._resolve_positions_for_allocation()
    assert positions is None  # fetch 失敗は None にフォールバック (fail-open)


def test_held_positions_reduce_available_slots():
    """突合された positions が system 別 active count を減らす (available_slots 算出)。"""
    positions = [_Pos("AAPL", 10), _Pos("MSFT", 5)]
    sym_map = {"AAPL": "system1", "MSFT": "system1"}
    counts = count_active_positions_by_system(positions, sym_map)
    assert counts.get("system1") == 2  # system1 は 2 枠使用中 → available_slots は 10-2=8
