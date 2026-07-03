# D3 Case A commit + push 手順 (Windows PowerShell)

**dispatch 完了日**: 2026-07-03
**branch**: `claude/monitor-webapp`
**target**: origin `claude/monitor-webapp` (direct push, no PR — solo dev workflow)

---

## Step 1: pytest 全 pass 確認 (Windows 実機)

sandbox は working tree の一部で mount stale のため pytest 実行不能。Windows 実機で先に確認:

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# 1. D3 直接関連 test を先に流す (fail 判定を早く得るため)
py -m pytest tests\test_d3_case_a_liquidity_filter.py tests\test_d3_docs_alignment.py tests\test_systems_filter_setup_spec_compliance.py -x -v

# 2. regression 影響範囲確認 (core/system5, common/*, strategies/system5) の関連 test
py -m pytest tests\test_system5.py tests\test_setup_predicates.py tests\test_symbol_universe_filter.py tests\test_today_filters.py -x -v

# 3. 全 test scan (時間ある時)
py -m pytest tests\ -q --tb=short
```

期待: 3 段階すべて **0 fail, 0 error**。redirect の警告は許容。

もし赤くなる test があれば、以下を確認:
- `test_systems_filter_setup_spec_compliance.py::TestSystem5Filter::test_atr_pct_boundary` が旧 2.5% 前提のまま → Case A で更新済のはず。stale なら pull し直し
- 既存 fixture が新 filter (avgvolume50/dollarvolume50 追加要件) で filter=False になる → fixture の該当行に spec 通過値を追加
- 予期しない失敗 → 私に連絡。

---

## Step 2: 変更内容を staged で確認

```powershell
cd C:\Repos\quant_trading_system_0510to0906
git status
git diff --stat
```

期待される変更ファイル一覧:

**code (working tree、前 dispatch で反映済み)**
- `core/system5.py`
- `common/system_setup_predicates.py`
- `common/system_constants.py`
- `common/today_signals.py`

**test**
- `tests/test_systems_filter_setup_spec_compliance.py` (modified)
- `tests/test_d3_case_a_liquidity_filter.py` (new)
- `tests/test_d3_docs_alignment.py` (new)

**docs**
- `docs/systems/システム1.txt` ~ `システム7.txt` (7 file、change log 追記)
- `docs/D3_CASE_A_IMPL_20260703.md` (new)
- `docs/D3_CASE_A_COMMIT_INSTRUCTIONS_20260703.md` (new — このファイル)

---

## Step 3: commit

```powershell
cd C:\Repos\quant_trading_system_0510to0906
git add core/system5.py common/system_setup_predicates.py common/system_constants.py common/today_signals.py
git add tests/test_systems_filter_setup_spec_compliance.py tests/test_d3_case_a_liquidity_filter.py tests/test_d3_docs_alignment.py
git add docs/systems/システム1.txt docs/systems/システム2.txt docs/systems/システム3.txt docs/systems/システム4.txt docs/systems/システム5.txt docs/systems/システム6.txt docs/systems/システム7.txt
git add docs/D3_CASE_A_IMPL_20260703.md docs/D3_CASE_A_COMMIT_INSTRUCTIONS_20260703.md

git commit -m "fix(system5): D3 Case A - enforce docs spec (ATR>4%, AvgVol50>500k, DV50>2.5M)

- core/system5.py: MIN_AVG_VOLUME_50 (500k) / MIN_DOLLAR_VOLUME_50 (2.5M) 追加、
  DEFAULT_ATR_PCT_THRESHOLD 0.025→0.04、_apply_filter_conditions に 2 条件追加
- common/system_setup_predicates.py: system5_setup_predicate 同期 (+DEFAULT_ATR_PCT_THRESHOLD 0.04)
- common/system_constants.py: SYSTEM5_MIN_DOLLAR_VOLUME 25M→2.5M (dead constant 是正)、
  SYSTEM5_ATR_PCT_THRESHOLD 0.025→0.04、SYSTEM5_MIN_AVG_VOLUME_50 (500k) 新設、
  SYSTEM5_REQUIRED_INDICATORS に avgvolume50/dollarvolume50 追加、SYSTEM_CONFIGS 配線
- common/today_signals.py: diagnostic block に 'gate は core に格上げ済み' コメント追記
- tests/: test_d3_case_a_liquidity_filter.py (新規、System5 特化 boundary)、
  test_d3_docs_alignment.py (新規、7 system 横断 alignment matrix)、
  test_systems_filter_setup_spec_compliance.py (System5 閾値 4% 化)
- docs/systems/システム{1..7}.txt: 2026-07-03 D3 alignment update 変更履歴を追記
  (System5 は主要 log、他は verify only の追記)
