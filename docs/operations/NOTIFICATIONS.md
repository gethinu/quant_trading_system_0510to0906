# 通知設定（Discord/Slack/Teams）

以下の環境変数を `.env` に設定すると、各種通知が有効になります。

- `DISCORD_WEBHOOK_URL`（共通フォールバック）
- `DISCORD_WEBHOOK_URL_SUMMARY`（日次サマリー専用、任意）
- `DISCORD_WEBHOOK_URL_BACKTEST`（バックテスト/売買結果専用、任意）
- `DISCORD_WEBHOOK_URL_SYSTEM1`〜`DISCORD_WEBHOOK_URL_SYSTEM7`（システム別、任意）
- `DISCORD_WEBHOOK_URL_SIGNALS` / `DISCORD_WEBHOOK_URL_EQUITY` / `DISCORD_WEBHOOK_URL_LOGS`（旧設定、任意）
- `SLACK_BOT_TOKEN` と `SLACK_CHANNEL` または `SLACK_CHANNEL_ID`
- `TEAMS_WEBHOOK_URL`

主な送信元:

- `common/notifier.py`: Discord/Slack にリッチ埋め込みで送信
- `tools/notify_signals.py`: Discord/Slack/Teams にシンプルなテキストで送信
- `scripts/tickers_loader.py`: ティッカー更新の通知を送信

補足:

- Slack が設定されている場合は Slack を優先し、なければ Discord を自動選択します。
- Discord は役割別 Webhook（summary/backtest/system1-7）を指定でき、未指定時は `DISCORD_WEBHOOK_URL` にフォールバックします。
- Teams はプレーンテキスト送信のみ対応します。
