# バックテスト計測可能性: 3 つのエンジン欠陥を修正 (2026-08-21)

**Status:** 実装済み。**バックテスト計測可能性だけ**が目的で、live のシグナル生成・
発注挙動は一切変えていない（§5 に根拠）。フラグ既定値の変更なし。

**Base:** `origin/main` = `f8fbba4`（指値到達判定 fix を含む）。

修正前は 7 系統のうち **3 系統だけ**がバックテストで建玉を持てた。System1 / System3 /
System6 / System7 は「シグナルが無い」のではなく**エンジン側の欠陥で消えていた**ため、
`common/validation/` の CPCV / DSR / bootstrap を含む全ての履歴指標が
「7 系統中 3 系統ぶん」でしかなかった。

---

## 1. 計測結果 (before / after)

実データ 60 銘柄（`data_cache/base` の SPY + 主要 59 銘柄、534 営業日 =
2024-07-02〜2026-08-19）、初期資金 100,000 USD。同一スクリプトを
pristine な `f8fbba4` worktree と本ブランチで実行して比較。

| System | books trades (before) | books trades (after) | 原因 |
|---|---|---|---|
| System1 | **NO** (0) | **YES** | GAP 3a |
| System2 | YES | YES | — |
| System3 | **NO** (0) | **YES** | GAP 1 |
| System4 | YES | YES | — |
| System5 | YES | YES | — |
| System6 | **NO** (0) | **YES** | GAP 2 |
| System7 | **NO** (0) | **YES** | GAP 3b |

数値の実測値は §6 の表を参照。

---

## 2. GAP 1 — System3 の候補が黙って全消えしていた

**症状:** System3 はバックテストで **建玉ゼロ**。ログも警告も出ない。

**原因:** `core/system3.py` のフルスキャンは候補を
`[{"symbol", "date", "drop3d", ...}]` の **list 形式**で返す。シグナル日は `date`
にあり `entry_date` は無い。一方エンジン側は

- `common/backtest_utils.py::simulate_trades_with_risk` — `{symbol: payload}` の
  **dict 形式のときだけ** `entry_date` を注入し、その後
  `df.index.get_loc(c["entry_date"])` を裸の `except Exception: continue` で囲んでいた。
- `common/integrated_backtest.py::_compute_entry_exit` — 同じく
  `df.index.get_loc(candidate["entry_date"])` → `except: return None`。

結果として System3 の候補は `KeyError` を握り潰されて 1 件残らず落ちていた。

**修正:** 新規 `common/candidate_schema.py`（driver 側の shim ではなくエンジン本体で解決）。

- `normalize_candidates_for_date(candidates, date, system=...)`
  dict 形式 / list 形式の双方を候補 dict のリストへ正規化する。dict 形式の
  `entry_date` 注入は従来と同一。
- `resolve_entry_bar(df, candidate, system=...)`
  候補が実際に約定するバーを解決する。`entry_date` があり、かつその足が df に
  存在すればそれを使う。無ければシグナル `date` の **次の足**（その銘柄自身の
  カレンダー基準）を採用する。`compute_entry` は `entry_idx - 1` の終値/ATR を
  参照するので、これがまさに戦略が値付けに使う足になる。
- **FAIL LOUD:** 日付キーを 1 つも持たない候補、mapping でない要素、mapping でも
  sequence でもないコンテナは `CandidateSchemaError` を送出する（黙って 0 建玉に
  ならない）。足が df に無いだけのケースは**データ条件**なので `None` を返して
  スキップ（例: 最終足でシグナルが出た）。

両エンジンが同じモジュールを使う:

- `common/backtest_utils.py:199,223` — 正規化 + `resolve_entry_bar`
- `common/integrated_backtest.py:82,345,373` — 同上

---

## 3. GAP 2 — System6 がバックテストでも最新 1 日に潰れていた

**症状:** `SYSTEM6_FORCE_LATEST_ONLY` の既定が `True` のため、
`generate_candidates_system6(latest_only=False)` と明示しても
`core/system6.py:411` で `latest_only = True` に上書きされ、バックテストでも
候補が最新 1 日ぶんしか出ない。

**consumer 一覧（修正前に全数確認）:**

| 場所 | 役割 |
|---|---|
| `config/environment.py:270` | `system6_force_latest_only` 既定 `True` |
| `core/system6.py:409-416` | 唯一の判定箇所 |
| `docs/technical/environment_variables.md:180` | 仕様記述 |

`FULL_SCAN_TODAY=1` / `SYSTEM6_FORCE_LATEST_ONLY=0` で無効化はできたが、
**どちらも live が読むグローバル**なので、バックテストのために倒すと当日実行の
挙動まで変わる。

