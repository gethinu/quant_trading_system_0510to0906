# System1 `_apply_filter_conditions` の大文字固定 `Close` 参照を修正 (2026-08-21)

**Status:** 実装済み。`docs/BACKTEST_ENGINE_GAPS_20260821.md` §7-1 の積み残しを解消する。

**Base:** `origin/main` = `ffb5acb`（engine-gaps 3 件の修正を含む）。

**結論を先に:** 修正は正しい方向であることを実データで確認した。ただし
**live のシグナルは 1 件も変わらない**（実測 0 件）。§7 が懸念していた
「live の setup が誤評価されている」という状態は、現在のデータ経路では
**発生していなかった**。詳細は §3。潜在バグを構造的に潰す修正である。

---

## 1. バグ

`core/system1.py::_apply_filter_conditions` は価格列を**大文字固定**で引いていた。

```python
_val_close = x.get("Close")          # 大文字固定
if _val_close is None:
    _close = pd.Series(0.0, index=x.index)   # 0.0 に潰れる
...
x["filter"] = (_close >= MIN_PRICE) & (_dv > MIN_DOLLAR_VOLUME_20)
```

`load_base_cache()` が返すフレームは**整数インデックス + 全小文字カラム**なので
`Close` は `None` → `0.0`。`0.0 >= 5.0` は常に False なので **`filter` が全行 False**、
`_apply_setup_conditions` は `filter` を掛け算するので **`setup` も全行 False** になる。
`dollarvolume20` 側は小文字なので偶然当たっていた。

同じ helper は live の latest_only fast path (`core/system1.py`) からも呼ばれる。
これが §7-1 で「live 挙動に触れるため判断待ち」とされた理由。

---

## 2. 修正

`core/system1.py`:

- `_resolve_column(df, name)` を新設（`_apply_filter_conditions` の直上）。
  正準名 → 大小文字ゆれ の順で列を解決する。実体は
  **既存の `common/indicator_access.get_indicator`**（このリポジトリで
  「指標アクセスは必ずここを経由」と定められている正準リゾルバ）。
  行 0 のフレームでは `get_indicator` が早期 `None` を返すので、その場合だけ
  素の case-insensitive スキャンにフォールバックする。
- `_apply_filter_conditions` の `x.get("Close")` / `x.get("dollarvolume20")` を
  `_resolve_column(x, ...)` に差し替え。

`get_indicator` は**正準名を優先**するため、`Close` を既に持つフレーム
（live のフレームは全部これ）では**同じ列を読む＝出力はビット同一**になる。

正準の行 predicate `common/system_setup_predicates.system1_setup_predicate` は
元から `get_indicator(row, "Close")` を使っていた。今回の修正で
**列経路と行経路が同じリゾルバを共有する**ので、両者が読む列が食い違うことは
構造的に起こらなくなった。

---

## 3. 方向が正しいことの証明（実データ）

`load_base_cache` 形状（整数 index + 全小文字）の**実キャッシュ 30 銘柄 / 16,050 行**で、
helper の出力を「生カラムから直接計算した正解」と突き合わせた。

正解の定義は spec (`docs/systems/システム1.txt`) どおり:

```
filter = close >= 5           AND dollarvolume20 > 50,000,000
setup  = filter AND sma25 > sma50 AND roc200 > 0
```

| 指標 | 修正前 | 修正後 | 正解 |
|---|---|---|---|
| helper が Close として読んだ値が 0.0 の行 | **16,050 / 16,050** | **0** | — |
| `filter` True 行 | 0 | **14,584** | 14,584 |
| `setup` True 行 | 0 | **4,622** | 4,622 |
| 正解との `filter` 不一致 | 14,584 | **0** | — |
| 正解との `setup` 不一致 | 4,622 | **0** | — |
| 正準 predicate との `setup` 不一致 | 4,622 | **0** | — |
| 実データが条件を満たさない行 | 11,428 | 11,428 | — |
| └ そのうち `setup` が False のまま | 11,428 | **11,428 (全件)** | — |

- 修正前は Close が全行 0.0 で読まれていた（**誤り**）。修正後は実 Close を読む（**正**）。
- False→True に転じるのは**実データが filter/setup を実際に満たす行だけ**。
  条件を満たさない 11,428 行は 1 行残らず False のまま＝「とにかく True にした」のではない。
- 独立に計算した正解とも、正準 predicate とも、**不一致 0 件**。

