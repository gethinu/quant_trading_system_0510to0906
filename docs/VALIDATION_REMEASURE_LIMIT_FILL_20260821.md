# 指値約定判定の修正後に methodology validation を再測定した結果

**日付**: 2026-08-21
**種別**: 測定のやり直し（記録目的）。**コードの挙動変更なし・ライブ発注は無変更**
**対象**: `docs/BACKTEST_LIMIT_FILL_FIX_20260820.md` §5 の要レビュー項目 **3.**
（「`common/validation/` で S3/5/6 を含めて出した数値は再実行が必要」）
**走らせたツリー**: ブランチ `claude/monitor-webapp` / HEAD `960487c`
（＝指値約定判定の修正コミットそのもの。詳細は §1）

> **この文書は「削るための資料」ではない。** System1–7 は書籍 (Bensdorp) 準拠の
> 7 本セットとして一括で維持する方針が既に決まっており、本再測定はその方針を
> 変えない。数字が悪い系統についても **枠は維持し、配分の公平性はアロケータ側で
> 扱う**。ここに書いてあるのは「いくつだったか」であって「どれを止めるか」ではない。

---

## 0. 要旨

- 修正前の S3/5/6 単独の CPCV/DSR レポートは **そもそも一度も存在しなかった**。
  `results_csv/` に残っていたのは 2026-08-11 の 10 銘柄デモ（`realrun_single_system1`
  と `realrun_integrated`）2 件だけで、しかも `results_csv/` は gitignore 対象。
  よって before/after は **引用ではなく測定** するしかない（§2）。
- そこで **同一候補集合に対する A/B** を実行した。arm `prefix` は修正前の
  `compute_entry`、arm `fixed` は現行ツリー。`prefix` が本当に修正前と同一である
  ことは、**実際の修正前コードを走らせて全トレードのダイジェスト一致で検証済み**（§3）。
- 結果（§4）: 3 系統すべてで数字は悪化した。とくに **System6 は
  bootstrap `P(SR≤0)` が 0.046 → 1.000** に反転する。修正前は 5% 有意に見えていた。
- **DSR は修正前も修正後も全系統・統合ともに閾値 0.95 を下回る（FAIL）**。
  つまり「修正で PASS が FAIL になった」のではなく、**元から一つも PASS していない**。
- 副産物として、validation を回すために踏まなければならなかった **エンジン側の
  既存の穴が 3 つ** 見つかった（System3 の候補スキーマ、System6 の latest_only 強制、
  System1/7 がそもそも走らない）。いずれも本修正とは無関係の先行不具合（§5）。

---

## 1. どのツリーで走らせたか（＋ブランチ状況の訂正）

再測定は **`claude/monitor-webapp` の HEAD `960487c`** で実行した。実行時の
`git rev-parse HEAD` は結果 JSON の `git_head` に記録してある。

依頼時の前提と実際のリポジトリ状態が食い違っていたので明記する:

| 主張 | 実際 |
|---|---|
| 修正は `claude/open-auto-run` の `ba9722d` にある | ❌ `ba9722d` は **`fix(obs): 非同期 close の 200 を…`** という別件。`origin/claude/open-auto-run` に `_limit_entry_filled` は **無い** |
| 修正は PR #162 で main にミラー済み | ❌ PR #162 は上記 obs 修正（branch `claude/fix-close-fill-accounting-20260820`）で **OPEN のまま**。指値修正とは無関係 |
| — | ✅ 指値修正 `960487c` を含むのは **`claude/monitor-webapp` だけ**（`git branch -a --contains 960487c` で確認）。`origin/main` の `strategies/base_strategy.py` に `_limit_entry_filled` は **無い** |

したがって「修正済みコードを走らせられる唯一のブランチ」は `claude/monitor-webapp`
であり、そこで走らせた。**指値修正はまだ main に載っていない**ので、main へ運ぶ
判断は別途必要（本再測定はその判断材料）。

---