**修正:** グローバルを倒す代わりに **実行コンテキストを見る**。

- 新規 `common/backtest_context.py` — `backtest_context()` (contextmanager) と
  `in_backtest_context()`。contextvar に加えて `QTS_BACKTEST_CONTEXT` env へも
  ミラーするので、コンテキスト内で起動した worker **プロセス**にも伝播する
  （退出時に元の値へ復元）。
- `core/system6.py:415` の強制条件に `and not in_backtest_context()` を追加。
- コンテキストを張るのはバックテストの入口だけ:
  `common/integrated_backtest.py::build_system_states` / `run_integrated_backtest`、
  `common/backtest_utils.py::simulate_trades_with_risk`、
  `common/ui_components.py::prepare_backtest_data`、
  `common/ui_bridge.py::prepare_backtest_data_ui`。

live はこのコンテキストに入らないので `in_backtest_context()` は常に `False`、
強制 latest_only は従来どおり効く。既定値・env・YAML は無変更。

---

## 4. GAP 3 — System1 / System7 がそもそも走らない

### 4a. System1

**症状:** 候補 0 件。`prepare_data` は 59 銘柄を返すのに `candidates_by_date` が空。

**原因は 2 つ**（どちらも batch prepare 経路 = `core/system1.py::_compute_indicators`）:

1. **日付インデックスが無い。** `load_base_cache()` は整数インデックス +
   全小文字カラムを返す。`_compute_indicators` はそれを正規化しないので、
   prepared frame は `RangeIndex` のまま。フルスキャンは `df.index` を日付として
   走るため候補キーが `0..533` の整数になり、エンジンの
   `df.index.get_loc(entry_date)` は原理的に当たらない。
2. **`setup` 列が全行 False。** `_apply_filter_conditions` は `x.get("Close")` と
   **大文字固定**で引くが、base cache のカラムは小文字 `close`。よって
   `Close → 0.0` に潰れて `filter` が全 False、`setup` も全 False。フルスキャン分岐は
   `setup` 列だけを見ているので候補が 1 件も立たない。

なお「フルスキャン分岐が削除済み」というのは**誤り**で、分岐自体は
`core/system1.py:1504-1692` に残っている（コメントが "unreachable" と書いているだけ）。
実際には *reachable だがデータ都合で必ず 0 件* だった。

**修正:**

- `common/system_common.py::normalize_ohlc_frame()` を新設（非破壊: 日付
  インデックスと大文字 OHLCV **別名**を足すだけで、小文字カラムは残す）。
  `_compute_indicators` がこれを通してから filter/setup を適用する。
- フルスキャン分岐の採否を live と同じ規約に揃える:
  `setup` 列が True、**または** 正準 predicate `system1_setup_predicate(row)` が
  True なら採用。live の latest_only 分岐（`core/system1.py:1172-1181`）が
  まさに `setup_col or predicate` で判定しているので、これは live との**収束**であり
  乖離ではない。
- `_resolve_entry_dates_bulk()` を新設し、フルスキャン分岐の entry_date 解決を
  1 回の NYSE schedule 呼び出しにまとめる（§6 末尾。分岐が動き出した途端に
  カレンダー再構築がボトルネックになったため）。

### 4b. System7

**症状:** `prepare_data` が `{}` を返す（SPY が落ちる）→ 候補 0、履歴 0。

**原因:** `prepare_data_vectorized_system7` は `Date`（大文字）列か datetime
インデックスを前提にしていた。base cache 形状（整数インデックス + 小文字）が来ると

- `else: df.index = pd.to_datetime(df.index).normalize()` が `0,1,2,...` を
  **1970 年のエポック日付**へ変換し、
- 続く `x["setup"] = x["Low"] <= x["min_50"]` が `Low` 不在で `KeyError`、
- それを外側の `except Exception` が拾って SPY を skip。

**「atr50 が欠落」は今回のキャッシュでは事実ではない**（`data_cache/base/SPY.feather`
には小文字 `atr50` / `min_50` / `max_70` がある）。真因はカラム大小文字と
インデックスだった。

**修正:**

- `normalize_ohlc_frame()` を通してから index を決める。日付が取れない場合は
  1970 を作らず `ValueError` を投げる。
- **バックテスト時のみ**、キャッシュに指標が無ければ OHLC から再計算する
  (`_derive_system7_indicators`)。式は `common/indicators_common.add_indicators`
  と同一:
  `atr50 = ta.volatility.AverageTrueRange(High, Low, Close, 50)`、
  `min_50 = Close.rolling(50).min()`、`max_70 = Close.rolling(70).max()`。
