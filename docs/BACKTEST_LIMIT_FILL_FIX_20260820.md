# バックテスト忠実度修正: System3/5/6 の指値エントリーは「必ず約定」ではない

**日付**: 2026-08-20
**種別**: バックテスト忠実度 (fidelity) の欠陥修正。**ライブ発注の挙動は変更なし**
**発見経路**: `outputs/SIGNAL_CALIBRATION_PROBE_20260820.md` §5「Fill realism」
**影響**: System3 / System5 / System6 の **過去のバックテスト勝率はすべて過大**

> **`main` 上での注記 (2026-08-21)**: 本 doc が参照する
> `outputs/SIGNAL_CALIBRATION_PROBE_20260820.md` / `outputs/impl/signal_calibration_probe/` /
> `common/validation/` は、いずれも別系統の作業として `claude/monitor-webapp` にのみ存在し
> **`main` には無い**。指値約定修正そのもの（`strategies/` の変更・テスト・再測定スクリプト
> `outputs/impl/limit_fill_fix/`）は `main` 上で完結しており、これらの参照は発見経路と
> 影響棚卸しの記録として残している。

---

## 1. バグ

System3 / System5 / System6 は、前日終値から離した **指値** で仕掛ける。

| system | side | 指値 | config |
|---|---|---|---|
| System3 | long (buy limit) | `prev_close × 0.93` | `strategies.system3.entry_price_ratio_vs_prev_close` |
| System5 | long (buy limit) | `prev_close × 0.97` | `strategies.system5.entry_price_ratio_vs_prev_close` |
| System6 | short (sell limit) | `prev_close × 1.05` | `strategies.system6.entry_price_ratio_vs_prev_close` |

指値は板がそこまで来なければ約定しない。ところが `compute_entry` は指値を計算した
あと、**当日バーがその値段を通過したかを一切確認せずに** `(entry_price, stop_price)`
を返していた。バックテストエンジン
(`common/backtest_utils.py::simulate_trades_with_risk`,
`common/integrated_backtest.py::_compute_entry_exit`) は `compute_entry` が
`None` 以外を返した候補を**必ず建玉にする**ため、実際には約定し得なかった候補まで
トレードとして計上され、そのぶん勝率が押し上げられていた。

**locus（修正前 / commit `e00e1c3`）**

| file | 行 | 内容 |
|---|---|---|
| `strategies/system3_strategy.py` | 200 → 216 | 指値算出から `return` まで、到達判定なし |
| `strategies/system5_strategy.py` | 218 → 238 | 同上 |
| `strategies/system6_strategy.py` | 216 → 233 | 同上 |

同じ「必ず約定」前提はテスト側にも埋め込まれていた（`tests/test_system6.py` の
エントリー日バーは高値 100〜101 なのに売り指値は 105.0 など）。テストが仕様として
バグを固定していたため、長期間検出されなかった。

**なぜ System1/2/4/7 は無事だったか**: いずれも寄り成行 (`Open`) エントリーで、
成行は必ず約定する。System2 は指値ではなく「上窓 +4% 以上」という**当日バーの条件**を
`compute_entry` 内で判定し、満たさなければ `None` を返している
(`strategies/system2_strategy.py:178-180`)。本修正はこの System2 の規約に揃えたもので、
新しいルールを発明したわけではない。

## 2. 検出

signal-score 校正 probe (`outputs/impl/signal_calibration_probe/`) が、候補バーの
うち実際に指値へ到達した割合を測ったところ:

| system | 指値に到達した候補バーの割合 |
|---|---|
| System3 | 32.0% |
| System5 | 52.5% |
| System6 | 40.3% |

つまり System3 の場合、バックテストが計上していたトレードの **約 2/3 は実在しない**。

なお `docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md` (2026-07-02) の proxy sim は
System5 について「fill 数 (T+1 limit 到達) 81 (36.5%)」と、**当時すでに約定判定を
モデル化していた**。約定判定の必要性は認識されていたが、エンジン本体には入っていなかった。

## 3. 修正

`strategies/base_strategy.py::StrategyBase._limit_entry_filled()` を追加し、
System3/5/6 の `compute_entry` から呼ぶ。約定判定の規約は
**本エンジンが exit 側で既に使っているもの**と同一にした
（`compute_exit` は `Low <= stop` / `High >= target` で到達を判定し、約定値は
その指定価格そのものとする）:

- long (buy limit) : `Low[entry_bar] <= limit` なら約定、約定値 = 指値
- short (sell limit): `High[entry_bar] >= limit` なら約定、約定値 = 指値
- 到達しなければ `compute_entry` は `None` を返す → エンジンは候補をスキップ
  （幻の建玉なし・先読みなし）
- `Low`/`High` が欠損 (NaN) の場合は **fail-closed**（約定しなかったものとして扱う）

**保守的側への倒し方（既知の近似）**: 窓開けして指値より有利に寄り付いた場合、
実際の約定値は寄値（long なら指値未満、short なら指値超）になるが、本実装は指値で
約定したものとして扱う。したがってリターンを**過小**評価する側に倒れる。これは
exit 側の stop/target 約定の扱いと一貫しており、意図的に据え置いた。

`next-bar` 規約は従来どおり変更なし（シグナルは `e-1` バー、エントリーは `e` バー）。

## 4. 修正前後の実測

probe と同一データ（`data_cache/rolling`, `data/universe_auto.txt` 4,654 銘柄、
シグナル日 2024-07-16〜2026-08-17、`--max-hold 60`、261,741 候補）で、**リポジトリの
（修正済み）`compute_entry` を実際に走らせて**再測定した。

