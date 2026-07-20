# Position Management P1 — standing-cap enforcement + fail-closed reconcile

**日付**: 2026-07-21
**対象 branch**: `claude/position-mgmt-p1-20260721` (off `origin/main` = `0eb5765`)
**前提 docs (先読済, source of truth)**:
- [`docs/POSITION_MANAGEMENT_AUDIT_20260703.md`](./POSITION_MANAGEMENT_AUDIT_20260703.md) §2 (per-system `max_positions=10`) / §5.2
- [`docs/POSITION_MANAGEMENT_PHASE5_20260707.md`](./POSITION_MANAGEMENT_PHASE5_20260707.md) §2 (`risk.portfolio.max_total_positions=70` 他 no-op デフォルトの導出)
- `logs/audit_20260719/AUDIT_REPORT.md` 🔴P1 (ポジ74 の構造的根因)

> **方針**: docs-first / paper 限定 / **ライブ発注しない** / **既存ポジションを勝手に閉じない**。
> 本 doc は「既に確定している per-system `max_positions=10` と portfolio
> `max_total_positions=70` を、これまで **enforcement 境界に無かった発注直前**でも
> 効かせる」ための実装記録。**新しいリスク値は導入しない** — 既存 spec の実効化のみ。

---

## 1. 根因 (2026-07-19 監査で確定) と本 fix の対応

ポジションが 74 まで積み上がった構造的根因は 3 つの複合:

| # | 根因 | 本 fix |
|---|---|---|
| A | **reconcile が fail-open**。`_resolve_positions_for_allocation` は live position fetch 失敗時に silent に `None`/`[]` へ縮退し、`available_slots` が per-run cap only に落ちる (held 未反映)。しかも `_fetch_positions_and_symbol_map` は fetch 例外を内部で握り潰して `[]`(=flat 口座に見える) を返す **二重 silent-fail**。 | **Fix 1**: fail-closed 化 (§3.1) |
| B | **submit 境界に count cap が無い**。`signals_json_to_orders` は per-symbol `already_held` のみで、**別銘柄で同一 system が上限を超えて積み上がる**のを止めない。07-13 の 10 銘柄 + 07-14 の別 10 銘柄が両方通り system 別 20 に。`finalize_allocation` の per-system slot cap は **open_auto_run 経路 (JSON→paper_trading_submit) を通らない**ので効かない。 | **Fix 2**: submit 境界に per-system standing cap + portfolio total cap (§3.2) |
| C | **delisted/orphan が held に未算入**。FOLD/CDTX 等 API で close 不能な legacy orphan は `symbol_system_map` 非帰属で `count_active_positions_by_system` から skip され、`held_total` 過少 → total cap が「まだ余裕がある」と誤認。 | **Fix 3**: delisted/orphan を total/side held に算入 (§3.3) |

