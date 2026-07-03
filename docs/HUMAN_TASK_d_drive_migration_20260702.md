# HUMAN TASK — data_cache を D: ドライブへ移設 (2026-07-02)

`C:\Repos\quant_trading_system_0510to0906\data_cache\` を D: 側の物理ストレージへ移し、
リポジトリ側は symlink (or 設定 override) 経由でアクセスするための **runbook**。

翌 06:00 JST の Task Scheduler tick (`register_task_scheduler.ps1` 由来の daily_pipeline)
までに完了させるか、あるいはその日を skip する判断を先に決める。

このドキュメントは **plan / runbook** であり、Claude 側では **実 move を実施していない**。
実行は user が下記手順を見て手動で行う。

---

## 0. なぜ移すか (goal)

- C: SSD (OS + Repo) の空き逼迫を回避
- feather ファイル 47,000+ 個 × 数十 KB の inode 圧を C: から外す
- `MT5_R2_Gaitame` (`D:\MT5_R2_Gaitame\`) が既に D: 常駐なので、D: は運用実績あり
- 明日以降の rolling / base cache 増加を D: で受ける

**scope 外**:

- Feather → CSV 化 (§5 で別 issue へ切る判断根拠を書く)
- `core/system1-7` のコード修正 (触らない)
- `common/alpaca_trading.py` の修正 (触らない)

---

## 1. 現状 sizing (2026-07-02 確認)

sandbox mount 経由での測定値。**Windows 実 FS 上では NTFS cluster size / sparse / ADS の関係で
数値が異なる可能性があるため、実行前に §1.1 の PowerShell で必ず再測定すること。**

### 1.0 mount 経由測定 (参考値)

| path                                  | size   | file 数 | 内訳                              |
|---------------------------------------|--------|---------|-----------------------------------|
| リポジトリ総計                        | 1.3 GB | —       | 下記の合計                        |
| `data_cache/`                         | 873 MB | 47,074  | ↓ 内訳                            |
| `data_cache/base/`                    | 237 MB | 15,692  | feather 15,691 + csv 1            |
| `data_cache/full_backup/`             | 161 MB | 15,691  | csv 15,691                        |
| `data_cache/rolling/`                 | 476 MB | 31,382  | feather 15,691 + csv 15,691       |
| `data_cache/indicators_system7_cache/`| 空     | 0       | 実行時再生成 (system7.py)         |
| `data_cache/signals/`                 | 空     | 0       | 実行時生成                        |
| `apps/dashboards/`                    | 298 MB | —       | node_modules 想定 (移さない)      |
| `.git/`                               | 78 MB  | —       | 移さない                          |
| `logs/`                               | 6.5 MB | —       | rotate 前提、移さない             |
| `results_csv/`                        | 104 KB | —       | 毎日再生成、移さない              |

**data_cache 全体を D: に移せば C: が約 873 MB (mount 実測) ~ 数 GB (Windows 実測次第) 空く見込み。**

user memory では「3 GB+」との記述だが sandbox mount では 873 MB。
NTFS の allocation-unit や cluster slack で膨らんでいる可能性が高い (47k 小ファイル)。
§1.1 で確定させる。

### 1.1 実行前に走らせて数字を確定させる PowerShell

```powershell
# --- (1) ドライブ空き ---
Get-PSDrive -PSProvider FileSystem | Where-Object { $_.Name -in 'C','D' } |
  Select-Object Name, @{n='Used(GB)';e={[math]::Round($_.Used/1GB,2)}},
                        @{n='Free(GB)';e={[math]::Round($_.Free/1GB,2)}}

# --- (2) 物理メディア (SATA vs NVMe 判定) ---
Get-PhysicalDisk | Select-Object DeviceId, FriendlyName, MediaType, BusType, Size

