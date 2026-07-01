# HUMAN_TASK: Vercel Daily Monitor 公開手順 (2026-07-01)

このドキュメントは `apps/dashboards/alpaca-next/` (Next.js static export) を
Vercel にデプロイして, gethinu.github.io/mt5-dashboard 側と統合するまでの手順。

対象 branch: `claude/monitor-webapp`
naming 候補: `quant-monitor.vercel.app` / `gesuinu-monitor.vercel.app` / `polygon-coverage.vercel.app`

---

## まず走らせる 3 コマンド

```bash
cd apps/dashboards/alpaca-next
npm install
npm run build   # out/ が生成されれば OK
```

その後, 下記 §1〜§5 を上から順に進める。

---

## §1. Vercel signup (5 分)

1. https://vercel.com/signup を開く。
2. **"Continue with GitHub" (SSO 推奨)** を選ぶ。GitHub ユーザ `gethinu` でログイン。
3. 個人 hobby プラン (無料) を選択。組織 (team) は今のところ不要。
4. Dashboard に到達したら OK。この時点では repo import は不要。

## §2. Vercel CLI セットアップ

ローカル (このリポの clone 環境) で:

```bash
npm i -g vercel        # 初回のみ
vercel --version       # 32.x 以上を確認
vercel login           # ブラウザで OAuth 承認
# → "Success! GitHub authentication complete for gethinu@…"
```

## §3. 初回デプロイ (preview URL)

```bash
cd apps/dashboards/alpaca-next
vercel                 # 対話式
```

質問への回答:
- Set up and deploy? **Y**
- Which scope? → 自分のアカウント (gethinu)
- Link to existing project? **N**
- Project name? → `quant-monitor` (or 上記 naming 候補から)
- Directory? → `./` (default)
- Modify settings? → **N** (`vercel.json` があるので自動で拾う)

出力される URL (例: `https://quant-monitor-abc123.vercel.app`) を控える。
ここまでで preview 環境が公開されている。

## §4. 本番デプロイと GitHub auto-deploy

```bash
vercel --prod          # 本番エイリアス (quant-monitor.vercel.app) に昇格
```

続けて Vercel dashboard 側で:
1. Project → Settings → Git → **Connect Git Repository**
2. `gethinu/quant_trading_system_0510to0906` を選択
3. Production branch を `main` に設定
4. Root directory を `apps/dashboards/alpaca-next` に指定 (重要)

以降 `main` に push すると自動デプロイされる。
`claude/monitor-webapp` などの feature branch は preview URL が自動発行される。

## §5. カスタムドメイン + bundle-of-edges dashboard 統合案

**カスタムドメイン (任意):**
- Vercel Settings → Domains で好きな独自ドメインを追加可能。
- 独自ドメインが無い場合は `<project>.vercel.app` のままで十分。

**bundle-of-edges (`gethinu.github.io/mt5-dashboard`) との統合:**

案 A — **iframe embed** (最短):
```html
<iframe src="https://quant-monitor.vercel.app/"
        width="380" height="480" frameborder="0"
        style="border-radius:12px"></iframe>
```
既存 `docs/dashboards/quant_trading_card.html` の外側を iframe wrapper に置き換えるだけで良い。

案 B — **link card** (軽量):
mt5-dashboard 側にサムネ + タイトル + 「Quant Monitor →」リンクだけ置き, クリックで Vercel URL に飛ばす。

**推奨**: 案 A を採用。既存 skeleton (`quant_trading_card.html`) は "静的モック" としてそのまま残しつつ, 実データは Vercel 側に移行。

---

## 運用メモ

- データ更新: `results_csv/polygon_daily_coverage_YYYYMMDD.json` が git commit されると
  Vercel が build 時に読み込むので, cron が commit すれば自動反映される。
- モックのみで動作確認したい場合は `apps/dashboards/alpaca-next/mock/` の JSON を使う
  (results_csv/ が空だと fallback される)。
- 監視: Vercel dashboard の Deployments タブで build 失敗を検知。
