# HUMAN TASK: 配信 publisher setup (ntfy.sh + SendGrid)

作成日: 2026-07-01
対象: 当日シグナル配信 (`scripts/publish_signals.py` / `scripts/daily_pipeline.ps1`)
所要: ntfy ≈ 3 分 / SendGrid ≈ 10 分

配信は **ntfy.sh (primary, iPhone push)** + **SendGrid Email (backup)** の 2 段構え。
Phase 1 は自分 1 宛先。Phase 2 で `config/subscribers.json` に fan-out する。

---

## A. ntfy.sh (primary, iPhone push) — 必須

ntfy は「topic 名 = URL path」がそのまま secret。**推測不能な文字列**にすること
(例: `quant-gethinu-` + ランダム 16 hex)。誰かが topic 名を知ると通知を読めてしまう。

1. **iPhone に ntfy を install**: App Store で「**ntfy**」を検索して install (無料)。
2. **topic を決める**: 例 `quant-gethinu-3f9a2c7d1e4b8a06`。
   ランダム hex は次で生成可: `python -c "import secrets;print('quant-gethinu-'+secrets.token_hex(8))"`
3. **iPhone で subscribe**: ntfy 起動 → 右上「**+**」→ *Subscribe to topic* → 上の topic 名を入力 → *Subscribe*。
4. **`.env` に記入**:
   ```
   NTFY_TOPIC=quant-gethinu-3f9a2c7d1e4b8a06
   NTFY_URL=https://ntfy.sh
   NTFY_PRIORITY=4
   ```
5. **smoke test** (どちらでも可):
   ```bash
   curl -H "X-Title: test" -d "hello from quant" https://ntfy.sh/quant-gethinu-3f9a2c7d1e4b8a06
   # または実 payload で:
   python scripts/publish_signals.py --input apps/dashboards/alpaca-next/mock/today_signals_20260701.json --publisher ntfy
   ```
   → iPhone に push が来れば OK。通知タップで「Open dashboard」アクションから Vercel dashboard へ飛べる。

> 注意: 無料 ntfy.sh は公開サーバー。topic 名を秘匿すれば実用上十分だが、
> 機密度を上げたい場合は self-host (`NTFY_URL` を自前サーバーに) へ差し替え可能。

---

## B. SendGrid Email (backup) — 推奨 (ntfy 失敗時の保険)

1. **signup**: https://sendgrid.com/ で無料 tier (100 通/日) 登録。
2. **API Key 発行**: Settings → API Keys → *Create API Key* (Full Access or Mail Send) → キーをコピー。
3. **Sender 認証**: Settings → Sender Authentication → *Single Sender Verification* で
   `SENDGRID_FROM_EMAIL` に使うアドレスを verify (確認メールのリンクを踏む)。
4. **`.env` に記入**:
   ```
   SENDGRID_API_KEY=SG.xxxxxxxx
   SENDGRID_FROM_EMAIL=quant-bot@あなたのドメイン
   SENDGRID_TO_EMAIL=gethteiben@gmail.com
   EMAIL_ALWAYS=0
   ```
5. **smoke test** (送信せず payload 検証):
   ```bash
   python scripts/publish_signals.py --dry-run --publisher email \
     --input apps/dashboards/alpaca-next/mock/today_signals_20260701.json
   ```
   実送信は `--dry-run` を外す。

---

## C. 配信モードの選び方 (`--publisher`)

| コマンド | 挙動 |
|---|---|
| `--publisher ntfy` (default) | ntfy のみ |
| `--publisher ntfy --fallback` | ntfy → 失敗時のみ email |
| `--publisher all` | ntfy + email を常に並列 |
| `EMAIL_ALWAYS=1` (env) | ntfy 成功時も email を並列送信 |

`daily_pipeline.ps1` の step4 は既定で `publish_signals.py --input <json>` (= ntfy)。
fallback を常用したい場合は `daily_pipeline.ps1` の `$pubArgs` に `"--fallback"` を追加。

---

## D. 監視 (publish_status)

配信後、signals JSON の `meta.publish_status` に `ok` / `partial` / `failed` が書き戻され、
Vercel dashboard の "Today's Signals" 見出しに badge 表示される。全宛先失敗 (`failed`) は
`daily_pipeline.ps1` が ntfy WARN (priority 5) も別途送るので、push が来なくても
dashboard 側で検知できる。