# --- (3) data_cache 内訳 (実測 & 47k 小ファイルの cluster slack 込み) ---
$root = 'C:\Repos\quant_trading_system_0510to0906\data_cache'
Get-ChildItem $root -Directory | ForEach-Object {
  $bytes  = (Get-ChildItem $_.FullName -Recurse -File -EA SilentlyContinue |
             Measure-Object Length -Sum).Sum
  $count  = (Get-ChildItem $_.FullName -Recurse -File -EA SilentlyContinue |
             Measure-Object).Count
  [PSCustomObject]@{
    Dir       = $_.Name
    Size_MB   = [math]::Round($bytes/1MB, 1)
    Files     = $count
  }
} | Format-Table -AutoSize

# --- (4) D: の MT5_R2_Gaitame 使用量と空き ---
if (Test-Path 'D:\MT5_R2_Gaitame') {
  $mt5 = (Get-ChildItem 'D:\MT5_R2_Gaitame' -Recurse -File -EA SilentlyContinue |
          Measure-Object Length -Sum).Sum
  "MT5_R2_Gaitame: $([math]::Round($mt5/1GB,2)) GB"
}

# --- (5) 予定移動先の path 衝突チェック ---
'D:\quant_data_cache' | ForEach-Object {
  if (Test-Path $_) { "EXISTS: $_" } else { "AVAILABLE: $_" }
}
```

このブロック全体を PowerShell (**管理者権限不要**) で流し、出力を貼れば
「移せる / どの案が最適か」の判断が確定する。

---

## 2. 移動対象の path 一覧

| path                                   | 判定       | 理由                                                     |
|----------------------------------------|------------|----------------------------------------------------------|
| `data_cache/base/`                     | **移す**   | 237 MB, 15k feather。読み専 (rebuild 可)。               |
| `data_cache/full_backup/`              | **移す**   | 161 MB, 15k csv。base の raw backup。                    |
| `data_cache/rolling/`                  | **移す**   | 476 MB, 31k file。build_rolling.py の主 target。         |
| `data_cache/indicators_system7_cache/` | **移す**   | 空だが `core/system7.py:58` が実行時に作る (§4b 参照)。 |
| `data_cache/signals/`                  | **移す**   | `outputs.signals_dir` (yaml)。実行時に書かれる。         |
| `data_cache/history/`                  | (未存在)   | 現在は無い。将来出来ても symlink 経由なら追従する。      |
| `data_cache_recent/`                   | 保留       | 現在空。scope 外。                                       |
| ---                                    | ---        | ---                                                      |
| `common/`, `core/`, `scripts/`, `apps/`| 移さない   | source code。                                            |
| `.git/`                                | 移さない   | 78 MB。versioning に必要。                               |
| `results_csv/`                         | 移さない   | 毎日再生成、104 KB。                                     |
| `logs/`                                | 移さない   | rotate 前提、6.5 MB。                                    |

**方針**: `data_cache/` **配下丸ごと** を D: に移す。個別 subdir 単位で分ける必要は無い
(全部 D: なら symlink 1 本で済む)。

---

## 3. 実装方式 — **3 案の trade-off**

### 案 A: Symlink (`mklink /D`) — **推奨**

`C:\Repos\quant_trading_system_0510to0906\data_cache` → `D:\quant_data_cache` の
ディレクトリ junction を貼る。

**pros**:

- **コード変更 0 行**。相対 path `"data_cache/..."` を書いている
  `core/system7.py:58` (`cache_dir = "data_cache/indicators_system7_cache"`) も
  自動追従する。**←これが重要**。ユーザー制約「core/system1-7 は触らない」に唯一適合する案。
- `.env` / `config.yaml` を書き換えなくて済む
- 再起動後も維持される (junction は NTFS 属性)
- rollback がほぼ瞬時 (symlink 削除 + robocopy back)

**cons**:

- `git status` が symlink target を追跡する場合がある。ただし現状 `data_cache/` は
  `.gitignore` 済 (`.gitignore` を要確認、§8.1) なので実害は無い見込み。
- Windows Backup / OneDrive などが symlink を辿ってしまい二重バックアップになる可能性。
  → 現状 C: 側の Backup 設定を要確認。
- junction は同一ドライブ限定ではないので OK (mklink `/J` ではなく `/D` を使う)。

### 案 B: 環境変数 + `config.yaml` 経由

`.env` の `DATA_CACHE_DIR=D:\quant_data_cache` と `config/config.yaml` の
`data.cache_dir`, `data.cache.full_dir`, `data.cache.rolling_dir`, `outputs.signals_dir`
の 4 箇所を書き換える。

**pros**:

- 意図が config に明示される (誰が見ても「D: 使ってる」と分かる)
- Cross-platform 対応の下地になる (Linux 移植時に env で振れる)

**cons**:

- **`core/system7.py:58` の hard-coded `"data_cache/indicators_system7_cache"` は
  この方式では追従しない**。system7 実行時 CWD 下の相対 path として C: に書かれる。
  → 修正には core/system7.py の edit が必要 = **ユーザー制約違反**。
- yaml 4 箇所 + `.env` 1 箇所 = 5 file を整合的に書き換え必要
- 既に hard-coded な他 file (`common/utils_spy.py`, `common/utils.py` 等の
  `folder="data_cache"` default 引数) が誰かの呼び出しで露出してないか要検証

### 案 C: ハイブリッド (symlink + config も揃える)

案 A + 案 B を両方適用。

**pros**:

- symlink が safety net。config も意図を残す。
- 将来 config を絶対 path で読み替えたくなった時、症状が最小。

**cons**:

- 二重管理。config 変更時に symlink 側と乖離するリスク。

---

### **推奨: 案 A (symlink 単独)**

理由:

1. **`core/system7.py` の hard-coded relative path** を触らずに済む唯一の案。
2. コード / config 差分ゼロ = code review 不要、CI green のまま。
3. 万一戻したくなった時、robocopy back + symlink 削除で終わる。

**判断ポイント (user 確認)**:

- [ ] `.gitignore` に `data_cache/` が含まれているか (§8.1)
- [ ] `data_cache/` が Windows Backup / OneDrive 同期対象になっていないか
- [ ] D: が **NVMe** か SATA か (§1.1 `Get-PhysicalDisk` で確認、§5 の判断にも使用)

---

## 4. Feather 維持 vs CSV 化検討

user 発言:「D: に置くならフェザーにする必要もなくなるよ」

### 4.1 Feather の value proposition

- `build_rolling.py` は SPY を anchor に 7,000 銘柄を並列 read → 現行 **1:30 で完了**。
- feather は columnar 二値バイナリ (Arrow IPC 形式)。pandas が **memcpy 相当** で読み込める。
- csv は text parse (数値変換 + strtod + タイムスタンプ parse) が必要。同じ 7,000 銘柄で
  **推定 5-10 倍** の読み込み時間 = build_rolling が **5-10 分** に伸びる。

### 4.2 D: が NVMe か SATA かで判断分岐

`Get-PhysicalDisk` の `MediaType` / `BusType` 出力で判定:

| D: の種類 | feather の速度優位 | 推奨            |
|-----------|--------------------|-----------------|
| NVMe SSD  | CPU decode がボトル → feather でも csv でも大差無し | **feather 維持** (安全側) |
| SATA SSD  | I/O 帯域が効く → feather 優位残る | **feather 維持**          |
| HDD       | seek 大 → feather **圧倒的**優位   | **feather 必須**          |

**いずれのケースでも feather 維持が合理的。**「D: だから csv でいい」は正しくない。
理由は **rolling read が I/O bound ではなく decode CPU bound** だから。

### 4.3 現状の冗長性 (別 issue 化候補)

`rolling/` は同一銘柄で `.feather` + `.csv` の **両方** が存在 (15,691 pair)。
どちらか片方あれば十分。ざっと **250 MB 削減余地**。

- feather 側を残す (速度優先)
- csv 側は `full_backup/` にも別 copy がある

**scope 外**: 「rolling/ csv を全削除して feather 単一にする」は別 issue で。
今回 migration 完了後、動作確認できてから判断。

---

## 5. 実行 timing / downtime

- daily_pipeline は **06:00 JST** に `register_task_scheduler.ps1` 由来の Task Scheduler で走る。
- migration 実行中は cache 書き込みが競合するため、**pipeline 停止**が必須。

### 5.1 実行 window の選び方

**選択肢 1: 今日 (2026-07-02) 夕方以降**
明日朝 06:00 tick 前に確実に完了 & 検証まで終える。
robocopy 873 MB × 47k files は SSD→SSD なら **5-15 分**、検証 5-10 分 → 計 20-30 分。

**選択肢 2: 週末 (次の土曜 07/04)**
時間に余裕。土曜 06:00 tick 自体が動くかは cron 設定確認要。
`config.yaml` の scheduler:
```yaml
- name: update_tickers
  cron: "0 6 * * 1-5"   # 月〜金
