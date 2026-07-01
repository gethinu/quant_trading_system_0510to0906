# HUMAN TASK — Alpaca Paper Trading 自動発注 (2026-07-01)

当日シグナル (`app_today_signals` / `run_all_systems_today`) の `final_df` を
**Alpaca Paper 口座** へ自動発注するための runbook。

> ⚠️ **このリポジトリの自動化 (Claude / CI) は実発注を一切行いません。**
> 実際の Paper order を送信するのは **あなた (user) が 1 回だけ手動で叩く** 下記コマンドのみです。
> Live 口座への移行も自動では行いません (Section 6)。

実装ファイル:

| 目的 | ファイル |
|------|----------|
| 発注 core (dry-run / paper guard / 監査ログ) | `common/alpaca_trading.py` |
| 低レベル SDK ラッパー (既存・拡張) | `common/broker_alpaca.py` |
| dry-run 可視化 CLI | `scripts/paper_trading_dryrun.py` |
| 実発注 CLI (user 手動) | `scripts/paper_trading_submit.py` |
| テスト (offline mock) | `tests/test_alpaca_trading_mock.py`, `tests/test_signals_to_orders.py` |

前提 (`.env`):

```
APCA_API_KEY_ID=...
APCA_API_SECRET_KEY=...
ALPACA_API_BASE_URL=https://paper-api.alpaca.markets
ALPACA_PAPER=true
```

---

## Section 1 — dry-run 確認手順 (実発注なし)

送信予定の注文を表形式で確認する。**API キー不要・発注しない。**

```bash
# 動作確認 (内蔵デモ fixture)
python scripts/paper_trading_dryrun.py --demo

# 当日シグナル CSV を指定
python scripts/paper_trading_dryrun.py --date 2026-06-30
python scripts/paper_trading_dryrun.py --signals-csv results_csv_test/signals_final_2026-06-30.csv
```

出力例:

```
===== DRY-RUN: 送信予定注文 (実発注なし) =====
symbol side  qty order_type  limit_price time_in_force  system       client_order_id
  AAPL  buy   10     market          NaN           day system1 system1-AAPL-20260630
  TSLA sell    8      limit        250.0           day system2 system2-TSLA-20260630
   SPY sell    3      limit        545.0           day system7  system7-SPY-20260630
```

- `system1/3/4/5` → market、`system2/6/7` → limit (既存 `submit_orders_df` と同じ規約)
- `client_order_id` = `{system}-{symbol}-{YYYYMMDD}` (冪等キー、重複発注防止)
- `SPY` (system7) は hedge short としてそのまま尊重される

---

## Section 2 — Paper 実発注 (★ user が 1 回だけ叩く)

### まず `--confirm` 無しで最終確認 (dry-run と等価):

```bash
python scripts/paper_trading_submit.py --date 2026-06-30
```

### 問題なければ実発注 (対話確認あり — 各注文で `[y/N]`):

```bash
python scripts/paper_trading_submit.py --date 2026-06-30 --confirm
```

### 無人実行 (対話プロンプトなし・Paper のみ):

```bash
python scripts/paper_trading_submit.py --date 2026-06-30 --confirm --yes
```

送信前に自動で以下を検証し、live 設定を検出したら **即中止** する:
- `ALPACA_PAPER=true`
- `ALPACA_API_BASE_URL` が `paper-api.alpaca.markets` を指す

> 💡 デモで一連の流れを試すなら `--date` の代わりに `--demo` を付ける
> (それでも `--confirm` 時は実際に Paper API へ送信するので注意)。

---

## Section 3 — 発注結果の確認

### Alpaca Paper dashboard
1. https://app.alpaca.markets/paper/dashboard/overview を開く
2. **Orders** タブ … 送信した注文の status (`accepted`/`filled`/`rejected`)
3. **Positions** タブ … 約定後の保有ポジション
4. `client_order_id` 列で `system1-AAPL-20260630` 形式のキーが一致するか確認

### 監査ログ (ローカル)
送信内容は JSON 1 行/注文で追記される:

```bash
cat logs/alpaca_orders_$(date +%Y%m%d).log
```

各行: `ts`, `event` (`dry_run`/`submitted`/`submit_error`), `symbol`, `qty`, `side`,
`order_id`, `status`, `client_order_id` など。

---

## Section 4 — 停止 / 取消 (誤発注時のロールバック)

### 未約定注文をすべてキャンセル

```python
from common import broker_alpaca as ba
client = ba.get_client(paper=True)   # ALPACA_PAPER=true 前提
ba.cancel_all_orders(client)         # 全 open 注文をキャンセル
```

### 特定ポジションをクローズ (成行)

既存の `scripts/exit_all_positions.py` を利用するか、単発なら:

```python
from common.alpaca_trading import submit_paper_order
# ロング 10 株を反対売買でクローズ
submit_paper_order("AAPL", 10, "sell", dry_run=False)
```

> ⚠️ 約定済ポジションは「キャンセル」できない。反対売買でクローズする。
> `client_order_id` により同一シグナルの二重発注は Alpaca 側で拒否される (冪等)。

---

## Section 5 — production 化 (日次自動発注)

> **user が最初の数日は必ず monitor すること。** 完全自動化はその後。

Windows Task Scheduler での日次実行例 (US 市場 open 前、ET 08:00 = JST 21:00〜22:00):

1. `scripts/run_all_systems_today.py` で当日シグナル (`final_df`) を CSV 出力
2. 続けて `scripts/paper_trading_submit.py --date <today> --confirm --yes` を実行

タスク登録 (PowerShell, 管理者):

