# 資金配分から system 別枠を導く設計（paper only、既定 OFF）

## 先に置く前提と対象外

- 対象はこのリポジトリだけであり、発注・MT5・scheduler・`.env`・deploy は変更しない。
- 枠は「候補を何銘柄まで通すか」の上限である。`risk_pct=2%`、`max_pct`、注文時の
  `equity_deploy_pct=0.5` と weight 比による発注額は変更しない。
- equity は実行時に `RunContext.start_equity` を使う。読めない場合だけ
  `finalize_allocation` の `default_capital`（既定 `$100,000`）へ退避し、**その退避は
  WARNING に残す**（`core/final_allocation.py:2239`）。どちらを使ったかは
  `system_diagnostics.capital_slots.equity_source` にも残る。
- `risk.slots_from_capital` は **既定 false**。本ドキュメントの内容は ON にしない限り
  一切実行されない。

## 現状の裏取り（本ブランチ `claude/pr167-fixed` の行番号）

| 経路 | 確認結果 |
|---|---|
| `config/config.yaml:4-18` | global の `risk.max_positions: 10` と `max_pct: 0.10`、新フラグ 3 本。`config/config.yaml:24-29` が total=70 / long=40 / short=30 / gross≤1.0 / \|net\|≤0.5。 |
| `config/settings.py:62-65,480-492` | YAML を `RiskConfig` に載せる。env `SLOTS_FROM_CAPITAL` で上書き可。 |
| `strategies/base_strategy.py:60-69` | 各 strategy に同じ global `max_positions` を初期設定してから system 固有設定を重ねる。既存 YAML に system 固有 `max_positions` はない。 |
| `core/final_allocation.py:1947` | `_resolve_max_positions` が system ごとの legacy 枠を解決する。 |
| `core/final_allocation.py:2132` | `finalize_allocation` 本体。 |
| `core/final_allocation.py:1360` | `_allocate_by_capital`。**自前で `strategy.config['max_positions']` から上限を組み直す**ため、導出枠を渡さないと capital mode では新方式が無効になる（下記 H-2）。 |
| `scripts/run_all_systems_today.py:5641-5656` | 本番 paper 経路の `finalize_allocation` 呼び出し。`cap_equity` / `cap_equity_source`（既存の CAP_USE_REAL_EQUITY 配線）と `slot_capital_equity`（新規）が**併存**する。 |

side は S1/S3/S4/S5 が long、S2/S6/S7 が short。資金配分は `config/config.yaml:242-251` の
long 各 25%、short は S2=40% / S6=40% / S7=20%。

## 設計判断

### 1. 導出式とサイジングの分離

ON 時だけ、system `i` の枠を次で求める。

```text
B_i = E × G × F × side_share_i × weight_i
N_i = E × max_pct_i
raw_slots_i = B_i / N_i
slots_i = max(min_slots, floor(raw_slots_i))    # B_i>0 の場合だけ最小枠を適用
```

- `E`: 開始 equity。`G`: `risk.portfolio.max_gross_exposure_pct`。
- `F`: `risk.slots_from_capital_gross_budget_factor`。**0 < F ≤ 1**（schema
  `config/schemas.py:35` が `gt=0, le=1`）。
- `weight_i`: `ui.long_allocations` / `ui.short_allocations` を side 内で正規化した値。
- `side_share_i`: `ui.default_long_ratio`。portfolio の net 上限に収まる範囲へクランプする。
- `N_i`: 日々の ATR や銘柄価格ではなく固定設定 `max_pct_i`。S1〜S6 は 10%、S7 は 20%。
  したがって枠は銘柄のボラティリティでは日々揺れない。

**この式は equity に依存しない。** `E` は `B_i` と `N_i` の両方に現れて約分され、

```text
raw_slots_i = G × F × side_share_i × weight_i / max_pct_i
```

だけが残る。equity を引き回しているのは監査記録（`$` 建ての予算と 1 枠あたり所要額）を
読めるようにするためだけで、口座サイズが変わっても枠の数は動かない。**枠の数字が動いた
のを「equity が動いたから」と読んではいけない。** 回帰テスト
`test_slots_are_equity_independent` が $10,000 と $10,000,000 で `slots` 一致を固定している。

注文サイズ自体は別会計で、`common/alpaca_trading.py` が `equity_deploy_pct=0.5` の予算を
signal weight で割る。そこは変更しないので二重計上しない。

### 2. 既存上限との衝突

既存上限を最上位に置く。導出枠は long=40 / short=30 / 合計=70 を超えないよう、同じ side の
raw 値の最大剰余法で縮める（`_cap_slots_by_side`, `core/final_allocation.py:166`）。
gross は `F≤1` で計画予算を越えず、net は `|2×long_share−1|×G ≤ max_net_exposure_pct`
になるよう side share を先にクランプする。最後の `_apply_portfolio_caps` は既存保有と実
notional を含む最終防波堤として残る。

### 3. 端数と最低枠

既定 `min_slots=1`。正の予算を持つ system が `floor` で 0 になっても一枠を持つ。S7 のヘッジ
可用性を残す明示的な方針であり、`risk.slots_from_capital_min_slots: 0` で 0 枠も選べる。
予算が 0 の system には最低枠を与えない。

