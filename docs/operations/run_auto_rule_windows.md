# run_auto_rule.py を Windows Task Scheduler で実行する手順

このドキュメントは `scripts/run_auto_rule.py` を Windows 環境で定期実行するための手順です。

前提

- リポジトリは `C:\Repos\quant_trading_system` に配置されている想定。
- Python 環境がインストール済みで、必要パッケージ（`requirements.txt`）がインストールされていること。
- 環境変数（`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`、通知関連 `SLACK_BOT_TOKEN` / `DISCORD_WEBHOOK_URL` / `DISCORD_WEBHOOK_URL_SIGNALS` など）はタスク実行時に利用可能である必要があります。

おすすめ構成

1. 仮想環境を用意（プロジェクト直下に `.venv` を作成）。
2. Task Scheduler に PowerShell wrapper を登録（`scripts/run_auto_rule.ps1`）。

PowerShell wrapper の使い方

- `scripts/run_auto_rule.ps1` は仮想環境の `Scripts\Activate.ps1` を自動で探して有効化し、その後 `python scripts/run_auto_rule.py` を呼び出します。
- 引数:
  - `-Paper`: 紙注文モードで送信（`--paper` を渡す）
  - `-DryRun`: 注文送信を行わないドライラン（`--dry-run` を渡す）

Task Scheduler 登録例

1. タスクスケジューラを開く。
2. 基本タスクの作成 → 名前を入力（例: AutoRuleDaily）。
3. トリガー: 毎日、平日など必要に応じて設定。
4. 操作: プログラムの開始
   - プログラム/スクリプト: `powershell.exe`
   - 引数の追加: `-ExecutionPolicy Bypass -File "C:\Repos\quant_trading_system\scripts\run_auto_rule.ps1" -Paper`
   - 開始 (オプション): `C:\Repos\quant_trading_system`
5. 完了後、タスクのプロパティで「最上位の特権で実行」をチェック（必要に応じて）。

環境変数の注意

- Task Scheduler で実行する場合、タスクはインタラクティブセッションとは別の環境で走るため、ユーザーの .env をロードしない可能性があります。
- 確実なのはタスクの「操作」→「引数」に環境変数を展開したコマンドを渡すか、システム環境変数（Windows のシステム設定）に登録する方法です。

例: ユーザー環境でのみ使う場合（PowerShell 引数内で直接設定）

```powershell
$env:APCA_API_KEY_ID = 'YOUR_KEY'
$env:APCA_API_SECRET_KEY = 'YOUR_SECRET'
powershell -ExecutionPolicy Bypass -File "C:\Repos\quant_trading_system\scripts\run_auto_rule.ps1"
```

ログ確認

- `scripts/run_auto_rule.py` は標準出力/標準エラーにログを出力します。Task Scheduler の「履歴」や出力先ログで確認してください。

検証手順

1. 最初は手動で PowerShell から以下を実行して動作確認します（dry-run 推奨）:

```powershell
python .\scripts\run_auto_rule.py --dry-run
```

2. 正常に候補が出ることを確認したら `--paper` を外して（または `-Paper` を付けて PowerShell wrapper を使い）実行テストします。

補足

- 本番での自動実行は十分なテストの上で行ってください。最初は `--dry-run` で数日または数回実行し挙動を確認することを推奨します。
