# CLAUDE.md — cross-cutting notes for future sessions

This file surfaces facts a new session/reader must know before proposing work.
It is docs-first: when code and docs disagree, reconcile here.

---

## Backtest fidelity — System3/5/6 の指値は「必ず約定」ではない

**Status:** fixed 2026-08-20。フラグ無しの無条件修正（バックテストのみ。ライブ発注は無変更）。
詳細・再測定手順・影響棚卸し: `docs/BACKTEST_LIMIT_FILL_FIX_20260820.md`。

System3 (`prev_close×0.93`) / System5 (`×0.97`) / System6 (`×1.05`) は前日終値から
離した**指値**で仕掛ける。2026-08-20 以前の `compute_entry` は指値を計算するだけで
**当日バーが到達したかを確認していなかった**ため、実際には約定しなかった候補まで
建玉として計上し、勝率を押し上げていた。

| system | 約定率 (実測) | 勝率 修正前 → 後 | 平均リターン 前 → 後 |
|---|---|---|---|
| System3 | 32.0% | 0.757 → **0.491** | +0.076 → **−0.005** |
| System5 | 52.5% | 0.636 → **0.468** | +0.030 → **−0.015** |
| System6 | 40.3% | 0.734 → **0.559** | +0.046 → **−0.002** |

約定判定は `StrategyBase._limit_entry_filled()`（exit 側の stop/target 到達判定と同一規約:
long は `Low <= limit`、short は `High >= limit`、約定値は指値、NaN は fail-closed）。

**読む前に知っておくこと**: 2026-08-20 より前に出力された **System3/5/6 のバックテスト
実績（およびそれらを含む統合バックテストの成績）はすべて過大**。継続可否の判断は再測定後の数字で行うこと。

---

## Backtest measurability — 2026-08-21 以前の履歴は「7 系統中 3 系統」しか無い

**Status:** fixed 2026-08-21。**バックテスト計測可能性のみ**の修正で、live のシグナル
生成・発注は無変更（フラグ既定値も変更なし）。詳細と before/after 実測:
`docs/BACKTEST_ENGINE_GAPS_20260821.md`。

エンジン側の 3 つの欠陥により、System1 / System3 / System6 / System7 は
バックテストで **1 件も建玉を持てなかった**（警告もログも出ない）。

| gap | 系統 | 症状 |
|---|---|---|
| 1 | System3 | フルスキャン候補が `date` だけの list 形式。エンジンは dict 形式にしか `entry_date` を注入せず、`get_loc` の `KeyError` を握り潰していた |
| 2 | System6 | `SYSTEM6_FORCE_LATEST_ONLY` 既定 True がバックテストでも最新 1 日へ潰す |
| 3 | System1 / System7 | base cache の整数インデックス + 小文字カラムを正規化しておらず、System1 は `setup` 全 False、System7 は SPY ごと prepare から脱落 |

**読む前に知っておくこと**: **2026-08-21 より前に出力されたバックテスト／統合
バックテスト／`common/validation/` の CPCV・DSR・bootstrap は、すべて
System2 / System4 / System5 の 3 系統ぶんでしかない**。System1/3/6/7 の
「シグナルが無い」「成績が無い」という過去の記述は、戦略の性質ではなく
エンジンの欠陥に由来する。

修正後は 60 銘柄 / 534 営業日の実データで **7 系統すべてが建玉を持つ**ことを確認済み
（既存 3 系統の単独バックテスト建玉数は 66 / 55 / 15 で修正前と完全一致 = 非退行）。

エンジンが候補の形を解釈できない場合は `common.candidate_schema.CandidateSchemaError`
で**即座に落ちる**。黙って 0 建玉に戻すことはしない。

バックテスト／検証の入口は `common.backtest_context.backtest_context()` を張る。
today 実行専用の高速パスを緩めたいときは、グローバルなフラグを倒すのではなく
`in_backtest_context()` を見ること（live は決してこのコンテキストに入らない）。
