# portfolio cap の equity 基準 fix + リプレイ計測 (2026-08-20)

**branch**: `claude/cap-real-equity-20260820` (`claude/open-auto-run` から分岐)
**worktree**: `C:/tmp/qts-cap-equity` — 本番ランナー `C:/tmp/qts-main-run` の
working-tree には**一切触れていない**。
**paper 限定 / 発注ゼロ / flatten 未実行 / live flip なし / prod へ merge なし**。
リプレイは既存 JSON を読むだけのオフライン計算で、Alpaca API を 1 回も叩いていない。

---

## 1. バグ実証 — `equity_base` が 100,000 に落ちる経路

| # | file:line | 事実 |
|---|---|---|
| 1 | [core/final_allocation.py:1865](core/final_allocation.py:1865) | `finalize_allocation(..., default_capital: float = 100000.0, ...)` — signature 既定 |
| 2 | [core/final_allocation.py:2339](core/final_allocation.py:2339) | `equity_base = _safe_positive_float(default_capital, allow_zero=True) or 100000.0` |
| 3 | [scripts/run_all_systems_today.py:5570-5588](scripts/run_all_systems_today.py:5570) | 本番の `finalize_allocation(...)` 呼び出しが **`default_capital` を渡していない** |
| 4 | [scripts/open_auto_run.py:277-292](scripts/open_auto_run.py:277) | signals stage は `app_today_signals.py --headless` を `--capital-long/--capital-short` **なし**で起動 |
| 5 | [common/signal_export.py:519-524](common/signal_export.py:519) | よって `compute_today_signals(capital_long=None, capital_short=None)` = slot モード |

→ 本番経路では `default_capital` が一度も上書きされず、cap の分母は常に
**100,000 USD 固定**。

**実測証拠** (`results_csv/today_signals_*.json` の `portfolio.caps`):

| date | gross_cap_usd | net_cap_usd | 実 equity |
|---|---|---|---|
| 2026-08-13 | 100,000 | 50,000 | 100,570.41 |
| 2026-08-14 | 100,000 | 50,000 | 100,275.16 |
| 2026-08-15 | 100,000 | 50,000 | 100,736.17 |
| 2026-08-16 | 100,000 | 50,000 | 100,614.58 |
| 2026-08-17 | 100,000 | 50,000 | 100,419.59 |
| 2026-08-18 | 100,000 | 50,000 | 100,132.49 |
| 2026-08-19 | 100,000 | 50,000 | 99,788.27 |

7 日すべて cap が固定額。equity は日々動いている。

### 実 equity の入手経路

1. `common/alpaca_trading.py:322` `fetch_account_equity()` — read-only GET (paper 固定)
2. `results_csv/alpaca_snapshot_<date>.json` の `account.equity` (オフライン)
3. `results_csv/alpaca_equity_history.json` (`{t, equity}` の日次列。リプレイで使用)

なお `scripts/open_auto_run.py` は `self.equity()` を **signals stage の後**に呼ぶため、
signal 生成時点では実 equity を持っていない。本 fix はここを snapshot / read-only
GET で埋める。

---

## 2. docs 整合 — cap は equity 連動が正しい

[docs/POSITION_MANAGEMENT_PHASE5_20260707.md](docs/POSITION_MANAGEMENT_PHASE5_20260707.md) が
portfolio 管理の single source of truth で、cap を明確に **equity 比**と規定している:

- §2 config コメント: `max_gross_exposure_pct: 1.0  # gross (long$+short$) / equity 上限` /
  `max_net_exposure_pct: 1.0  # |net| (|long$-short$|) / equity 上限`
- §2.1 根拠: 「cash account (paper, margin なし) では **gross ≤ equity が物理上限**」
- §3.1 実装規定: 「`position_value` 列の累積で side 別 gross と net を評価し、
  **`equity × pct`** を超える行を trim」

**固定額を意図した記述は docs のどこにもない** → 修正は docs 準拠への回帰として正当。
`docs/systems/INDEX.md` / `docs/README.md` はバケット配分 (25%×4 / 40+40+20) のみを
規定しており cap の分母には言及していないので、抵触しない。

docs には本件の顛末を §6 として追記した。

---

## 3. flag 実装