| system | 候補数 | 勝率 (修正前) | 建玉数 (修正後) | 約定率 | 勝率 (修正後) | 平均リターン (前→後) |
|---|---|---|---|---|---|---|
| System3 | 7,289 | 0.7573 | 2,332 | 32.0% | **0.4910** | +0.0760 → **−0.0048** |
| System5 | 2,268 | 0.6358 | 1,191 | 52.5% | **0.4677** | +0.0302 → **−0.0147** |
| System6 | 30,760 | 0.7339 | 12,381 | 40.3% | **0.5588** | +0.0461 → **−0.0023** |

検算: 修正後の `compute_entry` の約定/不約定の判定は、probe が独立に計算した
`limit_filled` フラグと **全行一致** (7,289/7,289, 2,268/2,268, 30,760/30,760)、
約定価格も一致。過補正・過小補正ではない。

再現:

```bash
python outputs/impl/signal_calibration_probe/build_dataset.py --out <dir> --max-hold 60
python outputs/impl/limit_fill_fix/remeasure_limit_fill.py <dir>/candidates.parquet <out.json>
```

結果 JSON: `outputs/impl/limit_fill_fix/remeasure_20260820.json`

**平均リターンが 3 系統とも 0 近傍〜マイナスに落ちる**点に注意。修正前の
「勝率 73〜76% / 平均 +4〜8%」は、約定しなかった候補（＝指値まで下げ／上げなかった、
つまりその後も逆行しなかった銘柄）を勝ちトレードとして数えていたことによる
生存者バイアスに近い構造だった。

## 5. 影響を受ける過去の評価 — 要レビュー 🚩

以下は**自動では書き換えない**。数字が変わる可能性があるものとして明示的に棚卸しする。

1. 🚩 **System3 / System5 / System6 のバックテスト由来の実績値すべて**
   （`simulate_trades_with_risk` および `run_integrated_backtest` 経由で出力された
   勝率・CAGR・PnL・エクイティカーブ。UI/レポート/`results_csv/` の過去成果物を含む）。
   3 系統が含まれる**統合バックテストのポートフォリオ指標も同様に過大**。
2. 🚩 **`docs/D3_LIQUIDITY_FILTER_ATR_THRESHOLD_20260702.md` の System5 判断**。
   同 doc の解釈節は「現行 backtest レポート (subscriber 向け) が正のリターンを
   示している」ことを、proxy sim のマイナス期待値を割り引く根拠にしている。その
   前提が本修正で崩れた。proxy sim 側は当時から約定判定を入れており、**マイナス
   期待値という proxy の結論のほうが正しかった**可能性が高い。再判断が必要。
3. 🚩 **methodology validation スタック (`common/validation/`) で S3/5/6 を含めて
   出した数値**（CPCV fold Sharpe / bootstrap CI / Deflated Sharpe）。
   `common/validation/evaluate.py` は両エンジンを**そのまま**走らせる設計なので、
   修正前に取得したレポートは同じ膨張を継承している。再実行が必要。
4. ℹ️ `outputs/SIGNAL_CALIBRATION_PROBE_20260820.md` §5 の「all rows」列（S3/5/6）は
   既に膨張済みと注記されているため、追加対応は不要。同 §7 の live 比較表は
   「fillable」列を使っており、そのまま有効。

**GO/デプロイ判定について**: 本リポジトリには System 単位の GO ゲート文書は存在せず、
7 システムは書籍 (Bensdorp) 準拠の構成として一括で有効化されている。したがって
「膨張した勝率のみを根拠に GO した」と特定できる決裁は無い。ただし上記 1〜3 は
運用継続の判断材料として参照され得るため、**System3/5/6 の継続可否は再測定後の
数字で判断すること**。3 系統とも修正後の平均リターンが 0 近傍〜マイナスであり、
これは無視できる差ではない。

## 6. ライブ発注への影響: なし

- ライブ/当日シグナル経路 (`common/today_signals.py::_compute_entry_stop`) は
  `candidate["entry_date"]` が**翌営業日**（`common/utils_spy.py::resolve_signal_entry_date`）
  で、その行は価格データにまだ存在しない。よって `compute_entry` は本修正の前から
  `df.index.get_loc` で `None` を返しており、挙動は変わらない。
- 既存建玉の exit 計画で使う `apps/app_today_signals.py::_entry_and_stop_prices` は
  `compute_entry` を経由しない独立実装で、**約定済み**建玉の理論エントリー価格を
  復元するもの。ここに約定判定を入れてはならず、変更していない。
- 発注そのもの（Alpaca への指値送信）は無変更。paper のみ。

## 7. 回帰テスト

- `tests/test_limit_entry_fill_realism.py` (新規, 12 件)
  - S3/S5/S6 それぞれ「指値に 1 セント届かないバー → 約定しない」「届くバー →
    指値ちょうどで約定」、境界（安値 = 指値）は約定側
  - `Low`/`High` が NaN のバーは約定しない (fail-closed)
  - エンジン経路 (`simulate_trades_with_risk`): 不約定候補はトレード表に現れない /
    約定候補は `entry_price` = 指値で計上される
- 既存テストのうち「必ず約定」を仕様として固定していたフィクスチャを修正:
  `tests/test_entry_exit_integration.py`, `tests/test_system6.py`,
  `tests/test_system5_old.py`, `tests/test_monthly_roll_forward.py`
  （`_gen_ohlc` に `force_gap_down_on` を追加し、S3/S5 の買い指値に届くバーを生成。
  併せて System5 も「エントリーが 1 件以上出ること」の sanity 対象に追加）
- 全体スイート: 失敗集合は修正前後で**完全一致**（既存 226 件の失敗はいずれも本件と
  無関係の先行不具合。新規失敗ゼロ）。
