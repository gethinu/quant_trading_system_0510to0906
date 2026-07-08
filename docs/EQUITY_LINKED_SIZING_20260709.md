# Equity 連動ポジションサイジング (2026-07-09)

## 背景 / 問題
従来のペーパー発注サイジングは **口座 equity($100k超) を一切使っていなかった**。予算は
tier 固定額 (`small=$1,000`) を各シグナルの weight で配分するだけ:

```
notional_i = weight_i × tier_notional     # tier="small" = $1,000 固定
```

このため 1 銘柄あたり ~$20〜51 にしかならず、**ショートは整数株必須 (Alpaca は
fractional short 不可)** なので高株価銘柄は `int(notional/price)==0` で毎日 skip されて
いた (2026-07-08: 5 ショートが 1 株未満で skip。`logs/short_sizing_analysis_20260708.md`)。

## 変更
サイジングを **equity 連動** にする:

```
deploy_budget = current_equity × equity_deploy_pct     # 既定 pct = 1.0
notional_i    = weight_i × deploy_budget               # Σweight で正規化 (Σnotional = budget)
```

`equity_deploy_pct` は config の単一ノブ。既定 **1.0** (gross 目標 ≈ 1.0×equity =
既存 gross cap いっぱい)。後から 0.5 等に変えれば全体を比例縮小できる。

### リスク層は全部維持 (絶対に緩めない)
`equity_deploy_pct` は **既存 cap の内側** で効く。予算を上げても cap は従来通り縛る。

| cap | 値 | どこで効くか |
|---|---|---|
| per-name max | `risk.max_pct` = 10% × equity | equity_linked サイジング内 (`compute_position_notionals`) で clamp |
| gross exposure | `risk.portfolio.max_gross_exposure_pct` = 1.0 × equity | 同上 (超過なら全体を比例縮小) |
| net exposure | `risk.portfolio.max_net_exposure_pct` = 0.5 × equity | 同上 (超過なら優勢サイドのみ縮小、gross は増やさない) |
| 件数 cap | 70 / 40 / 30 (total/long/short) | 上流 `core.final_allocation._apply_portfolio_caps` (today_signals 生成時) |
| system 別 max_positions スロット | 10/system | 上流 `core.final_allocation` |
| min_notional | $5 | `signals_json_to_orders` (従来通り) |
| ショート/非fractionable 整数株 floor | — | `plan_order_execution` (従来通り、submit 時) |

cap 適用順: **per-name clamp → gross 縮小 → net 縮小**。per-name は hard cap で、
削った分を他銘柄へ再配分しない。

### 後方互換
tier ベースは撤去せず `sizing.mode` で切替可能:

```yaml
sizing:
  mode: equity_linked        # equity_linked (既定) | fixed_tier
  equity_deploy_pct: 1.0
```

- `mode: fixed_tier` は従来の tier 固定予算 (small=$1k/medium=$10k/large=$100k)。
  **tier 経路には dollar cap を掛けない (完全な従来挙動)**。
- env override: `SIZING_MODE`, `EQUITY_DEPLOY_PCT`。
- 未知 mode → `equity_linked` に、`equity_deploy_pct <= 0`/非数 → `1.0` に安全フォールバック。

### equity の取得元
`equity_linked` 実行時、`resolve_sizing_equity` が Alpaca **paper** 口座の実 equity を
read-only で取得 (`get_account().equity`)。**発注は一切しない。**

- creds 無し / 取得失敗 / equity<=0 → `--equity`(既定 $10k) へ安全フォールバック。
- `TEST_MODE` 環境変数 or `--no-equity-fetch` 指定時は fetch を抑止 (テスト/決定論)。
- `fixed_tier` では equity を sizing に使わない (fallback をそのまま保持)。

出力 JSON (`paper_orders_*.json`) の meta に `sizing_mode` / `equity_deploy_pct` /
`account_equity_usd` / `equity_source` / cap 値を記録 (観測性)。

## dry-run 実証 (2026-07-08 の 45 シグナル, equity=$106,252)
`results_csv/today_signals_20260708.json` に対し、submit 時サイジングを再現
(long=fractional notional / short=整数株 floor / min_notional $5)。**発注なし。**

### before / after 集計
| 指標 | 旧 (fixed_tier $1k) | 新 (equity_linked pct=1.0) |
|---|---:|---:|
| 生成シグナル | 45 | 45 |
| 送信可 | 39 | **45** |
| skip 合計 | 6 | **0** |
| うち ショート端株 skip | **5** | **0** |
| gross deploy | $827 | **$105,783** |
| long deploy | $650 | $69,567 |
| short deploy | $177 | $36,217 |
| \|net\| | $473 | $33,350 |

### cap チェック (新, equity=$106,252)
| cap | 上限 | 実測 | 判定 |
|---|---:|---:|:--:|
| gross ≤ 1.0×equity | $106,252 | $105,783 | ✅ |
| \|net\| ≤ 0.5×equity | $53,126 | $33,350 | ✅ |
| per-name ≤ 10%×equity | $10,625 | max $5,461 | ✅ (clamp 0 件) |

pct=1.0 では最大 weight (GPGI 5.14%) でも per-name cap の半分程度なので clamp は発火せず。
net も 0.5×equity 以内。**全 cap の内側**に収まる。

### 以前 skip されていた 5 ショートが約定サイズに (端株 floor 後)
| 銘柄 | weight | 株価 | 旧 notional→株数 | 新 notional→株数 |
|---|---:|---:|---|---|
| LOAR | 4.10% | $81.29 | $41.00 → 0 (skip) | $4,355 → **53** |
| DD   | 2.37% | $141.05 | $23.70 → 0 (skip) | $2,517 → **17** |
| VRNS | 3.07% | $45.67 | $30.70 → 0 (skip) | $3,261 → **71** |
| QLYS | 2.66% | $158.26 | $26.60 → 0 (skip) | $2,826 → **17** |
| PLMR | 1.99% | $142.13 | $19.90 → 0 (skip) | $2,114 → **14** |

長い側でも SPCX ($160.42) は旧 $4.50 で min_notional skip → 新 $478 で送信可。

## ロールアウト
- 既定が `equity_linked` に変わるため、次ランから daily_pipeline (paper_orders step) は
  equity 連動でサイジングする。tier に戻したい場合は `sizing.mode: fixed_tier`。
- 2026-07-08 の実発注は完了済み。本変更は **次ラン反映** (今日は再発注しない)。

## 関連
- `common/alpaca_trading.py`: `compute_position_notionals`, `NotionalPlan`,
  `resolve_sizing_equity`, `fetch_account_equity`, `signals_json_to_orders`。
- `config/settings.py`: `SizingConfig`。`config/config.yaml`: `sizing` セクション。
- テスト: `tests/test_equity_linked_sizing.py`。
- 前提資料: `logs/short_sizing_analysis_20260708.md` (旧サイジングの実測分析)。
