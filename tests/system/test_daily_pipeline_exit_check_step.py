"""daily_pipeline.ps1 の exit_check step 契約 test.

subscriber サービスイン基準:
    - [exit_check] step が pipeline に存在する
    - default = dry-run (paper_exit_check.py + --output-json のみ)
    - -AutoSubmitPaper / -AutoSubmitPaperExits 時のみ --confirm --yes が渡る
    - SkipExitCheck switch が定義されている
    - step の位置は paper_orders (5b) の後、vercel (6) の前

既存の combined opt-in を保持しつつ exit-only opt-in を許す regression protection。
"""

from __future__ import annotations

from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "daily_pipeline.ps1"


def _read() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _code_section(txt: str) -> str:
    p = txt.find("param(")
    if p == -1:
        return txt
    close = txt.find(")\n", p)
    return txt[close:] if close != -1 else txt


def test_pipeline_declares_skip_exit_check_switch():
    txt = _read()
    assert "[switch]$SkipExitCheck" in txt


def test_pipeline_has_exit_check_step():
    code = _code_section(_read())
    assert "paper_exit_check.py" in code
    assert '"exit_check"' in code or "[exit_check]" in code


def test_exit_check_writes_json_output():
    txt = _read()
    assert "exit_orders_$DateCompact.json" in txt


def test_exit_check_default_is_dryrun():
    """AutoSubmit 無指定なら --confirm は入らない。"""
    code = _code_section(_read())
    # exit_check 区間だけを切り出して check
    start = code.find('Write-Log "[exit_check] SkipExitCheck')
    assert start != -1, "exit_check step が見つからない"
    end = code.find("# --- Step 6:", start)
    if end == -1:
        end = code.find('Write-Log "----- [vercel]', start)
    block = code[start:end]
    # AutoSubmit の中には --confirm --yes、else の中には無い
    idx_switch = block.find("if ($ExitSubmitEnabled)")
    idx_else = block.find("else {", idx_switch)
    assert idx_switch != -1 and idx_else != -1
    submit_block = block[idx_switch:idx_else]
    dryrun_block = block[idx_else:]
    assert "--confirm" in submit_block
    assert "--yes" in submit_block
    assert "--confirm" not in dryrun_block


def test_exit_check_between_paper_orders_and_vercel():
    code = _code_section(_read())
    idx_paper = code.find('Write-Log "[paper_orders] SkipPaperOrders')
    idx_exit = code.find('Write-Log "[exit_check] SkipExitCheck')
    idx_vercel = code.find('Write-Log "----- [vercel]')
    assert -1 not in (idx_paper, idx_exit, idx_vercel)
    assert idx_paper < idx_exit < idx_vercel


def test_exit_check_has_independent_submit_flag():
    """exit だけを実発注でき、entry の権限と分離されている。"""
    code = _code_section(_read())
    assert "[switch]$AutoSubmitPaperExits" in _read()
    assert "$ExitSubmitEnabled = $AutoSubmitPaper -or $AutoSubmitPaperExits" in code
    idx_exit = code.find('Write-Log "[exit_check] SkipExitCheck')
    tail = code[idx_exit:]
    assert "if ($ExitSubmitEnabled)" in tail


def test_exit_only_submit_can_be_enabled_by_primary_env():
    """既存 scheduler wrapper が primary .env を継承すれば追加引数なしで opt-in 可能。"""
    code = _code_section(_read())
    assert "$env:AUTO_SUBMIT_PAPER_EXITS" in code
    assert "$AutoSubmitPaperExits = $true" in code


def test_proposal_pass_does_not_arm_the_unsubmitted_time_exit_gate():
    """06:00 の提案 pass に --fail-on-unsubmitted-time-exit を配線しない。

    2026-08-25 (main 統合) の判断。daily_pipeline の exit_check は role=proposal
    の pass で、exit の実発注は夜の open_auto_run が行う。この pass で
    「期限到来 time exit が未送信」を失敗にすると、**未送信であることが正常**
    なので毎日鳴る (2026-08-22 に潰したばかりの cry-wolf と同型)。

    ゲート自体は paper_exit_check に opt-in で存在し (既定 OFF)、件数は
    artifact の time_exit_unsubmitted / execution_health で常に観測できる。
    """
    code = _code_section(_read())
    idx_exit = code.find('Write-Log "[exit_check] SkipExitCheck')
    tail = code[idx_exit:]
    assert '"--fail-on-unsubmitted-time-exit"' not in tail


def test_exit_check_exit_codes_are_mapped_without_collision():
    """exit=3 は broker_unreachable、gate は別コード 4。同じ数字に相乗りさせない。"""
    code = _code_section(_read())
    idx_exit = code.find('Write-Log "[exit_check] SkipExitCheck')
    tail = code[idx_exit:]
    assert (
        'elseif ($ec -eq 3) { $Failures += "exit_check(broker_unreachable)" }' in tail
    )
    assert (
        'elseif ($ec -eq 4) { $Failures += "exit_check(unsubmitted_time_exit)" }'
        in tail
    )
    # 未知コードも黙って成功にしない
    assert 'elseif ($ec -ne 0) { $Failures += "exit_check(exit=$ec)" }' in tail
