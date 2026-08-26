# 枠（スロット）モデルの実体と、ダッシュボード再設計モック — 2026-08-26

**Status:** READ-ONLY 調査 + 静的モック。**本番ダッシュボード（`apps/dashboards/alpaca-next`）・
ランナー・スケジューラは一切変更していない。deploy / publish も実行していない。**

目的: 「なぜ Entry が 0 なのか」を読者が**推測しないで済む**ダッシュボードを設計する。
そのために (1) コード上の「枠」の正を抽出し、(2) 今 artifact にあるデータで何が描けるかを
棚卸しし、(3) 実データを載せた静的モックを作った。

- モック: [`docs/mock/dashboard_redesign_20260826.html`](mock/dashboard_redesign_20260826.html)
  （単一ファイル・外部依存なし。ブラウザで直接開ける）

引用行番号はすべて **`origin/main` = `9835eb5`**（本番で実行されているコード）基準。
本 worktree のブランチ `claude/monitor-webapp` は main より古く、行番号がずれる。

---

## 1. 枠モデル（コード上の正）

### 1.1 system 別の枠

| system | side | 枠 (max_positions) | 充当優先度 | 資金配分ウェイト | 発注種別 |
|---|---|---|---|---|---|
| system1 | **long**  | 10 | **1** | 0.25 | market |
| system3 | **long**  | 10 | **2** | 0.25 | limit (前日終値 −7%) |
| system4 | **long**  | 10 | **3** | 0.25 | market |
| system5 | **long**  | 10 | **4** | 0.25 | limit (前日終値 −3%) |
| system2 | **short** | 10 | **5** | 0.40 | limit (前日終値 +4%) |
| system6 | **short** | 10 | **6** | 0.40 | limit (前日終値 +5%) |
| system7 | **short** | 10 | **7** | 0.20 | market (SPY ヘッジ) |

- **枠 10 は system 固有値ではなく、グローバル `risk.max_positions: 10` が全 system に配られている。**
  `config/config.yaml:8` → `config/settings.py:55` → `strategies/base_strategy.py:62`
  （`cfg["max_positions"] = settings.risk.max_positions`）→ `core/final_allocation.py:1668-1688`
  `_resolve_max_positions()` が `strategy.config["max_positions"]` を読む。
  `config.yaml` の `strategies.systemN` に `max_positions` を書いた system は **1 つも無い**ので、
  7 system すべて 10 になる。
- **side（long/short）は `long_allocations` / `short_allocations` のキーが定義する。**
  `config/config.yaml:234-243`、既定は `config/settings.py:189-204` と
  `core/final_allocation.py:93-105`（`DEFAULT_LONG_ALLOCATIONS` / `DEFAULT_SHORT_ALLOCATIONS`）。
- **優先度 = `side` 昇順 → system 番号昇順。** `core/final_allocation.py:1628`
  `sort_cols = ["side", "_system_no"]`。`"long" < "short"` なので **long 4 系統が先**、
  その中は S1→S3→S4→S5、続いて short が S2→S6→S7。この並び順のまま
  `_apply_portfolio_caps()` が**末尾から捨てる**ので、**S5 と S7 が最初に犠牲になる**。
  （system 内は `score` 降順。system4 だけ昇順 = 低スコア優先。同 1631-1640）
- `system8`（FOMC ドリフト）は `config.yaml:113-114` の注記どおり **意図的に配分ウェイト未登録**＝
  枠を持たない。本モデルは S1..S7 のみ。

### 1.2 ポートフォリオ上限（long / short は別プール）

| 上限 | 値 | 由来 |
|---|---|---|
| `max_total_positions` | **70** | 4 long × 10 + 3 short × 10 |
| `max_long_positions`  | **40** | S1/S3/S4/S5 × 10 |
| `max_short_positions` | **30** | S2/S6/S7 × 10 |
| `max_gross_exposure_pct` | 1.0 | gross(long$+short$) ≤ equity × 100% |
| `max_net_exposure_pct` | **0.5** | \|net\| ≤ equity × 50% |
| `drawdown_flatten_pct` / `max_positions_per_sector` | 0 = 無効 | off-by-default |

`config/config.yaml:16-24` → `config/settings.py:42-49` → `core/final_allocation.py:1689-1716`
`_load_portfolio_caps()`。適用は `_apply_portfolio_caps()`（同 1717-1852）:

```
allow_long  = max(0, max_long  - held_long)     # :1766
allow_short = max(0, max_short - held_short)    # :1767
allow_total = max(0, max_total - held_total)    # :1768
```