```
→ **土日は tick 走らない**ので週末が安全。

**推奨: 週末実行 (2026-07-04 土)**。焦って明日朝 06:00 に間に合わせる必要が無い。

### 5.2 明日 (2026-07-03 金) の Task Scheduler が動く前に間に合わない場合

- Task Scheduler の当該タスクを **1 回だけ disable** して土曜まで送る
- 手動 disable:

```powershell
Get-ScheduledTask | Where-Object { $_.TaskName -like '*quant*' -or $_.TaskName -like '*daily_pipeline*' } |
  Select TaskName, State
# 該当タスクを見つけて:
Disable-ScheduledTask -TaskName '<TaskName>'
# migration 完了後:
Enable-ScheduledTask  -TaskName '<TaskName>'
```

---

## 6. Rollback plan

### 6.1 migration 途中で失敗した場合

`data_cache_backup_YYYYMMDD/` を必ず保持しておく (§7.2 の robocopy 時に元 dir を rename)。

失敗検知: build_rolling の smoke test (§7.6) が fail、あるいは
`common/cache_manager.py` が `FileNotFoundError` を投げた場合。

### 6.2 復旧手順 (順に実行)

```powershell
# 1. symlink を削除
Remove-Item 'C:\Repos\quant_trading_system_0510to0906\data_cache' -Force

