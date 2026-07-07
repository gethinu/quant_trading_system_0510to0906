# スケジューラー使用ガイド

当日シグナル生成を含む全ての定期タスクを自動実行するスケジューラーの使用方法です。

## 📅 スケジュール一覧

| タスク名 | 実行時刻 | 説明 |
|---------|---------|------|
| update_tickers | 平日 06:00 | ティッカーリストの更新 |
| bulk_last_day | 平日 06:45 | 前営業日データの一括更新 |
| warm_cache | 平日 07:00 | キャッシュのウォームアップ |
| precompute_shared_indicators | 平日 07:30 | 共有指標の事前計算 |
| send_signals | 平日 08:30 | シグナル通知の送信 |
| update_trailing_stops | 平日 08:45 | トレーリングストップの更新 |
| notify_metrics | 平日 08:50 | メトリクス通知の送信 |
| build_metrics_report | 平日 08:55 | レポートの生成 |
| **run_today_signals** | **平日 11:00** | **当日シグナルの生成** ⭐ |
| daily_run | 火-土 06:15 | 日次バッチ処理 |

※ タイムゾーン: Asia/Tokyo (JST)

## 🚀 使い方

### 方法1: 手動起動（テスト用）

スケジューラーを手動で起動して動作を確認する場合:

```powershell
# プロジェクトルートで実行
.\start_scheduler.ps1
```

- スケジューラーが起動し、設定された時刻になると自動的にタスクを実行します
- `Ctrl+C` で停止できます
- ログは `logs/` ディレクトリに出力されます

### 方法2: 自動起動（本番運用）

Windowsログイン時に自動的にスケジューラーを起動する場合:

```powershell
# 管理者権限で実行
.\register_task_scheduler.ps1
```

これにより、次回のログイン時から自動的にスケジューラーが起動します。

**タスクの削除:**

```powershell
.\register_task_scheduler.ps1 -Unregister
```

## 📋 設定のカスタマイズ

スケジュールを変更したい場合は、`config/config.yaml` の `scheduler` セクションを編集します:

```yaml
scheduler:
  timezone: Asia/Tokyo
  jobs:
    - name: run_today_signals
      cron: "0 11 * * 1-5"  # 平日 11:00
      task: run_today_signals
```

### cron形式の説明

形式: `分 時 * * 曜日`

- **分**: 0-59
- **時**: 0-23
- **曜日**: 0-7 (0または7=日曜日、1=月曜日、...、5=金曜日)

**例:**
- `0 6 * * 1-5` : 平日の6時ちょうど
- `30 8 * * 1-5` : 平日の8時30分
- `0 12 * * *` : 毎日12時ちょうど
- `0 6 * * 1,3,5` : 月・水・金の6時

## 🔍 動作確認

### ログの確認

スケジューラーのログは以下のディレクトリに出力されます:

```
logs/
  ├── today_signals_YYYYMMDD_HHMM.log  # 当日シグナル生成のログ
  ├── scheduler_*.log                   # その他のタスクのログ
  └── app.log                           # アプリケーション全体のログ
```

### タスクが実行されたか確認

```powershell
# 最新のログを確認
Get-Content logs\today_signals_*.log -Tail 50
```

### Windowsタスクスケジューラーで確認

1. Windowsキー + R → `taskschd.msc` と入力
2. タスクスケジューラライブラリで "QuantTradingScheduler" を検索
3. 実行履歴タブで過去の実行状況を確認

## ⚠️ トラブルシューティング

### スケジューラーが起動しない

1. Python仮想環境が正しくアクティベートされているか確認
2. `.env` ファイルが存在し、必要な環境変数が設定されているか確認
3. `requirements.txt` の依存パッケージがインストールされているか確認

```powershell
pip install -r requirements.txt
```

### タスクが実行されない

1. ログファイルでエラーメッセージを確認
2. cron形式が正しいか確認
3. タイムゾーン設定が正しいか確認

### 特定のタスクのみ手動実行

```powershell
# 当日シグナルのみ実行
python -c "from schedulers.runner import task_run_today_signals; task_run_today_signals()"

# または、直接スクリプトを実行
python scripts\run_all_systems_today.py --save-csv
```

## 🎯 ベストプラクティス

1. **初回は手動起動でテスト**: まず `.\start_scheduler.ps1` で動作確認
2. **ログを定期的に確認**: エラーが発生していないかチェック
3. **タスクの実行時刻を調整**: 市場の開始時刻やデータ更新タイミングに合わせる
4. **バックアップ**: 重要なシグナルは別途保存やSlack通知で確保

## 📚 関連ドキュメント

- [README.md](README.md) - プロジェクト全体の説明
- [config/config.yaml](../config/config.yaml) - 設定ファイル
- [schedulers/runner.py](../schedulers/runner.py) - スケジューラー本体

## 💡 Tips

### すぐにタスクを実行したい場合

スケジュール時刻を待たずに、特定のタスクをすぐに実行したい場合:

```powershell
# 当日シグナル生成を今すぐ実行
python -m scripts.run_all_systems_today --save-csv

# または、Streamlit UIから実行
streamlit run apps/app_today_signals.py
```

### 複数タスクを連続実行

```powershell
# データ更新 → シグナル生成 → 通知を連続実行
python scripts/update_from_bulk_last_day.py
python scripts/run_all_systems_today.py --save-csv
python tools/notify_signals.py
```
