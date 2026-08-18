"""保護エンジン硬化 (2026-08-19) の回帰テスト。

2026-08-18 の paper run (exits 23 件) で観測した 5 つの穴を固定する:

    #1 orphan (system 帰属不能) が time も protection も持たず **完全に無保護**
       だった (FOLD/CDTX ≈ $4,286)。→ 既定の protective stop を張る。
    #2 ``entry - mult*ATR`` が 0 以下のとき long stop が ``max(0.01, ...)`` で
       $0.01 = 実質無保護に silent 化していた。→ % フロア + WARN。
    #3 Alpaca は 1 注文が qty を全量予約するため stop と target を同時常駐
       できず、後発が必ず code 40310000 で拒否されていた (当日 12 件)。
       → 優先度で 1 本だけ発注 (残りは skip_reason)、OCO は flag で選択可。
    #4 recon が「submitted でない」全件を armed に計上し、**ブローカー拒否が
       armed (=保護できた) として表示** されていた。→ rejected/suppressed を分離。
    #5 端株は native stop/limit を張れず日次 synthetic 判定に振替になるのが
       silent だった。→ WARN + coverage を artifact に残す。

flag 既定:
    PROTECT_STOP_FLOOR_ENABLED  既定 ON  (=0 で旧 $0.01 クランプへ)
    ORPHAN_DEFAULT_PROTECTION   既定 ON  (=0 で orphan 無保護へ)
    PROTECT_USE_OCO             既定 OFF (=1 で stop+target を OCO 1 本に)
"""

from __future__ import annotations

import logging

import pytest

import common.alpaca_trading as at
from common.alpaca_trading import (
    ExitReasonCode,
    PositionSnapshot,
    build_exit_orders_from_positions,
    submit_paper_exit_order,
)
from common.trade_management import SYSTEM_TRADE_RULES


def _snap(
    symbol="AAPL",
    system="system1",
    side="long",
    qty=100.0,
    entry=100.0,
    entry_date="2026-08-18",
):
    signed = qty if side == "long" else -abs(qty)
    return PositionSnapshot(
        symbol=symbol,
        qty=signed,
        side=side,
        avg_entry_price=entry,
        market_value=abs(qty) * entry,
        system=system,
        entry_date=entry_date,
    )


# =====================================================================
# #2 protective stop の $0.01 クランプ
# =====================================================================


