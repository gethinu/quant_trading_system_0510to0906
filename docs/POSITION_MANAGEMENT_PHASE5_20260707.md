# Position Management — Phase 5 portfolio-level 管理 (解禁 + spec)

**日付**: 2026-07-07
**対象 branch**: `claude/monitor-webapp`
**前提 doc**: [`docs/POSITION_MANAGEMENT_AUDIT_20260703.md`](./POSITION_MANAGEMENT_AUDIT_20260703.md) の
§5.2「future consideration」を **user が 2026-07-07 に解禁** したことを受けた spec + 実装記録。

> **方針**: docs-first。本 doc が portfolio-level 管理ルールの single source of truth。
> 2026-07-03 audit では「docs に無い機能は独自実装禁止 (Phase 5 future / user 判断待ち)」
> としていた 4 項目 (総 position cap / long-short 集約 cap / drawdown flatten / sector cap)
> を、user 判断により本 doc で spec 化し実装する。
>
> **重要 (リスク値の扱い)**: 採用したデフォルトは *新しいリスク値を勝手に確定したものではなく*、
> 既存の確定ルール (per-system `max_positions=10`、long/short `default_long_ratio=0.5`、
> bucket 配分 long 25%×4 / short 40+40+20) から **算術的に導出した「現状の implicit
> 上限を明示化しただけの no-op デフォルト」** である。実運用での締め付け値 (より小さい
> 上限) は user がリスク選好に応じて config で設定する。

---

## 1. 解禁される 4 項目 (audit §5.2 → 本 doc で spec 化)

| # | 項目 | audit での扱い | 本 doc |
|---|---|---|---|
| a | cross-system 同一銘柄 dedup 明文化 | option A/B/C | **A を追認** (system 番号順 first-come、既存 `chosen_symbols` 挙動)。挙動変更なし。 |
| b | portfolio 総 position cap | 未実装 | **実装** (config `max_total_positions`) |
| c | long/short 集約 cap (件数・$) | 未実装 | **実装** (件数 + gross/net $ exposure) |
| d | drawdown flatten | 未実装 | **設計 + off-by-default** (config で有効化) |
| e | sector cap | 未実装 | **設計 + off-by-default** (config で有効化) |

---

## 2. 新 config (`config/config.yaml` の `risk.portfolio`)

```yaml
risk:
  risk_pct: 0.02
  max_positions: 10
  max_pct: 0.10
  portfolio:                       # ← 2026-07-07 Phase 5 追加
    # --- active (保守的 no-op デフォルト = 現状 implicit 上限の明示化) ---
    max_total_positions: 70        # 全建玉数の上限
    max_long_positions: 40         # long 側 建玉数の上限
    max_short_positions: 30        # short 側 建玉数の上限
    max_gross_exposure_pct: 1.0    # gross (long$+short$) / equity 上限
    max_net_exposure_pct: 1.0      # |net| (|long$-short$|) / equity 上限
    # --- off-by-default (0 / 無効。値を入れると発火) ---
    drawdown_flatten_pct: 0.0      # peak からの drawdown がこの割合で全 flatten (例 0.30)
    max_positions_per_sector: 0    # 1 sector あたり建玉数上限 (例 5)
```

### 2.1 各デフォルトと根拠 (導出)

| key | default | 根拠 (既存ルールからの導出) | 締め付け推奨 |
|---|---:|---|---|
| `max_total_positions` | **70** | long 4 system + short 3 system、各 `max_positions=10` → 4×10 + 3×10 = 70。audit §2 row12「implicit ~70」の明示化。**現状 no-op** (allocation は元々ここを超えない)。 | 分散しすぎ (100k / 70 ≈ $1,428/建玉) を嫌うなら 20〜40 へ |
| `max_long_positions` | **40** | long system (sys1/3/4/5) 4 個 × 10。現状 implicit long 上限。**no-op**。 | 30 以下で long 集中を制限 |
| `max_short_positions` | **30** | short system (sys2/6/7) 3 個 × 10。現状 implicit short 上限。**no-op**。 | 20 以下 |
| `max_gross_exposure_pct` | **1.0** | cash account (paper, margin なし) では gross ≤ equity が物理上限。**no-op** の guardrail (将来 margin/leverage drift の歯止め)。 | margin 前提でも 1.0〜1.5 に留める |
| `max_net_exposure_pct` | **1.0** | `default_long_ratio=0.5` は net≈0 を志向。1.0 は無制約 = **no-op**。 | **0.5 推奨** (方向性リスクを capital 分割の意図どおり抑える) |
| `drawdown_flatten_pct` | **0.0 (無効)** | audit §5.2(c) option A は「-30% peak drawdown で翌営業日 open 全 close」。数値未確定なので **off**。 | 有効化するなら 0.30 |
| `max_positions_per_sector` | **0 (無効)** | audit §5.2(d)「sector 20% cap」。sector metadata 取得が要るため **off**。 | 有効化するなら 5 (= 70 の ~7%) 等 |

