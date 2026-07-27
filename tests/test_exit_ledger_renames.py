"""ticker rename マップ経由の round-trip 合成の regression test。

背景
----
ticker rename (旧 symbol で建て、新 symbol で決済) が起きると FIFO の帳簿が
symbol ごとに分断され、**決済済みなのに台帳に載らない** round-trip ができる。
実測で 3 対 / 11 本 / 実現 +$465.05 が抜けていた。

``config/ticker_renames.json`` の手動マップでこれを繋ぐが、手で書いた対を
無条件に信じると架空の決済と実現損益を作れてしまう。そこで

  1. まず rename 無しで建玉を再構成し、残差が **一意に打ち消し合う** 対を
     機械的に洗い出す (:func:`pair_rename_candidates`)
  2. config の対のうち、その裏づけがあるものだけ採用する
     (:func:`select_applicable_renames`)

という二段構えにしてある。ここではその契約を固定する。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.exit_ledger import (  # noqa: E402
    canonical_symbol,
    normalize_rename_map,
    pair_rename_candidates,
    parse_fills,
    reconcile_with_broker,
    reconstruct_round_trips,
    select_applicable_renames,
    summarize_realized,
)
from scripts import build_exit_ledger as bl  # noqa: E402


def fill(symbol, side, qty, price, ts, order_id="o1"):
    return {
        "symbol": symbol,
        "side": side,
        "qty": str(qty),
        "price": str(price),
        "transaction_time": ts,
        "order_id": order_id,
        "id": f"{symbol}-{ts}-{side}",
    }


# 旧 OLD で 100 株買い、rename 後の NEW で売った = 決済済みなのに分断された対。
SPLIT_ROUND_TRIP = [
    fill("OLD", "buy", 100, 5.0, "2026-01-05T14:30:00Z", order_id="e1"),
    fill("NEW", "sell", 100, 8.0, "2026-07-06T14:30:00Z", order_id="x1"),
]


def _candidates(fills_raw, broker):
    result = reconstruct_round_trips(parse_fills(fills_raw))
    reconcile_with_broker(result, broker)
    return result, pair_rename_candidates(result.discrepancies)


# ---------------------------------------------------------------------------
# 合成される / 合成されない
# ---------------------------------------------------------------------------


def test_without_the_map_the_round_trip_is_lost():
    """マップが無いと決済が 1 本も立たない (これが台帳から抜けていた状態)。"""
    result, _ = _candidates(SPLIT_ROUND_TRIP, {})
    assert result.closed_trades == []
    assert result.complete is False


def test_rename_map_synthesizes_the_missing_round_trip():
    fills = parse_fills(SPLIT_ROUND_TRIP)
    result = reconstruct_round_trips(fills, symbol_aliases={"NEW": "OLD"})
    reconcile_with_broker(result, {}, symbol_aliases={"NEW": "OLD"})

    assert len(result.closed_trades) == 1
    t = result.closed_trades[0]
    assert t.symbol == "OLD"  # canonical に寄る
    assert t.symbol_aliases == ["NEW"]  # 元の symbol も残す
    assert t.qty == 100
    assert float(t.realized_pl) == 300.0  # (8.0 - 5.0) * 100
    assert t.entry_order_id == "e1"
    assert t.exit_order_id == "x1"
    # 統合したことで建玉の食い違いも解消する
    assert result.discrepancies == []
    assert result.complete is True


def test_synthesized_trade_appears_in_the_realized_summary():
    fills = parse_fills(SPLIT_ROUND_TRIP)
    result = reconstruct_round_trips(fills, symbol_aliases={"NEW": "OLD"})
    summary = summarize_realized(result.closed_trades)
    assert summary["n_trades"] == 1
    assert summary["total_realized_pl"] == 300.0


def test_open_position_under_the_new_ticker_reconciles_after_aliasing():
    """未決済のまま rename された建玉 (UBXG -> MF 型) も突合が通る。"""
    fills = parse_fills(
        [fill("OLD", "buy", 100, 5.0, "2026-07-13T14:30:00Z", order_id="e1")]
    )
    aliases = {"NEW": "OLD"}
    result = reconstruct_round_trips(fills, symbol_aliases=aliases)
    reconcile_with_broker(result, {"NEW": 100}, symbol_aliases=aliases)
    assert result.discrepancies == []
    assert result.closed_trades == []  # 未決済なので損益は確定させない


def test_broker_positions_on_both_tickers_are_summed_not_dropped():
    fills = parse_fills(
        [fill("OLD", "buy", 100, 5.0, "2026-07-13T14:30:00Z", order_id="e1")]
    )
    aliases = {"NEW": "OLD"}
    result = reconstruct_round_trips(fills, symbol_aliases=aliases)
    reconcile_with_broker(result, {"NEW": 60, "OLD": 40}, symbol_aliases=aliases)
    assert result.discrepancies == []


# ---------------------------------------------------------------------------
# 一意に決まらない対は組まない (config で捏造できない)
# ---------------------------------------------------------------------------


def test_ambiguous_pair_is_rejected_even_when_it_is_in_the_config():
    """同じ株数の相手が 2 つあるなら、config に書いてあっても採用しない。"""
    _, candidates = _candidates(
        [fill("OLD", "buy", 100, 5.0, "2026-01-05T14:30:00Z")],
        {"NEW1": 100, "NEW2": 100},
    )
    assert candidates == []  # そもそも候補が立たない

    applied, rejected = select_applicable_renames(
        [{"alias": "NEW1", "canonical": "OLD"}], candidates
    )
    assert applied == []
    assert len(rejected) == 1
    assert rejected[0]["rejected_reason"].startswith("unique_offset_not_found")


def test_unsupported_pair_in_the_config_is_rejected_with_a_reason():
    """裏づけの無い対 (株数が合わない) は理由つきで拒否される。"""
    _, candidates = _candidates(SPLIT_ROUND_TRIP, {})
    applied, rejected = select_applicable_renames(
        [
            {"alias": "NEW", "canonical": "OLD"},  # 裏づけあり
            {"alias": "BOGUS", "canonical": "FAKE"},  # 裏づけ無し
        ],
        candidates,
    )
    assert [(a["alias"], a["canonical"]) for a in applied] == [("NEW", "OLD")]
    assert [(r["alias"], r["canonical"]) for r in rejected] == [("BOGUS", "FAKE")]
    assert "unique_offset_not_found" in rejected[0]["rejected_reason"]


def test_applied_pair_carries_the_observed_quantity_as_corroboration():
    _, candidates = _candidates(SPLIT_ROUND_TRIP, {})
    applied, _ = select_applicable_renames(
        [{"alias": "NEW", "canonical": "OLD"}], candidates
    )
    assert applied[0]["observed_qty"] == 100.0
    assert applied[0]["corroboration"]


def test_pair_direction_does_not_matter_for_corroboration():
    """config が逆向きに書かれていても、対として一致すれば採用する。"""
    _, candidates = _candidates(SPLIT_ROUND_TRIP, {})
    applied, rejected = select_applicable_renames(
        [{"alias": "OLD", "canonical": "NEW"}], candidates
    )
    assert len(applied) == 1
    assert rejected == []


def test_self_reference_and_incomplete_rows_are_rejected():
    _, candidates = _candidates(SPLIT_ROUND_TRIP, {})
    applied, rejected = select_applicable_renames(
        [
            {"alias": "OLD", "canonical": "OLD"},
            {"alias": "", "canonical": "OLD"},
            {"alias": "NEW", "canonical": ""},
        ],
        candidates,
    )
    assert applied == []
    reasons = [r["rejected_reason"].split(":")[0] for r in rejected]
    assert reasons == ["self_reference", "incomplete", "incomplete"]


# ---------------------------------------------------------------------------
# マップの正規化
# ---------------------------------------------------------------------------


def test_normalize_rename_map_uppercases_and_drops_self_reference():
    assert normalize_rename_map(
        [
            {"alias": "new", "canonical": "old"},
            {"alias": "SAME", "canonical": "SAME"},
            {"alias": "", "canonical": "X"},
        ]
    ) == {"NEW": "OLD"}


def test_normalize_rename_map_drops_chains():
    """A->B->C の連鎖は解決順で結果が変わるので採用しない。"""
    assert normalize_rename_map(
        [
            {"alias": "A", "canonical": "B"},
            {"alias": "B", "canonical": "C"},
        ]
    ) == {"B": "C"}


def test_canonical_symbol_applies_one_hop_only():
    aliases = {"NEW": "OLD"}
    assert canonical_symbol("new", aliases) == "OLD"
    assert canonical_symbol("OTHER", aliases) == "OTHER"
    assert canonical_symbol("NEW", None) == "NEW"


# ---------------------------------------------------------------------------
# config 読み込み
# ---------------------------------------------------------------------------


def test_shipped_rename_config_is_wellformed_and_marked_unconfirmed():
    """出荷している設定が読める形で、かつ「broker の裏づけ無し」と明記されている。"""
    rows = bl.load_rename_definitions()
    assert rows, "config/ticker_renames.json が読めていない"
    for row in rows:
        assert row["alias"] and row["canonical"]
        assert row["alias"] != row["canonical"]
        assert row["evidence"], f"{row['alias']} に根拠が書かれていない"
        assert row["confirmed_by_broker"] is False


def test_missing_rename_config_degrades_to_no_merging(tmp_path):
    assert bl.load_rename_definitions(tmp_path / "nope.json") == []


def test_broken_rename_config_degrades_instead_of_raising(tmp_path):
    p = tmp_path / "ticker_renames.json"
    p.write_text("{not json", encoding="utf-8")
    assert bl.load_rename_definitions(p) == []


def test_rename_config_without_a_renames_list_is_empty(tmp_path):
    p = tmp_path / "ticker_renames.json"
    p.write_text(json.dumps({"schema": "ticker_renames/v1"}), encoding="utf-8")
    assert bl.load_rename_definitions(p) == []