exit 失火 (code 40310000, cancel-before-close) は別 issue (PR #144 系) で本 fix の対象外。
本 fix は **これ以上積み増さない**歯止め (entry 側) であり、既存 74 を**閉じるものではない** (§5)。

---

## 2. cap 値の出所 (docs 根拠 / 新値でないことの明示)

| cap | 値 | docs 根拠 | per-run TOP_N=10 との違い |
|---|---:|---|---|
| **per-system standing cap** | **10** | `RiskConfig.max_positions=10` (config.yaml `risk.max_positions`)。docs/systems: S1/S2 は「最大10」明記、S3-S6 は global 既定 10。AUDIT_20260703 §2 row1。 | **TOP_N=10 は「1 run で system あたり何件の候補を生成するか」**。standing cap=10 は **「口座に同時に何件保有し続けるか」**。前者は毎 run リセット、後者は日を跨いで累積する残高。07-13 に 10 生成→fill、07-14 に別 10 生成→fill で **保有 20** になったのは、TOP_N は守れていても standing cap が enforcement されていなかったから。 |
| **portfolio total standing cap** | **70** | `risk.portfolio.max_total_positions=70` (PHASE5 §2.1、= long 4 + short 3 system × 10 の implicit 上限の明示化)。 | 同上。全 system 合算の同時保有上限。 |

いずれも **既存の確定値**。本 fix はデフォルトで新たに締め付けず、**docs 通りの上限を発注直前でも守らせる**だけ。運用でより厳しくしたい場合は config / env で下げられる (§4)。

---

## 3. 実装

### 3.1 Fix 1 — reconcile fail-closed (`scripts/run_all_systems_today.py`)

- 新例外 `PositionReconcileError(RuntimeError)`。
- `_fetch_positions_and_symbol_map`: **position fetch 例外を握り潰して `[]` を返すのをやめ**、`PositionReconcileError` を raise (「flat 口座」と「取得失敗」を区別)。`symbol_system_map` の読み込み失敗は従来通り寛容 (`{}`)。
- `_resolve_positions_for_allocation`:
  - env `ALLOCATION_RECONCILE_POSITIONS` off → `None` (従来の fail-open。意図的無効化)。
  - creds 無し → `None` (test/CI/backtest 保護。口座に触れない)。
  - **fetch 失敗** → env `ALLOCATION_RECONCILE_FAILCLOSED` (既定 `1`=有効) が truthy なら **`PositionReconcileError` を raise (fail-closed)**。opt-out (`0`) で従来の WARN+`None` (fail-open) に戻せる。
- 呼び出し側 (allocation 段): `PositionReconcileError` を捕捉し、**その run は新規エントリを一切生成しない** (`final_df` 空 + `AllocationSummary(mode="reconcile_failed")`)。**既存ポジションには一切触れない** (read の失敗なので当然)。distinct log で silent 縮退でないことを可視化。

> 思想は F2 audit fix (exit の broker 到達不能 → silent exit0 でなく distinct exit3+WARN) と同一。
> 「現保有を確認できないなら新規は開かない」= 保守的 fail-closed。

### 3.2 Fix 2 — submit 境界の standing cap (`common/alpaca_trading.py`)

`signals_json_to_orders` の **実発注ループ (非 dry_run)** に、`already_held` の直後 (step 0.5) として追加:

- ループ前に一度だけ現保有を集計: `count_held_positions_by_system(client, open_positions, symbol_system_map)`
  - 帰属優先順位 = **entry order の client_order_id (`system{N}-…` prefix) → `symbol_system_map` フォールバック** (exit 経路 `_hydrate_from_alpaca_coids` と同じ信頼できる帰属源。static な `symbol_system_map.json` 単独より正確)。
  - 帰属できた held → `per_system[sys]++`。帰属できない held (delisted/orphan) → `unmapped++`。
  - `total` は nonzero 保有すべて (unmapped 含む)。`long_total`/`short_total` は qty 符号で。
- 各新規注文について pure 判定 `evaluate_standing_cap(...)`:
  - `total_held + batch_total >= total_cap` → skip `standing_cap:portfolio_total_…`。
  - `held_by_system[sys] + batch_by_system[sys] >= per_system_cap` → skip `standing_cap:{sys}_…`。
  - **batch カウンタは実 submit 成功時のみ increment** (skip/失敗は cap 予算を消費しない)。
- skip は **silent drop せず** `skip_reason` 付き `PreparedOrder` として結果に残し、`_audit_log({"event":"skip_standing_cap"})` + `logger.info`。caller サマリ (paper_trading_submit) の skip 内訳に必ず出る (観測性、既存の skip 作法と一致)。
- `already_held` (同一銘柄) は温存。standing cap は **別銘柄での system 積み増し**を止める直交ガード。
- この境界は **open_auto_run (JSON→paper_trading_submit→signals_json_to_orders) と daily_pipeline の両方が通る**唯一の発注直前点。ゆえに finalize を経ない経路の**最終防波堤**になる。

旧コメント「件数 cap / max_positions は上流 (final_allocation) で既に効いているので、ここでは per-name/gross/net の dollar cap のみ」= **P1 で否定された前提**。→ コメント是正 + 実効化。

### 3.3 Fix 3 — delisted/orphan を held に算入 (`core/final_allocation.py`)

- 新 helper `count_positions_with_unmapped(positions, symbol_system_map)` → `(per_system, unmapped)`。`unmapped = {"long":n, "short":m, "total":k}` (system 帰属できない nonzero 保有を side 別に集計)。
- `_apply_portfolio_caps`: `held_total`/`held_long`/`held_short` に unmapped を**加算**。→ delisted が居ても total/side cap が過少にならない (allocation 経路 = daily_pipeline の finalize)。
- submit 境界 (§3.2) の `total` も unmapped を含む (実 Alpaca 保有をそのまま数えるため自然に算入)。

**設計判断 (docs 根拠)**: delisted/orphan は **total/side cap には算入**し、**per-system cap には算入しない**。
- *算入する側*: 実在する保有として real capital / exposure / 建玉スロットを消費している。AUDIT_REPORT が指摘した「held_total 過少」の直接是正。「70 の dead 建玉があるのに更に 70 積む」を許さない = 正しいリスク管理。
- *per-system に入れない側*: system 帰属が無い (それが delisted たる所以)。無理に特定 system へ寄せると別 system の枠を誤って潰す。
- *副作用の可視化*: delisted が total cap を圧迫して新規が絞られ得るが、その skip は理由付きで surface される (silent でない)。delisted の恒久解消は別 human task (清算待ち)。

---

## 4. config / env (すべて既定は docs 通り = 挙動を勝手に締めない)

| key | 既定 | 効果 |
|---|---|---|
| `SUBMIT_ENFORCE_STANDING_CAP` (env) | `1` (有効) | submit 境界 standing cap の on/off。`0` で従来挙動。 |
| `SUBMIT_MAX_POSITIONS_PER_SYSTEM` (env) | `risk.max_positions`=10 | per-system cap の上書き。 |
| `SUBMIT_MAX_TOTAL_POSITIONS` (env) | `risk.portfolio.max_total_positions`=70 | total cap の上書き。 |
| `ALLOCATION_RECONCILE_FAILCLOSED` (env) | `1` (fail-closed) | reconcile fetch 失敗時に raise するか。`0` で従来の fail-open。 |

新しい config キーは追加しない (既存 `risk.max_positions` / `risk.portfolio.max_total_positions` を読むだけ)。

---

## 5. 変更前後で「何がどう変わるか」

### 現在の 74 建玉はどうなるか
- **何も閉じない**。本 fix は entry 側の歯止めのみ。74 はそのまま (既存ポジションに触れないのは厳守事項)。
- 74 の縮小は **exit の正常発火** (cancel-before-close, PR #144 系) と **時間経過での time-exit** に委ねる。本 fix は「exit が減らした分を翌日また積み増して 74 に戻す」ループを止める。

### 次回以降の run
- **daily_pipeline (finalize 経路)**: reconcile が fetch 成功すれば held が available_slots に反映 (従来通り)。fetch 失敗時は **fail-closed で新規0** (従来は per-run cap で新規を出していた)。
- **open_auto_run (submit 境界経路)**: 各 system の保有が 10 に達していれば、その system の新規は **standing_cap で skip** (従来は別銘柄なら通って 20 まで積んだ)。total 70 (delisted 含む) 到達時も新規 skip。
- **通常運用への影響**: exit が正常に効いて各 system の保有 < 10 の日は、これまで通り不足分だけ新規で埋まる (cap は「上限まで」。減った枠は普通に補充される)。**シグナルは潤沢が正常、絞るのは entry のみ**という運用方針と一致。

### 月曜 22:35 runner (`QuantTrading_OpenAutoRun`, `C:\tmp\qts-main-run` = `claude/open-auto-run`) への影響
- runner は working-tree を読む。**本 fix は origin/main 宛の PR まで**で、runner tree には**まだ入らない**。
- runner に効かせるには `common/alpaca_trading.py` の変更を runner tree に反映する必要がある (= **arm は別判断**)。本 fix は **arm しない** — main マージ可否と合わせて指示を仰ぐ。

---

## 6. 変更 files

- `scripts/run_all_systems_today.py` — `PositionReconcileError` + fail-closed reconcile + 呼び出し側ガード。
- `common/alpaca_trading.py` — `HeldPositionCounts` / `count_held_positions_by_system` / `evaluate_standing_cap` / `_resolve_standing_caps` + `signals_json_to_orders` への配線 + 旧コメント是正。
- `core/final_allocation.py` — `count_positions_with_unmapped` + `_apply_portfolio_caps` の delisted 算入。
- `tests/test_position_standing_cap_20260721.py` — **新規** (fail-closed / per-system cap / total cap / delisted 算入 / pure decision)。
- `tests/test_allocation_position_reconcile_20260707.py` — fetch 失敗 test を fail-open→**fail-closed** に更新 (意図した挙動変更) + opt-out test 追加。
- `docs/POSITION_MANAGEMENT_P1_STANDING_CAP_20260721.md` — 本 doc。

## 7. rollback

```powershell
git revert <commit>   # または env で無効化 (挙動を即座に従来へ)
$env:SUBMIT_ENFORCE_STANDING_CAP = "0"
$env:ALLOCATION_RECONCILE_FAILCLOSED = "0"
```