---

## 4. live への影響: **0 件**（実測）

実運用と同じ経路を read-only で再現して計測した:

```
common/today_data_loader.load_basic_data(...)            <- scripts/run_all_systems_today.py
  -> System1Strategy.prepare_data(reuse_indicators=True, latest_only=True)
                                                         <- common/today_signals.py
  -> generate_candidates_system1(latest_only=True)
```

実ユニバース `data/universe_auto.txt` **4,655 銘柄**、当日キャッシュ
（`data_cache/`、最終足 2026-08-19）、`today=2026-08-20`。

| | 修正前 | 修正後 |
|---|---|---|
| basic frames | 4,557 | 4,557 |
| `_apply_filter_conditions` 到達フレームの列形状 | `Close` あり / `close` なし **4,557 件** | 同左 |
| prepared 最終行 `filter` True | 1,393 | 1,393 |
| prepared 最終行 `setup` True | 645 | 645 |
| 候補 (top_n=20) | 20 | 20 |
| 候補シンボル | 完全一致（下記） | 完全一致 |
| `predicate_only_pass_count` / `mismatch_flag` | 0 / 0 | 0 / 0 |

候補 20 件（前後で同一）:
`AEHR AMCR AMIX BFLY BNY CDNA DNTH ERAS FBRX IOVA IPST LQDA ORKA PFSA RVMD SLS SYRE TWST TXG WETO`

**4,557 銘柄ぶんの最終行 (`filter` / `setup` / `Close` / `dollarvolume20`) を含む
出力 JSON 全体が前後でビット同一**だった。

### なぜ live は元から正しかったのか

live のフレームは helper に届くまでに **2 回** 大文字化されている。

1. `common/today_data_loader.py::_normalize_ohlcv` — rolling キャッシュの
   `open/high/low/close/volume` を `Open/High/Low/Close/Volume` へ rename。
   さらに `_normalize_loaded` が `Date` 列を作る。
2. `core/system1.py::_rename_ohlcv` — latest_only fast path の先頭で
   小文字→PascalCase を再度適用（1 で済んでいるので no-op）。

したがって live の helper は**元から実 Close を読んでいた**。
`predicate_only_pass_count = 0` / `mismatch_flag = 0` は
「setup 列と predicate が 4,557 銘柄すべてで一致していた」ことを意味し、
これは live の setup が誤評価**されていなかった**ことの直接証拠である。

**§7-1 が懸念していた「live の setup が False→True に変わる」は起きない。**
§7-1 は helper 単体の欠陥としては正しかったが、live 経路への影響評価としては
過大だった（上流の正規化 2 段を勘定に入れていなかった）。

### 実際に修正が効く経路

現行コードで helper に**小文字のみ**のフレームが届きうるのは 1 か所だけ:

```python
prepare_data_vectorized_system1(raw_dict, reuse_indicators=True)   # latest_only 無し
```

この「通常 fast-path」は `_rename_ohlcv` を通さずに呼び出し側のフレームを
そのまま helper へ渡す。live はここを通らない（必ず `latest_only=True` で
latest_only 分岐へ入る）。バックテストも通らない
（`strategies/base_strategy.py::_prepare_data_template` 経由では
`reuse_indicators=None` が渡るため fast-path 条件が偽になり、
`_compute_indicators` = `normalize_ohlc_frame` 済みの batch 経路へ落ちる）。

つまり本修正は **今日の挙動を変えない代わりに、
「呼び出し側が事前に正規化していること」に依存していた暗黙の前提を外す**もの。
不変条件が helper 自身に入ったので、新しい呼び出し側が小文字フレームを渡しても
黙って全 False にはならない。

---

## 5. バックテスト数値の非影響

`docs/BACKTEST_ENGINE_GAPS_20260821.md` §6 と同じ形の 7 系統スイープを
**修正前 worktree と修正後 worktree で実行**して突合した
（60 銘柄 = SPY + 平均 dollarvolume20 上位 59、base キャッシュ全期間 535 行
= 2024-07-02〜2026-08-19、capital 100,000）。

```
system     prep            idx  OHLC  dates  cands                   form  single  integ
System1      60  DatetimeIndex  OHLC    335   3250        list:entry_date      45     33
System2      60  DatetimeIndex  OHLC    284    783     dict:NO_entry_date     112      7
System3      60  DatetimeIndex  OHLC     76    139              list:date      33     18
System4      60  DatetimeIndex  OHLC    253    834     dict:NO_entry_date      36     36
System5      60  DatetimeIndex  OHLC     47     55     dict:NO_entry_date      31     15
System6      60  DatetimeIndex  OHLC      2      2        dict:entry_date       1      1
System7       1  DatetimeIndex  OHLC     29     29        dict:entry_date      29      3
```

