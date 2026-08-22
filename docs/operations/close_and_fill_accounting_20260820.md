# 2026-08-20: 「全部成功なのに全部失敗に見えた」3 バグ (会計/観測のみ)

**分類:** reporting / observability のみ。**発注内容は一切変えていない** —
何を close するか、何を発注するか、どの建玉を閉じるかは変更なし。paper 限定。

その夜、実際には **close 39/39 が通り、entry 47/47 が fill していた**のに、
成果物と通知は `ok=0 failed=41` / `positions 41→41` / `fill 0` と報告した。
3 つの独立したバグが直列に効いて、完全成功の run が全損に見えていた。

---

## 実測 (ground truth)

| 事実 | 証拠 |
|---|---|
| flatten の close 41 件 = **HTTP 200 が 39 / HTTP 422 が 2** | `logs/open_run_20260820_oneshot_flatten/exit_orders.json` |
| 200 の 39 件はすべて `order_id: null` (非同期受理) | 同上 |
| 422 の 2 件は CDTX / FOLD (INACTIVE asset) | `run.log:44,51` |
| close は実際に全部通った | 22:35 の main run 時点の建玉が **CDTX/FOLD の 2 件だけ** (`logs/open_run_20260820/exit_orders.json:positions`) |
| entry 47 件はすべて fill 済み | broker を read-only で再 poll → `{"filled": 47}` |
| なのに recon は `entry_filled: 0` | `results_csv/recon_20260820.json` |

---

## BUG 1 — 非同期の 200 を「失敗」に数えていた

**場所:** `scripts/open_auto_run.py` (旧 :475)

```python
if st == 200 and oid:      # oid = getattr(r, "order_id", None)
    ok += 1
else:
    failed += 1
    self.log(f"[exit] close 失敗 sym={sym} http={st}")
```

**なぜ壊れるか:** alpaca-py の `ClosePositionResponse` は

```python
order_id: Optional[UUID] = None      # ← Optional。既定 None
status:   Optional[int]  = None
body:     Union[FailedClosePositionDetails, Order]   # ← 必須
```

で、`DELETE /v2/positions` は **非同期**。受理された時点で 200 を返し、
top-level の `order_id` は埋まらないことがある。つまり
**「200 かつ order_id なし」は成功**であって失敗ではない。
その夜のログは文字どおり `close 失敗 sym=ADVB http=200` と出ていた。

**修正:** 受理判定を **HTTP ステータスだけ**で行う。`parse_close_response()`
(`scripts/open_auto_run.py:95-151`) に切り出し、`_flatten_all_stage()` から呼ぶ
(`scripts/open_auto_run.py:458-590`)。

- `2xx` → 受理。`order_id` は取れれば拾う (成功時 `body` は `Order` なので
  `body.id` から復元できる) が、**無くても失敗にしない**。
- `2xx` 以外 → 実エラー。`body` の `message` / `code` を添えて
  `close 拒否 sym=CDTX http=422 reason=asset CDTX is not active (code=...)` と出す。
  **真のエラー経路は温存**(422 を成功に化けさせない)。
- 受理 symbol は `self.pending_flat_symbols` に溜め、BUG 2 の settle 待ちに使う。

`exit_orders.json` の各行に `accepted` / `error` を追加した (以後は artifact
だけで受理/拒否が判別できる)。

---

## BUG 2 — 非同期 fill が着地する前に建玉を撮っていた

**場所:** `scripts/open_auto_run.py` (旧 :514-516)

BUG 1 で `ok=0` になった結果 `market_ids=[]` になり、

```python
def wait_exit_fills(self, order_ids):
    if self.dry_run or not order_ids:
        self.log("[wait] exit fill 監視スキップ (dry-run または close 0)")
        return
```

が即 return。close の **16 秒後** (22:30:13 → 22:30:29) に
`final_positions.json` を撮ってしまい `total=41` = `positions_before_flatten`
と同じ数字が残った。これが「41→41 で 1 件も閉じていない」の正体で、
**実際には全部閉じていた**。

**修正:** close は非同期なので固定待ちでは足りない。**settle するまで poll** する。

- `wait_exit_fills()` (`scripts/open_auto_run.py:592-617`) は
  `order_ids` が空でも `pending_flat_symbols` があれば skip しない。
- `_poll_order_ids()` (:618-643) — order_id が判るぶんは status が終端化するまで poll。
- `_wait_positions_flat()` (:645-679) — **建玉そのもの**が消えるまで poll。
  order_id が返らない非同期受理でも「本当に閉じたか」を観測できる。
- どちらも `--poll-timeout` (既定 300s) を上限に、settle しなければ
  `flatten_settled` / `flatten_unsettled` に記録して先へ進む
  (**黙って成功に倒さない**)。verify snapshot はその後に撮る。

**close する対象は一切変えていない。** 変えたのは待つ→撮る の順序だけ。

---

## BUG 3 — `entry_filled` が構造的に 0 に固定されていた

