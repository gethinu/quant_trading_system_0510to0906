"""漏斗 (signal_pipeline/v1) の funnel phase を today_signals から配線するロジックの検証。

狙い (2026-08-12 observability fix ①):
  - ダッシュが読む pipeline_*.json は funnel phase (Tgt/FILpass/STUpass/TRDlist/Entry) が
    全 system measured=false。today_signals の per-system funnel を single source に、
    **7 system 全部を measured=true・実数** で埋める配線 (patch_pipeline_funnel)。
  - 数字が today_signals funnel と一致すること (結合テスト)。
  - grouped-daily 実測 phase と spy_only Tgt は保護 (上書きしない)。
  - honesty: signals が無ければ未計測を維持 (0 で誤魔化さない)。idempotent。

回帰不変 (本タスク mandate):
  (1) file 単調非減少     … check_file_monotonic (idempotent 再配線で後退しない)
  (2) rolling→filter→setup IT … check_funnel_monotonic (配線後 funnel が単調)
  (3) silent WARN 監視    … evaluate_gates().warnings で握り潰し違反を可視化
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.invariants.phase1_gates import (  # noqa: E402
    GateConfig,
    GateMode,
    check_file_monotonic,
    check_funnel_monotonic,
    evaluate_gates,
)
from scripts.build_execution_recon import (  # noqa: E402
    funnel_counts_from_signals,
    patch_pipeline_funnel,
)

ENFORCE = GateConfig(default=GateMode.ENFORCE)
WARN = GateConfig(default=GateMode.WARN)

_PHASES = ("Tgt", "FILpass", "STUpass", "TRDlist", "Entry")


def _phase(name: str, count, measured: bool) -> dict:
    return {
        "name": name,
        "label": name,
        "condition": name,
        "count": count,
        "measured": measured,
        "ratio_of_prev": None,
        "ratio_of_universe": None,
    }


def _pipeline_all_unmeasured() -> dict:
    """daily_polygon_monitor 出力形。funnel phase が全 system measured=false。"""

    def sysblock(sysid: str, tgt) -> dict:
        phases = [_phase(n, None, False) for n in _PHASES]
        phases[0]["count"] = tgt  # Tgt に count はあるが measured=false のことがある
        phases.append(_phase("Exit", None, False))
        return {"system_id": sysid, "phases": phases, "final_signals": None}

    return {
        "date": "2026-08-12",
        "schema": "signal_pipeline/v1",
        "systems": {f"sys{i}": sysblock(f"sys{i}", None) for i in range(1, 8)},
        "notes": ["phases are reference counts, not evaluation criteria."],
    }


def _today_signals() -> dict:
    """2026-08-12 実データ相当の funnel (診断 §1 の表)。"""
    fn = {
        "sys1": {"target": 6558, "filter_pass": 1520, "setup_pass": 641, "candidate_count": 10, "entry_count": 7},
        "sys2": {"target": 6558, "filter_pass": 1615, "setup_pass": 115, "candidate_count": 10, "entry_count": 10},
        "sys3": {"target": 6558, "filter_pass": 1101, "setup_pass": 10, "candidate_count": 10, "entry_count": 0},
        "sys4": {"target": 6558, "filter_pass": 505, "setup_pass": 10, "candidate_count": 10, "entry_count": 0},
        "sys5": {"target": 6558, "filter_pass": 1444, "setup_pass": 3, "candidate_count": 3, "entry_count": 0},
        "sys6": {"target": 6558, "filter_pass": 1096, "setup_pass": 0, "candidate_count": 0, "entry_count": 0},
        "sys7": {"target": 6558, "filter_pass": 1, "setup_pass": 0, "candidate_count": 0, "entry_count": 0},
    }
    return {
        "date": "2026-08-12",
        "systems": {k: {"funnel": v, "n_candidates_input": v["candidate_count"],
                        "n_signals_output": v["entry_count"]} for k, v in fn.items()},
    }


# --- 結合テスト: 7 system 全部が measured=true かつ today_signals と一致 -----------
def test_all_seven_systems_measured_and_match_signals():
    pipe = _pipeline_all_unmeasured()
    sig = _today_signals()
    _, n_patched, status = patch_pipeline_funnel(pipe, sig)
    assert status == "ok"
    assert n_patched > 0

    counts = funnel_counts_from_signals(sig)
    for i in range(1, 8):
        sysk = f"sys{i}"
        phases = {p["name"]: p for p in pipe["systems"][sysk]["phases"]}
        for name in _PHASES:
            if sysk == "sys7" and name == "Tgt":
                # spy_only: funnel target(6558=共通株) は使わない → 未計測を維持。
                assert phases[name]["measured"] is False
                continue
            assert phases[name]["measured"] is True, f"{sysk}.{name} 未計測"
            assert phases[name]["count"] == counts[sysk][name], f"{sysk}.{name} 数字不一致"
        # final_signals も Entry で補強
        assert pipe["systems"][sysk]["final_signals"] == counts[sysk]["Entry"]


# --- spy_only sys7 の Tgt を funnel target(共通株ユニバース)で壊さない -------------
def test_spy_only_tgt_protected():
    pipe = _pipeline_all_unmeasured()
    # sys7 Tgt を build 側の矯正値 1 (measured=false) に見立てる
    for p in pipe["systems"]["sys7"]["phases"]:
        if p["name"] == "Tgt":
            p["count"], p["measured"] = 1, False
    patch_pipeline_funnel(pipe, _today_signals())
    tgt = next(p for p in pipe["systems"]["sys7"]["phases"] if p["name"] == "Tgt")
    assert tgt["count"] == 1  # 6558 に戻っていない


# --- grouped-daily 実測 (measured=true) を上書きしない ------------------------------
def test_grouped_measured_phase_not_overwritten():
    pipe = _pipeline_all_unmeasured()
    tgt = next(p for p in pipe["systems"]["sys1"]["phases"] if p["name"] == "Tgt")
    tgt["count"], tgt["measured"] = 12330, True  # grouped-daily 実測
    patch_pipeline_funnel(pipe, _today_signals())
    tgt = next(p for p in pipe["systems"]["sys1"]["phases"] if p["name"] == "Tgt")
    assert tgt["count"] == 12330 and tgt["measured"] is True


# --- honesty: signals 無ければ未計測維持 --------------------------------------------
def test_no_signals_keeps_unmeasured():
    pipe = _pipeline_all_unmeasured()
    _, n, status = patch_pipeline_funnel(pipe, None)
    assert status == "no_signals" and n == 0
    for sysk in pipe["systems"]:
        assert all(p["measured"] is False for p in pipe["systems"][sysk]["phases"])


# --- idempotent + (1) file 単調非減少: 再配線で count が後退しない ----------------
def test_idempotent_and_file_monotonic():
    pipe = _pipeline_all_unmeasured()
    sig = _today_signals()
    patch_pipeline_funnel(pipe, sig)
    first = {s: {p["name"]: p["count"] for p in v["phases"]}
             for s, v in pipe["systems"].items()}
    _, n2, _ = patch_pipeline_funnel(pipe, sig)  # 2 回目
    assert n2 == 0  # 既に measured=true なので更新 0 = idempotent
    for s, v in pipe["systems"].items():
        for p in v["phases"]:
            prev, curr = first[s][p["name"]], p["count"]
            # (1) 累積カウンタ非減少不変: 再観測で後退しない
            assert not check_file_monotonic(prev, curr, ENFORCE).violated


# --- (2) rolling→filter→setup IT: 配線後 funnel が単調 (Tgt>=FIL>=STU) -----------
def test_wired_funnel_is_monotonic():
    pipe = _pipeline_all_unmeasured()
    patch_pipeline_funnel(pipe, _today_signals())
    for sysk, v in pipe["systems"].items():
        ph = {p["name"]: p["count"] for p in v["phases"]}
        rolling = ph["Tgt"] if ph["Tgt"] is not None else 6558  # sys7 Tgt 保護分を補完
        r = check_funnel_monotonic(rolling, ph["FILpass"], ph["STUpass"], ENFORCE)
        assert not r.violated and r.ok, f"{sysk} funnel 非単調: {ph}"


# --- (3) silent WARN 監視: 壊れた funnel が握り潰されず report に出る ---------------
def test_broken_funnel_surfaces_as_warning():
    # filter が rolling を超える (母集団混入) を WARN で評価 → warnings に必ず出る
    bad = check_funnel_monotonic(10, 12, 3, WARN)
    rep = evaluate_gates([bad])
    assert rep.ok is True            # WARN なので執行は止めない
    assert len(rep.warnings) == 1    # が silent にはならない
    assert "funnel_monotonic" in rep.summary()
