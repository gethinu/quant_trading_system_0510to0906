# 事業化ロードマップ — Quant Trading Signals

作成日: 2026-07-01
対象: 株システム (sys1-7 日足 / Polygon $0 データ基盤)
方針: **Phase 1 で「自分が毎日使える無人パイプライン」を完成**させ、そのまま
Phase 2 (β配信) → Phase 3 (課金) へ拡張できる設計を最初から敷く。

---

## 全体像

```
[Phase 1] 自家用 無人化          [Phase 2] β配信 (無償/招待)        [Phase 3] 課金事業化
 cache→signals→coverage→publish   subscribers DB + SSO + 履歴DB       Stripe + tier + SLA + 法務
 Discord 単一宛先                  per-subscriber routing              請求 / 契約 / 投資助言業確認
 Vercel dashboard (自分用)         private dashboard / β招待           tier別 dashboard / API 提供
```

各 Phase の技術要素は **同じ interface** の下で差し替える:
- 配信: `common/publishers/` の `Publisher` ABC (Discord/Webhook 実装済 → LINE/Email は skeleton)
- 配信先解決: `scripts/publish_signals.py` の `resolve_publishers()` (env 単発 → `subscribers.json` routing)
- シグナル schema: `today_signals_*.json` v1.0 (`run_id` で重複配信/発注を検出)

---

## Phase 1 — 自家用 無人化 (Complete: 2026-07-01)

### 実現した state
- **06:00 JST daily** に Windows Task Scheduler → `scripts/daily_pipeline.ps1` が
  cache → signals → coverage → publish を無人で通す。
- 生成物 `results_csv/today_signals_YYYYMMDD.json` (schema v1.0) が
  Vercel dashboard (https://quant-trading-monitor.vercel.app) に build-time で反映。
- Discord webhook に system 別 summary + gate 生存率 WARN が届く。
- iPhone Safari で dashboard を開けば当日シグナルが system 別 accordion で見える。

### 技術要素 (実装済)
| 領域 | 実装 |
|---|---|
| シグナル生成 (headless) | `apps/app_today_signals.py --headless --output-json` (Streamlit UI 併存) |
| JSON 標準化 | `common/signal_export.py` (`build_signals_json`, `run_id` 採番) |
| 配信抽象 | `common/publishers/` (`Publisher` ABC + Discord/Webhook 実装, LINE/Email skeleton) |
| 配信 CLI | `scripts/publish_signals.py` (env 単発 or subscribers routing) |
| Orchestrator | `scripts/daily_pipeline.ps1` (idempotent 4 段, 失敗時 Discord WARN) |
| Dashboard | `apps/dashboards/alpaca-next/` (Next.js SSG, "Today's Signals" section) |
| Coverage 監視 | `scripts/daily_polygon_monitor.py` (既存, gate 生存率) |

### コスト / 期間
- **月額 $0** (Polygon free tier + Vercel Hobby + Discord)。追加期間: 0 (本日完了)。

---

## Phase 2 — β配信 (Next: 目安 2026 Q3, 4-6 週)

自分以外の少人数 (招待制 β, 無償) に配信し、運用負荷と価値仮説を検証する。

### technical requirements
1. **subscribers DB**: Phase 1 の `config/subscribers.json` (skeleton 実装済) を
   Supabase (Postgres) へ昇格。schema: `subscribers(id, email, tier, systems[], channels[], created_at)`。
2. **SSO 認証**: dashboard を private 化。Google / GitHub OAuth (NextAuth.js or Supabase Auth)。
3. **per-subscriber routing**: `resolve_publishers()` を subscribers DB 参照に。
   `systems[]` フィルタ (subscriber が購読する system のみ配信) と channel 別 (Discord DM / LINE / Email) を有効化。
   → `LinePublisher` / `EmailPublisher` の skeleton を本実装。
4. **履歴 DB**: 配信済シグナルを `signal_history(run_id, date, system, symbol, side, ...)` に永続化。
   `run_id` で重複配信を DB 制約 (unique) で防止。
5. **private dashboard**: Streamlit Cloud or Vercel private page で subscriber 別ビュー。

### cost 見積 / 依存
- Supabase free (〜500MB) / Vercel Hobby: **月額 $0-25**。
- 依存: Phase 1 の `Publisher` ABC・`subscribers.json` schema・`run_id` はそのまま利用。
- リスク: β配信でも「投資助言」に該当しうる → Phase 3 の法務確認を前倒しで着手 (下記)。

---

## Phase 3 — 課金事業化 (Future: 目安 2026 Q4〜, 法務がクリティカルパス)

### technical requirements
1. **Stripe integration**: Checkout + Billing Portal + Webhook (`checkout.session.completed`,
   `customer.subscription.deleted`)。subscriber の `tier` を Stripe subscription と同期。
2. **tier 別提供**:
   | tier | 内容 | 想定価格 |
   |---|---|---|
   | Basic | sys1/4/7 の signal + dashboard | 月額 低 |
   | Pro | 全 sys1-7 + gate 生存率 + 履歴 API | 月額 中 |
   | Enterprise | 上記 + raw JSON API + SLA + 個別 slot 設定 | 応相談 |
3. **SLA**: 配信時刻保証 (06:30 JST までに配信) / 稼働率。
   監視: pipeline 失敗時の Discord WARN (Phase 1 実装済) を PagerDuty/Opsgenie に昇格。
4. **API 提供**: `today_signals_*.json` を認証付き REST/Webhook で Pro/Enterprise に配信。

### 法務 / 税務 (事業化の必須ゲート — 技術より先に確認)
- **投資助言業ライセンス**: 日本で対価を得て投資判断を助言/代理すると
  金融商品取引法上の「投資助言・代理業」登録が必要になりうる。
  → 「シグナル情報の提供」を助言と切り分けられるか、要 **専門家 (弁護士/行政書士) 確認**。
  → 免責 (投資は自己責任, 実発注は subscriber 自身) を利用規約に明記。
- **利用規約 / 契約書**: subscriber との契約, 免責, 返金ポリシー, 解約。
- **税務**: 継続課金の売上計上, インボイス, 消費税。
- **特商法表記**: 課金 web には必須。

### cost 見積 / 依存
- Stripe: 決済手数料 (〜3.6%)。インフラ: Supabase Pro / Vercel Pro で **月額 $45-70**。
- 法務確認: 初期 数十万円規模を見込む (**課金開始前の必須先行投資**)。
- 依存: Phase 2 の subscribers DB / SSO / 履歴 DB / per-subscriber routing 完了が前提。

---

## 拡張性を担保する設計原則 (Phase 1 で既に守っているもの)

1. **配信は `Publisher` ABC 越し**: 宛先追加 = クラス追加のみ (core/pipeline 不変)。
2. **secret は env 参照**: `subscribers.json` に生の webhook/token を置かず `*_env` キーで解決 → DB 化しても安全。
3. **`run_id` 一意採番**: 重複配信/発注検出の鍵を Phase 1 から全 payload に付与済。
4. **schema versioning**: `today_signals_*.json` に `version` フィールド → 破壊的変更時に共存可能。
5. **core/system1-7 + CacheManager 不変**: シグナル生成ロジックは事業レイヤと分離済。
