"""Phase1 常設ゲートのテスト。fail-closed / 段階導入 (WARN->ENFORCE) /
silent-WARN 監視 (d) / pipeline IT (c) を固定する。"""

from __future__ import annotations

import pytest

from common.invariants.phase1_gates import (
    GateBlocked,
    GateConfig,
    GateMode,
    check_file_monotonic,
    check_funnel_monotonic,
    check_measurement_invariant,
    check_served_today,
    check_snapshot_freshness,
    evaluate_gates,
    raise_if_blocked,
    verify_alpaca_snapshot,
)

WARN = GateConfig(default=GateMode.WARN)
ENFORCE = GateConfig(default=GateMode.ENFORCE)
OFF = GateConfig(default=GateMode.OFF)


# --- (a) measurement invariant ---------------------------------------------
def test_measurement_invariant_holds_fired_accounting():
    # 07-27 実発火日: fired 23 = close 20 + protect 3 (armed は式に入らない)
    p = {"exit_submitted": 23, "exit_close": 20, "exit_protect": 3, "exit_armed": 1}
    r = check_measurement_invariant(p, ENFORCE)
    assert not r.violated and r.ok


def test_measurement_invariant_detects_denominator_mix():
    # close+protect が submitted と食い違う = 分母混線 (旧バグ)
    p = {"exit_submitted": 14, "exit_close": 5, "exit_protect": 25}
    r = check_measurement_invariant(p, ENFORCE)
    assert r.violated and not r.ok and r.is_blocking


def test_measurement_invariant_all_armed_day_holds():
    # 07-29 pre-open: fired 0 = close 0 + protect 0、armed 25 は別枠 → 恒等式 OK
    p = {"exit_submitted": 0, "exit_close": 0, "exit_protect": 0, "exit_armed": 25}
    assert not check_measurement_invariant(p, ENFORCE).violated


# --- (a) served-today -------------------------------------------------------
def test_served_today_ok_and_stale():
    assert not check_served_today("20260806", "20260806", ENFORCE).violated
    stale = check_served_today("20260731", "20260806", ENFORCE)
    assert stale.violated and stale.is_blocking
    assert check_served_today(None, "20260806", ENFORCE).violated  # 実績なし


# --- (a) snapshot freshness -------------------------------------------------
def test_snapshot_freshness_bounds():
    now = 1_000_000.0
    assert not check_snapshot_freshness(now - 100, now, 300, ENFORCE).violated
    assert check_snapshot_freshness(now - 900, now, 300, ENFORCE).violated
    assert check_snapshot_freshness(None, now, 300, ENFORCE).violated  # missing


# --- (a) file monotonic -----------------------------------------------------
def test_file_monotonic_first_and_regression():
    assert not check_file_monotonic(None, 5, ENFORCE).violated  # 初回
    assert not check_file_monotonic(5, 5, ENFORCE).violated  # 据え置き
    assert not check_file_monotonic(5, 7, ENFORCE).violated  # 増加
    assert check_file_monotonic(7, 4, ENFORCE).violated  # 後退 = 違反


# --- (b) verify alpaca_snapshot --------------------------------------------
def test_verify_snapshot_ok():
    snap = {
        "as_of": "2026-08-06",
        "account_equity": 10120,
        "positions": [{"symbol": "SPY"}],
    }
    r = verify_alpaca_snapshot(snap, "20260806", ENFORCE)
    assert not r.violated and r.ok


@pytest.mark.parametrize(
    "snap,why",
    [
        (None, "missing"),
        ({"as_of": "2026-08-06", "positions": []}, "missing account_equity key"),
        ({"as_of": "2026-08-05", "account_equity": 1, "positions": []}, "stale as_of"),
        (
            {"as_of": "2026-08-06", "account_equity": 1, "positions": "nope"},
            "positions not list",
        ),
    ],
)
def test_verify_snapshot_rejects(snap, why):
    r = verify_alpaca_snapshot(snap, "20260806", ENFORCE)
    assert r.violated, why