2 つの欠陥の合わせ技。

### 3a. enum の `str()` が artifact に焼き付いていた

**場所:** `common/alpaca_trading.py:598, 1495, 1510, 2832`

```python
prepared.status = str(getattr(order, "status", "") or "")
```

`OrderStatus` は `str` 継承だが `__str__` が `Enum` 由来なので、
**明示的な `str()` は `'filled'` ではなく `'OrderStatus.FILLED'` を返す**。
その夜の `paper_orders_20260820.json` は 47 件すべて
`"status": "OrderStatus.PENDING_NEW"` だった。

読み手 `scripts/build_execution_recon.py` は

```python
status = str(o.get("status") or "").lower()     # -> "orderstatus.pending_new"
...
if status in _FILLED_STATUSES:                   # {"filled", "partially_filled"}
```

と突合するので、**何が起きても一致しない**。`entry_filled` は 0 から動けなかった。

> 注: `common/alpaca_order.py:232, 390` も生 enum を格納していた同型の欠陥。
> こちらは `paper_orders_*.json` の producer ではない (実際の producer は
> `common/alpaca_trading.py` → `PreparedOrder.to_row()` →
> `scripts/paper_trading_dryrun._write_orders_json`) が、潜在バグなので併せて直した。

### 3b. submit 時点のスナップショットを再 poll していなかった

status は submit 直後の `pending_new` で凍っていた。fill は非同期に後から
起きるので、この JSON をそのまま recon に食わせる限り fill は数えられない。

### 修正

1. **共有の正規化** — `common/order_status.py` を新設 (stdlib のみ、alpaca-py にも
   pandas にも非依存なので SDK 無し環境の recon からも import できる)。
   `normalize_order_status()` は enum / `"OrderStatus.FILLED"` / `"FILLED"` /
   `"filled"` のどれでも `"filled"` を返す。`is_working()` / `is_filled()` も提供。
   書き手・待ち手・読み手が同じ 1 つの定義を共有する。
2. **書き手** — `common/alpaca_trading.py` (4 箇所) と `common/alpaca_order.py`
   (2 箇所) が `normalize_order_status()` を通してから格納する。
3. **読み手** — `scripts/build_execution_recon.py:262` が読み込み時にも正規化する
   (旧 artifact / 他の producer に対する防御)。
4. **再 poll** — `Runner.reconcile_entry_fills()`
   (`scripts/open_auto_run.py:706-790`) を新設。entry_stage の直後・recon の前に
   走り、order が終端化するまで re-poll して `paper_orders_<date>.json` の status
   を実 fill へ書き戻す。**観測のみ — 発注も取消もしない。** artifact が無い /
   broker に届かない場合は log を残して run は継続する。

`SUMMARY.md` に `filled=` と `flatten: accepted/rejected/settled/unsettled` を追加。

---

## before / after (その夜の実 artifact を読み直した結果)

read-only で再解析 (broker へは order status の GET のみ、発注なし):

```
BUG 1 — flatten close の受理判定 (実データ 41 件)
  artifact 実測   : submitted=0  failed=41
  旧ロジック再現  : ok=0   failed=41
  新ロジック      : ok=39  rejected=2  -> [('CDTX', 422), ('FOLD', 422)]

BUG 2 — settle 待ち
  positions_before_flatten : 41
  final_positions (当夜)   : 41    <- 非同期 fill 前に撮った値
  受理 symbol 39 件のうち after にまだ居た数 : 39  <- 新実装はここが 0 になるまで待つ
  22:35 の main run 時点の建玉 : ['CDTX', 'FOLD'] (2 件)
  受理 39 件のうち残存 : 0  -> close は実際に全部通っていた

BUG 3 — entry status
  artifact の status 分布 : {'OrderStatus.PENDING_NEW': 47, 'None': 1}
  当夜の recon 実測       : entry_submitted=47  entry_filled=0
  broker の実 status      : {'filled': 47}
  修正後の recon          : entry_submitted=47  entry_filled=47
```

**結論: 2026-08-20 の run は成功だった。** close 39/39 受理・全建玉解消、
entry 47/47 fill。拒否された 2 件 (CDTX / FOLD) は INACTIVE asset で、
API では閉じられない既知の残件 (手動対応が必要)。

---

## テスト

`tests/test_open_run_close_and_fill_accounting.py` (27 件、全 pass)。
それぞれ**修正前なら落ちる**ものを入れてある:

- `test_async_200_without_order_id_is_accepted` — 200 + `order_id=None` は受理。
- `test_flatten_counts_tonights_39_accepted_and_2_rejected` — その夜の配分
  (200×39 + 422×2) を流して `ok=39 failed=2`。
- `test_422_inactive_asset_is_a_real_error` — 実エラー経路の非退行。
- `test_wait_polls_positions_when_no_order_ids` — order_id 0 件でも建玉 poll が走り、
  settle 後に verify snapshot を撮る。