**答え合わせ:「long枠・short枠・system枠 あるよね?」→ 3 つとも実在する。**

- **long と short は別プール**（40 と 30 で独立に判定）。ただし **合計 70 が最後に効く**
  ので完全独立ではない（片側を使い切っても、もう片側の空きは自動で回らない）。
- 3 段は **system枠 → long/short枠 → 合計枠** の順に効く。
  system枠は「候補をいくつ拾うか」（`available_slots`, `:1939-1946`）、
  long/short/合計枠は「拾った後に末尾から削る」（`_apply_portfolio_caps`）。
- **delisted / orphan（system 帰属が付かない実保有）も枠を食う。**
  `count_positions_with_unmapped()`（`:346-414`）が `held` に算入する。

### 1.3 割当モード

本番の日次実行は `scripts/daily_auto_run.ps1:83` の `python scripts/run_all_systems_today.py --parallel --save-csv`
＝ `--capital-long/--capital-short` を渡さないので `core/final_allocation.py:2062` で **`mode = "slot"`**。

```
slots_long  = Σ available_slots[long systems]    # :2068
slots_short = Σ available_slots[short systems]   # :2070
available_slots[s] = max(0, max_positions[s] - held[s])   # :1946
```

`_distribute_slots()`（`:577-`）が weight 比で slot を配り、候補数で頭打ちにする。

### 1.4 1 ポジションのサイジング

2 段構え。

1. **配分段（株数）** — `strategies/base_strategy.py:169-189` `calculate_position_size()`:
   `risk_pct = 0.02`（資金の 2% をストップ幅で割る）を `max_pct = 0.10`（1 銘柄 ≤ 資金の 10%）で頭打ち。
   system7 だけ `max_pct: 0.20`（`config/config.yaml:100`）。
2. **発注段（notional）** — `common/alpaca_trading.py:134,160-169` `compute_position_notionals()`:
   `deploy_budget = equity × equity_deploy_pct(0.5)` を weight 比で分配し、
   per-name `max_pct 0.10` / gross 1.0 / net 0.5 を**予算の内側で**適用。
   2026-08-26 実測: equity $101,097.41 × 0.5 = **$50,548.71** を 22 本に配分（合計一致を実測確認）。

---

## 2. 今日（2026-08-26）の実データで検証した枠の効き方

`results_csv/today_signals_20260826.json` の `portfolio.caps` と
`results_csv/exit_orders_20260826_proposal.json` の `positions`（06:42 JST の broker read, 30 件）が
**完全に一致**した。

| | long | short | total |
|---|---|---|---|
| 保有 (held) | 28 | 2 | 30 |
| 上限 (cap) | 40 | 30 | 70 |
| 空き (allow) | **12** | 28 | 40 |
| 本日採用 (kept) | **12** | 10 | 22 |
| 枠で落とした (trimmed) | **21** | 0 | 0 |

保有 30 の内訳（system 別）: S1 8 / S4 **10（満杯）** / S5 8 / S2 short 2 / orphan 2（CDTX, FOLD）。

**S4=10候補→0本 / S5=9候補→0本 の理由:**
ロング枠の空きは 12 しかなく、優先度 1 位の S1 が 9、2 位の S3 が 3 で使い切った。
S4（3 位）と S5（4 位）は 1 本も残っていない。**候補の質の問題ではない。**

計算トレース:

| 段階 | 計算 | 件数 |
|---|---|---|
| ロング候補 (TRDlist) | S1 10 + S3 10 + S4 10 + S5 9 | 39 |
| 割当段に入った行 | kept.long 12 + trimmed.long_count 21 | 33 |
| ロング枠の空き | 40 − 28 | 12 |
| 優先度順に採用 | S1 9 → S3 3 → S4 0 → S5 0 | 12 |

> ⚠ **39 → 33 の差 6 は artifact に理由が残っていない。** 重複除去（`_apply_slot_round_robin_dedup`）
> かサイジング予算切れのどちらかだが、どちらも診断値を吐いていない。観測性の穴。

---

## 3. 調査中に見つかった不整合（本書は報告のみ・未修正）

### 3.1 🔴 system枠（max_positions=10）が実際には効いていない

`_apply_portfolio_caps` の report は `held_unmapped = {long:28, short:2, total:30}` ＝
**held と完全に同数**。つまり 06:00 の配分時、`load_symbol_system_map()`（`common/symbol_map.py:201`）は
**保有 30 件を 1 件も system に帰属できていない**。

実測: `data/symbol_system_map.json` は 84 銘柄（最終更新 2026-07-01）で、
現在保有の 29〜30 銘柄と **重なりゼロ**。

