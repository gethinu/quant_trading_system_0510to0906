"""durable 修正 回帰テスト — entry 成功時のタグ永続化 round-trip。

背景 (2026-07-31): prod entry の paper_trading_submit.py が position_tracker /
symbol_system_map / position_entry_dates を一切更新しなかったため、タグが揮発し
MF (system3) が exit 経路で解決できず 17 日超過放置になった。durable 修正は entry
成功後に**記録のみ**で3台帳を更新する。

本テストの契約 (書式・パス一致の担保):
    1. ``_persist_entry_tags`` が3台帳を書く。
    2. 書いた内容を ``hydrate_system_tags`` (exit 経路の resolver) がそのまま読めて
       system / entry_date を復元できる = 読み書き不整合を作らない。
    3. system が coid にも無い注文は台帳に捏造しない (skip)。
    4. 発注はしない (submit 済み結果の記録のみ)。
"""

from __future__ import annotations

import types

import pytest

import common.position_tracker as pt
from common.alpaca_trading import PositionSnapshot, hydrate_system_tags
from common.position_tracker import load_tracker
from common.symbol_map import load_symbol_system_map
from common.position_age import load_entry_dates

# import target from the prod entry submitter
from scripts.paper_trading_submit import _persist_entry_tags


def _order(symbol, system=None, entry_date=None, coid=None, limit_price=10.5):
    return types.SimpleNamespace(
        symbol=symbol, system=system, entry_date=entry_date,
        client_order_id=coid, limit_price=limit_price,
    )


@pytest.fixture()
def _isolated_stores(tmp_path, monkeypatch):
    # tracker: module default path -> tmp; map/entry_dates: cwd-relative -> chdir tmp
    (tmp_path / "data").mkdir()
    monkeypatch.setattr(pt, "DEFAULT_TRACKER_PATH", tmp_path / "data" / "position_tracker.json")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def test_persist_roundtrip_hydrate_resolves(_isolated_stores):
    # 成功 entry: MF system3, entry 2026-07-13 (coid からも導出できる形)
    orders = [_order("MF", system="system3", entry_date="2026-07-13",
                     coid="system3-MF-20260713")]
    _persist_entry_tags(orders, date_str="2026-07-30")

    tr = load_tracker()
    smap = load_symbol_system_map()
    ed = load_entry_dates()
    assert tr.get("MF", {}).get("system") == "system3"
    assert tr.get("MF", {}).get("entry_date", "")[:10] == "2026-07-13"
    assert smap.get("mf") == "system3"       # loader は小文字キー
    assert ed.get("MF") == "2026-07-13"

    # exit 経路の resolver が persist 済みストアだけで解決できる (coid/Alpaca 不要)
    snap = PositionSnapshot(symbol="MF", qty=100.0, side="long", avg_entry_price=10.5,
                            market_value=1100.0, unrealized_pl=50.0, system=None, entry_date=None)
    hydrate_system_tags([snap], tracker=load_tracker(), entry_orders_index={},
                        symbol_map=load_symbol_system_map())
    assert snap.system == "system3"
    assert snap.entry_date == "2026-07-13"


def test_persist_derives_from_coid_when_fields_missing(_isolated_stores):
    # po.system/entry_date が無くても coid から導出して記録する
    orders = [_order("ABC", system=None, entry_date=None, coid="system2-ABC-20260720")]
    _persist_entry_tags(orders, date_str="2026-07-30")
    assert load_symbol_system_map().get("abc") == "system2"
    assert load_entry_dates().get("ABC") == "2026-07-20"


def test_persist_skips_when_no_system_origin(_isolated_stores):
    # system が po にも coid にも無い = 捏造しない (skip)
    orders = [_order("ZZZ", system=None, entry_date=None, coid="manual-order-xyz")]
    _persist_entry_tags(orders, date_str="2026-07-30")
    assert "zzz" not in load_symbol_system_map()
    assert "ZZZ" not in load_entry_dates()


def test_persist_empty_is_noop(_isolated_stores):
    _persist_entry_tags([], date_str="2026-07-30")  # 例外なく何もしない
    assert load_symbol_system_map() == {}


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