## 2. なぜ「旧レポートの引用」ではなく A/B なのか

再測定前に `results_csv/` にあった validation 成果物は以下がすべて:

| ファイル | 中身 |
|---|---|
| `validation_realrun_single_system1_20260811_152946.json` | 10 銘柄・System1・`n_groups=5` のデモ |
| `validation_realrun_integrated_20260811_152950.json` | 同条件の統合エンジン デモ |

`outputs/methodology_upgrade_20260811.md` §7 自身が
「Numbers are a small-sample demonstration, not a strategy claim」と断っており、
`membership_path` が `/sessions/upbeat-busy-planck/...` と別環境（sandbox）を指している。
**System3/5/6 単独の CPCV/DSR は一度も出力されていない。**

よって「膨張した旧数値」は文書として存在しない。before/after を出すには、
**修正前のコードを実際に走らせて before を作る**しかない。それが §3 の A/B。

これら 2 件は上書きせず `results_csv/archive/pre_limit_fill_fix_20260811/` に
（当時の `logs/validation_reports.log` の該当 2 行つきで）退避した。

---

## 3. A/B の作り方と、その正当性の検証

### 3.1 仕組み

`outputs/impl/limit_fill_fix/revalidate_limit_fill.py`

- `build_system_states()` を **1 回だけ** 走らせ、両 arm で同じ候補集合を共有する
  （修正は候補生成に一切触れていないので、差分はすべて約定判定に帰属する）。
- arm `fixed`: 現行ツリーそのまま。
- arm `prefix`: `StrategyBase._limit_entry_filled` を `True` を返すよう差し替える。
  修正コミットが S3/5/6 に加えたのは

  ```python
  if not self._limit_entry_filled(df, entry_idx, entry_price, <side>):
      return None
  ```

  という **加算的なガード 1 個だけ**（`git show 960487c` で確認）なので、述語を
  恒真にすれば修正前の `compute_entry` と等価になる。

### 3.2 「等価」を測定で確認した

コードを読んだ議論だけで済ませず、**修正前コードを実際に走らせて突合**した
（`outputs/impl/limit_fill_fix/verify_prefix_equivalence.py`）。
`960487c` が触ったのは `strategies/` と tests/docs だけなので、修正前ランタイムは
**`git archive e00e1c3 strategies` ＋ 現行の `common`/`core`/`config`** で完全に再現できる。
3 つの子プロセス（`prefix_tree` / `forcefill` / `fixed`）でそれぞれ
`simulate_trades_with_risk` を走らせ、全トレード行の SHA-256 ダイジェストを比較:

| system | 修正前ツリー実走 | ForceFill 再現 | 現行（修正済み） | 判定 |
|---|---|---|---|---|
| System3 | n=579 pnl=−21,936.73 `3280aedc3a935fcd` | n=579 pnl=−21,936.73 `3280aedc3a935fcd` | n=430 pnl=−65,637.74 `2f34a3e18564e03f` | 再現=**完全一致** / 修正の効果=あり |
| System5 | n=216 pnl=−36,791.32 `da705b0c398bb3a5` | n=216 pnl=−36,791.32 `da705b0c398bb3a5` | n=197 pnl=−41,317.42 `424f079a72bc345f` | 同上 |
| System6 | n=7 pnl=−382.98 `f91a6dc9604f415c` | n=7 pnl=−382.98 `f91a6dc9604f415c` | n=5 pnl=−1,088.43 `0903f9d3a30447fe` | 同上 |

（`--limit 400` の検証用サブセット。結果 JSON: `validation_20260821/prefix_equivalence_400.json`）

**もう一つの対照**: 指値を使わない System2 / System4 は、本測定でも両 arm が
トレード数・勝率・平均リターン・fold Sharpe・DSR まで **完全同値**（§4）。
差分が S3/5/6 だけに出ていることが本文中で確認できる。

### 3.3 実行条件