class TestStopFloor:
    def test_normal_stop_is_unchanged(self):
        """回帰: raw stop が正なら従来どおり ATR ベースの値をそのまま使う。"""
        snap = _snap(entry=100.0)
        rules = SYSTEM_TRADE_RULES["system1"]  # mult=5.0
        assert at._stop_price_for(snap, rules, atr_value=2.0) == pytest.approx(90.0)

    def test_short_side_is_unchanged(self):
        snap = _snap(side="short", entry=100.0)
        rules = SYSTEM_TRADE_RULES["system1"]
        assert at._stop_price_for(snap, rules, atr_value=2.0) == pytest.approx(110.0)

    def test_negative_raw_stop_uses_pct_floor_not_one_cent(self, monkeypatch):
        """ATR が entry を食い潰すケース: $0.01 ではなく 50% フロアが入る。"""
        monkeypatch.delenv("PROTECT_STOP_FLOOR_ENABLED", raising=False)
        monkeypatch.delenv("PROTECT_STOP_FLOOR_PCT", raising=False)
        snap = _snap(entry=10.0)
        rules = SYSTEM_TRADE_RULES["system1"]  # mult=5.0 -> dist=25 -> raw=-15
        px = at._stop_price_for(snap, rules, atr_value=5.0)
        assert px == pytest.approx(5.0)
        assert px != pytest.approx(0.01)

    def test_floor_emits_warning(self, monkeypatch, caplog):
        """silent success を潰す: フロア適用時は必ず WARNING が立つ。"""
        monkeypatch.delenv("PROTECT_STOP_FLOOR_ENABLED", raising=False)
        snap = _snap(entry=10.0)
        rules = SYSTEM_TRADE_RULES["system1"]
        with caplog.at_level(logging.WARNING, logger=at.logger.name):
            at._stop_price_for(snap, rules, atr_value=5.0)
        assert any("FLOOR" in r.message or "FLOOR" in r.getMessage() for r in caplog.records)

    def test_custom_floor_pct(self, monkeypatch):
        monkeypatch.setenv("PROTECT_STOP_FLOOR_PCT", "0.25")
        snap = _snap(entry=10.0)
        rules = SYSTEM_TRADE_RULES["system1"]
        assert at._stop_price_for(snap, rules, atr_value=5.0) == pytest.approx(7.5)

    @pytest.mark.parametrize("bad", ["abc", "0", "1", "-0.3", "1.5"])
    def test_invalid_floor_pct_falls_back_to_default(self, monkeypatch, bad):
        monkeypatch.setenv("PROTECT_STOP_FLOOR_PCT", bad)
        assert at._stop_floor_pct() == pytest.approx(0.50)

    def test_kill_switch_restores_legacy_clamp(self, monkeypatch):
        """可逆性: =0 で旧 $0.01 挙動へ戻せる。"""
        monkeypatch.setenv("PROTECT_STOP_FLOOR_ENABLED", "0")
        snap = _snap(entry=10.0)
        rules = SYSTEM_TRADE_RULES["system1"]
        assert at._stop_price_for(snap, rules, atr_value=5.0) == pytest.approx(0.01)

    def test_non_positive_entry_yields_no_stop(self, monkeypatch):
        """entry price が壊れている場合は誤った stop を出さない。"""
        monkeypatch.delenv("PROTECT_STOP_FLOOR_ENABLED", raising=False)
        snap = _snap(entry=0.0)
        rules = SYSTEM_TRADE_RULES["system1"]
        assert at._stop_price_for(snap, rules, atr_value=5.0) is None


# =====================================================================
# #1 orphan の既定保護
# =====================================================================


class TestOrphanDefaultProtection:
    def test_orphan_gets_default_protective_stop(self, monkeypatch):
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        snap = _snap(symbol="FOLD", system=None, entry=10.0, entry_date=None)
        exits = build_exit_orders_from_positions([snap], today="2026-08-19")
        assert len(exits) == 1
        po = exits[0]
        assert po.reason == ExitReasonCode.PROTECT_STOP
        assert po.order_type == "stop"
        assert po.side == "sell"
        # ATR 無し → entry からの 50% フロア
        assert po.stop_price == pytest.approx(5.0)

    def test_orphan_stop_uses_atr_when_available(self, monkeypatch):
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        snap = _snap(symbol="FOLD", system=None, entry=100.0, entry_date=None)
        # orphan は S1 相当 (period=20, mult=5.0) を流用する
        exits = build_exit_orders_from_positions(
            [snap], today="2026-08-19", atr_by_symbol={"FOLD": {20: 2.0}}
        )
        assert len(exits) == 1
        assert exits[0].stop_price == pytest.approx(90.0)

    def test_orphan_does_not_fabricate_time_exit(self, monkeypatch):
        """捏造しない契約は維持: close/time exit は作らない (stop だけ)。"""
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        snap = _snap(symbol="CDTX", system=None, entry=10.0, entry_date="2020-01-01")
        exits = build_exit_orders_from_positions([snap], today="2026-08-19")
        assert all(e.reason != ExitReasonCode.TIME for e in exits)
        assert all(e.order_type != "market" for e in exits)

    def test_orphan_still_surfaced_in_unassigned(self, monkeypatch):
        """既存契約の維持: 保護を張っても unassigned への surface は消えない。"""
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        out: list[dict] = []
        snap = _snap(symbol="FOLD", system=None, entry=10.0, entry_date=None)
        build_exit_orders_from_positions(
            [snap], today="2026-08-19", unassigned_out=out
        )
        assert len(out) == 1
        assert out[0]["symbol"] == "FOLD"
        assert out[0]["classification"] == "orphan_no_system_origin"
        # 「保護付きで継続 or 手動 close」の判断材料
        assert out[0]["default_protection"] == "default_stop"

    def test_orphan_protection_disabled_by_flag(self, monkeypatch):
        monkeypatch.setenv("ORPHAN_DEFAULT_PROTECTION", "0")
        out: list[dict] = []
        snap = _snap(symbol="FOLD", system=None, entry=10.0, entry_date=None)
        exits = build_exit_orders_from_positions(
            [snap], today="2026-08-19", unassigned_out=out
        )
        assert exits == []
        assert out[0]["default_protection"] == "none:disabled_by_flag"

    def test_orphan_coid_is_stable_and_dedups(self, monkeypatch):
        """同じ coid が open なら二重に張らない (日跨ぎで積み上がらない)。"""
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        snap = _snap(symbol="FOLD", system=None, entry=10.0, entry_date=None)
        first = build_exit_orders_from_positions([snap], today="2026-08-19")
        coid = first[0].client_order_id
        assert "noentry" in coid
        again = build_exit_orders_from_positions(
            [snap], today="2026-08-20", existing_protect_coids={coid}
        )
        assert again == []

    def test_fractional_orphan_gets_no_native_stop(self, monkeypatch):
        """端株 orphan は native stop 不可 → 張らず、理由を残す。"""
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        out: list[dict] = []
        cov: list[dict] = []
        snap = _snap(symbol="ERAS", system=None, qty=61.7, entry=10.0, entry_date=None)
        exits = build_exit_orders_from_positions(
            [snap],
            today="2026-08-19",
            unassigned_out=out,
            protection_coverage_out=cov,
        )
        assert exits == []
        assert out[0]["default_protection"] == "none:fractional_native_unsupported"
        assert cov[0]["mode"] == "unprotected"
        assert cov[0]["resident_order"] is False


