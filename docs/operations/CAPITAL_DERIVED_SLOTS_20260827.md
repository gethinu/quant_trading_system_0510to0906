# 資金配分から system 別枠を導く設計（paper only）

## 先に置く前提と対象外

- 対象はこのリポジトリだけであり、発注・MT5・scheduler・`.env`・deploy は変更しない。
- 枠は「候補を何銘柄まで通すか」の上限である。`risk_pct=2%`、`max_pct`、注文時の `equity_deploy_pct=0.5` と weight 比による発注額は変更しない。
- equity は実行時に `RunContext.start_equity` を使う。読めない場合だけ、既存 `finalize_allocation` 引数の `default_capital`（既定 `$100,000`）へ明示的に退避する。どちらを使ったかは `system_diagnostics.capital_slots.equity_source` に残す。
- 8/26 リプレイは公開済み `today_signals`（すでに旧枠・既存保有・portfolio cap を通った出力）を再選抜するもの。上流の候補全件と system 別既存保有の対応は成果物にないため、上流候補からの完全再現ではない。

## 現状の裏取り

| 経路 | 確認結果 |
|---|---|
| `config/config.yaml:4-10` | global の `risk.max_positions: 10` と `max_pct: 0.10`。`config/config.yaml:24-29` は total=70 / long=40 / short=30 / gross≤1.0 / |net|≤0.5。 |
| `config/settings.py:54-62,423-441` | YAML を `RiskConfig` に載せる。 |
| `strategies/base_strategy.py:60-69` | 各 strategy に同じ global `max_positions` を初期設定してから system 固有設定を重ねる。既存 YAML に system 固有 `max_positions` はない。 |
| `core/final_allocation.py:1812-1834` | system ごとの枠を解決する。実際の `finalize_allocation` は `core/final_allocation.py:1940` からであり、指定された `scripts/final_allocation.py` は存在しない。 |
| `scripts/run_all_systems_today.py:5325-5337` | 本番 paper 経路から `finalize_allocation` を呼ぶ。 |
| `core/final_allocation.py:1758-1779,1863-1920,2518-2535` | `[side, _system_no]` に並べ、最後に portfolio cap で末尾を落とす。従来は S5/S7 が後ろになりやすい。 |

side は S1/S3/S4/S5 が long、S2/S6/S7 が short。資金配分は `config/config.yaml:219-228` の long 各 25%、short は S2=40% / S6=40% / S7=20% で、side 内で正規化して使う。

## 設計判断

### 1. 導出式とサイジングとの分離

ON 時だけ、system `i` の枠を次で求める。

```text
B_i = E × G × F × side_share_i × weight_i
N_i = E × max_pct_i
raw_slots_i = B_i / N_i
slots_i = max(min_slots, floor(raw_slots_i))    # B_i>0 の場合だけ最小枠を適用
```

- `E`: 上記の開始 equity。
- `G`: `risk.portfolio.max_gross_exposure_pct`。
- `F`: 新規 `risk.slots_from_capital_gross_budget_factor`（0〜1）。既存 gross 上限を越す係数は受け付けない。
- `weight_i`: `ui.long_allocations` または `ui.short_allocations`。side 内で合計 1 に正規化される既存の出所。
- `side_share_i`: `ui.default_long_ratio`（現設定 50/50）。ただし portfolio の net 上限に収まる範囲へクランプする。
- `N_i`: 日々の ATR や銘柄価格ではなく、固定設定 `max_pct_i`。S1〜S6 は 10%、S7 は system 固有 20%。したがって枠は銘柄のボラティリティでは日々揺れない。

これは駐車場の区画数を、各チームに割り当てた駐車面積と「車一台の最大面積」から決める形である。注文サイズそのものは別の会計で、`common/alpaca_trading.py:685-718` が `equity_deploy_pct=0.5` の tier 予算を signal weight で割る。ここを変更しないので二重計上しない。

### 2. 既存上限との衝突

既存上限を残し、優先順位を最上位にする。導出された枠は long=40、short=30、合計=70 を超えないよう、同じ side の raw 値の最大剰余法で縮める。gross は `F≤1` で計画予算から超えず、net は `|2×long_share-1|×G≤max_net_exposure_pct` になるよう side share を先にクランプする。最後の `_apply_portfolio_caps` は既存保有と実際の notional を含む最終防波堤として残る。