```
universe   : data/universe_auto.txt 4,654 銘柄（全量）
価格データ : data_cache/rolling（SPY 基準で 2024-07-02 〜 2026-08-18、534 営業日）
capital    : 100,000
CPCV       : n_groups=6, k_test=2, embargo=0.01 → 15 combinations / 5 paths
bootstrap  : n_boot=2000, seed=12345（moving-block）
DSR        : N=15（CPCV 組合せ数）、閾値 0.95、benchmark は fold 間 Sharpe 分散から推定
env        : VALIDATION_ENABLED=1  SYSTEM6_FORCE_LATEST_ONLY=0  SURVIVORSHIP_GUARD=warn
```

`SYSTEM6_FORCE_LATEST_ONLY=0` が必要な理由は §5.2。

---

## 4. 結果 — before（修正前）/ after（修正後）

### 4.1 約定判定の効き方（フルサンプル・単独エンジン）

| system | 指値 | 候補数 | 建玉 修正前 | 建玉 修正後 | 約定率 | 勝率 前→後 | 平均トレードリターン 前→後 | 累積 PnL / 初期資本 前→後 |
|---|---|---:|---:|---:|---:|---|---|---|
| System1 | — | — | — | — | — | — | — | 候補 0（§5.3） |
| System2 | なし | 4,811 | 547 | 547 | 1.000 | 0.4022 → 0.4022 | −0.0495 → −0.0495 | −166.6% → −166.6% |
| **System3** | あり | 3,604 | 1,293 | **999** | **0.773** | 0.4354 → **0.3223** | −0.0359 → **−0.0692** | −135.2% → −133.3% |
| System4 | なし | 3,158 | 83 | 83 | 1.000 | 0.1566 → 0.1566 | +0.0009 → +0.0009 | −1.3% → −1.3% |
| **System5** | あり | 2,530 | 750 | **685** | **0.913** | 0.4747 → **0.4292** | −0.0351 → **−0.0529** | −69.0% → −82.0% |
| **System6** | あり | 165 | 112 | **54** | **0.482** | 0.6071 → **0.3148** | **+0.0092 → −0.0213** | **+10.1% → −10.9%** |
| System7 | — | — | — | — | — | — | — | 候補 0（§5.3） |

System2 / System4 が両 arm 完全同値であること自体が、修正の作用範囲が
S3/5/6 に限定されていることの本文内証拠になっている。

> **約定率が `docs/BACKTEST_LIMIT_FILL_FIX_20260820.md` §4（32.0% / 52.5% / 40.3%）と
> 違うのは正常**。あちらは probe が生成した「全セットアップ行 261,741 件」に対する
> 比率で、こちらは **エンジンが日ごとに top-N ランキングで絞った候補** に対する比率。
> 母集団が違う。約定判定そのもののロジックは同一（§3.2 でダイジェスト一致）。

### 4.2 Sharpe が解釈可能かどうかのガード（equity のゼロ交差）

`common/validation/metrics.py` は equity を `capital + cumsum(pnl)` で作り
`pct_change()` を取る。**この系列がゼロを跨ぐと騰落率の符号が反転し、
そこから出た Sharpe / DSR は成績の記述ではなくなる**。黙って載せず明示する:

| system | arm | 最小 equity | 最終 equity | ゼロ交差 | Sharpe は解釈可能か |
|---|---|---:|---:|---|---|
| System2 | 修正前 | −66,628 | −66,628 | **あり** | **不可** |
| System2 | 修正後 | −66,628 | −66,628 | **あり** | **不可** |
| System3 | 修正前 | −37,690 | −35,219 | **あり** | **不可** |
| System3 | 修正後 | −34,089 | −33,266 | **あり** | **不可** |
| System4 | 修正前/後 | 74,679 | 98,660 | なし | 可 |
| System5 | 修正前 | 30,600 | 30,978 | なし | 可 |
| System5 | 修正後 | 18,002 | 18,021 | なし | 可 |
| System6 | 修正前 | 97,322 | 110,057 | なし | 可 |
| System6 | 修正後 | 89,072 | 89,072 | なし | 可 |
| **統合 (7)** | 修正前 | 71,223 | 72,727 | なし | **可** |
| **統合 (7)** | 修正後 | 59,133 | 61,182 | なし | **可** |