# 2. backup を戻す
Rename-Item 'C:\Repos\quant_trading_system_0510to0906\data_cache_backup_20260702' `
            'data_cache'

# 3. smoke test 再実行
cd C:\Repos\quant_trading_system_0510to0906
python -c "from common.cache_manager import CacheManager; from config.settings import get_settings; cm = CacheManager(get_settings(create_dirs=True)); print(cm.read_base('SPY').tail(3))"
```

### 6.3 D: 側 data を後片付け (rollback 確定後)

```powershell
Remove-Item 'D:\quant_data_cache' -Recurse -Force
```

---

## 7. 実行手順 — **step-by-step runbook** (案 A 前提)

### 7.0 事前準備

```powershell
# 前提: PowerShell 5.1+ / mklink は cmd 経由、または New-Item -ItemType Junction
# 前提: robocopy は Windows 標準
# 管理者 PowerShell を開く (mklink /D は管理者 or Developer Mode 有効時)
```

### 7.1 §1.1 の測定 PowerShell を実行して数値を確定

出力を確認して、D: 空きが `data_cache` サイズの **2 倍以上** あることを確認
(migration 中は C: と D: 両方に存在するため)。

### 7.2 pipeline 停止 & 現行 dir を backup 名で退避

```powershell
# --- (a) Task Scheduler tick 停止 ---
Get-ScheduledTask | Where-Object { $_.TaskName -like '*quant*' -or $_.TaskName -like '*daily*' } |
  ForEach-Object {
    Write-Host "Disabling: $($_.TaskName)"
    Disable-ScheduledTask -TaskName $_.TaskName
  }

# --- (b) 走ってる python 系プロセスが無いか確認 ---
Get-Process | Where-Object { $_.ProcessName -match 'python|streamlit|node' } |
  Select ProcessName, Id, StartTime

# 何か走ってたら手動で止める (ダッシュボード、streamlit 等)

# --- (c) data_cache を退避 rename (物理 move ではない、同一 vol なので瞬時) ---
cd C:\Repos\quant_trading_system_0510to0906
Rename-Item 'data_cache' 'data_cache_backup_20260702'
```

