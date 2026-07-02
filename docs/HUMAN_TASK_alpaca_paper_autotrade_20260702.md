# Alpaca Paper 自動売買 wiring — 2026-07-02 追加分

## 目的

`daily_pipeline.ps1` に **Alpaca Paper 口座向け発注 intent 生成 step (`paper_orders`)** を追加し、既存 subscriber tier (small/medium/large) に応じた notional を毎日算出する。

**絶対原則**:
- **live account (実マネー) は本 pipeline では扱わない**。別プロセス。
- default は必ず **dry-run** (JSON 出力のみ、実発注 0)。実発注は user が `-AutoSubmitPaper` を明示指定した時のみ。
- `assert_paper_env()` が実発注前に必ず走り、`ALPACA_PAPER=false` / live host / strict mode 未 opt-in を fail-fast で block。

## 変更 files

| ファイル | 変更内容 |
|---|---|
| `common/alpaca_trading.py` | (1) `signals_json_to_orders()` 新設 — today_signals JSON → tier notional で PreparedOrder に変換、fractional (notional 発注) / integer share 両対応。(2) `TIER_NOTIONAL_USD` (small=$1k / medium=$10k / large=$100k) + `resolve_tier_notional()`。(3) strict mode 追加: `ALPACA_PAPER_STRICT=1` で `ALPACA_PAPER` 未設定を fail-fast。(4) `PreparedOrder` に `notional_usd`, `tier`, `dry_run` フィールド追加。 |
| `scripts/paper_trading_dryrun.py` | `--signals-json` + `--tier` + `--output-json` + `--min-notional` + `--no-fractional` を追加。JSON 経路は `_dryrun_from_json()`、CSV 経路は既存維持。 |
| `scripts/paper_trading_submit.py` | 同上の CLI 引数追加。`--confirm --yes` かつ JSON 指定で `signals_json_to_orders(dry_run=False)` を呼び、実発注 (paper) を行う。`assert_paper_env()` を必ず先に呼ぶ。 |
| `scripts/daily_pipeline.ps1` | (1) 新 param: `-AutoSubmitPaper`, `-SkipPaperOrders`, `-Tier`。(2) 新 step: `paper_orders` を `publish` と `vercel` の間に挿入。default は `paper_trading_dryrun.py`、`-AutoSubmitPaper` 時のみ `paper_trading_submit.py --confirm --yes`。 |
| `.env.example` | `ALPACA_PAPER_STRICT=0`, `ALPACA_TIER=small`, `ACCOUNT_EQUITY_USD=10000` を追加。 |

## 追加 test 一覧

| test | 内容 |
|---|---|
| `tests/test_alpaca_paper_only_enforce.py` | `assert_paper_env()` が false / live host / strict-mode 未設定で `LiveAccountGuardError` を raise することを 8 パターン固定化 |
| `tests/test_paper_trading_dryrun_output_schema.py` | `signals_json_to_orders()` 出力 JSON の schema (symbol/side/qty/notional_usd/tier/dry_run 等 11 field) と tier 別 notional 配分 (small=$1k / medium=$10k / large=$100k)、min_notional skip、fractional / integer 両モードを 12 test で固定化 |
| `tests/test_alpaca_no_live_url.py` | repo 全体を grep して `api.alpaca.markets` (live URL) の直参照が code (.py/.ps1) に無いことを assert。allowlist は guard test のみ。 |
| `tests/system/test_daily_pipeline_paper_orders_step.py` | `daily_pipeline.ps1` に paper_orders step が存在し、default が dryrun、`-AutoSubmitPaper` で submit、`--confirm --yes` 併用、step 順序 (publish → paper_orders → vercel) を 10 test で固定化 |

sandbox 実行結果: **31 passed** (pytest via mount で v2 file 経由。Windows で `pytest tests/` を通すのが最終判定)。

## `daily_pipeline.ps1` の新 flag semantics

```
-AutoSubmitPaper  無 → paper_orders_dryrun (JSON 出力のみ、実発注 0)  ← default
                  有 → paper_orders_submit (Paper 口座へ実発注)
-SkipPaperOrders  step 自体を丸ごとスキップ (緊急停止用)
-Tier <small|medium|large>  未指定なら env ALPACA_TIER、それも無ければ "small"
```

