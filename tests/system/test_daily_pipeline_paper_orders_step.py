"""daily_pipeline.ps1 paper_orders step の存在と semantics を固定化する。

pytest だが PowerShell 実行はせず、ps1 の文字列パターンで固定化する。
paper_orders step が pipeline から外れる/勝手に AutoSubmit になる regression を block する。
"""

from __future__ import annotations

from pathlib import Path

PIPELINE = Path(__file__).resolve().parents[2] / "scripts" / "daily_pipeline.ps1"


def _read() -> str:
    return PIPELINE.read_text(encoding="utf-8")


def _code_section(txt: str) -> str:
    """docstring / param ブロック以降の code 本体だけ返す。docstring の言及と
    code の言及を混同しないため。"""
    p = txt.find("param(")
    if p == -1:
        return txt
    close = txt.find(")\n", p)
    return txt[close:] if close != -1 else txt


def test_pipeline_declares_autosubmit_switch():
    txt = _read()
    assert "[switch]$AutoSubmitPaper" in txt


def test_pipeline_declares_tier_param():
    txt = _read()
    assert "[string]$Tier" in txt


def test_pipeline_has_paper_orders_step():
    txt = _read()
    assert "paper_orders_dryrun" in txt
    assert "paper_orders_submit" in txt


def test_dryrun_is_default_path():
    txt = _read()
    assert "paper_trading_dryrun.py" in txt
    assert "autosubmit not enabled" in txt


def test_submit_requires_autosubmit_switch():
    code = _code_section(_read())
    idx_switch = code.find("if ($AutoSubmitPaper)")
    idx_submit = code.find("paper_trading_submit.py")
    assert idx_switch != -1 and idx_submit != -1
    assert idx_switch < idx_submit


def test_submit_uses_confirm_and_yes():
    txt = _read()
    idx_switch = txt.find("if ($AutoSubmitPaper)")
    idx_else = txt.find("else", idx_switch)
    submit_block = txt[idx_switch:idx_else]
    assert "--confirm" in submit_block
    assert "--yes" in submit_block


def test_paper_orders_step_writes_json_output():
    txt = _read()
    assert "paper_orders_$DateCompact.json" in txt


def test_paper_orders_between_publish_and_vercel():
    code = _code_section(_read())
    idx_publish = code.find('Write-Log "[publish] SkipPublish')
    idx_paper = code.find('Write-Log "[paper_orders] SkipPaperOrders')
    idx_vercel = code.find('Write-Log "----- [vercel]')
    assert -1 not in (idx_publish, idx_paper, idx_vercel)
    assert idx_publish < idx_paper < idx_vercel


def test_skip_paper_orders_switch_exists():
    txt = _read()
    assert "$SkipPaperOrders" in txt


def test_tier_resolves_from_env_when_unset():
    txt = _read()
    assert "ALPACA_TIER" in txt
    assert '"small"' in txt or "'small'" in txt