- `test_wait_records_unsettled_on_timeout` — 未 settle を黙って成功にしない。
- `test_wait_still_skips_when_nothing_to_watch` / `..._in_dry_run` — 非退行。
- `test_producer_writes_bare_token_not_enum_repr` — 旧 `str(enum)` が
  `"OrderStatus.FILLED"` を返すことを明示し、新実装が `"filled"` を返すことを固定。
- `test_recon_counts_enum_serialized_filled_status` /
  `test_recon_does_not_count_pending_new` — 読み手側の正規化と逆側の非退行。
- `test_reconcile_entry_fills_rewrites_status_from_broker` — 再 poll → artifact 書き戻し
  → recon が `entry_filled=3` を出すまでを end-to-end で固定。
- `test_reconcile_entry_fills_bails_out_when_broker_is_blind` — broker 不達時に
  `poll_timeout` を空回りせず 3 回で離脱 (notify/publish を遅らせない)。

周辺 suite も実行:

```
pytest tests/ -k "alpaca or recon or open_auto or open_run or exit or fill or order"
  -> 385 passed, 1 skipped, 5 failed
```

落ちた 5 件は **すべて HEAD でも同じく落ちる既存の failure** (本変更を stash して
確認済み) で、本変更とは無関係:

- `test_open_auto_run_thin_signals_exit.py::test_exit_check_script_does_not_read_signals_json`
- `test_paper_exit_check_broker_unreachable.py::test_genuine_flat_book_returns_0_not_flagged`
- `test_strategies_optimization.py::TestSystem1StrategyBasics::test_compute_exit_basic_calculation`
- `test_strategies_optimization.py::TestSystem1StrategyBasics::test_compute_exit_immediate_stop`
- `test_strategies_optimization.py::TestSystem7StrategyBasics::test_compute_exit_basic_structure_system7`

同様に、collection 段階で import error になる 4 ファイル
(`test_app_imports`, `test_cache_manager_final`, `test_check_rolling_freshness_absolute`,
`test_core_system4_enhanced`, `test_high_impact_modules`) も HEAD で同じ状態のため
sweep から除外した。

`ruff check` は変更ファイル全部 clean。`ruff format` は既存 4 ファイルが HEAD 時点で
既に未フォーマット (`.ruff.toml` が `pyproject.toml` を shadow している既知の不整合)
なので、diff を埋めないよう既存ファイルの一括整形はしていない。新規 2 ファイルは
format 済み。

---

## 未修正 (フラグのみ) — `data/symbol_system_map.json` の凍結

runner tree の `data/symbol_system_map.json` は **2026-07-08 の git snapshot で
凍結**している (mtime `7月 8 18:35`、working tree に差分なし)。原因:

- `common/symbol_map.py:28` — `DEFAULT_SYMBOL_SYSTEM_MAP_PATH =
  Path("data/symbol_system_map.json")` が **CWD 相対**。runner の cwd 次第で
  別の場所に書く / 書けない。
- `common/symbol_map.py:268-274` — 書き込み失敗を `logger.debug` に落として
  `return`。既定 log level では**完全に無音**。
- `common/alpaca_order.py:291-292` — `dump_symbol_system_map(sys_map_store)` が
  裸の `except Exception: pass` の中。ここでも失敗が消える。

**影響:** cap には影響しない (cap は建玉数を見る) が、**exit の system 帰属**に
効く。map に載っていない現役銘柄が orphan 扱いになり、
`exit skip (UNMANAGED)` が増える (2026-08-19 の orphan 39 件のうち delisted は
FOLD/CDTX の 2 件だけで、残りは map 網羅率の問題)。

**今回は直していない。** 「路径を絶対にする」は 1 行に見えて (a) どの root を
anchor にするかの決定と (b) 本番 runner tree が exit 帰属データを書き始める
という挙動変化を伴う。今回の会計/観測に閉じたスコープを超えるので、別 PR とする。
無音化 (`debug` → `warning`、bare `except: pass` の解消) だけでも先に入れる価値がある。

---

## 変更ファイル

| ファイル | 内容 |
|---|---|
| `common/order_status.py` | **新規**。status 正規化の single source (stdlib のみ)。 |
| `scripts/open_auto_run.py` | `parse_close_response` / `_poll_order_ids` / `_wait_positions_flat` / `reconcile_entry_fills` を追加、`_flatten_all_stage` と `wait_exit_fills` を修正、SUMMARY に fill/settle を追加。 |
| `common/alpaca_trading.py` | status 格納 4 箇所を正規化 (:598, :1495, :1510, :2832)。 |
| `common/alpaca_order.py` | status 格納 2 箇所を正規化 (:232, :390)。 |
| `scripts/build_execution_recon.py` | 読み込み時にも正規化 (:262)、`_FILLED_STATUSES` を共有定義へ。 |
| `tests/test_open_run_close_and_fill_accounting.py` | **新規**。26 件の回帰テスト。 |