### 4. 枠は「常設の上限」であって「その run の発注枠」ではない

配分段は `available_slots[i] = max(0, slots_i − held_i)` を使う。導出枠だけを見て
「新規が何本入るか」を語ってはいけない。実際 2026-08-26 は S1 が 7 銘柄・S4 が 9 銘柄・
S5 が 7 銘柄を既に保有していたため、導出枠 1 の側は**空きがゼロ**だった（下記リプレイ）。

### 5. 後方互換

`risk.slots_from_capital: false` が既定。OFF では新しい計算を一切呼ばず、既存 global
`max_positions=10` の経路をそのまま通る。

## 失敗時の振る舞い（すべて fail-SAFE = legacy へ退避、0 枠にはしない）

| 事象 | 実装 | 振る舞い |
|---|---|---|
| `F` が (0,1] の外 | `config/schemas.py:35` / `config/settings.py:308` / `core/final_allocation.py:143` | schema が拒否。すり抜けても settings と policy loader が 1.0 へ寄せ、いずれも WARNING。 |
| `nan` / `inf` が式に混入 | `_finite_or` (`core/final_allocation.py:118`) | 既定値へ寄せる。全 system 0 枠にはしない。 |
| 導出枠の合計が 0 | `core/final_allocation.py:2301` | `logger.error` + **legacy `max_positions` へ退避**。診断キー `capital_slots` も出さない。 |
| 導出中に例外 | `core/final_allocation.py:2336` | `logger.error` + legacy 据え置き。その日を潰さない。 |
| system が `ui.*_allocations` に無い（例: System8） | `core/final_allocation.py:2319` | `logger.warning` + **その system だけ legacy 枠を維持**。黙って 0 枠にしない。 |
| settings が読めない | `core/final_allocation.py:155` | `logger.warning` + OFF。設定ミスが「黙って OFF」にならない。 |
| equity が読めない | `core/final_allocation.py:2239` | `default_capital` へ退避し WARNING。枠の値自体は equity 非依存なので変わらない。 |

## 切替と監査情報

```yaml
risk:
  slots_from_capital: false       # false: 現行と同一、true: 新方式
  slots_from_capital_gross_budget_factor: 1.0   # 0 < F <= 1
  slots_from_capital_min_slots: 1
```

env `SLOTS_FROM_CAPITAL=1` / `=0` が YAML を上書きする（非常口。既定は未設定 = YAML に従う）。
解釈できない値は YAML 値へ落ちるが WARNING を出す。

ON の run summary には `capital_slots` として equity/source、raw slots、system budget、
一枠必要額、適用 pool cap を記録する。OFF にはこのキーを追加しない。

## 実測 — 2026-08-26（ブランチ `claude/pr167-fixed`, `origin/main` = `f268c4a` 上）

入力は `results_csv/paper_orders_20260826.json` の `account_equity_usd=$100,879.96`
（`equity_source=alpaca`）、現設定 `G=1.0, F=1.0, long/short=0.5/0.5`。

### 導出枠（equity 非依存なので equity を変えても同じ）

| system | weight | B_i（資金配分） | N_i（一枠必要額） | raw | 導出枠 |
|---|---:|---:|---:|---:|---:|
| S1 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S2 | 40% | $20,175.99 | $10,088.00 | 2.00 | 2 |
| S3 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S4 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S5 | 25% | $12,610.00 | $10,088.00 | 1.25 | 1 |
| S6 | 40% | $20,175.99 | $10,088.00 | 2.00 | 2 |
| S7 | 20% | $10,088.00 | $20,175.99 | 0.50 | 1（最低枠） |

合計 long=4 / short=5 / total=9（`4≤40, 5≤30, 9≤70`）。計画予算は long=$50,439.98 /
short=$50,439.98、gross=$100,879.96≤equity、net=$0≤$50,439.98。上限衝突なし。

### 既存保有を引いた「本当の after」

保有は `results_csv/alpaca_snapshot_20260826.json` から取る。このスナップショットは
`generated_at=2026-08-26T13:56:08Z`（22:56 JST）で、**当日 22:35 の run 自身の約定 5 件を
既に含む**。したがって `entry_date == 2026-08-26` の 5 行を除いた 27 建玉が「その run の
直前の保有」である（うち 25 件が system 帰属あり、2 件は delisted の CDTX/FOLD で
portfolio プールは食うが system 枠は食わない）。この除外は
`tools/replay_capital_slots.py --positions-basis pre-run`（既定）が行う。

| system | 導出枠 | 既存保有 | 空き枠 | signals before→after | paper submitted before→after | 選ばれる symbol |
|---|---:|---:|---:|---:|---:|---|
| S1 | 1 | 7 | **0** | 9→0 (−9) | 3→0 | — |
| S2 | 2 | 2 | **0** | 10→0 (−10) | 8→0 | — |
| S3 | 1 | 0 | 1 | 4→1 (−3) | 4→1 | TNON |
| S4 | 1 | 9 | **0** | 0→0 | 0→0 | — |
| S5 | 1 | 7 | **0** | 0→0 | 0→0 | — |
| S6 | 2 | 0 | 2 | 0→0 | 0→0 | —（候補なし） |
| S7 | 1 | 0 | 1 | 0→0 | 0→0 | —（候補なし） |
| 合計 | 9 | 25 | 4 | **23→1 (−22)** | **15→1** | — |