単独エンジンは **アロケータを通さず 1 系統に資本 100,000・`risk_pct=2%`・
`max_positions=10` をフル適用する**ため、2 年で累積損失が初期資本を超える系統が出る。
これは実運用構成ではない。**実運用構成である統合ランは両 arm ともゼロ交差なし**なので、
統合の数字は素直に読める。System2 / System3 単独の Sharpe/DSR は
「参考値」として扱い、代わりに §4.1 の勝率・平均リターン・PnL を見ること。

### 4.3 CPCV / bootstrap / Deflated Sharpe

| system | arm | フルサンプル Sharpe | fold Sharpe 平均±標準偏差 | fold 最小/最大 | fold>0 の割合 | bootstrap 95%CI | P(SR≤0) | DSR (N=15) | 判定 |
|---|---|---:|---|---|---:|---|---:|---:|---|
| System1 | — | — | — | — | — | — | — | — | 候補 0 |
| System2 † | 修正前 | 0.465 | −2.713 ± 0.514 | −3.787 / −2.062 | 0.00 | [−0.686, 1.031] | 0.143 | 0.134 | FAIL |
| System2 † | 修正後 | 0.465 | −2.713 ± 0.514 | −3.787 / −2.062 | 0.00 | [−0.686, 1.031] | 0.143 | 0.134 | FAIL |
| **System3** † | 修正前 | 0.619 | −2.491 ± 0.974 | −4.068 / −0.968 | 0.00 | [−1.186, 1.843] | 0.216 | 0.034 | FAIL |
| **System3** † | **修正後** | 0.007 | **−3.849 ± 1.481** | −5.958 / −1.392 | 0.00 | [−2.314, 1.232] | 0.527 | **0.000** | FAIL |
| System4 | 修正前 | 0.077 | 0.070 ± 0.633 | −1.510 / 0.950 | 0.60 | [−4.409, 0.961] | 0.475 | 0.064 | FAIL |
| System4 | 修正後 | 0.077 | 0.070 ± 0.633 | −1.510 / 0.950 | 0.60 | [−4.409, 0.961] | 0.475 | 0.064 | FAIL |
| **System5** | 修正前 | −2.067 | −2.002 ± 1.208 | −4.198 / 0.422 | 0.07 | [−3.603, −0.776] | 1.000 | 0.000 | FAIL |
| **System5** | **修正後** | **−3.587** | **−3.017 ± 1.437** | −6.383 / −0.927 | 0.00 | [−4.738, −2.351] | 1.000 | 0.000 | FAIL |
| **System6** | 修正前 | **+1.072** | **+0.702 ± 0.928** | −1.125 / 2.149 | **0.80** | [−0.174, 2.135] | **0.046** | 0.144 | FAIL |
| **System6** | **修正後** | **−2.051** | **−1.718 ± 0.628** | −3.289 / −0.897 | **0.00** | [−2.790, −1.197] | **1.000** | 0.000 | FAIL |
| System7 | — | — | — | — | — | — | — | — | 候補 0 |
| **統合 (7)** | 修正前 | −1.940 | −1.625 ± 0.716 | −2.797 / −0.370 | 0.00 | [−3.125, −0.773] | 1.000 | 0.000 | FAIL |
| **統合 (7)** | **修正後** | **−2.977** | **−2.311 ± 0.860** | −4.106 / −0.523 | 0.00 | [−4.241, −1.711] | 1.000 | 0.000 | FAIL |

† = §4.2 のゼロ交差により、この行の Sharpe / DSR は解釈不能（値は記録のため掲載）。

統合ランの survivorship 監査は両 arm とも **BIASED**（`data/universe_membership.csv`
が無く、現在のメンバーシップを過去価格に当てているため。CLAUDE.md の既知事項）。