| file | 内容 |
|---|---|
| **`common/cap_equity.py`** (新規) | `CAP_USE_REAL_EQUITY` (既定 OFF) / `CAP_EQUITY_USD` (明示上書き)。解決順 = env → Alpaca read-only GET → snapshot → `None`。OFF なら `(None, "disabled")` を即返す |
| `core/final_allocation.py` | `finalize_allocation(cap_equity=None, cap_equity_source=None)` を additive 追加。`cap_equity` が正のときだけ `equity_base` を差し替える。**サイジング予算 (`capital_long/short`) には触らない** |
| `core/final_allocation.py` | `_apply_portfolio_caps(equity_source=None)` を additive 追加。`equity_source` が渡ったときだけ report に `caps.equity_base_usd` / `caps.equity_source` を足す → **OFF では診断 JSON が 1 key も変わらない** |
| `scripts/run_all_systems_today.py` | `finalize_allocation` 呼び出し直前で `resolve_cap_equity()`。例外は握って従来値に退避 |
| `scripts/replay_portfolio_caps.py` (新規) | オフラインのリプレイ計測ツール |

有効化 (paper のみ):

```bash
CAP_USE_REAL_EQUITY=1 python scripts/open_auto_run.py --date 2026-08-20 --dry-run
```

---

## 4. リプレイ計測 (オフライン・発注ゼロ)

`scripts/replay_portfolio_caps.py` で 2026-08-13..19 の 7 営業日を再生。
本物の `_apply_portfolio_caps` に、記録された caps / held / held_unmapped と
再構成した pre-cap frame を流す。

**自己検証**: arm A (現行本番と同じ入力) がその日実際に記録された per-system 本数 /
allow / kept / trimmed を再現できるかを毎日チェック → **7/7 日 一致**。
再構成が現実とズレていないことを担保した上で B/C を比較している。

| arm | 内容 |
|---|---|
| A_fixed100k | 現行本番 (equity_base = 100,000) |
| B_real_equity | 本 fix (equity_base = その日の実 equity) |
| C_no_orphan | 診断専用の counterfactual。equity は実値、held から unmapped を除外。**提案する変更ではない** |

### before / after

| date | arm | allow_long | net_cap | sys1 | sys3 | sys4 | sys5 | \|net\| | trims |
|---|---|---:|---:|---:|---:|---:|---:|---:|---|
| 08-13 | A | 8 | 50,000 | 7 | 1 | 0 | 0 | 6,730 | long_count:18 |
| 08-13 | **B** | **8** | 50,285 | **7** | **1** | **0** | **0** | 6,730 | long_count:18 |
| 08-13 | C | 38 | 50,285 | 7 | 10 | 9 | 0 | 10,765 | — |
| 08-14 | A | 10 | 50,000 | 8 | 2 | 0 | 0 | 6,489 | long_count:16 |
| 08-14 | **B** | **10** | 50,138 | **8** | **2** | **0** | **0** | 6,489 | long_count:16 |
| 08-14 | C | 38 | 50,138 | 8 | 10 | 8 | 0 | 7,651 | — |
| 08-15 | A | 8 | 50,000 | 8 | 0 | 0 | 0 | 6,828 | long_count:24 |
| 08-15 | **B** | **8** | 50,368 | **8** | **0** | **0** | **0** | 6,828 | long_count:24 |
| 08-15 | C | 38 | 50,368 | 10 | 10 | 10 | 2 | 16,615 | — |
| 08-16 | A | 8 | 50,000 | 8 | 0 | 0 | 0 | 6,531 | long_count:26 |
| 08-16 | **B** | **8** | 50,307 | **8** | **0** | **0** | **0** | 6,531 | long_count:26 |
| 08-16 | C | 38 | 50,307 | 10 | 10 | 10 | 4 | 18,842 | — |
| 08-17 | A | 8 | 50,000 | 8 | 0 | 0 | 0 | 7,070 | long_count:24 |
| 08-17 | **B** | **8** | 50,210 | **8** | **0** | **0** | **0** | 7,070 | long_count:24 |
| 08-17 | C | 38 | 50,210 | 10 | 10 | 10 | 2 | 17,065 | — |
| 08-18 | A | 10 | 50,000 | 8 | 2 | 0 | 0 | 6,063 | long_count:22 |
| 08-18 | **B** | **10** | 50,066 | **8** | **2** | **0** | **0** | 6,063 | long_count:22 |
| 08-18 | C | 38 | 50,066 | 8 | 10 | 10 | 4 | 14,245 | — |
| 08-19 | A | 10 | 50,000 | 8 | 2 | 0 | 0 | 6,917 | long_count:26 |
| 08-19 | **B** | **10** | 49,894 | **8** | **2** | **0** | **0** | 6,917 | long_count:26 |
| 08-19 | C | 38 | 49,894 | 8 | 10 | 10 | 8 | 17,222 | — |