結果として `available_slots[s] = 10 − 0 = 10` が全 system に立ち、**system枠は素通り**する。
今日効いたのは long枠 40 / short枠 30 / 合計 70 だけ。

観測できる帰結（本日）:
- **S1**: 自身の枠の空きは 2（8 保有）のはずが、9 本のエントリー提案。
  ただし 9 本中 7 本は既保有銘柄の再提案で、発注時に `already_held` で skip される（後述 3.2）ため
  結果的に 8+2=10 に収まる。**偶然の救済であって設計どおりではない。**
- **S2**: 保有 2 + 新規 10 = **12 で自身の枠 10 を超過**。こちらは重複が無いので救済されない。

一方でダッシュボードのスナップショット（`alpaca_snapshot_*.json` の `exposure.by_system`）は
別経路で system 帰属を出せている（S1 9 / S4 10 / S5 8）。**配分エンジンとダッシュボードで
system 帰属の真値が食い違っている。**

### 3.2 エントリー提案の 1/3 は既保有銘柄の再提案

2026-08-26 のエントリー 22 本のうち **7 本**（S1: WETO, BNY, IPST, SLS, ERAS, FBRX, AMCR）は
既に保有中。`common/alpaca_trading.py:801` の `already_held:` ゲートで submit 直前に落ちる
（08-25 の `recon` にも `drop_breakdown: {already_held: 2}` として現れている）。

「エントリー 22 本」と表示すると読者は 22 枠増えると読むが、実際に枠を増やすのは **15 本**。

### 3.3 `exit_orders_<date>.json` の件数はポジション減少数ではない

本日の 17 件の内訳は `time_based` 8（＝実際に決済）と `protect_stop` / `protect_target` /
`protect_trailing` 9（＝**常駐の保護注文を置くだけ。置いた日にポジションは減らない**）。
08-25 の実測でも `exit_close: 17` に対し `exit_protect: 2` で、
実際に閉じた 19 ポジションのうち 2 件（S5 の EAT, MRVI）は**前日以前に置いた保護注文が場中に約定**したもので、
`exit_orders_20260825_*.json` には現れず `exit_ledger` にしか無い。

---

## 4. データ在庫（今のままで描けるか / 新 artifact が要るか）

| ビューの構成要素 | 出所 | 状態 |
|---|---|---|
| **昨日ポジション**（system 別・銘柄付き） | `exit_orders_<date>_proposal.json` → `positions[]`（`symbol, system, side, qty, entry_date`） | ✅ **今すぐ描ける**。06:4x JST の broker 実測 |
| 同（P&L・保有日数付き） | `alpaca_snapshot_<date>.json` → `positions[]`（28 フィールド） | ✅ 今すぐ描ける（ただし取得時刻が日により 06:26 / 22:57 とばらつく → §4.1） |
| **今日エントリー**（system 別・銘柄・notional） | `paper_orders_<date>.json` → `orders[]` | ✅ 今すぐ描ける |
| エントリーの skip 理由 | 同 `orders[].skip_reason`（`already_held:` / `already_open:` / `wash_trade_conflict:` / `skip:below_min_notional` / `skip:below_1_share`） | ⚠ **06:00 の dry_run では常に null。** 実値は 22:35 の実発注版のみ |
| **今日エグジット**（system 別・銘柄・理由） | `exit_orders_<date>_proposal.json` / `_execution.json` → `exits[]` | ✅ 今すぐ描ける。ただし `reason` で close / protect を分けないと誤読（§3.3） |
| **今日ポジション**（実測） | `alpaca_snapshot_<date>.json` | ❌ **朝には存在しない**（22:5x 生成）。朝は「昨日 − close + 新規」の**見込み**しか出せない |
| **枠の占有**（system別 filled/free） | `exit_orders_*_proposal.json:positions[].system` を数える | ✅ 今すぐ描ける |
| **long/short/合計 の cap メーター** | `today_signals_<date>.json` → `portfolio.caps`（held / caps / allow / kept / trimmed） | ✅ **今すぐ描ける。既に全部入っている** |
| **候補 → エントリーの落ち理由** | 同 `portfolio.caps.trimmed`（`long_count` / `short_count` / `total` / `gross_exposure` / `net_exposure`） | ⚠ **合計しか無い。system 別に割れない** → §4.2 |
| ファネル 6 フェーズ × 7 system | `pipeline_<date>.json` → `systems.sysN.phases[]` | ✅ 今すぐ描ける（本日 35/35 measured） |
| エントリー約定（filled） | `recon_<date>.json` → `portfolio.entry_filled` | ⚠ **artifact 上は `pending_new` 止まりで信用できない**（既知。broker GET が唯一の真） |
| 実現損益・決済銘柄 | `alpaca_snapshot_*.json` → `realized.closed_trades[]`（`exit_session` で当日抽出可） | ✅ 描ける。ただし**約定単位**なので銘柄で dedup 必要（08-25 は 76 fill = 19 ポジション） |