### 4.4 スイート自身の閾値を通っているか

**通っていない。修正前も修正後も、単独 5 系統・統合ともに DSR は 0.95 未満で FAIL。**

- 修正で PASS→FAIL になった系統は **ゼロ**。元から一つも PASS していない。
- **最も情報量のある変化は System6**: bootstrap `P(SR≤0)` が **0.046 → 1.000**。
  修正前は「5% 水準で SR>0」に見えていた唯一の系統だったが、その見かけは
  **到達していない売り指値（＝その後も上がらなかった銘柄）を勝ちトレードとして
  数えていたこと**によるものだった。fold>0 の割合も 0.80 → 0.00。
- System3 は `P(SR≤0)` 0.216 → 0.527、fold Sharpe 平均 −2.491 → −3.849。
- System5 は修正前から既に `P(SR≤0)=1.000` で、修正はそれをさらに悪化させた
  （fold 平均 −2.002 → −3.017）。**`docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md`
  の proxy sim が当時示していたマイナス期待値のほうが正しかった**、という
  `docs/BACKTEST_LIMIT_FILL_FIX_20260820.md` §5-2 の見立てを、今回の再測定は支持する。
- 統合も −1.940 → −2.977 と悪化。**S3/5/6 を含むポートフォリオ指標も過大だった**
  という修正 doc §5-1 の予告どおり。

### 4.5 これは削減判断ではない

上の数字は **測定結果であって処分ではない**。System1–7 は Bensdorp 準拠の
7 本セットとして維持する方針であり、本再測定はそれを変更しない。
弱く見える系統についても **枠は維持し、スロットの公平性はアロケータ側で扱う**。

数字の使いどころは「どれを消すか」ではなく、たとえば

- 統合バックテスト由来の実績を対外的に引用する際の訂正、
- D3 の System5 判断の再検討（§4.4）、
- §5 のエンジン側の穴を塞いだうえでの再測定、

といった **測定・記述の正確さ** の側にある。

---

## 5. 副産物: validation を回すために踏んだエンジン側の既存の穴

いずれも **本修正とは無関係の先行不具合**。ここでは塞がず、記録に残す。

### 5.1 System3 の候補スキーマが両エンジンで落ちる（`date` vs `entry_date`）

`core/system3.py` の backtest 経路が返す候補は `{date: [レコード…]}` の **リスト形式**で、
レコードは `date` を持つが `entry_date` を持たない。ところが両エンジンは
**dict 形式 `{date: {symbol: payload}}` のときだけ** `entry_date` を注入する
（`common/backtest_utils.py:168-179`, `common/integrated_backtest.py:315-327`）。
リストはそのまま通り、直後の `df.index.get_loc(c["entry_date"])` が KeyError →
`except: continue` で **全件が黙って捨てられる**。

結果、**System3 は単独エンジンでも統合エンジンでも建玉ゼロ**（実測: 候補 3,604 件 → 0 件）。
統合バックテストの過去の成果物にも System3 は入っていなかった可能性が高い。

今回はリポジトリ自身の `common/system_candidates_utils.normalize_candidates_by_date`
（`core/system5` / `core/system6` が既に呼んでいる正規化）をドライバ側で適用して
測定した（`--normalize-list-candidates`）。**プロダクションコードは未変更**。

### 5.2 System6 は既定でバックテストでも `latest_only` に強制切替される

`SYSTEM6_FORCE_LATEST_ONLY` の既定が **True**（`config/environment.py:270-271`）で、
`FULL_SCAN_TODAY` が偽・pytest 外なら `latest_only=False` の明示指定を
**上書きして** fast-path に落ちる（`core/system6.py:381-425`）。
その状態だと候補は **最新 1 日ぶんだけ**（実測: 4,654 銘柄で 1 日 10 件）になり、
CPCV は組めない。`SYSTEM6_FORCE_LATEST_ONLY=0` を立てて初めて
95 日 165 件の履歴が出る。
同フラグの docstring 自身が「将来 System6 の過去日ランキング分析をする際は
このフラグを False に設定」と書いており、今回がまさにその場面。