**2026-08-26 に ON だった場合の新規エントリーは 1 件（S3 / TNON）だけである。**
実際のその日の新規建玉は 5 件（S1×3, S3×2）だったので、−4 件になる。

これは `finalize_allocation` を実データで通した ON スモークとも一致する
（`available_slots = {S1:0, S2:0, S3:1, S4:0, S5:0, S6:2, S7:1}`、`final_counts = {system3: 1}`、
`equity_source=account_start_equity`）。

`paper submitted` は既存 order 行を残存 symbol で照合しただけで、新方式で注文額を再計算した
値ではない。サイズ計算を変えないという本変更の制約を守るためである。

### 訂正記録

初版（PR #167 の元コミット `ddb0e6a`）は保有を引かずに「合計 23→4、submitted 15→4」と
書いていた。導出枠を per-run の発注枠として読んだ誤りで、**保有を引いた正しい値は 23→1 /
15→1**。上の表がそれを置き換える。

## テストと再現コマンド

```powershell
$env:PYTHONUTF8=1
pytest -q -o addopts='' tests/test_capital_weighted_slots_20260827.py
python tools/replay_capital_slots.py `
  --signals results_csv/today_signals_20260826.json `
  --paper-orders results_csv/paper_orders_20260826.json `
  --positions results_csv/alpaca_snapshot_20260826.json
```

`--positions` を省くと導出枠だけの「容量ビュー」になり、出力の `basis` が
`derived_capacity_only` になる。新規本数の議論に使ってはいけない。

### 実測結果（2026-08-27、このブランチ）

| 項目 | 実測 |
|---|---|
| `tests/test_capital_weighted_slots_20260827.py` | **19 passed** |
| 配分/設定まわり 17 ファイル（`test_final_allocation*`, `test_config_*`, `test_settings_*`, `test_portfolio_*`, `test_alloc_*`）| baseline `f268c4a` **16 failed / 207 passed**、本ブランチも **16 failed / 207 passed**。失敗 ID 集合は `comm` 突合で**完全一致**（新規失敗 0）。既存 16 失敗は `test_settings_focused.py` などの先行債務。 |
| OFF byte-parity | slot / slot(no-TM) / capital / cap_equity の 4 経路で、最終 CSV の sha256 と summary JSON の sha256 が baseline と**一致**。 |
| lint | `black --check` / `isort --check-only` / `ruff check` を **git 管理下の .py 696 ファイル**に対して実行し全て clean。 |

比較時の注意: baseline worktree には `.env` が無く、`test_config_settings_enhanced.py::
TestSettingsIntegration::test_get_settings_with_environment_variables` は
`EODHD_API_KEY` の有無で挙動が変わる。両側で `EODHD_API_KEY` を揃えて初めて失敗集合が
一致する（揃えないと本ブランチだけ 1 件少なく見え、コード差だと誤読する）。

### mutation による検証

「テストが本当に効いているか」を、意図的な退行を入れて確認した。

| mutation | 期待 | 実測 |
|---|---|---|
| ON ブロックを `if True:` にしてフラグを無視（OFF 退行） | 検出 | `test_flag_off_is_byte_identical_...` / `test_capital_mode_on_is_not_a_no_op` / `test_real_config_off_leaves_no_capital_slots_diagnostics` の **3 件が fail**、OFF parity harness の sha256 も乖離 |
| capital mode の `slot_limits=` を外す（H-2 を戻す） | 検出 | `test_capital_mode_on_is_not_a_no_op` が **fail** |
| settings の flag を `False and ...` で殺す（実 config 配線を殺す） | 検出 | `test_real_config_on_wiring_fires_...` / `test_env_override_...` の **2 件が fail** |

既存の ON テストは全て `_load_capital_slot_policy` を monkeypatch しており、実 config 配線
が死んでいても緑のままだった（2026-08-26 の coid map 退行と同じ死角）。
`test_real_config_on_wiring_fires_without_monkeypatching_the_policy` は `APP_CONFIG` に
`slots_from_capital: true` の YAML を渡すだけで、policy も portfolio caps も patch しない。

## 未確認・積み残し

- 8/26 の system 別「旧枠適用前」候補**全件**は成果物に無い。リプレイの入力は公開済み
  `today_signals`（既に旧枠・既存保有・portfolio cap を通った出力）なので、旧ソート末尾で
  落ちた S5/S7 候補を含む完全再演算はできていない。上の表は「公開済み候補を新しい枠で
  再選抜したらどうなるか」であって、上流からの完全再現ではない。
- delisted 2 件（CDTX/FOLD）は system 帰属が無いため system 枠を消費しない扱いにした。
  portfolio プール側では引き続き枠を食う。
- ON を実運用で有効化する判断、および ON での paper run はこの変更では行わない。
