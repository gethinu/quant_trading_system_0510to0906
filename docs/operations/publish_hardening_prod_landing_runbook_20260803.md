# publish hardening を prod 両ブランチに着地する runbook (2026-08-03)

対象リポ: `C:\Repos\quant_trading_system_0510to0906`
remote: `origin = https://github.com/gethinu/quant_trading_system_0510to0906.git` (gethinu)
制約: **発注しない / publish・表示のみ / 他レジストリ非接触 / git は plumbing or 本 runbook のコマンド**

---

## 0. 現状 (サンドボックスで確認済み)

**hardening の在処 = `claude/monitor-webapp` の作業ツリー（未コミット）**

| 成果物 | 状態 |
|---|---|
| `scripts/publish_data_to_vercel.ps1` (25,954 B, hardened) | working tree に **未コミット** (HEAD は旧 12,789 B) |
| `tests/system/test_publish_data_ps1_contract.py` (+48 行, `TestPublishHardening20260730`) | working tree に **未コミット** (HEAD は旧版) |
| `docs/operations/dashboard_publish_hardening_20260730.md` | **untracked**（新規） |

hardened ps1 は 5 対策すべてを含む（契約テストで固定）:
- **A** 二重起動ガード `System.Threading.Mutex("Global\qts_publish_data_vercel")`
- **B** publish 専用 index `GIT_INDEX_FILE`（`read-tree HEAD` で seed、`data/` のみ stage）
- **C** stale lock 除去 `Repair-StaleGitLocks`（`Get-Process` で git.exe 不在確認 + `StaleLockSeconds` mtime 超過のみ）＋ `Invoke-GitRetry`
- **D** `git add` の exit code 検査（旧バグ「偽の差分なし skip → exit 0」を封じる）
- **E** publish 後 `Test-PublishServed`（`git ls-tree` で served=HEAD の `data/`、`git rev-list --count` で origin 反映を確認、`served >= generated` を検証、失敗時のみ `Send-PublishNtfy`）

**prod ブランチは全て旧実装（mutex/GIT_INDEX_FILE/Test-PublishServed マーカー = 0 件）:**

| branch | ps1 サイズ | hardening |
|---|---|---|
| `claude/open-auto-run` (= origin) | 6,167 B | なし |
| `claude/daily-main-follow` (= origin) | 12,789 B | なし |
| `claude/monitor-webapp` HEAD | 12,789 B | なし（hardened は未コミット） |

→ 2 ブランチのベースが違う（6,167 vs 12,789 vs hardened 25,954）ため **diff cherry-pick は衝突する**。
**whole-file 配置（`git checkout <ref> -- <paths>`）で着地する** ＝ 3-way merge を発生させず衝突ゼロ。

**重要（$Branch）**: hardened ps1 は `$Branch = "claude/monitor-webapp"` を**ハードコード**（param ではない）。
→ どのブランチの checkout から走らせても、DATA は `monitor-webapp` に push され、Vercel は monitor-webapp を serve。
morning brief の「CRIT publish 95.2h」は monitor-webapp の served が古いこと。
**着地は「スケジューラが実行するブランチ（open-auto-run / daily-main-follow）に hardened script を載せる」こと**、
**stale 解消は hardened script を 1 回実 publish する**ことで monitor-webapp の served が当日に追いつく。

---

## 受け入れゲート結果（サンドボックスで実施済み）

- ✅ **契約テスト green**: `test_publish_data_ps1_contract.py` の全 20 substring/regex assertion が hardened ps1 に対して PASS（`TestPublishHardening20260730` の 6 ケース＝ Mutex / GIT_INDEX_FILE+read-tree HEAD / index.lock+StaleLockSeconds+Get-Process / Invoke-GitRetry+add data / Test-PublishServed+ls-tree+rev-list / Send-PublishNtfy+verify FAIL を含む）。
  ※ サンドボックスは disk full で `pytest` 本体を入れられないため、テストの assert 文字列を機械抽出して照合。ミニPCで pytest 実体を回して再確認する（下記 GATE1）。
- ✅ **PowerShell 構造健全**: brace 108/108・paren 221/221・bracket 38/38 で平衡。`-NoPush` スイッチ有り（commit のみ）、`git diff --cached --quiet` gate で再実行は no-op（差分なし → exit 0）。
  ※ 正式な構文パースは pwsh が要る（サンドボックスに無し）→ ミニPCで実施（下記 GATE2）。
