"""C1 (2026-07-07): 配分の現保有ポジション突合の配線を検証する。

docs today_signal_scan/6 (配分フェーズ) の「現保有と突合して空き枠算出」を
_resolve_positions_for_allocation() が env / creds gating 付きで解決すること、
finalize_allocation が渡された positions を available_slots に反映することを確認。
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_allocation import count_active_positions_by_system  # noqa: E402
import scripts.run_all_systems_today as rast  # noqa: E402


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


def test_reconcile_fetch_failure_is_fail_closed(monkeypatch):
    """P1 fix (2026-07-21): fetch 失敗は既定で **fail-closed** (raise)。

    従来は None へ silent フォールバック (fail-open) で、held が available_slots に
    反映されず per-run cap only で新規を積み増していた (audit 🔴P1 root-cause A)。
    """
    import pytest

    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "1")
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    monkeypatch.delenv(
        "ALLOCATION_RECONCILE_FAILCLOSED", raising=False
    )  # 既定=fail-closed

    def _boom():
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(rast, "_fetch_positions_and_symbol_map", _boom)
    with pytest.raises(rast.PositionReconcileError):
        rast._resolve_positions_for_allocation()


def test_reconcile_fetch_failure_failopen_optout(monkeypatch):
    """opt-out (ALLOCATION_RECONCILE_FAILCLOSED=0) で従来の fail-open (None) に戻せる。"""
    monkeypatch.setenv("ALLOCATION_RECONCILE_POSITIONS", "1")
    monkeypatch.setenv("APCA_API_KEY_ID", "k")
    monkeypatch.setenv("APCA_API_SECRET_KEY", "s")
    monkeypatch.setenv("ALLOCATION_RECONCILE_FAILCLOSED", "0")

    def _boom():
        raise RuntimeError("alpaca down")

    monkeypatch.setattr(rast, "_fetch_positions_and_symbol_map", _boom)
    positions, _ = rast._resolve_positions_for_allocation()
    assert positions is None  # opt-out: fetch 失敗は None にフォールバック


def test_held_positions_reduce_available_slots():
    """突合された positions が system 別 active count を減らす (available_slots 算出)。"""
    positions = [_Pos("AAPL", 10), _Pos("MSFT", 5)]
    sym_map = {"AAPL": "system1", "MSFT": "system1"}
    counts = count_active_positions_by_system(positions, sym_map)
    assert (
        counts.get("system1") == 2
    )  # system1 は 2 枠使用中 → available_slots は 10-2=8