### 7.3 D: 側に移動先を作成 & dry-run

```powershell
# --- (a) 移動先 dir 作成 ---
New-Item -ItemType Directory -Path 'D:\quant_data_cache' -Force | Out-Null

# --- (b) dry-run (robocopy /L = list only) ---
robocopy 'C:\Repos\quant_trading_system_0510to0906\data_cache_backup_20260702' `
         'D:\quant_data_cache' `
         /E /L /NFL /NDL /NP /R:0 /W:0

# 出力の "Files: xxxxx" が §1.1 の file 数と一致するか確認
```

### 7.4 実 copy

```powershell
# /MIR = mirror (delete extras) 使わず、/E = subdir 含めコピーのみ
robocopy 'C:\Repos\quant_trading_system_0510to0906\data_cache_backup_20260702' `
         'D:\quant_data_cache' `
         /E /R:1 /W:1 /MT:16 /NP `
         /LOG:C:\Repos\quant_trading_system_0510to0906\logs\migration_20260702.log

# 想定所要: SSD→SSD で 5-15 分
# robocopy exit code: 0 or 1 = 成功、2+ = 要確認
Write-Host "robocopy exit code: $LASTEXITCODE"
```

### 7.5 symlink (junction) 作成

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# 方式 1: cmd 経由 (伝統的)
cmd /c 'mklink /D "data_cache" "D:\quant_data_cache"'

# 方式 2: PowerShell native (推奨、管理者権限不要な場合もあり)
# New-Item -ItemType Junction -Path 'data_cache' -Target 'D:\quant_data_cache'

# 確認: junction として見えるか
Get-Item data_cache | Select LinkType, Target, FullName
# 期待: LinkType=Junction, Target=D:\quant_data_cache
```

### 7.6 smoke test (**必須**)

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# --- (a) SPY base cache が D: 経由で読めるか ---
python -c "
from common.cache_manager import CacheManager
from config.settings import get_settings
cm = CacheManager(get_settings(create_dirs=True))
spy = cm.read_base('SPY')
print('SPY rows:', len(spy))
print('tail:'); print(spy.tail(3))
"

# --- (b) rolling も読めるか ---
python -c "
from common.data_loader import load_price
df = load_price('SPY', cache_profile='rolling')
print('rolling SPY tail:'); print(df.tail(3))
"

# --- (c) system7 の indicators_system7_cache dir が D: 側に作られるか ---
python -c "
from core.system7 import prepare_data_vectorized_system7
from common.data_loader import load_price
raw = {'SPY': load_price('SPY', cache_profile='rolling')}
out = prepare_data_vectorized_system7(raw)
print('system7 keys:', list(out.keys())[:3])
"
# → D:\quant_data_cache\indicators_system7_cache\ が生成されていれば成功
```

### 7.7 Task Scheduler 再開

```powershell
Get-ScheduledTask | Where-Object { $_.State -eq 'Disabled' -and (
  $_.TaskName -like '*quant*' -or $_.TaskName -like '*daily*'
) } | ForEach-Object {
  Write-Host "Enabling: $($_.TaskName)"
  Enable-ScheduledTask -TaskName $_.TaskName
}
```

### 7.8 24 時間後、確認取れたら backup を削除

```powershell
# 明日以降の daily_pipeline が正常完了したら:
Remove-Item 'C:\Repos\quant_trading_system_0510to0906\data_cache_backup_20260702' `
            -Recurse -Force
```

---

## 8. 検証 checklist (実行後)

### 8.1 `.gitignore` 確認