- **live は無変更。** 導出は `in_backtest_context()` が真のときだけ走るので、
  当日実行では従来どおり
  `IMMEDIATE_STOP: System7 missing indicator atr50 for SPY.` で止まる。
  古い SPY キャッシュを黙って埋めることはしない。

**外部データ依存: なし。** 3 指標はいずれも SPY の OHLC から導出できる純粋な
ローリング統計で、有償データも外部 API も不要。

---

## 5. live が同一であることの根拠

| 修正 | live に届くか | 根拠 |
|---|---|---|
| GAP 1 (`candidate_schema` + 両エンジン) | 届かない | `scripts/run_all_systems_today.py` から `common.backtest_utils` / `common.integrated_backtest` / `common.candidate_schema` / `common.ui_components` / `common.ui_bridge` は **静的到達不能**（関数内 import も含めて全 import ノードを辿って確認）。 |
| GAP 2 (`backtest_context` + system6 条件) | 届かない | live はコンテキストに入らないので `in_backtest_context()` は常に `False` → 強制 latest_only の条件式は従来と同値。既定値・env は無変更。 |
| GAP 3a (`_compute_indicators` 正規化 / フルスキャン採否) | 届かない | live は `common/today_signals.py:409` で `reuse_indicators=True, latest_only=True` を渡し、`prepare_data_vectorized_system1` の fast path で return する。batch 経路 (`_compute_indicators`) には来ない。候補生成も latest_only 分岐のみ。 |
| GAP 3b (System7 prepare) | **通るが出力は同一** | live のフレームは `core/today_pipeline/phase02_basic_data.py::_normalize_loaded` が既に大文字 `Date` + 大文字 OHLCV を付けている。`normalize_ohlc_frame` はその形に対して何も足さない no-op で、続く index 設定も従来と同じ `Date` 由来。指標導出は backtest コンテキスト限定。 |

**触っていないもの（意図的）:**
`core/system1.py::_apply_filter_conditions` の `x.get("Close")` 大文字固定は
バグだが、これは live の latest_only fast path も共有しているため**本作業では直さない**。
詳細は §7。

---

## 6. 実測 (60 銘柄 / 534 営業日 / capital 100k)

**BEFORE** — pristine `f8fbba4` worktree:

```
system     prep            idx  OHLC  dates  cands                   form  single  integ
System1      59     RangeIndex  none      0      0                      -       0      0
System2      59  DatetimeIndex  OHLC    255    567     dict:NO_entry_date      66     11
System3      59  DatetimeIndex  OHLC     22     37              list:date       0      0
System4      59  DatetimeIndex  OHLC    277   1141     dict:NO_entry_date      55     51
System5      59  DatetimeIndex  OHLC     24     26     dict:NO_entry_date      15      6
System6      59  DatetimeIndex  OHLC      0      0                      -       0      0
System7       0              -     -      0      0                      -       0      0
```

**AFTER** — 本ブランチ（同一スクリプト・同一データ）:

```
system     prep            idx  OHLC  dates  cands                   form  single  integ
System1      59  DatetimeIndex  OHLC    334   3256        list:entry_date      24     16
System2      59  DatetimeIndex  OHLC    255    567     dict:NO_entry_date      66     11
System3      59  DatetimeIndex  OHLC     22     37              list:date       5      1
System4      59  DatetimeIndex  OHLC    277   1141     dict:NO_entry_date      55     49
System5      59  DatetimeIndex  OHLC     24     26     dict:NO_entry_date      15     12
System6      59  DatetimeIndex  OHLC      4      4        dict:entry_date       2      2
System7       1  DatetimeIndex  OHLC     29     29        dict:entry_date       3      3
```

- `single` = `common/backtest_utils.simulate_trades_with_risk` の建玉数
- `integ`  = `common/integrated_backtest.run_integrated_backtest` の建玉数

**読み方:**

- 元から動いていた System2 / System4 / System5 の **single 値は 66 / 55 / 15 で完全一致**。
  候補件数・候補形式も一致しており、既存 3 系統に退行は無い。
- `integ` 側は System4 51→49、System5 6→12 と動くが、これは統合エンジンが
  **資金とスロットを 7 系統で奪い合う**設計だからで、今まで参加していなかった
  4 系統が入れば当然の再配分。single 側が不変なのがエンジン非退行の根拠。
- System1 の候補が 3,256 件と多いのは top_n × 334 日ぶんの候補で、
  `max_positions` により実建玉は 24 件に絞られる（想定どおり）。

### 付随: フルスキャンの性能

