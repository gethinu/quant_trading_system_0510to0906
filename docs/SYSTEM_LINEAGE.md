# システムの血統 (lineage) — System1-7 と System8 は別系統

**目的**: 「System8」という番号だけを見て「System1-7 と同じ枠組みの 8 番目」と
誤解されるのを恒久的に防ぐ。番号は連番でも、**出自も設計思想も別物**である。

作成: 2026-07-28（System8 を main に land した際に併設）

---

## 1. 2 つの血統

| lineage | 該当 | 出自 | 骨格 |
|---|---|---|---|
| `bensdorp` | System1 – System7 | Laurens Bensdorp の自動売買システム本に準拠した定型システム群 | 広いユニバース → 指標フィルター → セットアップ → ランキング上位 N。モメンタム / 平均回帰 × ロング / ショートの組み合わせ |
| `original` | System8 のみ | **当リポジトリ独自開発**（Bensdorp 準拠ではない） | イベントカレンダー駆動。指標クロスでも top-N ストックピッキングでもない |

### System1-7 (`bensdorp`)

Bensdorp 本の定型パターンに沿った 7 本。System7 だけは SPY 固定の
カタストロフィー・ヘッジだが、**指標セットアップ + ATR ストップ**という
骨格は共有しており、血統としては同じ `bensdorp` に属する。

### System8 (`original`)

SPY オーバーナイト FOMC プレドリフト。予定 FOMC 声明日 T の前営業日 T-1 の
引け（MOC）でロングし、T の寄り（MOO）で手仕舞う 1 泊のイベント保有。

- セットアップは指標ではなく **`data/events/fomc.csv` のカレンダー**が決める
  （「翌営業日が予定 FOMC 声明日か」だけが条件）。
- ストップなし・等ノーショナル・同時 1 ポジション。
- 出所は別リポジトリの研究成果 `n0150_fomc_macro_event_drift_spy`（凍結ルール v03）。
  証跡は `docs/SYSTEM8_FOMC_DRIFT_MIGRATION_20260716.md` を参照。

**思想が根本的に違う**ため、System1-7 に効くチューニング・共通化・
リファクタの前提を System8 にそのまま当てはめてはいけない。
逆も同じ（System8 のイベント駆動構造を 1-7 に一般化しない）。

---

## 2. どこに血統が記録されているか

血統は 1 か所で決め、他はすべてそこを参照する。

### 正準（single source of truth）

- `common/system_constants.py`
  - `SYSTEM_LINEAGE: dict[str, str]` — system 名 → `"bensdorp"` / `"original"`
  - `LINEAGE_BENSDORP` / `LINEAGE_ORIGINAL` / `LINEAGE_LABELS`
  - `get_system_lineage(system_name)` — 番号から推測せずここを引く
  - `SYSTEM_CONFIGS[<system>]["lineage"]` — 既存の設定辞書からも辿れる
    （`SYSTEM_LINEAGE` との一致は `tests/test_system_lineage.py` が強制）

### 表示・派生

- `common/system_groups.py`
  - `LINEAGE_MARKER = "◆"`、`lineage_marker()` / `format_system_label()` /
    `lineage_legend()`
  - `GROUP_DISPLAY_NAMES["long"]` は System8 を 1/3/5 と同列に並べず
    `Long (System1,3,5 / System8◆)` と区別して表示する
- 各実装ファイルの先頭 `🧬 Lineage:` 行
  - `core/system1..8.py`、`strategies/system1..8_strategy.py`
- ダッシュボード `apps/dashboards/alpaca-next/lib/format.ts`
  - `SYSTEM_LINEAGE` / `LINEAGE_MARKER` / `LINEAGE_LEGEND` / `sysLineage()`
  - `sysShort('system8')` → `S8◆`（全チップに自動で印が付く）
  - Signal Pipeline の System8 行には「独自」バッジ、凡例を各所に併記

**新しい system を足すときは `common/system_constants.py` の `SYSTEM_LINEAGE` と
`format.ts` の同名マップの両方を更新すること。** 片方だけだと表示と実体がずれる。

---

## 3. 血統と「ライブ配線」は別の話

血統の区別は**分類**であって、稼働状態ではない。2026-07-28 時点で System8 は
main に統合済みだが、以下は**意図的に未配線**（人間の判断待ち）:

- 実資金の配分ウェイト（`long_allocations` / `short_allocations`）— 未登録
- ライブ日次自動発注ループ（`scripts/run_all_systems_today.py` 等の `range(1, 8)`）— 未拡張
- runner ツリー（`C:\tmp\qts-main-run`）への arm — 未実施

詳細は `docs/SYSTEM8_FOMC_DRIFT_MIGRATION_20260716.md` §4-5。