**この表は修正前・修正後で完全に同一**（JSON 全体が一致）。統合建玉も 113 で同数。

§6 の System1 = 24 / 16 とは絶対値が違うが、これは銘柄選定が異なるため
（§6 の「主要 59 銘柄」の具体的リストは残っていないので、こちらは
「平均 dollarvolume20 上位 59」という再現可能な定義で選び直した。ETF が入る）。
**重要なのは同一設定での前後差が 0 であること**で、これは System1 が
バックテストで通る経路（`_compute_indicators` → `normalize_ohlc_frame` で
大文字 `Close` が既に付与済み）が修正の影響を受けないことを意味する。
したがって §6 の 24 / 16 も変化しない。

---

## 6. テスト

`tests/test_backtest_engine_gaps.py` に **7 件追加**（`sec7_` prefix）:

| テスト | 内容 |
|---|---|
| `test_sec7_filter_reads_close_from_a_lowercase_frame` | 小文字フレームで `filter`/`setup` が正解列と完全一致 |
| `test_sec7_filter_still_false_where_the_data_genuinely_fails` | close<5 / dv20 非超過 / sma25<=sma50 / roc200<=0 は False のまま |
| `test_sec7_column_route_agrees_with_the_canonical_row_predicate` | 列経路と `system1_setup_predicate` が行ごとに一致 |
| `test_sec7_capitalised_live_shape_frames_are_unchanged` | live 形状（大文字）の答えが従来どおり |
| `test_sec7_capitalised_column_wins_when_both_cases_are_present` | `Close` と `close` が両方あるときは `Close` が勝つ |
| `test_sec7_missing_close_entirely_still_yields_false` | 価格列が無ければ fail-closed で False |
| `test_sec7_reuse_indicators_fast_path_on_a_lowercase_frame` | 実際に効く経路（§4 末尾）の end-to-end |

修正前のコードに対して **小文字系 3 件が FAIL、regression guard 4 件は PASS**。
修正後は 7 件とも PASS。

---

### 非退行（失敗 ID 集合の突合）

`origin/main` = `ffb5acb` の pristine worktree と本ブランチで同一コマンドを実行:

```
pytest tests -o addopts='' -q -p no:randomly --tb=no -rf \
    --ignore=tests/test_app_imports.py --continue-on-collection-errors
```

| | baseline (`ffb5acb`) | 本ブランチ |
|---|---|---|
| failed | 228 | 228 |
| passed | 2412 | 2419 (+7 = 新規テスト) |
| skipped / xfailed / xpassed | 61 / 1 / 13 | 61 / 1 / 13 |
| collection errors | 13 | 13 |

`comm` で突合して **新規失敗 0 件 / 修復 0 件**（失敗 ID 集合が完全一致）。
System1 周辺 13 ファイルに絞った sweep でも失敗 20 件が前後で同一で、
20 件すべてが baseline の 228 件に含まれる既存失敗だった。

**lint / 型:** `ruff check` は変更ファイル 2 つとも clean
（`ruff format --check` は baseline でも同じ 2 ファイルを指摘するので既存事象）。
`mypy --config-file mypy.ini core/system1.py` は baseline 3 件 → 本ブランチ 3 件で
**新規指摘 0 件**（行番号だけヘルパー追加ぶんずれる）。

---

## 7. 積み残し（本作業では触っていない）

1. `_apply_setup_conditions` は `sma25` / `sma50` / `roc200` / `filter` を
   小文字固定で引いている。観測されたどのフレーム形状でもこれらは小文字なので
   実害は出ていないが、同じ種類の脆さは残っている。
2. `_apply_filter_conditions` の dv20 条件は `> 50M`、
   `system1_setup_predicate` は `>= 50M`。実データでは境界一致が起きないため
   今回の突合でも不一致 0 件だったが、規約としては揃っていない。
3. `docs/BACKTEST_ENGINE_GAPS_20260821.md` §7 の 2（System7 フルスキャン
   `entry_price` payload の look-ahead）と 3（`common/validation/` が main に無い）は
   未着手のまま。
