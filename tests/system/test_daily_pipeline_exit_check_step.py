"""daily_pipeline.ps1 の exit_check step 契約 test.

subscriber サービスイン基準:
    - [exit_check] step が pipeline に存在する
    - default = dry-run (paper_exit_check.py + --output-json のみ)
    - -AutoSubmitPaper 時のみ --confirm --yes が渡る
    - SkipExitCheck switch が定義されている
    - step の位置は paper_orders (5b) の後、vercel (6) の前

paper_orders_step 側と同じ opt-in flag をシェアする regression protection。
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
    idx_switch = block.find("if ($AutoSubmitPaper)")
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


def test_exit_check_shares_autosubmit_flag_with_entry():
    """entry (5b) と exit_check (5c) は同じ -AutoSubmitPaper flag で連動する。"""
    code = _code_section(_read())
    # exit_check の submit 分岐が同じ $AutoSubmitPaper を参照している
    idx_exit = code.find('Write-Log "[exit_check] SkipExitCheck')
    tail = code[idx_exit:]
    idx_switch = tail.find("if ($AutoSubmitPaper)")
    assert idx_switch != -1, "exit_check が AutoSubmitPaper flag を参照していない"
