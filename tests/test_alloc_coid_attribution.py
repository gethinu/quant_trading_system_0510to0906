"""スレッド2 回帰テスト — allocation の保有帰属を entry coid で行う。

背景 (2026-07-31): allocation の ``count_active_positions_by_system`` は保有を stale/
不完全な ``symbol_system_map.json`` のみで帰属していた。0731 実データでは 43 保有中
2 件しか帰属できず、available_slots が inflation → 満杯の system1/2/4 を「空きあり」と
誤認して配分し続け、真に空きのある sys3/sys5 が枯れていた。

修正: ``_fetch_positions_and_symbol_map`` が Alpaca 全注文の entry coid
(``system{N}-SYM-YYYYMMDD``) から symbol->system を構築し、static map に coid 優先で
マージ。これで held が正しく帰属し available_slots が正しくなる。

本テストの契約:
    1. ``_build_coid_symbol_system_map`` が coid から symbol->system を全件ページングで拾う。
    2. coid マージ後の map で ``count_active_positions_by_system`` が held を正しく数える。
    3. static map が stale でも coid が優先されて正しく帰属する。
"""

from __future__ import annotations

import types

from core.final_allocation import count_active_positions_by_system

# 修正対象 (prod entry 側)
from scripts.run_all_systems_today import _build_coid_symbol_system_map


def _order(symbol, coid, submitted_at=1):
    return types.SimpleNamespace(
        symbol=symbol, client_order_id=coid, submitted_at=submitted_at
    )


class _FakeClient:
    """get_orders を1ページ (<500) で返す fake。"""

    def __init__(self, orders):
        self._orders = orders

    def get_orders(self, _req):
        # until 付きの2ページ目以降は空 (1ページで終わる)
        if getattr(_req, "until", None) is not None:
            return []
        return self._orders


def _pos(symbol, qty):
    return types.SimpleNamespace(symbol=symbol, qty=qty)


def test_build_coid_map_parses_entry_coids():
    client = _FakeClient([
        _order("MF", "system3-MF-20260713"),
        _order("AAPL", "system1-AAPL-20260728"),
        _order("SPY", "exit-system7-SPY-20260730"),   # exit coid -> 無視
        _order("XYZ", "manual-order"),                  # 非 entry -> 無視
    ])
    m = _build_coid_symbol_system_map(client)
    assert m.get("MF") == "system3"
    assert m.get("AAPL") == "system1"
    assert "SPY" not in m and "XYZ" not in m


def test_coid_attribution_fixes_stale_map_undercount():
    # static map は stale (MF/AAPL を含まない)
    static = {"other": "system2"}
    coid = {"MF": "system3", "AAPL": "system1", "AMZN": "system1"}
    merged = dict(static)
    for k, v in coid.items():
        merged[k] = v
        merged[k.lower()] = v
    positions = [_pos("MF", 10), _pos("AAPL", 5), _pos("AMZN", 3)]
    before = count_active_positions_by_system(positions, static)
    after = count_active_positions_by_system(positions, merged)
    assert before.get("system1", 0) == 0 and before.get("system3", 0) == 0  # stale = 取りこぼし
    assert after.get("system1") == 2 and after.get("system3") == 1          # coid = 正しい


def test_coid_takes_precedence_over_stale_static():
    # static が誤って MF=system9 としていても coid の system3 が勝つ
    static = {"mf": "system9"}
    merged = dict(static)
    merged["MF"] = "system3"; merged["mf"] = "system3"
    after = count_active_positions_by_system([_pos("MF", 10)], merged)
    assert after.get("system3") == 1 and "system9" not in after


if __name__ == "__main__":
    import pytest

    raise SystemExit(pytest.main([__file__, "-q"]))
