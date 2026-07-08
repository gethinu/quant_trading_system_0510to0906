"""System test: publish_data_to_vercel.ps1 の purge / commit 契約を固定する.

Phase 1 audit gap (2026-07-02 hygiene):
    - `Remove-Item` + `git add <path>` の組合せは deletion を stage しない
      (path staging では deletion 追跡は `-A` が必要). この抜けにより
      dashboard の data/ に stale JSON が commit されずに永久残留する事象
      発生. 本 test は `git rm` / `git add -A` の使用を強制する.
    - default KeepDays=7 が誤って倍増しないこと (disk 使用量 / git 履歴).

PowerShell を Linux で走らせられないため source (regex + 部分文字列) で契約固定.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PS1 = Path(__file__).resolve().parents[2] / "scripts" / "publish_data_to_vercel.ps1"


@pytest.fixture(scope="module")
def ps1_text() -> str:
    assert PS1.exists(), f"{PS1} が存在しない = publish step 消失"
    return PS1.read_text(encoding="utf-8-sig", errors="replace")


class TestPublishDataContract:
    def test_default_keepdays_is_7(self, ps1_text: str):
        """-KeepDays default = 7 (disk 使用量 / git 履歴 hygiene)."""
        # `[int]$KeepDays = 7` の形態
        assert (
            "[int]$KeepDays = 7" in ps1_text
        ), "KeepDays default が 7 でない. purge 効率が下がる (git 履歴が肥大化)"

    def test_purge_uses_git_rm_not_bare_remove_item(self, ps1_text: str):
        """★ 2026-07-02 fix: prune は `git rm` で行い commit に deletion を載せる.

        従来: `Remove-Item ... -Force` + `git add <path>` の組合せは
              path staging で deletion を認識しない → stale が残留.
        新版: `git rm -f` で explicit に stage.
        """
        assert "git rm" in ps1_text, (
            "git rm による explicit deletion staging が欠落. "
            "Remove-Item だけでは commit に載らず data/ が肥大化する"
        )

    def test_git_add_uses_all_flag(self, ps1_text: str):
        """git add -A -- $RelData で削除も含めて stage する."""
        assert (
            "git add -A --" in ps1_text or "git add -A -- " in ps1_text
        ), "git add に -A flag が無い. path staging だけでは deletion が消えない"

    def test_purge_source_flag_present(self, ps1_text: str):
        """PurgeSource flag で results_csv/ side の source も整理する."""
        assert "$PurgeSource" in ps1_text
        # results_csv/ 側で prune している (SrcDir を触る block)
        assert "pruned (source)" in ps1_text

    def test_purge_covers_all_four_prefixes(self, ps1_text: str):
        """4 prefix (today_signals_/pipeline_/polygon_daily_coverage_/narrative_)
        が prune 対象に入っている.
        """
        for p in (
            "today_signals_",
            "pipeline_",
            "polygon_daily_coverage_",
            "narrative_",
        ):
            assert f'"{p}"' in ps1_text, f"prune 対象 prefix `{p}` が欠落"

    def test_branch_target_unchanged(self, ps1_text: str):
        """push 先 branch が claude/monitor-webapp を維持."""
        assert "claude/monitor-webapp" in ps1_text