# =====================================================================
# #3 stop / target の qty 競合
# =====================================================================


def _s2_short():
    """S2 (short, stop_atr_period=10 mult=3.0, target 4%)、time exit 未満の保有。"""
    return _snap(
        symbol="ESTC", system="system2", side="short", entry=100.0,
        entry_date="2026-08-19",
    )


class TestQtyContention:
    def test_stop_wins_and_target_is_suppressed_not_submitted(self, monkeypatch):
        monkeypatch.delenv("PROTECT_USE_OCO", raising=False)
        exits = build_exit_orders_from_positions(
            [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        by_reason = {e.reason: e for e in exits}
        assert set(by_reason) == {
            ExitReasonCode.PROTECT_STOP,
            ExitReasonCode.PROTECT_TARGET,
        }
        # stop は発注対象 (skip_reason なし)
        assert by_reason[ExitReasonCode.PROTECT_STOP].skip_reason is None
        assert by_reason[ExitReasonCode.PROTECT_STOP].stop_price == pytest.approx(106.0)
        # target は「送れば必ず 40310000」なので送らない
        tgt = by_reason[ExitReasonCode.PROTECT_TARGET]
        assert tgt.skip_reason == "qty_reserved:stop_takes_priority"

    def test_suppressed_leg_is_never_submitted(self, monkeypatch):
        """skip_reason 付きは submit されない (broker を叩かない)。"""
        monkeypatch.delenv("PROTECT_USE_OCO", raising=False)
        exits = build_exit_orders_from_positions(
            [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        tgt = next(e for e in exits if e.reason == ExitReasonCode.PROTECT_TARGET)

        class _Boom:
            def submit_order(self, *a, **k):  # pragma: no cover
                raise AssertionError("suppressed leg must not reach the broker")

        out = submit_paper_exit_order(tgt, dry_run=False, client=_Boom())
        assert out.order_id is None
        assert out.error is None
        assert out.skip_reason == "qty_reserved:stop_takes_priority"

    def test_existing_trailing_suppresses_new_stop(self, monkeypatch):
        """S1: 前日からの trailing が qty を握る → 新規 stop は送らない。"""
        monkeypatch.delenv("PROTECT_USE_OCO", raising=False)
        snap = _snap(symbol="ADVB", system="system1", entry=100.0)
        trail_coid = "protect-system1-ADVB-20260818-protect-trail"
        exits = build_exit_orders_from_positions(
            [snap],
            today="2026-08-19",
            atr_by_symbol={"ADVB": {20: 2.0}},
            existing_protect_coids={trail_coid},
        )
        stop = next(e for e in exits if e.reason == ExitReasonCode.PROTECT_STOP)
        assert stop.skip_reason == "qty_reserved:trailing_order_already_open"

    def test_suppression_warns(self, monkeypatch, caplog):
        monkeypatch.delenv("PROTECT_USE_OCO", raising=False)
        with caplog.at_level(logging.WARNING, logger=at.logger.name):
            build_exit_orders_from_positions(
                [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
            )
        assert any("protection 抑止" in r.getMessage() for r in caplog.records)

    def test_oco_flag_emits_single_order_with_both_legs(self, monkeypatch):
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        exits = build_exit_orders_from_positions(
            [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        assert len(exits) == 1
        oco = exits[0]
        assert oco.order_type == "oco"
        assert oco.reason == ExitReasonCode.PROTECT_OCO
        assert oco.stop_price == pytest.approx(106.0)
        assert oco.limit_price == pytest.approx(96.15)
        assert oco.skip_reason is None
        assert oco.client_order_id.endswith("protect-oco")

    def test_oco_is_off_by_default(self, monkeypatch):
        monkeypatch.delenv("PROTECT_USE_OCO", raising=False)
        assert at._protect_use_oco() is False

    def test_oco_maps_take_profit_and_stop_loss(self, monkeypatch):
        """OCO は take_profit/stop_loss 引数で送る必要がある。"""
        monkeypatch.setenv("PROTECT_USE_OCO", "1")
        monkeypatch.setenv("ALPACA_PAPER", "true")
        exits = build_exit_orders_from_positions(
            [_s2_short()], today="2026-08-19", atr_by_symbol={"ESTC": {10: 2.0}}
        )
        seen: dict = {}

        def _fake_submit(client, symbol, qty, **kw):
            seen.update(kw)
            return type("O", (), {"id": "oid-1", "status": "accepted"})()

        monkeypatch.setattr(at.ba, "submit_order_with_retry", _fake_submit)
        submit_paper_exit_order(exits[0], dry_run=False, client=object())
        assert seen["order_type"] == "oco"
        assert seen["take_profit"] == pytest.approx(96.15)
        assert seen["stop_loss"] == pytest.approx(106.0)
        # 冪等キーが落ちていないこと (再送で二重発注しない)
        assert seen["client_order_id"].endswith("protect-oco")


# =====================================================================
# #4 recon: armed / rejected / suppressed の分離
# =====================================================================


def _recon(exits_rows):
    from scripts.build_execution_recon import build_recon

    return build_recon(
        signals=None,
        paper_orders=None,
        exit_orders={"date": "2026-08-18", "exits": exits_rows},
        date_str="2026-08-18",
    )


class TestReconArmedVsRejected:
    def test_broker_rejection_is_not_armed(self):
        r = _recon(
            [
                {
                    "system": "system2",
                    "reason": "protect_target",
                    "order_id": None,
                    "error": '{"code":40310000,"existing_qty":"47"}',
                }
            ]
        )
        p = r["portfolio"]
        assert p["exit_rejected"] == 1
        assert p["exit_rejected_protect"] == 1
        assert p["exit_armed"] == 0
        assert p["exit_submitted"] == 0

    def test_suppressed_is_not_armed(self):
        r = _recon(
            [
                {
                    "system": "system2",
                    "reason": "protect_target",
                    "order_id": None,
                    "skip_reason": "qty_reserved:stop_takes_priority",
                }
            ]
        )
        p = r["portfolio"]
        assert p["exit_suppressed"] == 1
        assert p["exit_armed"] == 0
        assert p["exit_rejected"] == 0

    def test_genuine_unsent_still_counts_as_armed(self):
        r = _recon(
            [{"system": "system1", "reason": "protect_stop", "order_id": None}]
        )
        p = r["portfolio"]
        assert p["exit_armed"] == 1
        assert p["exit_rejected"] == 0
        assert p["exit_suppressed"] == 0

    def test_submitted_unchanged(self):
        r = _recon(
            [{"system": "system1", "reason": "protect_stop", "order_id": "o1"}]
        )
        p = r["portfolio"]
        assert p["exit_submitted"] == 1
        assert p["exit_protect"] == 1
        assert p["exit_armed"] == 0

    def test_20260818_shape_armed_zero_rejected_twelve(self):
        """当日の実データ形状: armed 12 と表示されていたものは全て拒否だった。"""
        rows = [
            {"system": "system1", "reason": "protect_stop", "order_id": f"o{i}"}
            for i in range(10)
        ]
        rows.append(
            {"system": "system1", "reason": "time_based", "order_id": "t1"}
        )
        rows += [
            {
                "system": "system2",
                "reason": "protect_target",
                "order_id": None,
                "error": '{"code":40310000}',
            }
            for _ in range(10)
        ]
        rows += [
            {
                "system": "system1",
                "reason": "protect_stop",
                "order_id": None,
                "error": '{"code":40310000}',
            }
            for _ in range(2)
        ]
        p = _recon(rows)["portfolio"]
        assert p["exit_submitted"] == 11
        assert p["exit_rejected"] == 12
        assert p["exit_armed"] == 0

    def test_oco_reason_counts_as_protect(self):
        r = _recon(
            [{"system": "system2", "reason": "protect_oco", "order_id": "o1"}]
        )
        assert r["portfolio"]["exit_protect"] == 1


# =====================================================================
# #5 端株の常駐注文なしを可視化
# =====================================================================


class TestFractionalVisibility:
    def test_fractional_without_breach_warns(self, caplog):
        snap = _snap(symbol="ERAS", system="system1", qty=61.7, entry=100.0)
        with caplog.at_level(logging.WARNING, logger=at.logger.name):
            build_exit_orders_from_positions(
                [snap],
                today="2026-08-19",
                atr_by_symbol={"ERAS": {20: 2.0}},
                price_by_symbol={"ERAS": 100.0},
            )
        assert any("常駐保護なし (端株)" in r.getMessage() for r in caplog.records)

    def test_fractional_coverage_row(self):
        cov: list[dict] = []
        snap = _snap(symbol="ERAS", system="system1", qty=61.7, entry=100.0)
        build_exit_orders_from_positions(
            [snap],
            today="2026-08-19",
            atr_by_symbol={"ERAS": {20: 2.0}},
            price_by_symbol={"ERAS": 100.0},
            protection_coverage_out=cov,
        )
        assert cov[0]["mode"] == "synthetic_daily"
        assert cov[0]["resident_order"] is False
        assert cov[0]["evaluated_at"] == "2026-08-19"

    def test_whole_share_coverage_marks_resident(self):
        cov: list[dict] = []
        snap = _snap(symbol="AAPL", system="system1", qty=100.0, entry=100.0)
        build_exit_orders_from_positions(
            [snap],
            today="2026-08-19",
            atr_by_symbol={"AAPL": {20: 2.0}},
            protection_coverage_out=cov,
        )
        assert cov[0]["mode"] == "native_resident"
        assert cov[0]["resident_order"] is True

    def test_protection_summary_counts_no_resident(self):
        import importlib.util
        from pathlib import Path

        root = Path(__file__).resolve().parents[1]
        spec = importlib.util.spec_from_file_location(
            "pec", root / "scripts" / "paper_exit_check.py"
        )
        m = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(m)
        summary = m._protection_summary(
            [
                {"symbol": "A", "resident_order": True, "mode": "native_resident"},
                {"symbol": "B", "resident_order": False, "mode": "synthetic_daily"},
                {"symbol": "C", "resident_order": False, "mode": "unprotected"},
            ],
            "2026-08-19",
        )
        assert summary["positions"] == 3
        assert summary["no_resident_order"] == 2
        assert summary["no_resident_symbols"] == ["B", "C"]
        assert summary["by_mode"]["synthetic_daily"] == 1
        assert summary["daily_evaluation_date"] == "2026-08-19"


# =====================================================================
# #1b orphan: 価格が凍った銘柄の ATR≈0 で stop がハエ叩きになるのを防ぐ
# =====================================================================


class TestOrphanStaleAtrGuard:
    def test_stale_atr_falls_back_to_pct_floor(self, monkeypatch):
        """実測ケース: FOLD entry=14.26 / ATR20=0.03 (0.21%) は stale。

        5*ATR stop なら $14.11 = entry の 1% 下 (最初の実約定で発火する
        ハエ叩き) になる。% フロアに退避して $7.13 になること。
        """
        monkeypatch.delenv("ORPHAN_MIN_ATR_PCT", raising=False)
        monkeypatch.delenv("PROTECT_STOP_FLOOR_PCT", raising=False)
        snap = _snap(symbol="FOLD", system=None, qty=143.0, entry=14.26)
        assert at._orphan_stop_price(snap, atr_value=0.03) == pytest.approx(7.13)

    def test_stale_atr_warns(self, monkeypatch, caplog):
        monkeypatch.delenv("ORPHAN_MIN_ATR_PCT", raising=False)
        snap = _snap(symbol="FOLD", system=None, qty=143.0, entry=14.26)
        with caplog.at_level(logging.WARNING, logger=at.logger.name):
            at._orphan_stop_price(snap, atr_value=0.03)
        assert any("stale" in r.getMessage() for r in caplog.records)

    def test_healthy_atr_is_still_used(self, monkeypatch):
        """CDTX entry=221.17 / ATR20=2.5 (1.13%) は健全 → ATR stop を使う。"""
        monkeypatch.delenv("ORPHAN_MIN_ATR_PCT", raising=False)
        snap = _snap(symbol="CDTX", system=None, qty=10.0, entry=221.17)
        assert at._orphan_stop_price(snap, atr_value=2.5) == pytest.approx(208.67)

    def test_threshold_is_configurable(self, monkeypatch):
        monkeypatch.setenv("ORPHAN_MIN_ATR_PCT", "0.02")
        monkeypatch.delenv("PROTECT_STOP_FLOOR_PCT", raising=False)
        snap = _snap(symbol="CDTX", system=None, qty=10.0, entry=221.17)
        # 1.13% < 2% -> stale 扱いになりフロアへ退避
        assert at._orphan_stop_price(snap, atr_value=2.5) == pytest.approx(110.585)

    def test_guard_only_applies_to_orphans(self):
        """system 付きは従来どおり ATR stop (strategy の意図を尊重する)。"""
        snap = _snap(symbol="FOLD", system="system1", entry=14.26)
        rules = SYSTEM_TRADE_RULES["system1"]
        assert at._stop_price_for(snap, rules, atr_value=0.03) == pytest.approx(14.11)

    def test_end_to_end_fold_stop_is_not_hair_trigger(self, monkeypatch):
        """planner 経由でも FOLD に entry 直下の stop を張らないこと。"""
        monkeypatch.delenv("ORPHAN_DEFAULT_PROTECTION", raising=False)
        monkeypatch.delenv("ORPHAN_MIN_ATR_PCT", raising=False)
        snap = _snap(symbol="FOLD", system=None, qty=143.0, entry=14.26,
                     entry_date=None)
        exits = build_exit_orders_from_positions(
            [snap], today="2026-08-19", atr_by_symbol={"FOLD": {20: 0.03}}
        )
        assert len(exits) == 1
        assert exits[0].stop_price == pytest.approx(7.13)


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-q"]))
