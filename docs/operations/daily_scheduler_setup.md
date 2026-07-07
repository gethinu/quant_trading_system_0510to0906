# 日次自動更新のスケジューラ設定ガイド

このドキュメントでは、毎朝の自動キャッシュ更新とヘルスチェックを設定する手順を説明します。

## 概要

- **目的**: 毎朝 06:00 に Bulk Last Day API から最新データを取得し、キャッシュを更新する
- **スクリプト**: `scripts/scheduler_update_with_healthcheck.py`
- **ヘルスチェック**: SPY の鮮度を確認し、必要に応じて 1 回リトライ
- **ログ**: `logs/scheduler_update_health_YYYYMMDD.log` に結果を記録

## 前提条件

- `.env` ファイルに `EODHD_API_KEY` が設定済み
- Python 仮想環境が `C:\Repos\quant_trading_system\venv` にある
- 依存パッケージがインストール済み

## 方法 1: PowerShell スクリプトで自動設定（推奨）

**注意**: 管理者権限が必要です。

1. PowerShell を**管理者として実行**
2. 以下のコマンドを実行:

```powershell
cd C:\Repos\quant_trading_system
powershell -ExecutionPolicy Bypass -File scripts\setup_daily_scheduler.ps1
```

3. 成功したら、タスクスケジューラで確認:

```powershell
Get-ScheduledTask -TaskName "QuantTradingSystem_DailyUpdate"
```

4. 手動テスト実行:

```powershell
Start-ScheduledTask -TaskName "QuantTradingSystem_DailyUpdate"
```

## 方法 2: タスクスケジューラ GUI で手動設定

### ステップ 1: タスクスケジューラを開く

1. `Win + R` → `taskschd.msc` → Enter
2. 右クリック → 「基本タスクの作成」または「タスクの作成」

### ステップ 2: 全般タブ

- **名前**: `QuantTradingSystem_DailyUpdate`
- **説明**: `Quant Trading System - Daily cache update with health check`
- **セキュリティオプション**:
  - ☑ ユーザーがログオンしているかどうかにかかわらず実行する
  - ☑ 最上位の特権で実行する（必要に応じて）

### ステップ 3: トリガータブ

1. 「新規」をクリック
2. **タスクの開始**: 毎日
3. **開始**: 今日の日付、時刻 `06:00:00`
4. **繰り返し間隔**: 1 日
5. **有効**: ☑
6. OK

### ステップ 4: 操作タブ

1. 「新規」をクリック
2. **操作**: プログラムの開始
3. **プログラム/スクリプト**:
   ```
   C:\Repos\quant_trading_system\venv\Scripts\python.exe
   ```
4. **引数の追加（オプション）**:
   ```
   scripts\scheduler_update_with_healthcheck.py
   ```
5. **開始（オプション）**:
   ```
   C:\Repos\quant_trading_system
   ```
6. OK

### ステップ 5: 条件タブ

- ☐ コンピューターを AC 電源で使用している場合のみタスクを開始する（バッテリー駆動でも実行したい場合）
- ☑ タスクを実行するためにスリープを解除する（必要に応じて）

### ステップ 6: 設定タブ

- ☑ タスクが失敗した場合の再起動の間隔: 15 分
- **再起動を試行する回数**: 2 回
- ☑ タスクを要求時に実行する
- **実行時間の制限**: 2 時間

### ステップ 7: 保存とテスト

1. 「OK」をクリックして保存
2. タスクを右クリック → 「実行」で手動テスト
3. `logs/scheduler_update_health_YYYYMMDD.log` でログ確認

## 環境変数の調整（オプション）

タスク実行時の動作を調整したい場合、以下の環境変数を設定できます:

- `SCHEDULER_WORKERS=4`: 並列処理のワーカー数（デフォルト: CPU 数 ×2）
- `SCHEDULER_TAIL_ROWS=240`: 指標再計算に使うテール行数（デフォルト: 240）

**設定方法**: タスクの「操作」編集で、引数の前に環境変数を設定するラッパースクリプトを使用するか、Windows のシステム環境変数で設定します。

## 動作確認

### 手動実行テスト

```powershell
# タスクスケジューラから手動実行
Start-ScheduledTask -TaskName "QuantTradingSystem_DailyUpdate"

# ログ確認（今日の日付）
Get-Content C:\Repos\quant_trading_system\logs\scheduler_update_health_$(Get-Date -Format yyyyMMdd).log -Tail 20
```

### 期待される出力

ログに以下のような行があれば OK:

```
[2025-10-08 06:00:15] prev_bd=2025-10-07 latest(SPY)=2025-10-07 status=OK
```

- `status=OK`: 成功
- `status=NG`: 失敗（前営業日のデータが取得できなかった）

### 本番パイプライン実行テスト

更新後、シグナル生成パイプラインを実行して鮮度警告がないことを確認:

```powershell
C:\Repos\quant_trading_system\venv\Scripts\python.exe scripts\run_all_systems_today.py --parallel --save-csv
```

**期待される結果**:

- SPY 鮮度警告なし
- `entry_date` が当日で正規化
- `results_csv/` に成果物が保存される

## トラブルシューティング

### 問題: タスクが失敗する（終了コード 1）

**原因**: SPY の鮮度チェックが前営業日に一致しなかった

**対処**:

1. API が正常にデータを返しているか確認
2. 営業日カレンダーの問題（米国祝日など）の可能性
3. 数分待って手動で再実行

### 問題: API エラーで更新に失敗

**原因**: ネットワークまたは API 側の問題

**対処**:

1. `SCHEDULER_WORKERS` を下げる（例: 2）
2. 時間を置いて再実行
3. `.env` の `EODHD_API_KEY` が有効か確認

### 問題: パイプライン実行で鮮度警告が出る

**原因**: スケジューラ実行が完了していない、または失敗した

**対処**:

1. ヘルスログで `status=OK` を確認
2. 手動で `scheduler_update_with_healthcheck.py` を実行
3. キャッシュのタイムスタンプを確認:
   ```powershell
   Get-ChildItem C:\Repos\quant_trading_system\data_cache\rolling\SPY.* | Select-Object Name,LastWriteTime
   ```

## 運用監視

### 日次チェック項目

- ヘルスログで `status=OK` を確認
- パイプライン実行が正常完了
- 成果物 CSV が生成されている

### Slack/Discord 通知（オプション）

失敗時に通知を送りたい場合は、`scheduler_update_with_healthcheck.py` を拡張して、`status=NG` 時に通知を送信するロジックを追加できます。

例:

```python
if not ok:
    # Slack通知
    notify_failure(prev_bd, latest)
    sys.exit(1)
```

## 関連ドキュメント

- キャッシュ更新の詳細
- [当日シグナル処理フロー](../today_signal_scan/INDEX.md)
- [環境変数設定](../technical/environment_variables.md)

---

**最終更新**: 2025-10-08  
**担当者**: 自動化チーム