**A → B の差分は 7 日すべてゼロ** (allow / kept / per-system / trim すべて同一)。
`--assumed-pv-mode` を `mean` / `budget` / `max` に振っても結論は不変。

---

## 5. 判定

### cap を実 equity にするだけで sys5 (long 4 system) は枠を得るか → **得ない**

理由は cap 判定ループの構造にある ([core/final_allocation.py:1776-1808](core/final_allocation.py:1776)):

1. `allow_long = max_long_positions(40) − held_long` は **件数**の式で、equity を
   一切参照しない。
2. ループは **件数 cap を先に**評価し、そこで落ちた行は exposure cap まで到達しない。
3. 観測 7 日の trim 理由は毎日 `long_count` のみ。`gross_exposure` / `net_exposure`
   による trim は **1 件も無い** (新規 long notional $5.4k〜6.2k、\|net\| $6.1k〜7.1k に
   対し net cap $50k / gross cap $100k)。

→ 分母を 99,788〜100,570 に変えても binding constraint が動かない。**数値上ゼロ効果**。

### 主因は held 独占 → **その通り。数値で断定できる**

- held_long は 30〜32。うち **`held_unmapped` (delisted/orphan) が 28〜30**。
  正規に system 帰属できる long 建玉は **わずか 2**。
- orphan を held から外すと `allow_long` は 8〜10 → **38** に増え、
  sys3 が 10、sys4 が 8〜10、sys5 が 0〜8 本入る (7 日すべてで long が増加)。

**結論: 「cap 修正だけで一部解決」ではない。sys5 兵糧攻めの主因は orphan 建玉に
よる long 枠の占有であり、解決には flatten / orphan 整理 (あるいはデプラド系の
分散設計) が要る。** 本 fix は docs 準拠への回帰として単独で正当だが、sys5 解放の
手段ではない。

ただし **orphan 解消後は exposure cap が binding になり得る**: C arm の gross は
$31.8k〜$42.9k (悲観仮定で $45.0k〜$62.2k)、\|net\| は $7.7k〜$18.8k
(悲観仮定で $20.8k〜$37.7k) まで上がり、悲観仮定では 08-16/17/19 に実際に
`net_exposure` trim が発生する。その局面では分母が実 equity かどうかが効くので、
**本 fix は orphan 解消の前に入れておく価値がある**。

---

## 6. テスト

- `tests/test_cap_real_equity_20260820.py` — **27 passed**
  (flag 既定 OFF / truthy-falsy / 解決順 / 壊れた snapshot skip / report の
  byte 一致 / ON でのみ新 key / 件数 cap が equity 非依存 / finalize_allocation 配線 /
  非正値 fail-safe)
- 既存回帰 `test_portfolio_caps_20260707` `test_portfolio_caps_observability_20260727`
  `test_net_exposure_cap_20260707` `test_portfolio_guard_20260707`
  `test_position_standing_cap_20260721` `test_final_allocation*` — **131 passed**
- 既知の pre-existing failure (本変更と無関係。base branch でも同一):
  `test_final_allocation_comprehensive.py` 5 件 /
  `test_open_auto_run_thin_signals_exit.py::test_exit_check_script_does_not_read_signals_json` 1 件 /
  `tests/test_app_imports.py` の collection INTERNALERROR
- lint: `ruff` / `black` clean

---

## 7. 実行しなかったこと

- **発注ゼロ** (Alpaca API 未接続。リプレイは JSON 読取のみ)
- **flatten 未実行 / reset 未実行**
- **flag を flip していない** (`CAP_USE_REAL_EQUITY` は既定 OFF のまま)
- **prod branch (`claude/open-auto-run`) へ merge していない**
- 本番ランナー worktree `C:/tmp/qts-main-run` の branch / working-tree は不変
- force push なし

durable ログ: `logs/cap_equity_replay_20260820/replay_{mean,budget,max}.{txt,json}`
(`logs/*` は gitignore なのでコミットされない)