**重要**: Task Scheduler の Action argument に `-AutoSubmitPaper` を **含めない限り**、無人 tick では発注は起きない。存在するだけの switch のため、手動実行時のみ user が付ける。

## Windows 側の試験手順

### 1. 環境変数確認 (.env)

```
APCA_API_KEY_ID=<paper key>
APCA_API_SECRET_KEY=<paper secret>
ALPACA_PAPER=true
ALPACA_PAPER_STRICT=0        # 1 にすると ALPACA_PAPER の明示設定を強制
ALPACA_TIER=small            # small / medium / large
ACCOUNT_EQUITY_USD=10000
```

### 2. 単体テスト (dryrun script のみ)

```powershell
cd C:\Repos\quant_trading_system_0510to0906
python scripts/paper_trading_dryrun.py `
    --signals-json results_csv/today_signals_20260701.json `
    --tier small `
    --output-json results_csv/paper_orders_20260701.json
```
期待: 49 orders (49 signals から生成)、total_notional=$1000、`paper_orders_20260701.json` が生成される。

### 3. pipeline dryrun 経路 (発注なし)

```powershell
.\scripts\daily_pipeline.ps1 -Date 2026-07-01 -SkipCache
```
paper_orders step で `paper_trading_dryrun.py` が呼ばれ、`paper_orders_20260701.json` が生成される。ntfy WARN は無し。log 末尾に `[paper_orders] dry-run (submit skipped: autosubmit not enabled)` が出る。

### 4. pipeline 実発注 (paper 口座、user 明示 opt-in)

```powershell
.\scripts\daily_pipeline.ps1 -Date 2026-07-01 -SkipCache -AutoSubmitPaper -Tier small
```
paper_orders step で `paper_trading_submit.py --confirm --yes` が呼ばれ、Alpaca Paper 口座へ **実発注**する。`assert_paper_env()` を通過するため事前に `.env` の `ALPACA_PAPER=true` 必須。

### 5. pytest regression check

```powershell
python -m pytest tests/test_alpaca_paper_only_enforce.py tests/test_paper_trading_dryrun_output_schema.py tests/test_alpaca_no_live_url.py tests/system/test_daily_pipeline_paper_orders_step.py -v
```
期待: 全 pass。

## Task Scheduler 統合 (user が判断して行うタスク)

現状 Task Scheduler は `daily_pipeline.ps1` (argument なし) を呼んでいる想定。**この構成のままなら paper 発注は起きない (dryrun のみ)**。

paper 自動発注を実運用に載せるには Action argument に `-AutoSubmitPaper -Tier small` を追加する。**推奨: 最初は数営業日 dryrun output (`paper_orders_YYYYMMDD.json`) を目視レビュー**してから AutoSubmit を有効化する。

## live 口座 (実マネー) 移行について

**本 pipeline では絶対に扱わない**。live 移行時は:
1. 別 branch / 別 repo で live 用 pipeline を作る
2. `common/alpaca_trading.py` の `_PAPER_HOST` guard は残したまま、live 用 client init は別モジュールに切り出す
3. `tests/test_alpaca_no_live_url.py` は live pipeline では allowlist を差し替える
4. UI / dashboard で「live 発注中」を絶対に見落とさないよう視覚的警告を付ける

この 4 段階を経ずに live 発注する code path を書かないこと。

## regression 保護まとめ

- `test_alpaca_paper_only_enforce.py` — paper guard が偽/live URL/strict mode 未設定で必ず raise
- `test_alpaca_no_live_url.py` — `api.alpaca.markets` (live) の直参照を CI で block
- `test_paper_trading_dryrun_output_schema.py` — 出力 JSON schema と tier notional 配分の固定化
- `test_daily_pipeline_paper_orders_step.py` — ps1 に AutoSubmitPaper switch と dryrun default が残ることを固定化

これらを消したり weaken するな。paper 誤爆の最終防波堤。