System1 のフルスキャン分岐は候補 1 件ごとに
`common/utils_spy.resolve_signal_entry_date()` を呼んでいた。この関数は
**呼び出しのたびに NYSE カレンダーの schedule を組み直す**ため、計測で
**1 呼び出し ≈ 100〜250 ms**。分岐が実際に候補を出すようになった途端、
60 銘柄・534 日でも 30 分以上かかる状態だった（cProfile: 31.3 s のうち
30.7 s が `resolve_signal_entry_date`）。

`_resolve_entry_dates_bulk()` を新設し、全シグナル日ぶんのエントリー日を
**1 回の schedule 呼び出し**で二分探索して作る。120 日ぶんで 30 s → **0.10 s**。
正準ヘルパーとの一致は `tests/test_backtest_engine_gaps.py::
test_gap3a_bulk_entry_dates_match_the_canonical_resolver` で検証（差分 0 件）。

---

## 7. 積み残し / 要判断

1. **`core/system1.py::_apply_filter_conditions` の大文字固定 `Close` 参照。**
   base cache 形状に対して `filter` / `setup` 列を常に False にしてしまう。
   直せば列が正しくなるが、この helper は **live の latest_only fast path も
   使っている**（`core/system1.py:750`）。現状 live は `setup` 列が False でも
   predicate へフォールバックするので採否は変わらない見込みだが、
   merged frame / diagnostics の `setup` 値が False→True に変わる。
   live 挙動に触れるため**本作業では変更せず、判断待ち**とする。
2. **System7 フルスキャンの `entry_price` payload に look-ahead がある。**
   `core/system7.py` のフル経路は履歴上のどの候補にも
   `last_price = df["Close"].iloc[-1]`（フレーム最終足の終値）を入れている。
   実約定価格は `System7Strategy.compute_entry` が `entry_idx` の `Open` を読むので
   トレード結果には影響しないが、payload の表示値としては誤り。別件。
3. **`common/validation/` は `origin/main` に無い**（`claude/monitor-webapp` のみ）。
   CPCV / DSR / bootstrap の再測定は、その package を main に載せた後に
   別途実施する必要がある。

---

## 8. テストと非退行の確認

**新規: `tests/test_backtest_engine_gaps.py`（22 件、全 pass）**

- candidate schema 単体 8 件 — dict 形式の `entry_date` 注入が従来どおりであること、
  `date` のみの list 形式が受理されること、`entry_date` 指定が優先されること、
  最終足シグナルは `None`（例外ではない）で落ちること、
  **日付キーが無い候補・mapping でない候補は `CandidateSchemaError`** で落ちること。
- GAP 1 — System3 形状の候補が **単独エンジンと統合エンジンの双方で建玉を持つ**こと
  （合成フィクスチャ、`System3Strategy` 実体を使用）。両エンジンともスキーマ不一致で
  **fail loud** すること。
- GAP 2 — backtest コンテキスト **外**では従来どおり 1 日に潰れ、**内**では全 setup 日が
  残ること（`PYTEST_CURRENT_TEST` を一時的に外して live 相当の条件を再現）。
  System6 が **2 日以上にまたがって建玉を持つ**こと。
- GAP 3a — `normalize_ohlc_frame` が非破壊であること、`setup` 列が無いフレームでも
  フルスキャンが predicate 経由で候補を出すこと、System1 が建玉を持つこと、
  bulk エントリー日が正準ヘルパーと完全一致すること。
- GAP 3b — base cache 形状の SPY から backtest コンテキスト内で指標を導出して
  prepare が通ること、**コンテキスト外では従来どおり SPY を落とす**こと
  （live の IMMEDIATE_STOP ガードが生きている証明）、System7 が建玉を持つこと。

**エンジンスイープ非退行:** §6 のとおり、既存 3 系統の単独バックテスト建玉数・
候補件数・候補形式が完全一致。

**フルスイート非退行（失敗 ID 集合の突合）:**

```
pytest tests -o addopts='' -q -p no:randomly \
    --ignore=tests/test_app_imports.py --continue-on-collection-errors
```

| | baseline (`f8fbba4`) | 本ブランチ |
|---|---|---|
| failed | 226 | 226 |
| passed | 2392 | 2414 (+22 = 新規テスト) |
| collection errors | 13 | 13 |

`comm` で突合した結果 **新規失敗 0 件 / 修復 0 件**（失敗 ID 集合が完全一致）。
baseline の 226 failed + 13 errors はいずれも本作業以前から存在するもの。
`tests/test_app_imports.py` は import 時に `sys.exit(1)` して collection 全体を
中断させるため、baseline / after ともに除外して比較した。

**lint / 型:** 変更した全ファイルで `ruff check` は clean。`mypy --config-file mypy.ini`
は baseline と突合して **新規指摘 0 件**（既存 13 件はそのまま）。