**確認済み (Claude, 2026-07-02)**: `.gitignore:2` に `data_cache/` 、`.gitignore:3` に
`data_cache_recent/` の記載あり。symlink 化後も git status がノイズを吐くリスクは無い。

推奨: `data_cache_backup_*/` を追加しておく (backup 名 pattern を将来使う場合に備え)。

```powershell
Add-Content C:\Repos\quant_trading_system_0510to0906\.gitignore 'data_cache_backup_*/'
```

### 8.2 build_rolling smoke run

```powershell
cd C:\Repos\quant_trading_system_0510to0906
python scripts/build_rolling_with_indicators.py --symbols SPY,AAPL,MSFT --dry-run
```

エラーなしで完了 & 出力 path が `D:\quant_data_cache\rolling\` を指していれば成功。

### 8.3 daily_pipeline 全体 dry-run (可能なら)

```powershell
python scripts/run_daily_pipeline.py --dry-run
# あるいは pipeline_output.txt を確認して、パス参照が D: 側になっているか
```

### 8.4 dashboard 起動 test

```powershell
python -m streamlit run apps/dashboards/app_alpaca_dashboard.py --server.headless true
# ブラウザで開き、cache 読み込みでエラーが出ないこと
# Ctrl+C で停止
```

---

## 9. user が **明日実行前に決める** 判断項目 checklist

- [ ] **実行 timing**: 明日夕方 (2026-07-03) or 週末 (07-04 土) → **推奨: 週末**
- [ ] **方式**: 案 A (symlink) / 案 B (env+yaml) / 案 C (両方) → **推奨: 案 A**
  - 決め手: `core/system7.py:58` の hard-coded 相対 path を触らないため
- [ ] **feather 維持**: yes / csv 化 → **推奨: 維持**
  - 決め手: rolling read は CPU decode bound、D: が SATA/NVMe いずれでも feather 優位
- [ ] **D: 空き容量**: `data_cache` 実サイズ × 2 以上を §1.1 で確認済み
- [ ] **`.gitignore`**: `data_cache/` エントリありを §8.1 で確認済み
- [ ] **backup 保持期間**: 24h 後 削除 / 1 週間保持 / 永続 → **推奨: 24h**
- [ ] **Task Scheduler**: 該当 task 名を §7.2 で確認済み
- [ ] **rollback 判断基準**: どこまで smoke test fail したら rollback するか事前合意
  - 提案: §7.6 (a)(b) いずれかが fail → 即 rollback

---

## 10. 参考: 触ったコード一覧 (この runbook では 0 行変更)

- 案 A では **ソース変更なし**
- 案 B/C を選ぶ場合の対象:
  - `.env` line 3: `DATA_CACHE_DIR=data_cache` → `DATA_CACHE_DIR=D:\quant_data_cache`
  - `config/config.yaml`:
    - `data.cache_dir` (l.85)
    - `data.cache.full_dir` (l.94)
    - `data.cache.rolling_dir` (l.95)
    - `outputs.signals_dir` (l.128)
  - **注意**: `core/system7.py:58` の hard-coded は残る (案 B の限界)

---

## 11. 開かれた質問 (Claude 側で確定できず、user 判断)

1. ~~`.gitignore` に `data_cache/` が載っているか~~ → **確認済 `.gitignore:2,3`**
2. Windows Backup / OneDrive の C: バックアップ対象範囲
3. D: の MediaType (NVMe vs SATA) — §1.1 (2) の出力次第
4. Task Scheduler の該当 task 名の正確な文字列
5. daily_pipeline を **明日** 動かす必要があるか、飛ばして良いか
6. `data_cache_recent/` (`.gitignore:3` にあり、現在空) は将来使う予定があるか — もし
   yes なら symlink 対象に含めるか個別判断

---

**作成**: 2026-07-02 (Claude, plan doc のみ)
**次アクション**: user が §1.1 の PowerShell 出力を確認 → §9 の checklist を埋める → 実行