# --- (c) funnel monotonic (rolling -> filter -> setup) IT -------------------
def _run_pipeline(universe, min_price, want_setup):
    """rolling -> filter -> setup を模した最小 pipeline。各段の件数を返す。"""
    rolling = list(universe)
    filtered = [s for s in rolling if s["price"] >= min_price]
    setup = [s for s in filtered if s["symbol"] in want_setup]
    return len(rolling), len(filtered), len(setup)


def test_pipeline_it_funnel_is_monotonic():
    universe = [{"symbol": f"S{i}", "price": i} for i in range(1, 21)]  # 20 銘柄
    rolling, filtered, setup = _run_pipeline(
        universe, min_price=10, want_setup={"S15", "S18"}
    )
    assert rolling == 20 and filtered == 11 and setup == 2
    r = check_funnel_monotonic(rolling, filtered, setup, ENFORCE)
    assert not r.violated and r.ok


def test_pipeline_it_detects_broken_funnel():
    # filter が rolling を超える (母集団混入) = 不変条件違反
    r = check_funnel_monotonic(10, 12, 3, ENFORCE)
    assert r.violated and r.is_blocking


# --- 段階導入: WARN では止めない / ENFORCE では止める -----------------------
def test_warn_mode_records_but_does_not_block():
    p = {"exit_submitted": 14, "exit_close": 5, "exit_protect": 25}
    r = check_measurement_invariant(p, WARN)
    assert r.violated is True  # 事実は保持
    assert r.ok is True  # だが執行は止めない
    assert r.is_warn and not r.is_blocking


def test_off_mode_does_not_evaluate():
    p = {"exit_submitted": 14, "exit_close": 5, "exit_protect": 25}
    r = check_measurement_invariant(p, OFF)
    assert r.violated is False and r.ok is True and r.detail == "off"


def test_staged_rollout_per_gate():
    # measurement だけ ENFORCE、他は WARN に個別昇格できる
    cfg = GateConfig(
        default=GateMode.WARN, modes={"measurement_invariant": GateMode.ENFORCE}
    )
    bad = {"exit_submitted": 14, "exit_close": 5, "exit_protect": 25}
    m = check_measurement_invariant(bad, cfg)
    s = check_served_today("20260731", "20260806", cfg)
    assert m.is_blocking  # 昇格済 → ブロック
    assert s.is_warn  # 既定 WARN → 握り潰し (まだ)


# --- fail-closed 集約 + (d) silent-WARN 監視 --------------------------------
def test_report_ok_blocks_on_any_enforce_violation():
    good = check_served_today("20260806", "20260806", ENFORCE)
    bad = check_measurement_invariant(
        {"exit_submitted": 14, "exit_close": 5, "exit_protect": 25}, ENFORCE
    )
    rep = evaluate_gates([good, bad])
    assert rep.ok is False
    assert len(rep.blocking) == 1
    with pytest.raises(GateBlocked):
        raise_if_blocked(rep)


def test_report_surfaces_silent_warnings_for_monitoring():
    # (d) WARN で握り潰された違反も report.warnings で必ず可視化される
    bad = check_measurement_invariant(
        {"exit_submitted": 14, "exit_close": 5, "exit_protect": 25}, WARN
    )
    rep = evaluate_gates([bad])
    assert rep.ok is True  # 執行は続く
    assert len(rep.warnings) == 1  # が silent にはならない
    assert "measurement_invariant" in rep.summary()
    raise_if_blocked(rep)  # WARN のみ → 投げない


def test_report_summary_tags_each_gate():
    results = [
        check_served_today("20260806", "20260806", ENFORCE),  # OK
        check_measurement_invariant(
            {"exit_submitted": 1, "exit_close": 1, "exit_protect": 0}, ENFORCE
        ),  # OK
        check_file_monotonic(7, 4, ENFORCE),  # BLOCK
        check_snapshot_freshness(0.0, 10_000.0, 300, WARN),  # WARN
    ]
    summary = evaluate_gates(results).summary()
    assert "[OK]" in summary and "[BLOCK]" in summary and "[WARN]" in summary
