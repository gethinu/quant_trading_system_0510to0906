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