- docs/D3_CASE_A_IMPL_20260703.md: 実装レポート (before/after 表 + 影響 sim + 確認手順)

判断根拠: docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md (Case A/B/C 比較) の
Case A 採用 (ユーザ判断: 事業重視 = 設計思想の透明性優先)。5y proxy sim で
top-20 候補 236→44 / unique 銘柄 73→16 (~19%) が予想される。実運用スリッページ
risk 解消が目的。"
```

---

## Step 4: push

```powershell
cd C:\Repos\quant_trading_system_0510to0906
git push origin claude/monitor-webapp
```

もし `non-fast-forward` エラーが出たら:

```powershell
git fetch origin
git log --oneline HEAD..origin/claude/monitor-webapp   # remote に何が来てるか確認
# 通常はここに他の作業が挟まってないはず (solo dev)
# 万一必要なら rebase:
git pull --rebase origin claude/monitor-webapp
# rebase 後は必ず pytest を再走らせる:
py -m pytest tests\test_d3_case_a_liquidity_filter.py tests\test_d3_docs_alignment.py -x -v
git push origin claude/monitor-webapp
```

---

## Step 5: 明日 06:00 tick 後の filter 数変化確認方法

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# 5.1 daily_polygon_monitor / today_signals 実行結果 (pipeline_YYYYMMDD.json) の system5 phase 数を確認
Get-Content .\reports\pipeline_20260704.json | ConvertFrom-Json |
    Select-Object -ExpandProperty pipeline_by_system |
    Where-Object { $_.system -eq "sys5" } |
    ForEach-Object {
        [PSCustomObject]@{
            system  = $_.system
            FILpass = ($_.phases | Where-Object name -eq "FILpass").count
            STUpass = ($_.phases | Where-Object name -eq "STUpass").count
            TRDlist = ($_.phases | Where-Object name -eq "TRDlist").count
            Entry   = ($_.phases | Where-Object name -eq "Entry").count
        }
    } | Format-Table -AutoSize

# 5.2 前日 (Case B 時代) との比較 (Case B の当日 log が残ってれば)
$prev  = Get-Content .\reports\pipeline_20260703.json | ConvertFrom-Json
$today = Get-Content .\reports\pipeline_20260704.json | ConvertFrom-Json
$prevS5  = $prev.pipeline_by_system  | Where-Object system -eq "sys5"
$todayS5 = $today.pipeline_by_system | Where-Object system -eq "sys5"
$prevF   = ($prevS5.phases  | Where-Object name -eq "FILpass").count
$todayF  = ($todayS5.phases | Where-Object name -eq "FILpass").count
$prevT   = ($prevS5.phases  | Where-Object name -eq "TRDlist").count
$todayT  = ($todayS5.phases | Where-Object name -eq "TRDlist").count
Write-Host "FILpass:  prev=$prevF   today=$todayF   ratio=$([math]::Round($todayF / [math]::Max(1,$prevF) * 100, 1))%"
Write-Host "TRDlist:  prev=$prevT   today=$todayT   ratio=$([math]::Round($todayT / [math]::Max(1,$prevT) * 100, 1))%"

# 5.3 today_signals.py の "🧪 system5集計" line で AvgVol50 / DV50 / ATR 4% の pass 数を確認
Get-ChildItem .\logs\today_signals_20260704*.log | ForEach-Object {
    Write-Host "=== $($_.Name) ==="
    Select-String -Path $_.FullName -Pattern "system5集計|system5セットアップ集計"
}
```

**期待値** (proxy sim 相対、5y sample):
- `FILpass sys5`: 前日比 ~15-20% レベル
- `TRDlist sys5`: 同水準 ~19%
- `s5_av` / `s5_dv` / `s5_atr` diagnostic 数はほぼ同じ (診断のみのため)
- `Entry sys5`: 0-1 銘柄 (以前は 1-3 銘柄) 想定。ゼロ日数が増える。

**もし FILpass=0 が連続 3 日以上続いたら**:
- 相場が単に mean-reversion モードに入っていない可能性が高い (System5 は高 ADX 環境限定)
- 4% ATR 銘柄が消えている (低ボラ相場) → 想定内挙動
- 数週間続けば `DEFAULT_ATR_PCT_THRESHOLD` を config 化して 3-3.5% で trial する余地あり
  (D3 report の "後続 phase" ロードマップ参照)

---

## Rollback (万一の safety net)

```powershell
cd C:\Repos\quant_trading_system_0510to0906
# 1. 直前 commit を revert
git revert HEAD --no-edit
git push origin claude/monitor-webapp

# 2. or force rollback (履歴も消したい場合、solo dev だから可)
git reset --hard HEAD~1
git push --force-with-lease origin claude/monitor-webapp
```