**過去に System6 を含めて回した validation / バックテストが
このフラグを落としていなかったなら、System6 は実質不在だった。**

### 5.3 System1 と System7 はそもそもバックテスト履歴を作れない

- **System1**: 全期間スキャン側の分岐が削除済み。`core/system1.py:1439-1441` に
  「Original else block (latest_only=False) is now unreachable because we always
  use latest_only=True in production」と明記されている。実測でも候補 0 件。
- **System7**: SPY のキャッシュに事前計算済み `atr50` が無く、
  `prepare_data_vectorized_system7` が `IMMEDIATE_STOP` を投げて prepared 0 件。
  SPY は universe 外で rolling 同期の対象外、という既知の運用事情に起因する。

どちらも指値を使わないので本 A/B の結論には影響しないが、
**「7 系統ぶんの CPCV」は現状のエンジンでは物理的に出せない**（出せるのは 5 系統）
という事実は記録しておく。

---

## 6. 成果物

| 何 | どこ |
|---|---|
| A/B ドライバ | `outputs/impl/limit_fill_fix/revalidate_limit_fill.py` |
| 修正前等価性の検証 | `outputs/impl/limit_fill_fix/verify_prefix_equivalence.py` |
| 表の生成 | `outputs/impl/limit_fill_fix/build_revalidation_table.py` |
| 再測定 生データ（summary + 全 12 レポート + fold CSV） | `outputs/impl/limit_fill_fix/validation_20260821/` |
| 生成された before/after 表 | `outputs/impl/limit_fill_fix/validation_20260821/before_after_table.md` |
| 等価性検証の結果 | `outputs/impl/limit_fill_fix/validation_20260821/prefix_equivalence_400.json` |
| 退避した修正前レポート（2026-08-11） | `results_csv/archive/pre_limit_fill_fix_20260811/`（gitignore 下・ローカルのみ） |
| 退避した中間ラン | `results_csv/archive/superseded_ab_runs_20260821/`（同上） |

### 再現手順

```bash
VALIDATION_ENABLED=1 SYSTEM6_FORCE_LATEST_ONLY=0 SURVIVORSHIP_GUARD=warn \
python outputs/impl/limit_fill_fix/revalidate_limit_fill.py \
    --normalize-list-candidates \
    --n-groups 6 --k-test 2 --embargo 0.01 --n-boot 2000 --seed 12345 \
    --out <出力ディレクトリ>

# 修正前 arm が本当に修正前コードと一致するかの検証
python outputs/impl/limit_fill_fix/verify_prefix_equivalence.py \
    --prefix-tree <git archive e00e1c3 strategies を展開したディレクトリ> \
    --limit 400 --normalize
```

`build_system_states` に約 16 分（4,654 銘柄 × 7 系統）、CPCV 評価はすべて合わせて
約 90 秒。`--states-cache` を渡すと 2 回目以降は再構築を省ける。

---

## 7. テストと安全性

- **ライブ発注は無変更**。本作業はバックテスト／検証経路のみ。paper 限定。
- `common/validation/` にも `strategies/` にも **プロダクションコードの変更は無い**。
  追加したのは `outputs/impl/limit_fill_fix/` 配下のドライバと成果物、および本 doc。
- テスト: `tests/test_validation_*.py` + `tests/test_limit_entry_fill_realism.py` +
  修正コミットが触った既存 4 ファイル = **83 passed / 6 failed**。
  失敗 6 件はすべて `tests/test_system5_old.py` の先行不具合
  （`compute_entry() got an unexpected keyword argument 'current_capital'` など）。
  同ファイルを修正前 (`e00e1c3`) のツリーで走らせると **8 failed / 3 passed** なので、
  修正コミットは失敗を 2 件減らしており、**新規失敗はゼロ**。
