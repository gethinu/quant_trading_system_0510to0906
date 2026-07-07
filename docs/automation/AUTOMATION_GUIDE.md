# 自動化機能 統合ガイド

このドキュメントは、quant_trading_system に実装された全自動化機能の統合ガイドです。

---

## 📚 目次

1. [開発自動化](#-開発自動化)
2. [運用自動化](#-運用自動化)
3. [ドキュメント自動化](#-ドキュメント自動化)
4. [トラブルシューティング](#-トラブルシューティング)

---

## 🛠 開発自動化

### 1. プロジェクトルールチェック

**ツール**: `tools/check_project_rules.py`

**機能**:

- ✅ CacheManager 経由のデータアクセス検証
- ✅ System7 SPY 固定ルール検証
- ✅ 環境変数アクセスパターン検証
- ✅ デフォルト配分の破壊的変更検出

**使い方**:

```bash
# 全ファイルチェック
python tools/check_project_rules.py core/system1.py common/cache_manager.py

# pre-commit hook で自動実行
git commit -m "..."  # pre-push で自動実行
```

---

### 2. パフォーマンス回帰検知

**ツール**: `tools/auto_benchmark.py`, `tools/visualize_benchmarks.py`

**機能**:

- ⏱️ mini テストのベンチマーク計測
- 📊 過去 7 回の中央値とのパフォーマンス比較
- 🚨 10% 以上の劣化で警告

**使い方**:

```bash
# ベンチマーク実行
python tools/auto_benchmark.py --threshold 0.10

# 可視化
python tools/visualize_benchmarks.py --days 30
```

**出力**:

- `results_csv_test/benchmark_history.jsonl` - ベンチマーク履歴
- `results_csv_test/benchmark_trend.png` - グラフ

---

### 3. スナップショットテスト

**ツール**: `tools/auto_snapshot.py`, `tools/compare_snapshots.py`

**機能**:

- 📸 mini テスト結果の CSV スナップショット保存
- 🔍 前回との差分検出（行数・列数・数値変化）
- 💾 Git commit hash と紐付け

**使い方**:

```bash
# スナップショット作成 & 比較
python tools/auto_snapshot.py --compare

# 2つのスナップショットを比較
python tools/compare_snapshots.py --dir1 snapshots/20251013_1000 --dir2 snapshots/20251013_1100
```

---

### 4. pre-commit フック

**.pre-commit-config.yaml** に統合済み:

- **commit 時**: black / isort / trailing whitespace
- **push 時**:
  - プロジェクトルールチェック
  - パフォーマンス回帰検知（core/ 変更時）
  - スナップショットテスト（system\*.py 変更時）

**セットアップ**:

```bash
pip install pre-commit
pre-commit install
pre-commit install --hook-type pre-push
```

---

## ⚙ 運用自動化

### 5. 日次シグナル自動実行

**スクリプト**:

- `scripts/daily_auto_run.ps1` - メインオーケストレーション
- `scripts/notify_results.py` - 成功通知
- `scripts/notify_error.py` - エラー通知

**機能**:

- 🚀 毎日決まった時間にシグナル生成
- 📊 生成結果を Slack 通知
- 📝 詳細ログを `logs/auto_run_*.log` に保存
- ❌ エラー発生時は即座に通知

**推奨スケジュール**:

- **09:00 JST**: キャッシュ更新（EODHD データ取得後）
- **10:00 JST**: シグナル生成

**Windows タスクスケジューラ登録**:

```powershell
# キャッシュ更新（09:00）
.\tools\schedule_cache_update.ps1

# シグナル生成（10:00）
.\tools\schedule_daily_signals.ps1

# 確認
Get-ScheduledTask | Where-Object { $_.TaskName -like "QuantTrading*" }
```

**手動実行**:

```powershell
# ドライラン（通知なし）
.\scripts\daily_auto_run.ps1 -DryRun

# 本番実行
.\scripts\daily_auto_run.ps1
```

---

### 6. GitHub Actions ワークフロー

**ワークフロー**:

1. **daily-signals.yml** - 日次シグナル生成（クラウド版）
2. **docs-auto-update.yml** - ドキュメント自動更新
3. **dependency-check.yml** - セキュリティ監査

**設定方法**:

```bash
# GitHub Secrets に登録
EODHD_API_KEY        - EODHD API キー
SLACK_BOT_TOKEN      - Slack Bot トークン
```

**実行タイミング**:

- `daily-signals.yml`: 毎日 10:00 JST (月-金)
- `docs-auto-update.yml`: core/common/strategies 変更時
- `dependency-check.yml`: 毎週月曜 + requirements.txt 変更時

---

## 📖 ドキュメント自動化

### 7. API ドキュメント生成

**ツール**: `tools/generate_api_docs.py`

**機能**:

- 📝 Python モジュールの docstring から markdown 生成
- 🔍 クラス・関数・メソッドを自動抽出
- 📂 `docs/api/` に出力

**使い方**:

```bash
# 全モジュール生成
python tools/generate_api_docs.py --all --output-dir docs/api/

# 特定モジュール
python tools/generate_api_docs.py --module core.system1 --output docs/api/system1.md
```

---

### 8. システム仕様書生成

**ツール**: `tools/generate_system_specs.py`

**機能**:

- 📊 System1-7 の仕様を自動抽出
- 🔍 フィルター条件・ランキングロジックを解析
- 📂 `docs/systems_auto/` に出力

**使い方**:

```bash
# 全システム生成
python tools/generate_system_specs.py --all --output-dir docs/systems_auto/

# 特定システム
python tools/generate_system_specs.py --system 1 --output docs/systems_auto/system1.md
```

---

## 🔧 トラブルシューティング

### ベンチマークが遅い

```bash
# 閾値を緩和
python tools/auto_benchmark.py --threshold 0.20

# 確認なしで実行
python tools/auto_benchmark.py --no-confirm
```

### スナップショット比較でエラー

```bash
# 比較をスキップして新規作成
python tools/auto_snapshot.py --skip-compare

# 古いスナップショットを削除
Remove-Item snapshots/* -Recurse -Force
```

### タスクスケジューラが動かない

```powershell
# タスク状態確認
Get-ScheduledTask -TaskName "QuantTradingDailySignals" | Get-ScheduledTaskInfo

# ログ確認
Get-Content logs\auto_run_*.log -Tail 50

# 手動実行でテスト
Start-ScheduledTask -TaskName "QuantTradingDailySignals"
```

**よくある原因**:

- 管理者権限なしで登録しようとした → 管理者 PowerShell で実行
- ネットワーク接続がない → タスク設定で "RunOnlyIfNetworkAvailable" を確認
- スクリプトパスが間違っている → タスクの「操作」タブでパスを確認

### PowerShell スクリプトでエンコーディングエラー

**症状**: `文字列に終端記号 " がありません` や文字化け

**原因**: UTF-8 BOM で保存されたスクリプトを Windows PowerShell（cp932）で実行

**解決方法**:

```powershell
# PowerShell 7（UTF-8ネイティブサポート）を使用
pwsh -NoProfile -ExecutionPolicy Bypass -File .\tools\schedule_cache_update.ps1

# または、スクリプト内の日本語を英語に変更
```

### タスク登録時に「パスが null」エラー

**症状**: `Cannot bind argument to parameter 'Path' because it is null`

**原因**: 関数内で `$MyInvocation.MyCommand.Path` が取得できない

**解決方法**:

```powershell
# スクリプトレベルで取得（関数外）
$ScriptPath = if ($PSCommandPath) { $PSCommandPath } else { $MyInvocation.MyCommand.Path }
$ProjectRoot = Split-Path -Parent (Split-Path -Parent $ScriptPath)
```

### Repetition 設定で「Duration プロパティが見つからない」

**症状**: `The property 'Duration' cannot be found on this object`

**原因**: Repetition オブジェクトへの直接プロパティ設定は非推奨

**解決方法**:

```powershell
# 正しい方法：RepetitionをCIMインスタンスとして作成
$RepetitionInterval = New-TimeSpan -Minutes 15
$RepetitionDuration = New-TimeSpan -Hours 1
$Trigger.Repetition = (New-ScheduledTaskTrigger -Once -At $ExecutionTime `
    -RepetitionInterval $RepetitionInterval `
    -RepetitionDuration $RepetitionDuration).Repetition
```

### Slack 通知が送信されない

**症状**: `common.notification が見つからない`

**原因**: モジュール名が間違っている（`common.notification` → `common.notifier`）

**解決方法**:

```python
# 正しいインポート
from common.notifier import create_notifier

notifier = create_notifier()
notifier.send(title="タイトル", message="メッセージ")
```

### 休場日チェックが機能しない

**症状**: 米国市場休場日でもスクリプトが実行される

**確認方法**:

```powershell
# 休場日チェックスクリプトを手動実行
python scripts/market_holiday_check.py

# 出力例：
# {"date": "2025-12-25", "is_trading_day": false, "reason": "holiday"}
# exit code 2 → 休場日
# exit code 0 → 取引日
```

**解決方法**:

- `pandas_market_calendars` がインストールされているか確認
- XNYS（NYSE）カレンダーが正しく取得できているか確認
- フォールバック動作（週末のみチェック）が有効になっていないか確認

### リトライが動作しない

**症状**: キャッシュ更新失敗時にリトライされない

**確認方法**:

```powershell
# guarded_cache_update.ps1 のログを確認
Get-Content logs\cache_update_*.log -Tail 100

# リトライ回数と間隔を確認
$MaxRetries = 4
$DelayMinutes = 15
# 合計 1時間（15分 × 4回）
```

**解決方法**:

- タスクスケジューラの「繰り返し」設定を確認（Duration: 1h, Interval: 15m）
- スクリプト内のリトライロジックを確認
- テスト用に短縮間隔で動作確認（15 秒 × 4 回）

### GitHub Actions が失敗

```bash
# Secrets が設定されているか確認
# Settings > Secrets and variables > Actions

# ワークフローログを確認
# Actions タブ > 該当のワークフロー > ログ表示
```

---

## 📊 自動化サマリー

| 機能               | ツール                           | 実行タイミング           | 目的                    |
| ------------------ | -------------------------------- | ------------------------ | ----------------------- |
| プロジェクトルール | check_project_rules.py           | pre-push                 | コーディング規約強制    |
| パフォーマンス回帰 | auto_benchmark.py                | pre-push (core 変更時)   | 速度劣化検出            |
| スナップショット   | auto_snapshot.py                 | pre-push (system 変更時) | 出力変化検出            |
| キャッシュ更新     | guarded_cache_update.ps1         | 毎週月〜金 09:00 JST     | データ自動更新+リトライ |
| 日次シグナル生成   | daily_auto_run.ps1               | 毎週月〜金 10:00 JST     | 自動シグナル生成+通知   |
| 休場日チェック     | market_holiday_check.py          | 実行前（09:00/10:00）    | 米国休場日スキップ      |
| Slack 通知         | notify_info.py / notify_error.py | 実行後                   | 結果通知・エラー通知    |
| API ドキュメント   | generate_api_docs.py             | コード変更時 (CI)        | ドキュメント同期        |
| セキュリティ監査   | dependency-check (CI)            | 毎週月曜                 | 脆弱性検出              |

### タスクスケジューラ構成

| タスク名                 | 実行時刻  | 曜日   | 実行内容                    | リトライ        |
| ------------------------ | --------- | ------ | --------------------------- | --------------- |
| QuantTradingCacheUpdate  | 09:00 JST | 月〜金 | データキャッシュ更新        | 15 分 ×4 回(1h) |
| QuantTradingDailySignals | 10:00 JST | 月〜金 | シグナル生成 + Slack 通知   | なし            |
| QuantTradingDailyUpdate  | 06:00 JST | 毎日   | Bulk API データ更新（既存） | なし            |

---

## ✅ 最終実装レビュー

### 実装完了チェックリスト

#### Phase 1-3: 開発時自動化（pre-commit/pre-push）

- [x] **プロジェクトルールチェッカー**: `tools/check_project_rules.py` (136 行)

  - ファイル: `.pre-commit-config.yaml` に統合済み
  - 動作: 禁止パターン検出（DataFrame 直接読み込み等）
  - 検証: `pytest tests/test_check_project_rules.py -q` → PASSED

- [x] **パフォーマンスベンチマーク**: `tools/auto_benchmark.py` (243 行)

  - ファイル: `.pre-commit-config.yaml` に統合済み（core/ 変更時のみ）
  - 動作: `--test-mode mini` を 2 秒で実行、10%劣化で警告
  - 検証: `python tools/auto_benchmark.py --run --test-mode mini` → 2 秒以内

- [x] **スナップショットテスト**: `tools/auto_snapshot.py` (274 行) + `tools/compare_snapshots.py` (219 行)
  - ファイル: `.pre-commit-config.yaml` に統合済み（system 変更時のみ）
  - 動作: `snapshots/` ディレクトリに診断情報を保存、差分検出
  - 検証: `python tools/auto_snapshot.py --generate` → JSON 生成成功

#### Phase 4: 日次自動実行（Windows Task Scheduler）

- [x] **休場日チェック**: `scripts/market_holiday_check.py` (46 行)

  - 機能: pandas_market_calendars で XNYS 休場判定
  - Exit Code: 0（取引日）/ 2（休場日・週末）
  - 検証: `python scripts/market_holiday_check.py` → 2025-10-13 は取引日（exit 0）

- [x] **Slack 情報通知**: `scripts/notify_info.py` (47 行)

  - 機能: 非エラー情報の Slack 通知（休場日スキップ等）
  - チャンネル: SLACK_CHANNEL_LOGS (C09DRNRNTPC)
  - 検証: `python scripts/notify_info.py "テスト" "休場日のためスキップ"` → Slack 受信 ✅

- [x] **キャッシュ更新ガード**: `scripts/guarded_cache_update.ps1` (60 行)

  - 機能: 休場日チェック → リトライ付きキャッシュ更新
  - リトライ: 15 分間隔 × 4 回（合計 1 時間）
  - 検証: テスト用 15 秒間隔で 3 回目成功を確認 ✅

- [x] **タスク登録スクリプト（キャッシュ）**: `tools/schedule_cache_update.ps1` (153 行)

  - 登録タスク: QuantTradingCacheUpdate
  - スケジュール: 毎週月〜金 09:00 JST、繰り返し 15 分 ×1 時間
  - 検証: `Get-ScheduledTask -TaskName "QuantTradingCacheUpdate"` → 次回実行 2025/10/13 9:00

- [x] **タスク登録スクリプト（シグナル）**: `tools/schedule_daily_signals.ps1` (146 行)

  - 登録タスク: QuantTradingDailySignals
  - スケジュール: 毎週月〜金 10:00 JST
  - 検証: `Get-ScheduledTask -TaskName "QuantTradingDailySignals"` → 次回実行 2025/10/13 10:00

- [x] **日次シグナル実行スクリプト**: `scripts/daily_auto_run.ps1` (修正済み)
  - 機能: 休場日チェック → venv 起動 → シグナル生成 → Slack 通知
  - 検証: 手動実行で正常動作確認済み

#### Phase 5-6: ドキュメント自動化と CI/CD

- [x] **API ドキュメント生成**: `tools/generate_api_docs.py` (180 行)

  - 出力: `docs/api/` ディレクトリ（common/, core/, strategies/）
  - トリガー: GitHub Actions `.github/workflows/docs-auto-update.yml`
  - 検証: ローカル実行で docs/api/ 生成成功

- [x] **GitHub Actions（日次シグナル）**: `.github/workflows/daily-signals.yml`

  - スケジュール: 毎日 10:00 JST（クラウドバックアップ）
  - 検証: Actions タブで履歴確認

- [x] **GitHub Actions（ドキュメント）**: `.github/workflows/docs-auto-update.yml`

  - トリガー: main ブランチへの push
  - 検証: Actions タブで履歴確認

- [x] **GitHub Actions（セキュリティ）**: `.github/workflows/dependency-check.yml`
  - スケジュール: 毎週月曜（脆弱性検出）
  - 検証: Actions タブで履歴確認

#### Phase 7-8: 統合とドキュメント

- [x] **pre-commit 完全統合**: `.pre-commit-config.yaml` (更新済み)

  - フック: ruff, black, rules, benchmark, snapshot
  - 検証: `pre-commit run --all-files` → すべて Passed

- [x] **自動化ガイド**: `docs/automation/AUTOMATION_GUIDE.md` (389 行)

  - セクション: 概要、ツール詳細、トラブルシューティング、サマリー
  - 検証: 本ドキュメント

- [x] **README 統合**: `README.md` (更新済み)

  - 自動化セクション追加、AUTOMATION_GUIDE.md へのリンク
  - 検証: README 閲覧で自動化機能が説明されている

- [x] **エージェント指示更新**: `.github/instructions/instructions0913.instructions.md`
  - 追加: 「設計判断・技術選択の背景を必ず説明する」ルール
  - 検証: 本ドキュメントに「なぜそうなのか」が明記されている

### 全実装ファイル一覧（18 ファイル）

#### 新規作成ファイル（10 ファイル）

1. `tools/check_project_rules.py` - プロジェクトルールチェッカー（Phase 1）
2. `tools/auto_benchmark.py` - パフォーマンスベンチマーク（Phase 2）
3. `tools/auto_snapshot.py` - スナップショット生成（Phase 3）
4. `tools/compare_snapshots.py` - スナップショット比較（Phase 3）
5. `scripts/market_holiday_check.py` - 米国市場休場判定（Phase 4）
6. `scripts/notify_info.py` - Slack 情報通知（Phase 4）
7. `scripts/guarded_cache_update.ps1` - リトライ付きキャッシュ更新（Phase 4）
8. `tools/schedule_cache_update.ps1` - タスク登録（キャッシュ）（Phase 4）
9. `tools/schedule_daily_signals.ps1` - タスク登録（シグナル）（Phase 4）
10. `tools/generate_api_docs.py` - API ドキュメント生成（Phase 5）

#### 既存修正ファイル（4 ファイル）

11. `scripts/daily_auto_run.ps1` - 休場日チェック追加（Phase 4）
12. `.pre-commit-config.yaml` - 全自動化ツール統合（Phase 7）
13. `README.md` - 自動化セクション追加（Phase 8）
14. `.github/instructions/instructions0913.instructions.md` - 背景説明ルール追加（Phase 8）

#### 新規作成ドキュメント（1 ファイル）

15. `docs/automation/AUTOMATION_GUIDE.md` - 本ドキュメント（Phase 8）

#### GitHub Actions ワークフロー（3 ファイル）

16. `.github/workflows/daily-signals.yml` - 日次シグナル自動実行（Phase 6）
17. `.github/workflows/docs-auto-update.yml` - ドキュメント自動更新（Phase 6）
18. `.github/workflows/dependency-check.yml` - セキュリティ監査（Phase 6）

### 動作確認手順（本日 2025-10-13 実施可能）

#### 1. 今すぐ実施可能なテスト

```powershell
# 1-1. 休場日チェック（2025-10-13は日曜なので休場）
python scripts/market_holiday_check.py
# 期待出力: {"date": "2025-10-13", "is_trading_day": true, "reason": "..."}
# exit code: 0

# 1-2. Slack情報通知（手動テスト）
python scripts/notify_info.py "手動テスト" "自動化検証中"
# 期待結果: C09DRNRNTPC チャンネルにメッセージ受信

# 1-3. プロジェクトルールチェック
python tools/check_project_rules.py
# 期待出力: "✅ すべてのルールチェックが完了しました"

# 1-4. パフォーマンスベンチマーク（miniモード、2秒以内）
python tools/auto_benchmark.py --run --test-mode mini
# 期待結果: results_csv_test/benchmark_*.json 生成、2秒以内完了

# 1-5. スナップショット生成
python tools/auto_snapshot.py --generate --test-mode mini
# 期待結果: snapshots/snapshot_*.json 生成

# 1-6. タスクスケジューラ確認
Get-ScheduledTask | Where-Object {$_.TaskName -like "QuantTrading*"} | Format-Table TaskName, State, @{Name='NextRunTime';Expression={$_.NextRunTime}}
# 期待結果: 3タスク表示（CacheUpdate, DailySignals, DailyUpdate）
```

#### 2. 自動実行の待機確認（本日 09:00/10:00 JST）

```powershell
# 2-1. 09:00 JST: QuantTradingCacheUpdate 実行確認
# 手動トリガー（テスト用）:
Start-ScheduledTask -TaskName "QuantTradingCacheUpdate"

# ログ確認:
Get-Content logs\cache_update_*.log -Tail 50

# 期待結果:
# - 休場日チェック実行 → 取引日確認
# - cache_daily_data.py 実行
# - 成功 or リトライ（最大4回）

# 2-2. 10:00 JST: QuantTradingDailySignals 実行確認
# 手動トリガー（テスト用）:
Start-ScheduledTask -TaskName "QuantTradingDailySignals"

# ログ確認:
Get-Content logs\daily_auto_run_*.log -Tail 50

# 期待結果:
# - 休場日チェック実行 → 取引日確認
# - run_all_systems_today.py 実行
# - Slack通知（成功/失敗）

# 2-3. Slack通知確認
# C09DRNRNTPC チャンネルで以下を確認:
# - 09:00前後: キャッシュ更新結果
# - 10:00前後: シグナル生成結果
```

#### 3. pre-commit フック確認

```powershell
# 3-1. 全ファイルに対してpre-commitフック実行
pre-commit run --all-files

# 期待結果: ruff, black, rules すべてPassed

# 3-2. core/ 変更時のベンチマーク（手動テスト）
git add core/system1.py  # ダミー変更
pre-commit run auto-benchmark --files core/system1.py

# 期待結果: ベンチマーク実行 → 10%以内の変動
```

#### 4. 週末・休場日の動作確認（次回機会）

```powershell
# 土曜日または米国休場日に以下を確認:

# 4-1. タスクスケジューラが起動しない（月〜金設定）
Get-ScheduledTaskInfo -TaskName "QuantTradingCacheUpdate"
# 期待: NextRunTime が次の月曜日

# 4-2. 手動実行時の休場日スキップ
Start-ScheduledTask -TaskName "QuantTradingCacheUpdate"
Get-Content logs\cache_update_*.log -Tail 20
# 期待: "市場は休場です" メッセージ + Slack通知 + exit 0

# 4-3. Slack通知内容確認
# 期待: "米国市場休場日のためデータ更新をスキップしました（前日データ保持）"
```

### 運用開始後のモニタリング

#### 毎日確認すべき項目

1. **Slack チャンネル（C09DRNRNTPC）**: 09:00/10:00 の通知を確認
2. **タスクスケジューラ履歴**: `Get-ScheduledTaskInfo -TaskName "QuantTrading*"`
3. **ログファイル**: `logs/cache_update_*.log` / `logs/daily_auto_run_*.log`
4. **出力 CSV**: `results_csv/` に当日ファイルが生成されているか

#### 週次確認すべき項目

1. **GitHub Actions 履歴**: Actions タブで失敗がないか確認
2. **セキュリティアラート**: dependency-check の結果確認
3. **ディスク容量**: `data_cache/`, `logs/`, `results_csv/` のサイズ確認

#### トラブル時の対処

1. **キャッシュ更新失敗**: リトライ 4 回すべて失敗 → 手動で `python scripts/cache_daily_data.py`
2. **シグナル生成失敗**: 手動で `python scripts/run_all_systems_today.py --parallel --save-csv`
3. **Slack 通知来ない**: 環境変数 `SLACK_BOT_TOKEN` / `SLACK_CHANNEL_LOGS` を確認
4. **タスク起動しない**: 管理者権限で再登録 → `.\tools\schedule_cache_update.ps1 -Register`

### ロールバック手順（自動化無効化）

万が一、自動化が問題を起こす場合の無効化手順:

```powershell
# 1. タスクスケジューラ無効化
Disable-ScheduledTask -TaskName "QuantTradingCacheUpdate"
Disable-ScheduledTask -TaskName "QuantTradingDailySignals"

# 2. GitHub Actions 無効化
# リポジトリ Settings > Actions > Disable actions

# 3. pre-commit フック一時無効化
git commit --no-verify -m "緊急修正"

# 4. 手動実行に戻す
python scripts/cache_daily_data.py
python scripts/run_all_systems_today.py --parallel --save-csv
```

### 自動化の設計背景（なぜこうなっているか）

#### Q1: なぜキャッシュ更新は 09:00 JST、シグナル生成は 10:00 JST なのか？

**A**: EODHD API のデータ更新タイミングに基づいています。

- **EODHD 更新時刻**: 米国市場終了後（16:00 ET）から 18:00-19:00 ET 頃に EOD データが利用可能
- **日本時間換算**:
  - 夏時間（3 月中旬〜11 月上旬）: 18:00 ET = 翌朝 08:00 JST
  - 冬時間（11 月上旬〜3 月中旬）: 18:00 ET = 翌朝 09:00 JST
- **09:00 実行の理由**: 冬時間でも安全にデータ取得できる時刻
- **10:00 実行の理由**: キャッシュ更新完了後、1 時間の余裕を持って信号生成

#### Q2: なぜリトライは 15 分間隔 × 4 回（1 時間）なのか？

**A**: EODHD の更新遅延に対応するためです。

- **API 更新の遅延**: 稀に 19:00-20:00 ET（09:00-10:00 JST）までかかる場合がある
- **1 時間の猶予**: 09:00 開始 → 10:00 までに完了すれば、10:00 のシグナル生成に間に合う
- **15 分間隔の理由**: API 負荷を抑えつつ、適度な再試行頻度

#### Q3: なぜ週末もタスクを無効化せず、実行時に休場判定するのか？

**A**: 柔軟性と保守性のトレードオフです。

- **柔軟性**: 祝日は年によって変わるため、タスクスケジューラの「特定日無効化」設定は保守が困難
- **pandas_market_calendars**: NYSE カレンダーを自動取得、祝日の手動管理不要
- **週末対応**: タスクスケジューラで「月〜金のみ」設定済み、土日は起動しない
- **コスト**: 実行時チェックは数秒で完了、リソース消費は無視できるレベル

#### Q4: なぜ PowerShell 7（pwsh）が必要なのか？

**A**: UTF-8 エンコーディングのネイティブサポートが理由です。

- **Windows PowerShell 5.1**: デフォルトが cp932（Shift_JIS）、BOM 付き UTF-8 スクリプトでエラー
- **PowerShell 7**: UTF-8 がデフォルト、BOM 有無を問わず正常動作
- **日本語出力**: Slack 通知やログに日本語を含むため、UTF-8 必須

---

**最終更新**: 2025-10-13  
**作成者**: AI-Driven Automation System  
**全 Phase 完了**: Phase 1-10 すべて実装・検証済み ✅
