"""System test: publish_data_to_vercel.ps1 の commit-target / purge 契約を固定する.

2026-08-04 root-cause fix (branch-target):
    旧実装は private index を **current worktree の HEAD** から seed し `git commit`
    で current HEAD を進めていた。open-auto-run / daily-main-follow の worktree から
    publish が呼ばれると data commit が執行ブランチに載り、`git push origin $Branch`
    は stale な local $Branch を送るだけで origin/claude/monitor-webapp が凍結した
    (Vercel が数営業日 stale)。恒久修正で publish は plumbing (commit-tree) を使い、
    どの worktree/HEAD から走っても base=origin/$Branch tip を親にコミットを作り、
    その新コミットを直接 origin/$Branch へ push する。working tree は一切触らない。

    本 test は「data commit の親 = $Branch tip」「push 先 = refs/heads/$Branch」
    「current HEAD を commit しない」を source レベルで固定する。

Phase 1 hygiene (2026-07-02) と 2026-07-30 hardening の契約も維持する。

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


def _code_lines(text: str) -> str:
    """block-comment (<# #>) と行コメント (#...) を落とした「実行行だけ」を返す.

    契約の一部は「あるトークンが *コード上* に存在しない」ことなので、歴史的経緯を
    説明する散文コメントに同じ語が出てきても誤検知しないよう素朴に除去する.
    """
    out = []
    in_block = False
    for ln in text.splitlines():
        s = ln.strip()
        if s.startswith("<#"):
            in_block = True
        if in_block:
            if "#>" in s:
                in_block = False
            continue
        if s.startswith("#"):
            continue
        # 行末コメントを削る (文字列内 # は無視するが本 script では実害なし)
        if "#" in ln:
            ln = ln.split("#", 1)[0]
        out.append(ln)
    return "\n".join(out)


@pytest.fixture(scope="module")
def ps1_code(ps1_text: str) -> str:
    return _code_lines(ps1_text)


class TestBranchTargetContract20260804:
    """★ headline: data commit は「常に claude/monitor-webapp の tip」に載る."""

    def test_base_is_branch_tip_via_origin_not_head(self, ps1_code: str):
        """base は origin/$Branch (無ければ local $Branch)。current HEAD は使わない."""
        assert (
            'rev-parse --verify -q "refs/remotes/origin/$Branch"' in ps1_code
        ), "commit base を origin/$Branch tip から解決していない"
        assert (
            "read-tree $BaseCommit" in ps1_code
        ), "private index を base($Branch tip) の tree から seed していない"
        # ★ 旧バグ: current HEAD から seed して current HEAD に commit していた
        assert (
            "git read-tree HEAD" not in ps1_code
        ), "read-tree HEAD が残存 = current worktree の HEAD から seed している (再発)"

    def test_commit_created_via_commit_tree_on_base(self, ps1_code: str):
        """コミットは commit-tree で base を親に作る (checkout/HEAD 前進をしない)."""
        assert "git commit-tree" in ps1_code, "commit-tree でのコミット生成が欠落"
        assert (
            "-p $BaseCommit" in ps1_code
        ), "commit-tree の親が base($Branch tip) でない"

    def test_does_not_commit_onto_current_head(self, ps1_code: str):
        """current worktree の HEAD を進める `git commit` を使わない."""
        assert (
            "git commit --no-verify" not in ps1_code
        ), "HEAD を進める git commit が残存"
        assert "git commit -m" not in ps1_code, "HEAD を進める git commit が残存"

    def test_push_targets_new_commit_to_branch_ref(self, ps1_code: str):
        """新コミットを直接 origin の refs/heads/$Branch へ push する."""
        assert "git push origin" in ps1_code
        assert ":refs/heads/$Branch" in ps1_code, (
            "push が <new-commit>:refs/heads/$Branch の explicit refspec でない "
            "(stale な local $Branch を送る旧挙動に戻っている)"
        )

    def test_no_working_tree_mutation(self, ps1_code: str):
        """publish は working tree を触らない (執行 worktree/freeze-baseline 非汚染)."""
        assert (
            "git add -A --" not in ps1_code
        ), "working tree を stage する git add -A が残存"
        assert (
            "git rm " not in ps1_code
        ), "working tree を触る git rm が残存 (index-only にする)"
        assert (
            "Copy-Item $src" not in ps1_code
        ), "data/ への物理 copy が残存 (執行 worktree を汚す)"

    def test_stage_via_hash_object(self, ps1_code: str):
        """当日 JSON は results_csv から hash-object で直接 index へ stage する."""
        assert "git hash-object -w" in ps1_code, "hash-object による直接 stage が欠落"
        assert "update-index --add --cacheinfo" in ps1_code, "cacheinfo staging が欠落"

    def test_prune_is_index_only(self, ps1_code: str):
        """世代整理は index-only (--force-remove) で working tree を触らない."""
        assert "update-index --force-remove" in ps1_code, "index-only prune が欠落"


class TestPublishDataContract:
    def test_default_keepdays_is_7(self, ps1_text: str):
        assert "[int]$KeepDays = 7" in ps1_text, "KeepDays default が 7 でない"

    def test_purge_source_flag_present(self, ps1_text: str):
        assert "$PurgeSource" in ps1_text
        assert "pruned (source)" in ps1_text

    def test_purge_covers_all_four_prefixes(self, ps1_text: str):
        for p in (
            "today_signals_",
            "pipeline_",
            "dashboard_bundle_",
            "polygon_daily_coverage_",
            "narrative_",
        ):
            assert f'"{p}"' in ps1_text, f"prune 対象 prefix `{p}` が欠落"

    def test_branch_target_unchanged(self, ps1_text: str):
        assert "claude/monitor-webapp" in ps1_text

    def test_autolatest_switch_present(self, ps1_text: str):
        assert "$AutoLatest" in ps1_text, "self-heal -AutoLatest param が欠落"

    def test_autolatest_picks_newest_today_signals(self, ps1_text: str):
        assert "today_signals_*.json" in ps1_text
        assert "today_signals_(\\d{8})" in ps1_text

    def test_autolatest_does_not_regenerate_volatile_account_snapshot(
        self, ps1_code: str
    ):
        auto_latest = ps1_code.index("if ($AutoLatest)")
        disable_refresh = ps1_code.index("$RefreshAccount = $false", auto_latest)
        refresh_block = ps1_code.index("if ($RefreshAccount)", disable_refresh)
        assert auto_latest < disable_refresh < refresh_block

    def test_idempotent_via_tree_compare(self, ps1_code: str):
        """再実行安全: private index の tree == base tree なら commit/push しない."""
        assert "git write-tree" in ps1_code, "差分ゲート用の write-tree が欠落"
        assert "$newTree -eq $baseTree" in ps1_code, "tree 比較による冪等ゲートが欠落"


class TestPublishHardening20260730:
    """2026-07-30 恒久修正の hardening 契約 (root-cause fix 後も維持)."""

    def test_single_instance_mutex_guard(self, ps1_text: str):
        assert "System.Threading.Mutex" in ps1_text, "二重起動ガード (Mutex) が欠落"

    def test_uses_private_git_index(self, ps1_text: str):
        assert "GIT_INDEX_FILE" in ps1_text, "専用 index (GIT_INDEX_FILE) が欠落"
        assert (
            "read-tree $BaseCommit" in ps1_text
        ), "専用 index を base tree から seed していない"

    def test_stale_lock_reaped_conditionally(self, ps1_text: str):
        assert "index.lock" in ps1_text
        assert "StaleLockSeconds" in ps1_text
        assert "Get-Process" in ps1_text

    def test_git_retry_wrapper_present(self, ps1_text: str):
        assert "Invoke-GitRetry" in ps1_text, "git retry ラッパが欠落"
        assert "New-DataCommitOnBase" in ps1_text, "commit 生成関数が欠落"

    def test_post_publish_verification(self, ps1_text: str):
        assert "Test-PublishServed" in ps1_text, "publish 後 verify が欠落"
        assert "Get-NewestSignalDateInRef" in ps1_text, "served(ref) 参照が欠落"
        assert "git ls-tree" in ps1_text

    def test_bundle_preflight_runs_before_private_index_staging(self, ps1_code: str):
        preflight = ps1_code.index("prepare_dashboard_bundle.py")
        staging = ps1_code.index("git hash-object -w")
        assert preflight < staging
        assert "--require-exit" in ps1_code
        assert "$bundleExit -ne 0" in ps1_code

    def test_manifest_is_staged_with_the_bundle(self, ps1_text: str):
        assert '"dashboard_bundle_$DateCompact.json"' in ps1_text

    def test_same_date_publish_uses_exact_blob_verification(self, ps1_code: str):
        assert "git hash-object -- $localPath" in ps1_code
        assert "git rev-parse" in ps1_code
        assert "blob mismatch" in ps1_code

    def test_notify_only_on_failure(self, ps1_text: str):
        assert "Send-PublishNtfy" in ps1_text
        assert "verify FAIL" in ps1_text