### 3. 端数と最低枠

既定 `min_slots=1`。正の予算を持つ system が `floor` で 0 になっても一枠を持つ。これは S7 のヘッジ可用性を残す明示的な方針であり、必要なら `risk.slots_from_capital_min_slots: 0` で 0 枠を許可できる。予算が 0 の system には最低枠を与えない。

### 4. 後方互換

`risk.slots_from_capital: false` が既定。OFF では新しい計算を一切呼ばず、既存 global `max_positions=10` の経路をそのまま通る。OFF 時に `slot_capital_equity` を渡しても DataFrame CSV bytes と summary JSON が一致するテストを固定した。

## 8/26 の実数による S1〜S7 計算例

入力は `results_csv/paper_orders_20260826.json` の `account_equity_usd=$100,879.96`、現設定 `G=1.0, F=1.0, long/short=0.5/0.5`。

| system | weight | B_i（資金配分） | N_i（一枠必要額） | raw | 導出枠 |
|---|---:|---:|---:|---:|---:|
| S1 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S2 | 40% | $20,175.99 | $10,088.00 | 2.00 | 2 |
| S3 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S4 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S5 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S6 | 40% | $20,175.99 | $10,088.00 | 2.00 | 2 |
| S7 | 20% | $10,088.00 | $20,175.99 | 0.50 | 1（最低枠） |

導出枠の合計は long=4、short=5、total=9 で、`4≤40, 5≤30, 9≤70`。計画予算は long=$50,439.98 / short=$50,439.98、gross=$100,879.96≤equity、net=$0≤$50,439.98。従ってこの実データでは上限衝突なしを数値で確認できる。

## 切替と監査情報

```yaml
risk:
  slots_from_capital: false       # false: 現行と同一、true: 新方式
  slots_from_capital_gross_budget_factor: 1.0
  slots_from_capital_min_slots: 1
```

ON の run summary には `capital_slots` として equity/source、raw slots、system budget、一枠必要額、適用 pool cap を記録する。OFF にはこのキーを追加しない。

## テストと 8/26 リプレイ

実行コマンド:

```powershell
pytest -q -o addopts='' tests/test_capital_weighted_slots_20260827.py
python tools/replay_capital_slots.py --signals results_csv/today_signals_20260826.json --paper-orders results_csv/paper_orders_20260826.json
```

テストは (a) 上式どおりの S1〜S7、(b) count pool と gross/net 計画予算を超えない、(c) OFF の bytes/summary 完全一致、(d) ON が各 system の枠を実際に置換する、を検証する。

公開済み signals を rank 順に新枠まで再選抜した before→after は次のとおり。paper order は同じ symbol の既存成果物行を照合した結果であり、再発注はしていない。

| system | 枠 | signals before→after | paper submitted before→after | artifact notional before→after | 選ばれる symbol |
|---|---:|---:|---:|---:|---|
| S1 | 1 | 9→1 (-8) | 3→1 | $14,292.50→$1,387.38 | WETO |
| S2 | 2 | 10→2 (-8) | 8→2 | $32,358.68→$9,287.86 | XNCR, RGEN |
| S3 | 1 | 4→1 (-3) | 4→1 | $3,788.81→$751.71 | TNON |
| S4 | 1 | 0→0 | 0→0 | $0.00→$0.00 | — |
| S5 | 1 | 0→0 | 0→0 | $0.00→$0.00 | — |
| S6 | 2 | 0→0 | 0→0 | $0.00→$0.00 | — |
| S7 | 1 | 0→0 | 0→0 | $0.00→$0.00 | — |
| 合計 | 9 | 23→4 (-19) | 15→4 | $50,439.99→$11,426.95 | — |

`artifact notional` は既存 order 行を残存 symbol で集計しただけで、新方式で注文額を再計算した値ではない。サイズ計算を変えないという本変更の制約を守るためである。

## 未確認

- 8/26 の system 別「旧枠適用前」候補全件、および system 別既存保有の対応表は二つの指定成果物に含まれない。そのため、旧ソート末尾で落ちた S5/S7 候補を含む完全な再演算は未確認である。
- ON を実運用で有効化する判断、または paper run はこの変更では行わない。
