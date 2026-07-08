"""Contract test for the 2026-07-02 sys3/sys5 STUpass correction log (item 5).

Bug context:
    - `scripts/run_all_systems_today.py` line ~4638 prints
      `🧩 セットアップ結果: system1=... system3=0件 ... system5=0件 ...`
      before candidate generation. For sys3/sys5, `s3_setup / s5_setup` are
      declared as None (comment: 「core の diagnostics から取得するため」),
      and the summary line coerces them via `int(s3_setup or 0)` -> 0.
    - The true value only lands in stage_metrics after candidate generation
      writes back `diagnostics.setup_predicate_count`.

Fix: after the candidate-generation loop completes, look up the stage
snapshot for sys3/sys5 and emit a `🧩 セットアップ結果 (確定):` log line so
downstream ops readers see the correct STUpass count.

This test asserts the source contract for the corrected log — we don't
execute the whole 8000-line signals pipeline in unit tests.
"""

from __future__ import annotations

from pathlib import Path

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "run_all_systems_today.py"


def _read() -> str:
    return SCRIPT.read_text(encoding="utf-8")


def test_correction_log_present():
    """After the candidate loop, a correction log for sys3/sys5 is emitted."""
    txt = _read()
    assert "セットアップ結果 (確定)" in txt, (
        "sys3/sys5 の STUpass 補正ログが未実装. line ~4638 の事前 print だけだと "
        "sys3/sys5 は常に 0 になる (candidate 生成前は setup_count=None)."
    )


def test_correction_log_uses_setup_count_from_snapshot():
    """The correction reads setup_count via _get_stage_snapshot (post-candidate)."""
    txt = _read()
    # 補正 block が snapshot 経由で setup_count を引く形態
    assert "setup_count" in txt and "_get_stage_snapshot" in txt
    # 補正 log は sys3 と sys5 の両方を扱う
    # (sys1/2/4/6/7 は事前確定なので pre-print で正しい値が出る)
    #
    # 補正 line 内に system3 / system5 の label が両方揃うこと
    idx = txt.find("セットアップ結果 (確定)")
    # 直後 ~500 文字以内に system3 / system5 の言及がある
    window = txt[idx : idx + 800]
    assert "system3" in window, "correction log should mention system3"
    assert "system5" in window, "correction log should mention system5"


def test_correction_log_after_candidate_loop():
    """補正 log は candidate loop 終了後 = order_1_7 定義前後にいる."""
    txt = _read()
    idx_correction = txt.find("セットアップ結果 (確定)")
    idx_order = txt.find('order_1_7 = [f"system{i}" for i in range(1, 8)]')
    assert idx_correction > 0
    assert idx_order > 0
    # 補正 log は order_1_7 の前後 (loop 完了後) にいるべき
    # 直近 loop の end 付近と order_1_7 の間に位置していれば OK
    strategies_done_idx = txt.find('"strategies_done"')
    assert strategies_done_idx > 0
    assert idx_correction < strategies_done_idx, (
        "correction log は strategies_done 進捗通知の前に emit されるべき "
        "(candidate loop 完走後の位置)"
    )


def test_pre_print_still_present_for_other_systems():
    """事前 print (system1/2/4/6/7 用) は残っている (regression 検知).

    sys1/2/4/6/7 は事前確定できるので、事前 print 自体を消すと dashboard 側の
    log timing が変わる。補正は「後付け emit」であって pre-print の置換ではない。
    """
    txt = _read()
    # 事前 print も残っている
    assert (
        "🧩 セットアップ結果: " in txt
    ), "事前 print が消えている. 補正は追加 emit のみが望ましい"
