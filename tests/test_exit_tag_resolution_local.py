"""A 局所修正 回帰テスト — exit 経路のタグ解決に symbol_map を追加。

背景 (2026-07-31): MF は実在の system3 ポジ (entry 2026-07-13) だが、exit 経路
``hydrate_system_tags`` が entry_orders_index + tracker しか見ず、tracker 不在 +
paper_orders 未登録 + Alpaca coid の limit=500 truncation で system を解決できず、
time-exit が発火しないまま 17 日超過していた。

本テストの契約:
    1. tracker/entry_index が空でも ``symbol_map`` から system を解決できる。
    2. symbol_map で system3 を得た overdue ポジ (17d > max 3d) は time_based exit を生む。
    3. どのソースにも無い symbol は従来どおり system=None のまま (捏造しない)。
"""

from __future__ import annotations

from common.alpaca_trading import (
    PositionSnapshot,
    build_exit_orders_from_positions,
    hydrate_system_tags,
)


def _snap(symbol, *, system=None, entry_date=None, qty=100.0, side="long"):
    return PositionSnapshot(
        symbol=symbol, qty=qty, side=side, avg_entry_price=10.0,
        market_value=abs(qty) * 11.0, unrealized_pl=abs(qty), system=system,
        entry_date=entry_date,
    )


def test_symbol_map_resolves_when_tracker_and_index_empty():
    snap = _snap("MF")
    hydrate_system_tags([snap], tracker={}, entry_orders_index={}, symbol_map={"MF": "system3"})
    assert snap.system == "system3"


def test_symbol_map_does_not_override_existing_system():
    snap = _snap("MF", system="system2")
    hydrate_system_tags([snap], symbol_map={"MF": "system3"})
    assert snap.system == "system2"  # 既存タグを上書きしない


def test_overdue_mf_gets_time_exit_via_symbol_map():
    # system3 max_holding_days=3。entry 2026-07-13, today 2026-07-30 = 17d 超過。
    snap = _snap("MF", entry_date="2026-07-13")
    exits = build_exit_orders_from_positions(
        [snap], today="2026-07-30", symbol_map={"MF": "system3"}
    )
    assert any(str(e.reason) == "time_based" and e.side == "sell" for e in exits)


def test_unknown_symbol_still_unmanaged_and_surfaced():
    out: list[dict] = []
    exits = build_exit_orders_from_positions(
        [_snap("ZZZ")], today="2026-07-30", symbol_map={"MF": "system3"}, unassigned_out=out
    )
    # 2026-08-19: 帰属不能でも下方保護の stop だけは張る (time/close は作らない)。
    assert all(e.reason == "protect_stop" for e in exits)
    assert out and out[0]["symbol"] == "ZZZ"


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