> 上表の「default」は **現状挙動を変えない値**。締め付けたい場合のみ user が config を変更する。
> `max_net_exposure_pct` だけは 50/50 の意図を活かすため **0.5 への変更を推奨** するが、
> デフォルトは no-op (1.0) のままとし、勝手にリスクを締めない。

---

## 3. 実装 (適用点)

### 3.1 count / exposure cap (active)

`core/final_allocation.py::finalize_allocation` の最終段 (sort 後) に
`_apply_portfolio_caps()` を追加。優先度順 (side → system 番号 → score) にソート済の
`final_df` に対し、**既保有 (active_positions) + 新規** が上限を超えないよう新規側を末尾から trim する。

- count cap: `keep_long = max(0, max_long_positions - held_long)` 等。total も同様。
- exposure cap: `position_value` 列 (capital sizing 後に付与) の累積で side 別 gross と
  net を評価し、`equity × pct` を超える行を trim。`position_value` が無い (pure slot) 場合は
  exposure cap は skip (count cap のみ)。
- trim 結果は `AllocationSummary.system_diagnostics["portfolio_caps"]` に記録 (観測性)。

held の side 別内訳は `count_active_positions_by_system(active_positions, symbol_system_map)`
を long_alloc / short_alloc の system 集合で振り分けて算出する。

### 3.2 drawdown flatten / sector cap (off-by-default)

`common/portfolio_guard.py` に純関数として実装し、config 値が偽 (0/false) の間は
**必ず no-op** を返す。発火は user が config を設定したときのみ:

- `evaluate_drawdown_flatten(equity, peak_equity, pct)`: `pct>0` かつ
  `drawdown >= pct` で `FlattenDecision(flatten=True, ...)`。exit orchestration
  (`scripts/paper_exit_check.py`) が将来 opt-in で参照する hook。本 dispatch では
  判定関数 + config + test のみ (自動 flatten の実 wiring は別 dispatch / user 有効化後)。
- `filter_by_sector_cap(rows, sector_of, cap)`: `cap>0` のとき 1 sector あたり
  `cap` 件を超える候補を落とす。sector metadata provider (`sector_of`) は呼び出し側が渡す
  (未提供なら no-op)。

**paper のみ / ライブ発注なし**: 本 Phase の変更で live 発注経路は追加しない。
count/exposure cap は allocation 段 (発注前) の候補 trim であり、submit 経路は不変。

---

## 4. 変更 files

- `config/config.yaml` — `risk.portfolio` セクション追加。
- `config/schemas.py` — `PortfolioRiskModel` 追加 (validation)。
- `config/settings.py` — `PortfolioRiskConfig` dataclass + `RiskConfig.portfolio`。
- `core/final_allocation.py` — `_apply_portfolio_caps()` + finalize_allocation への wiring。
- `common/portfolio_guard.py` — drawdown / sector の純関数 (off-by-default)。
- `tests/test_portfolio_caps_20260707.py` — count/exposure cap の回帰。
- `tests/test_portfolio_guard_20260707.py` — drawdown/sector の off-by-default + 発火。

## 5. rollback

```powershell
git revert <commit-hash>   # config + final_allocation + portfolio_guard を戻す
```

count/exposure cap のデフォルトは no-op なので、revert しなくても config を
既定値に戻せば挙動は従来と一致する。