### 4.1 今すぐ描ける（新 artifact 不要）

**モックの 2 ビューは、どちらも既存 artifact だけで描ける。**
「今日ポジション」列を**見込み**と明示すれば、新しいパイプライン出力は 1 つも要らない。

### 4.2 新しい小さな artifact があると良いもの（本タスクでは作らない）

1. **`trimmed_by_system`** — 現状 `caps.trimmed` は `{"long_count": 21}` の合計値だけ。
   「S3 が 7、S4 が 10、S5 が 4 落ちた」を出すには
   `_apply_portfolio_caps()` の trim ループ（`core/final_allocation.py:1776-1806`）で
   `row["system"]` を数えて report に足すだけ（数行、挙動不変）。
   これが入ると **VIEW B の「候補 N → 0 ✗理由」を推定でなく実測で書ける。**
2. **`available_slots` / `held_by_system` の同梱** — `summary.system_diagnostics` には既にあるが
   `today_signals` JSON には出ていない。今はダッシュボード側が
   `exit_orders_*_proposal.json` の positions を数え直している（二重の真実）。
3. **`positions_eod`（当日引け後スナップショット）** — 現状 `alpaca_snapshot` の生成時刻が
   日によって 06:26 / 22:51 / 22:57 とばらつき、「前日終値時点」の確定値が無い。
   フロービューの左端が日によって別の意味になる。
4. **配分段の脱落理由（39 → 33 の 6 件）** — §2 の穴。

---

## 5. モック

`docs/mock/dashboard_redesign_20260826.html`（単一ファイル、外部依存なし、ライト/ダーク対応）

- **VIEW B「枠ビュー」を先頭に置いた。** ユーザーの問いが「なぜ 0 なのか」なので、
  答えを一番上に出す。long枠 / short枠 / 合計枠の 3 メーター +
  system 別のスロット箱（■保有 / ▨本日 / □空き / 赤=超過）+ 右列に落ちた理由を 1 行。
- **VIEW A「フロービュー」**は 4 列（昨日 → エグジット − → エントリー + → 今日）。
  system ごとに横 1 行で連続性が追える。既保有の再提案チップは黄枠、決済済み銘柄は取り消し線。
  各行の末尾に `y − out + net` の算術を出して「なぜその数字か」を消さない。
- **ファネル 6×7 は既定で閉じた `<details>`。** 「見るところが多い」という指摘に対し、
  トップは 3 メーター + 7 行のスロット表 + 7 行のフロー表だけにした。
- ほかに閉じたトグルで「枠モデル + 自己検査」「本日の枠計算トレース」。

### 5.1 自己検査（モック内でも実行、10/10 一致）

| 検査 | 値 | 結果 |
|---|---|---|
| long 系統の枠合計 = `max_long` | 4×10 = 40 | 一致 |
| short 系統の枠合計 = `max_short` | 3×10 = 30 | 一致 |
| 枠合計 = `max_total` | 70 = 70 | 一致 |
| モックの保有 long（系統別 + orphan）= artifact `held.long` | 8+0+10+8+2 = 28 | 一致 |
| モックの保有 short = artifact `held.short` | 2 = 2 | 一致 |
| `allow.long` = `max_long − held.long` | 40−28 = 12 | 一致 |
| `allow.short` = `max_short − held.short` | 30−2 = 28 | 一致 |
| 採用 long = `allow.long`（枠を使い切った） | 12 = 12 | 一致 |
| モックのエントリー合計 = `kept.total` | 9+10+3 = 22 | 一致 |
| S1+S3 のエントリー = `kept.long` | 9+3 = 12 | 一致 |

モックは自己検査結果を画面上でも計算して表示するので、データを差し替えたときに
枠モデルとの不整合がその場で「不一致」と出る。

---

## 6. スコープ外（やっていないこと）

- 本番ダッシュボード（`apps/dashboards/alpaca-next`）のコード変更 — **なし**
- Vercel publish / deploy hook の実行 — **なし**
- ランナー・スケジューラ・`.env`・発注コードへの変更 — **なし**
- §3 の不整合の修正 — **なし**（報告のみ）
- §4.2 の新 artifact 追加 — **なし**（設計提案のみ）
