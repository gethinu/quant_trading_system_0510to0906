"""決済 (round-trip) の **system 帰属** の regression test。

なぜ独立した module か
----------------------
dashboard の「system 別（実現のみ）」が 652 本中 264 本を ``unknown`` に落として
いた。原因は帰属ロジックが

  1. ``data/symbol_system_map.json`` を ``isinstance(v, str)`` で読んでいた
     (実際の保存形式は ``{"AAA": ["system1"]}`` の **list**) → map が丸ごと空
  2. entry 注文の ``client_order_id`` を一切見ず symbol 単位でしか引かなかった

の 2 点。ここではその両方と、「根拠が無いものを推測で埋めない」契約を固定する。

守りたい契約
------------
- trade 単位の確定根拠 (entry 注文の client_order_id) が symbol 単位の推定に勝つ
- symbol_system_map の list 形式を落とさない
- 帰属できないものは ``system=None`` のまま + **なぜ** 不明かを型で残す
- 帰属は集計の配り直しであって、実現損益・件数を動かさない
- exit 理由 (「記録なし」) と system 帰属 (「unknown」) は別軸で二重計上しない
- ticker rename は「候補」提示まで。断定して架空の round-trip を作らない
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.exit_ledger import (  # noqa: E402
    SYSTEM_SOURCE_ENTRY_ORDER,
    SYSTEM_SOURCE_ORDER_FILE,
    SYSTEM_SOURCE_SYMBOL_MAP,
    UNKNOWN_ENTRY_ORDER_NOT_FOUND,
    UNKNOWN_ENTRY_ORDER_UNTAGGED,
    UNKNOWN_NO_ENTRY_ORDER_ID,
    attribute_systems,
    pair_rename_candidates,
    parse_fills,
    reconcile_with_broker,
    reconstruct_round_trips,
    summarize_attribution,
    summarize_by_exit_reason,
    summarize_by_system,
    summarize_realized,
)
from common.symbol_map import load_symbol_system_map  # noqa: E402


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


def round_trip(symbol="AAA", entry_oid="entry-1"):
    """entry / exit 1 往復から ClosedTrade 1 本を作る。"""
    fills = parse_fills(
        [
            fill(symbol, "buy", 10, 100, "2026-01-05T14:30:00Z", order_id=entry_oid),
            fill(symbol, "sell", 10, 110, "2026-07-06T14:30:00Z", order_id="exit-1"),
        ]
    )
    trades = reconstruct_round_trips(fills).closed_trades
    assert len(trades) == 1
    return trades[0]


# ---------------------------------------------------------------------------
# entry 注文を round-trip まで運ぶ
# ---------------------------------------------------------------------------


def test_entry_order_id_is_carried_from_the_opening_fill():
    """trade 単位の帰属には entry 側の order_id が要る (exit だけでは足りない)。"""
    t = round_trip(entry_oid="entry-42")
    assert t.entry_order_id == "entry-42"
    assert t.exit_order_id == "exit-1"
    assert t.to_row()["entry_order_id"] == "entry-42"


def test_partial_exits_all_point_back_to_the_same_entry_order():
    """分割決済でもどの片割れも同じ entry に紐づく (帰属がばらけない)。"""
    fills = parse_fills(
        [
            fill("AAA", "buy", 10, 100, "2026-01-05T14:30:00Z", order_id="e1"),
            fill("AAA", "sell", 4, 110, "2026-07-06T14:30:00Z", order_id="x1"),
            fill("AAA", "sell", 6, 120, "2026-07-07T14:30:00Z", order_id="x2"),
        ]
    )
    trades = reconstruct_round_trips(fills).closed_trades
    assert len(trades) == 2
    assert {t.entry_order_id for t in trades} == {"e1"}

    attribute_systems(
        trades, system_by_order_id={"e1": "system4"}, known_order_ids=["e1"]
    )
    assert [t.system for t in trades] == ["system4", "system4"]


# ---------------------------------------------------------------------------
# 優先順位 — 確定根拠が推定に勝つ
# ---------------------------------------------------------------------------


def test_entry_client_order_id_wins_over_symbol_level_guesses():
    t = round_trip(entry_oid="o-sys3")
    attribute_systems(
        [t],
        system_by_order_id={"o-sys3": "system3"},
        known_order_ids=["o-sys3"],
        # symbol 単位の記録は別 system を主張しているが採用してはいけない
        order_file_system_map={"AAA": "system1"},
        symbol_system_map={"AAA": "system2"},
    )
    assert t.system == "system3"
    assert t.system_source == SYSTEM_SOURCE_ENTRY_ORDER
    assert t.system_unknown_reason is None


def test_order_file_record_beats_symbol_map():
    t = round_trip(entry_oid="untagged")
    attribute_systems(
        [t],
        known_order_ids=["untagged"],
        order_file_system_map={"AAA": "system2"},
        symbol_system_map={"AAA": "system5"},
    )
    assert t.system == "system2"
    assert t.system_source == SYSTEM_SOURCE_ORDER_FILE


def test_symbol_map_is_used_only_as_the_last_resort():
    t = round_trip(entry_oid="untagged")
    attribute_systems(
        [t], known_order_ids=["untagged"], symbol_system_map={"AAA": "system5"}
    )
    assert t.system == "system5"
    assert t.system_source == SYSTEM_SOURCE_SYMBOL_MAP


# ---------------------------------------------------------------------------
# symbol_system_map の形式吸収 (unknown 264 の直接原因)
# ---------------------------------------------------------------------------


def test_symbol_system_map_list_form_is_absorbed_not_dropped(tmp_path):
    """``{"AAA": ["system4"]}`` の list 形式を落とさない。

    ここが落ちていたせいで map (84 銘柄) が丸ごと無視され、古い決済が全部
    「system 不明」になっていた。形式吸収の正本は
    :func:`common.symbol_map.load_symbol_system_map`。
    """
    path = tmp_path / "symbol_system_map.json"
    path.write_text(
        json.dumps({"AAA": ["system4"], "BBB": "system5", "CCC": []}),
        encoding="utf-8",
    )
    loaded = {k.upper(): v for k, v in load_symbol_system_map(path).items()}
    assert loaded["AAA"] == "system4"
    assert loaded["BBB"] == "system5"
    assert "CCC" not in loaded  # 空 list から system をでっち上げない

    t = round_trip(entry_oid="untagged")
    attribute_systems([t], known_order_ids=["untagged"], symbol_system_map=loaded)
    assert t.system == "system4"
    assert t.system_source == SYSTEM_SOURCE_SYMBOL_MAP


def test_multi_system_symbol_takes_the_primary_entry(tmp_path):
    """複数 system を持つ銘柄は先頭 (primary) を採る。黙って落とさない。"""
    path = tmp_path / "symbol_system_map.json"
    path.write_text(json.dumps({"AAA": ["system3", "system5"]}), encoding="utf-8")
    loaded = {k.upper(): v for k, v in load_symbol_system_map(path).items()}
    assert loaded["AAA"] == "system3"


# ---------------------------------------------------------------------------
# 推測で埋めない / なぜ不明かを型で残す
# ---------------------------------------------------------------------------


def test_unattributable_trade_stays_unknown_with_a_typed_reason():
    t = round_trip(entry_oid="untagged")
    attribute_systems([t], known_order_ids=["untagged"])
    assert t.system is None
    assert t.system_source is None
    assert t.system_unknown_reason == UNKNOWN_ENTRY_ORDER_UNTAGGED
    assert t.to_row()["system"] is None
    assert t.to_row()["system_unknown_reason"] == UNKNOWN_ENTRY_ORDER_UNTAGGED


def test_missing_entry_order_is_distinguished_from_an_untagged_one():
    """「注文履歴に無い」と「履歴にはあるが tag が無い」は別の unknown。"""
    seen = round_trip("AAA", entry_oid="known-but-untagged")
    unseen = round_trip("BBB", entry_oid="never-seen")
    attribute_systems([seen, unseen], known_order_ids=["known-but-untagged"])
    assert seen.system_unknown_reason == UNKNOWN_ENTRY_ORDER_UNTAGGED
    assert unseen.system_unknown_reason == UNKNOWN_ENTRY_ORDER_NOT_FOUND


def test_fill_without_an_order_id_gets_its_own_unknown_reason():
    fills = parse_fills(
        [
            fill("AAA", "buy", 10, 100, "2026-01-05T14:30:00Z", order_id=""),
            fill("AAA", "sell", 10, 110, "2026-07-06T14:30:00Z", order_id="x"),
        ]
    )
    trades = reconstruct_round_trips(fills).closed_trades
    attribute_systems(trades, known_order_ids=["x"])
    assert trades[0].system_unknown_reason == UNKNOWN_NO_ENTRY_ORDER_ID


def test_attribution_is_idempotent_and_does_not_keep_a_stale_source():
    """2 度目で根拠が消えたら unknown に戻る (前回の帰属が残留しない)。"""
    t = round_trip(entry_oid="o1")
    attribute_systems([t], system_by_order_id={"o1": "system1"}, known_order_ids=["o1"])
    assert t.system == "system1"
    attribute_systems([t], known_order_ids=["o1"])
    assert t.system is None
    assert t.system_source is None
    assert t.system_unknown_reason == UNKNOWN_ENTRY_ORDER_UNTAGGED


# ---------------------------------------------------------------------------
# 内訳の整合 — 取りこぼしも二重計上もしない
# ---------------------------------------------------------------------------


def test_attribution_summary_accounts_for_every_trade_exactly_once():
    a = round_trip("AAA", entry_oid="o-tag")
    b = round_trip("BBB", entry_oid="o-untagged")
    c = round_trip("CCC", entry_oid="o-map")
    attribute_systems(
        [a, b, c],
        system_by_order_id={"o-tag": "system1"},
        known_order_ids=["o-tag", "o-untagged", "o-map"],
        symbol_system_map={"CCC": "system6"},
    )
    summary = summarize_attribution([a, b, c])
    assert summary["n_trades"] == 3
    assert summary["n_attributed"] + summary["n_unknown"] == 3
    assert summary["n_ground_truth"] == 1
    assert sum(r["n_trades"] for r in summary["by_source"]) == summary["n_attributed"]
    assert (
        sum(r["n_trades"] for r in summary["unknown_by_reason"]) == summary["n_unknown"]
    )
    # 「system 別」表の unknown バケットと帰属内訳の unknown は同じものを指す
    assert summarize_by_system([a, b, c])["unknown"]["n_trades"] == summary["n_unknown"]


def test_attribution_summary_reports_the_money_behind_unknown():
    """件数だけだと unknown が軽く見える。実現損益も一緒に出す。"""
    t = round_trip(entry_oid="untagged")  # +10 * 10 株 = +100
    attribute_systems([t], known_order_ids=["untagged"])
    row = summarize_attribution([t])["unknown_by_reason"][0]
    assert row["n_trades"] == 1
    assert row["realized_pl"] == 100.0
    assert row["symbols"] == ["AAA"]
    assert row["label"]  # 人間可読な理由が必ず付く


def test_attribution_never_changes_realized_totals():
    """帰属は集計の *配り直し*。損益や件数を動かしてはいけない。"""
    trades = [round_trip("AAA", entry_oid="o1"), round_trip("BBB", entry_oid="o2")]
    before = summarize_realized(trades)
    attribute_systems(
        trades, system_by_order_id={"o1": "system1"}, known_order_ids=["o1", "o2"]
    )
    after = summarize_realized(trades)
    assert before == after
    assert sum(s["n_trades"] for s in summarize_by_system(trades).values()) == len(
        trades
    )


# ---------------------------------------------------------------------------
# exit 理由と system 帰属は別軸 (「記録なし」と「unknown」を混ぜない)
# ---------------------------------------------------------------------------


def test_exit_reason_totals_are_independent_of_system_attribution():
    a = round_trip("AAA", entry_oid="o1")
    b = round_trip("BBB", entry_oid="o2")
    a.exit_reason = "time_based"
    # b は exit 理由の記録なし かつ system も unknown -- 別々の軸で 1 回ずつ数える
    attribute_systems(
        [a, b], system_by_order_id={"o1": "system1"}, known_order_ids=["o1", "o2"]
    )
    reasons = summarize_by_exit_reason([a, b])
    assert sum(r["n_trades"] for r in reasons) == 2
    assert {r["reason"]: r["n_trades"] for r in reasons} == {"time_based": 1, None: 1}
    by_system = summarize_by_system([a, b])
    assert by_system["system1"]["n_trades"] == 1
    assert by_system["unknown"]["n_trades"] == 1


def test_exit_reason_totals_cover_every_trade():
    trades = [round_trip("AAA", entry_oid="o1"), round_trip("BBB", entry_oid="o2")]
    trades[0].exit_reason = "protect_stop"
    trades[1].exit_reason = "protect_stop"
    reasons = summarize_by_exit_reason(trades)
    assert reasons == [{"reason": "protect_stop", "n_trades": 2, "realized_pl": 200.0}]


# ---------------------------------------------------------------------------
# ticker rename は「候補」まで — 断定して損益を合成しない
# ---------------------------------------------------------------------------


def test_rename_candidates_pair_the_offsetting_residuals():
    """旧 symbol に居座る建玉と、新 symbol 側の不足を対にして提示する。"""
    result = reconstruct_round_trips(
        parse_fills(
            [
                fill("OLD", "buy", 100, 5.0, "2026-01-05T14:30:00Z"),
                fill("KEEP", "buy", 5, 10.0, "2026-01-05T14:31:00Z"),
            ]
        )
    )
    reconcile_with_broker(result, {"NEW": 100, "KEEP": 5})
    candidates = pair_rename_candidates(result.discrepancies)
    assert [(c["from_symbol"], c["to_symbol"], c["qty"]) for c in candidates] == [
        ("OLD", "NEW", 100.0)
    ]
    assert candidates[0]["confirmed"] is False
    # 候補にしただけで round-trip は 1 本も増えない (架空の実現損益を作らない)
    assert result.closed_trades == []


def test_rename_candidates_pair_two_stranded_books():
    """旧 symbol に買い残、新 symbol に売り残 (どちらも broker 0) の形も対にする。"""
    result = reconstruct_round_trips(
        parse_fills(
            [
                fill("OLD", "buy", 23, 66.0, "2025-11-03T14:30:00Z"),
                fill("NEW", "sell", 23, 68.6, "2025-12-12T14:30:00Z"),
            ]
        )
    )
    reconcile_with_broker(result, {})
    candidates = pair_rename_candidates(result.discrepancies)
    assert [(c["from_symbol"], c["to_symbol"]) for c in candidates] == [("OLD", "NEW")]
    assert result.closed_trades == []  # 別 symbol なので決済は組まれないまま


def test_rename_candidates_refuse_to_guess_when_the_match_is_ambiguous():
    """同じ株数の相手が複数居るなら組まない (当て推量を避ける)。"""
    result = reconstruct_round_trips(
        parse_fills([fill("OLD", "buy", 100, 5.0, "2026-01-05T14:30:00Z")])
    )
    reconcile_with_broker(result, {"NEW1": 100, "NEW2": 100})
    assert pair_rename_candidates(result.discrepancies) == []


def test_rename_candidates_are_empty_when_everything_reconciles():
    result = reconstruct_round_trips(
        parse_fills([fill("AAA", "buy", 10, 1.0, "2026-01-05T14:30:00Z")])
    )
    reconcile_with_broker(result, {"AAA": 10})
    assert pair_rename_candidates(result.discrepancies) == []