```powershell
$action  = New-ScheduledTaskAction -Execute "python" `
  -Argument "scripts\paper_trading_submit.py --date TODAY --confirm --yes" `
  -WorkingDirectory "C:\Repos\quant_trading_system_0510to0906"
$trigger = New-ScheduledTaskTrigger -Daily -At 21:30
Register-ScheduledTask -TaskName "AlpacaPaperSubmit" -Action $action -Trigger $trigger
```

監視項目:
- `logs/alpaca_orders_*.log` の `submit_error` 行
- Alpaca dashboard の rejected 注文 (資金不足 / 市場休場 / 無効シンボル)
- `signals_to_orders` の open-position 照合で重複買いが抑制されているか

---

## Section 6 — Live 口座移行チェックリスト (慎重に)

> 🛑 **Live 移行は完全に user の判断。このリポジトリは Paper 前提で固定。**
> Paper で最低 **数週間** 安定稼働し、約定・スリッページ・数量を検証してから。

`ALPACA_PAPER=false` へ切り替える前に確認:

- [ ] Paper で 2〜4 週間、想定どおりの約定 (fill rate / slippage) を確認した
- [ ] `common/alpaca_trading.assert_paper_env()` は live で **例外を出す** 設計。
      Live 発注するには **意図的に** このガードを緩める必要がある (安全側のデフォルト)
- [ ] Live 用 API キー (`APCA_API_KEY_ID` / `APCA_API_SECRET_KEY`) は Paper と別物
- [ ] `ALPACA_API_BASE_URL=https://api.alpaca.markets` (paper ホスト検証も要更新)
- [ ] ポジションサイジング (`finalize_allocation` の `default_capital`) が
      **実口座残高** に一致しているか — Paper のダミー残高のままだと過大発注
- [ ] PDT (Pattern Day Trader) 規制 / 空売り可否 (`get_shortable_map`) / 手数料を再確認
- [ ] 1 注文あたり最大数量・1 日最大発注数の上限を設ける (誤発注時の被害限定)
- [ ] 最初は極小サイズ (1〜2 株) で 1 日だけ live smoke → dashboard 突合

### Live 化の主なリスク (要理解)

| リスク | 内容 |
|--------|------|
| 実資金の損失 | Paper と違い誤発注・バグがそのまま金銭損失になる |
| 過大発注 | `default_capital` が実残高とずれると想定外サイズを発注 |
| 約定差 | Paper は理想約定。Live はスリッページ / 部分約定 / 板薄で乖離 |
| 規制 | PDT・空売り規制・borrow 不可銘柄で reject / 強制クローズ |
| レート制限 | free tier 200 orders/min。大量発注時は backoff 必須 (実装済) |

---

## Section 7 — account_equity scale 運用 ($1k / $10k / $100k)

signals JSON (`results_csv/today_signals_YYYYMMDD.json`, Phase 1 pack) から
**資本額に応じて** orders を生成する新経路 (`signals_json_to_orders`)。
`$1k でも $100k でも` 同じ signals で動く。

追加/拡張ファイル:

| 目的 | ファイル |
|------|----------|
| JSON→orders + tier sizing | `common/alpaca_trading.py` (`signals_json_to_orders`, `OrderPlan`, `resolve_tier`) |
| notional (fractional) 発注 | `common/broker_alpaca.py` (`submit_order` に `notional` param 追加) |
| scale 別 dry-run preview | `scripts/paper_trading_dryrun.py --account-equity` |
| scale 別 実発注 + 突合 | `scripts/paper_trading_submit.py --account-equity` |
| pipeline preview step | `scripts/daily_pipeline.ps1 -AccountEquity` |
| dashboard | `apps/dashboards/alpaca-next` (Today's Orders Preview) |
| tests | `tests/test_signals_json_to_orders.py`, `tests/test_paper_trading_submit_mock.py` |

### tier 決定マトリクス

| equity | tier | signals | sizing | hedge | fractional |
|--------|------|---------|--------|-------|------------|
| < $10k | small | 各 sys の rank==1 のみ | `weight×equity`、$5未満 skip | 標準 | 必須 (notional) |
| $10k–100k | medium | 全 signals | `weight×equity` | 標準 | 推奨 |
| >= $100k | large | 全 signals | `weight×equity` | SPY(sys7) weight ×1.5 | whole share 可 |

### 各 scale の dry-run preview (実発注なし)

```powershell
python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 1000
python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 10000
python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 100000
# -> results_csv/orders_preview_YYYYMMDD_${equity}.json
```

### 実発注 (★ user 手動、Paper のみ、preview 突合あり)

```powershell
# 事前に同じ --account-equity で preview を生成しておく (突合基準)
python scripts/paper_trading_submit.py --date 2026-07-01 --account-equity 10000 --confirm --yes
```

### rollback (全 open orders cancel)

```powershell
python -c "from alpaca.trading.client import TradingClient; TradingClient(paper=True).cancel_orders()"
```

### Live 移行 追加確認 (`ALPACA_PAPER=false`)

Section 6 に加え: 1 発注あたりの上限 notional / 1 日発注上限の設定、
live 用 monitoring (ntfy/email) の分離、法務 (助言・運用業登録要否)・税務・リスク説明の文書化。

---

## まとめ — user が最初に叩く 2 コマンド

```powershell
# ① dry-run で確認 (実発注なし、preview JSON 生成)
python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 10000

# ② 確認できたら Paper へ実発注 (これだけが実発注、Paper のみ)
python scripts/paper_trading_submit.py --date 2026-07-01 --account-equity 10000 --confirm --yes
```