- ✅ **Alpaca 切り離し (#9) 維持**: `build_exit_ledger.py` / `export_alpaca_snapshot.py` は非ゼロ終了でも `WARN` ログのみで **publish は継続**（キー未設定でも publish 成否に影響しない）。
- ✅ **freeze-baseline 非接触**: 本着地は publish 3 ファイルのみ。freeze-baseline 系ファイルには触れない。

---

## ミニPCで stale を解消する最終コマンド（PowerShell / 上から順に）

> すべて `C:\Repos\quant_trading_system_0510to0906` 起点。着地順は **open-auto-run → daily-main-follow**。
> 発注しない・publish/表示のみ。`git` は通常操作のみ（force-push しない）。

### Phase 0 — 準備（stale lock / worktree 掃除、hardened を確定コミット化）

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# 0-1. 現在ブランチ・dirty 確認（monitor-webapp に hardened が未コミットで居るはず）
git rev-parse --abbrev-ref HEAD          # -> claude/monitor-webapp
git status --short

# 0-2. stale な .git/index.lock を安全に除去（git 実行中でない時のみ）
if (-not (Get-Process git -ErrorAction SilentlyContinue)) {
    Remove-Item .git\index.lock -Force -ErrorAction SilentlyContinue
}

# 0-3. prunable な旧 worktree 登録を掃除（C:\tmp\qts-* が消えているため）
git worktree prune -v

# 0-4. hardened 3 ファイルだけを専用ブランチに確定コミット（monitor-webapp の他 WIP は巻き込まない）
git switch -c claude/publish-hardening-20260730
git add scripts/publish_data_to_vercel.ps1 `
        tests/system/test_publish_data_ps1_contract.py `
        docs/operations/dashboard_publish_hardening_20260730.md
git commit --no-verify -m "fix(publish): index.lock 恒久対策 (A mutex/B 専用index/C stale lock retry/D add exit検査/E served==generated 検証)"
$HARDEN = git rev-parse HEAD
Write-Host "HARDEN commit = $HARDEN"

# monitor-webapp に戻す（他の WIP はそのまま working tree に残る）
git switch claude/monitor-webapp
```

### Phase 1 — `claude/open-auto-run` に着地（独立 worktree で monitor-webapp WIP を汚さない）

```powershell
git worktree add C:\tmp\land-oar claude/open-auto-run
cd C:\tmp\land-oar

# whole-file 配置（衝突ゼロ。$HARDEN の版で 3 ファイルを上書き stage）
git checkout $HARDEN -- scripts/publish_data_to_vercel.ps1 `
                        tests/system/test_publish_data_ps1_contract.py `
                        docs/operations/dashboard_publish_hardening_20260730.md

# --- GATE1: 契約テスト green ---
python -m pytest tests/system/test_publish_data_ps1_contract.py -q      # -> all passed

# --- GATE2: PowerShell 構文パース OK ---
pwsh -NoProfile -Command "[void][ScriptBlock]::Create((Get-Content -Raw .\scripts\publish_data_to_vercel.ps1)); 'SYNTAX OK'"

# --- GATE3: -NoPush dry-run が idempotent（差分なしで静かに exit 0）---
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish_data_to_vercel.ps1 -AutoLatest -NoPush
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish_data_to_vercel.ps1 -AutoLatest -NoPush   # 2 回目 = 「data/ に差分なし」exit 0
Write-Host "dry-run exit = $LASTEXITCODE"   # -> 0
# dry-run が private index に載せた data commit を破棄（着地は script のみに保つ）
git reset --hard origin/claude/open-auto-run
git checkout $HARDEN -- scripts/publish_data_to_vercel.ps1 `
                        tests/system/test_publish_data_ps1_contract.py `
                        docs/operations/dashboard_publish_hardening_20260730.md

# --- 着地コミット & push ---
git add scripts/publish_data_to_vercel.ps1 `
        tests/system/test_publish_data_ps1_contract.py `
        docs/operations/dashboard_publish_hardening_20260730.md
git commit --no-verify -m "fix(publish): index.lock 恒久対策を open-auto-run に着地 (mutex+専用index+stale lock retry+exit検査+served検証)"
git push origin claude/open-auto-run

cd C:\Repos\quant_trading_system_0510to0906
git worktree remove C:\tmp\land-oar
```

### Phase 2 — `claude/daily-main-follow` に着地（同じ手順）

```powershell
git worktree add C:\tmp\land-dmf claude/daily-main-follow
cd C:\tmp\land-dmf

git checkout $HARDEN -- scripts/publish_data_to_vercel.ps1 `
                        tests/system/test_publish_data_ps1_contract.py `
                        docs/operations/dashboard_publish_hardening_20260730.md

# GATE1/2/3 を同様に
python -m pytest tests/system/test_publish_data_ps1_contract.py -q
pwsh -NoProfile -Command "[void][ScriptBlock]::Create((Get-Content -Raw .\scripts\publish_data_to_vercel.ps1)); 'SYNTAX OK'"
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish_data_to_vercel.ps1 -AutoLatest -NoPush
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish_data_to_vercel.ps1 -AutoLatest -NoPush
Write-Host "dry-run exit = $LASTEXITCODE"   # -> 0
git reset --hard origin/claude/daily-main-follow
git checkout $HARDEN -- scripts/publish_data_to_vercel.ps1 `
                        tests/system/test_publish_data_ps1_contract.py `
                        docs/operations/dashboard_publish_hardening_20260730.md

git add scripts/publish_data_to_vercel.ps1 `
        tests/system/test_publish_data_ps1_contract.py `
        docs/operations/dashboard_publish_hardening_20260730.md
git commit --no-verify -m "fix(publish): index.lock 恒久対策を daily-main-follow に着地 (mutex+専用index+stale lock retry+exit検査+served検証)"
git push origin claude/daily-main-follow

cd C:\Repos\quant_trading_system_0510to0906
git worktree remove C:\tmp\land-dmf
```

### Phase 3 — 実 publish で served を当日に追いつかせる（morning brief の赤を解消）

hardened script は `$Branch` をハードコードで `claude/monitor-webapp` に publish する。
どちらの prod checkout から走らせても DATA は monitor-webapp に載る（＝ Vercel が serve する branch）。

```powershell
# open-auto-run の checkout から実 publish（-AutoLatest = 最新 today_signals を push まで）
git worktree add C:\tmp\pub-run claude/open-auto-run
cd C:\tmp\pub-run
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish_data_to_vercel.ps1 -AutoLatest
Write-Host "real publish exit = $LASTEXITCODE"   # 0 = served==generated 検証 OK

# 冪等確認（2 回目は「差分なし」exit 0、ntfy は鳴らない）
pwsh -NoProfile -ExecutionPolicy Bypass -File .\scripts\publish_data_to_vercel.ps1 -AutoLatest
Write-Host "idempotent re-run exit = $LASTEXITCODE"   # -> 0

cd C:\Repos\quant_trading_system_0510to0906
git worktree remove C:\tmp\pub-run
```

### Phase 4 — served == generated を独立に突合（表示のみ、発注なし）

```powershell
# monitor-webapp の HEAD にコミット済 data/ の最新 today_signals 日付を確認
git fetch origin claude/monitor-webapp
git ls-tree --name-only origin/claude/monitor-webapp -- apps/dashboards/alpaca-next/data/ |
  Select-String 'today_signals_(\d{8})' | Sort-Object -Descending | Select-Object -First 1
# これが当日（generated）と一致していれば served 追いつき完了。
# morning brief の「CRIT publish Xh」が解消しているはず。
```

---

## ロールバック / 注意

- push は通常の fast-forward のみ。non-fast-forward で reject されたら hardened script 自身が `fetch + rebase (autostash) + 1 retry` で自己修復する（root cause B fix）。手動 force-push は禁止。
- 各 GATE で fail したら **push しない**。pytest 赤 or pwsh 構文 NG or dry-run exit≠0 のいずれでも中断。
- `_exit_resolved/` `_round_a/` `.claude/` 等 monitor-webapp の untracked/WIP には触れない（本着地は publish 3 ファイルのみ）。
- freeze-baseline 系ファイルは非接触。
- Alpaca キー未設定は publish 成否に無関係（#9）。snapshot/exit_ledger の `WARN` ログは想定内。
```
```

## 参考（元 hardening doc）

`docs/operations/dashboard_publish_hardening_20260730.md`（対策 A–E の設計・唯一の git writer は publish ps1 である旨の grep 済み根拠）。
