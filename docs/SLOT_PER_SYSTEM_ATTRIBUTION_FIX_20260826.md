# system 別スロットの帰属バグ修正 — 2026-08-26

**Status:** 修正済み・**未デプロイ**（ブランチ `claude/slot-system-attribution-20260826`、
base = `origin/main` `9835eb5`）。paper のみ。発注・publish・deploy は一切していない。

診断の出発点: `docs/SLOT_MODEL_AND_DASHBOARD_REDESIGN_20260826.md` §3.1
（「system 枠（`max_positions=10`）が実際には効いていない」）。
**同 doc はまだ main に無く、ブランチ `claude/monitor-webapp` の `96eae83` にある。**

---

## 1. 根本原因 — coid 由来の帰属は作られていたが、呼び出し側が捨てていた

保有を system に帰属する仕組みは **2 つ** ある。

1. `data/symbol_system_map.json`（static・84 銘柄・最終更新 **2026-07-01**）
2. entry の client_order_id `system{N}-SYM-YYYYMMDD`（＝実際にその建玉を開いた system）

`09956a5`（2026-08-04）が 2 を導入し、`_fetch_positions_and_symbol_map()` の中で
「static に coid を **coid 優先でマージ**」して返すようにした。ここまでは正しく動いている。

壊れていたのは**呼び出し側** `scripts/run_all_systems_today.py` の
`_resolve_positions_for_allocation()`:

```python
symbol_system_map = load_symbol_system_map()      # ← static だけ (84 銘柄)
...
positions, fetched_map = _fetch_positions_and_symbol_map()   # ← static ∪ coid
...
if not symbol_system_map and fetched_map:         # ← static が「空のときだけ」取り込む
    symbol_system_map = fetched_map
return positions, symbol_system_map               # ← 実際には static だけが返る
```

`data/symbol_system_map.json` は 1 件でも入っていれば truthy なので、この分岐は
**一度も成立しない**。結果、coid 由来の帰属は毎回まるごと捨てられ、配分には
2026-07-01 で凍った static map だけが渡っていた。現保有 30 銘柄と static 84 銘柄の
**重なりはゼロ**なので、`count_active_positions_by_system()` は 1 件も帰属できず、

```
available_slots[s] = max_positions[s] - held[s] = 10 - 0 = 10   （全 system）
```

となって **system 枠が素通り**していた。効いていたのは long 40 / short 30 / 合計 70 の
プール上限だけ。

### 本番ログに残っていた証拠（2026-08-26 22:35 の実 run）

```
logs/today_signals_20260826_2235.log:111
  🔗 保有帰属を entry coid で補強: 277 銘柄 (static symbol_system_map の stale/欠落を補正)
logs/today_signals_20260826_2235.log:112
  📊 現保有ポジション 29 件を配分の空き枠算出に反映
```

**277 銘柄の coid map を構築したうえで破棄していた。** 同じ run の artifact:

```
results_csv/today_signals_20260826.json .portfolio.caps
  held          = {long: 27, short: 2, total: 29}
  held_unmapped = {long: 27, short: 2, total: 29}      ← 29/29 が未帰属
```

`held_unmapped == held` が、この 3 週間ずっと出ていた指標。

### なぜテストで捕まらなかったか

既存の `tests/test_alloc_coid_attribution.py` は **テスト内で `merged` を手で組み立てて**
`count_active_positions_by_system` に渡していた。本番の配線
（`_resolve_positions_for_allocation` が merged を採用するか）を一度も通していない。
実際、修正前のコードに対してこのテストは **全部 green のまま通る**（§4 の mutation で確認）。

---

## 2. 修正 — 「static が空のときだけ」→「常にマージ（coid 優先）」

`scripts/run_all_systems_today.py::_resolve_positions_for_allocation`

```python
if fetched_map:
    merged_map: dict[str, Any] = dict(symbol_system_map or {})
    merged_map.update(fetched_map)      # fetched (= static ∪ coid) が勝つ
    symbol_system_map = merged_map
```

**なぜこの機構を選んだか**

- 帰属の真値は「その建玉を実際に開いた注文」であり、それは entry coid に入っている。
  exit / submit 境界（`common/alpaca_trading.py` の standing cap・`already_held` 判定）も
  同じ coid を信頼源にしているので、**配分段だけが別の真値を見ている**状態を解消できる。
- 口座の回転に**自動追随**する。static JSON を書き直す運用が要らない
  （そもそも 2026-07-01 から更新されておらず、その運用は現に失敗している）。
- 既に本番で毎 run 構築されている（277 銘柄）。新しい I/O も新しい artifact も増えない。
- static map は捨てずにマージする（置換ではない）ので、coid 取得に失敗した場合の
  従来の縮退挙動は保たれる。

**採らなかった案**: `data/symbol_system_map.json` を live 保有から書き戻す。
配分 run に書き込み副作用が増え、06:00 の dry-run が本番 map を書き換える事故を招く。
帰属の真値は注文履歴側にあるので、JSON を中間キャッシュとして持つ必要がない。

### 副次: 全滑りを黙って通さない

帰属カバレッジを 1 行ログにした。`attributed == 0` なら WARNING を出す。
今回のバグ（coid の取得失敗・coid 規則変更）が再発したときの早期警報になる。

```
🧮 保有 system 帰属: 27/29 件 (未帰属 2 件 = delisted/orphan) → {'system1': 7, ...}
⚠️ 保有 29 件を 1 件も system に帰属できません ... system 別の枠が実質無効になります。
```

**サイジング・リスク計算・40/30/70 プール上限には一切触れていない。**
直したのは「上限に食わせる入力（held の system 帰属）」だけ。

---

## 3. リプレイ — 発注判断は変わるか

2026-08-26 22:35 の実 run を配分段だけ再生した（実 artifact の候補数・実 broker read の
保有 29 件・実 equity。発注は行っていない）。

| | 修正前（static のみ） | 修正後（coid マージ） |
|---|---|---|
| `held_by_system` | `{}` | `{s1: 7, s2: 2, s4: 10, s5: 8}` |
| `held_unmapped` | 29 | **2**（CDTX / FOLD = delisted） |
| `available_slots` | 全 system **10** | s1 3 / s2 8 / s3 10 / **s4 0** / s5 2 / s6 10 / s7 10 |
| `caps.held` | long 27 / short 2 / total 29 | **同一** |
| `caps.allow` | long 13 / short 28 / total 41 | **同一** |
| 採用 (kept) | s1 10 / s2 10 / s3 3 = 23 | s1 3 / s2 8 / s3 10 = 21 |

**プール上限（40/30/70）の算術は before/after で bit 一致**。変わったのは
system 別の内訳だけ。これはテスト `test_pool_caps_unchanged_by_attribution_fix` で固定した。

### 実際に破られていた per-system 上限（＝バグが表に出た箇所）

真の保有（coid 帰属）で測ると、修正前の配分は **2 系統で自分の枠を超えていた**:

| system | 保有 | 修正前の新規 | 合計 | `max_positions` |
|---|---|---|---|---|
| **system1** | 7 | 9（実 run）/ 10（再生） | **16 / 17** | 10 ❌ |
| **system2** | 2 | 10 | **12** | 10 ❌ |

どちらも実データで裏が取れる:

- **system2** — 発注境界の standing cap が実際に 2 件落としていた。
  `results_csv/paper_orders_20260826.json`:
  `skip_reason = "standing_cap:system2_held=2+batch=8>=cap=10"`（IAG, NG）。
  つまり**配分段で枠が効いていなかったので、発注段が尻拭いしていた**。
- **system1** — 9 本の提案のうち 6 本が既保有銘柄の再提案で、
  `already_held:` で skip されていた（BNY / IPST / SLS / ERAS / FBRX / AMCR）。
  枠の中に収まったのは偶然であって設計どおりではない。

### 発注そのものへの影響（同 run を再生した場合）

| system | 修正前 提案 → 実発注 | 修正後 提案 → 実発注（見込み） |
|---|---|---|
| system1 | 9 → **3**（WETO, ASST, AMIX） | 3（上位 WETO, BNY, IPST）→ **1**（BNY/IPST は既保有で skip） |
| system2 | 10 → **8**（IAG, NG は standing cap で却下） | 8 → **8**（**同一銘柄・変化なし**） |
| system3 | 4 → **4** | 10 → **最大 10**（+6、s3 は保有 0） |
| 合計 | 23 → **15** | 21 → **15〜19** |

- **system2 の実発注は 1 銘柄も変わらない**。配分段で 8 に絞るだけで、
  発注段の standing cap が落としていた 2 件が最初から出なくなる。
- **system3 が +6**。枠が 10 空いているのに 4 本しか出せていなかったのは、
  枠を使い切っていた system1 が優先度 1 位で 9〜10 本を取り、
  プール上限（allow.long=13）を先に食い潰していたため。**候補の質の問題ではない。**
- **system1 は −2**（ASST, AMIX が出なくなる）。これは枠 3 に対して上位 3 本のうち
  2 本が既保有だったため。§5 の follow-up と直結する。

**リプレイの忠実度について**: 候補の identity は artifact に残っていない（trim された行は
symbol が記録されない）ため、再生は **system 別の本数レベル**で忠実。実 run は
long 候補 39 のうち 33 行しか cap 適用段に入っておらず（kept 13 + trimmed 20）、
その欠落 6 行（dedup かサイジング予算切れ）の理由が artifact に無い（redesign doc（`96eae83`）§2 の観測性の穴）ので、修正前の内訳は実 run の
s1 9 / s3 4 に対し再生では s1 10 / s3 3 と ±1 ずれる。**超過の有無と方向は
実 artifact（standing cap の skip_reason）で独立に裏取り済み。**

---

## 4. テスト

`tests/test_alloc_per_system_slot_attribution_20260826.py`（5 本）

| テスト | 契約 |
|---|---|
| `test_resolve_positions_for_allocation_keeps_coid_attribution` | **本番配線を通す。** stale な static map が非空でも、`_resolve_positions_for_allocation()` の戻り値で保有 27/29 件が system に帰属する |
| `test_resolve_positions_reports_orphans_not_total_blackout` | 未帰属は delisted/orphan の 2 件だけ（29 件全滅ではない） |
| `test_per_system_cap_holds_with_coid_attribution` | 実データ形状の book で全 system が `held + 新規 <= max_positions`、かつ `available_slots` が保有を反映（s4 = 0） |
| `test_stale_static_map_alone_violates_per_system_cap` | **空振り検知**: static だけだと同じ検査が落ちる（＝上のテストが無意味でないことの証明） |
| `test_pool_caps_unchanged_by_attribution_fix` | `caps.held` / `caps.allow` / `caps.caps` が before/after で同一。変わるのは `held_unmapped` だけ |

保有・候補数は 2026-08-26 22:35 の実 artifact（`exit_orders_20260826_execution.json` の
29 件、`today_signals_20260826.json` の `n_candidates_input`）をそのまま定数化している。

### mutation 検証

`if fetched_map: merged...` を旧 `if not symbol_system_map and fetched_map:` に戻すと:

- 新テスト **2 本が落ちる**（`assert 29 == 2` 等）
- 既存 `tests/test_alloc_coid_attribution.py` は **3 本とも通ったまま**
  → 既存テストが本番配線を一度も通していなかったことの証明

### 回帰

配分まわり 11 ファイル: **143 passed / 10 failed**。同じ 11 ファイルを
clean な `origin/main`（`9835eb5`）で走らせても **143 passed / 10 failed で失敗 ID 集合が一致**
（`test_final_allocation_comprehensive` 5 本 + `test_structured_ndjson_logging` 5 本の既存債務）。
本修正による regression はゼロ。

lint: `isort` / `black` ともに clean。

---

## 5. 残る問題（本修正のスコープ外）

1. **既保有銘柄の再提案が枠を食う。** 配分は候補から既保有 symbol を除外しないので、
   正しくなった枠（system1 なら 3）を「発注段で `already_held` に落ちるだけの行」が
   占有する。今回の再生では system1 の実発注が 3 → 1 に減る主因がこれ。
   redesign doc（`96eae83`）§3.2。**枠を正しくしたことで、この問題の影響が相対的に大きくなる。**
2. **他の consumer は依然 static map のみ。** `common/profit_protection.py:201` と
   `apps/app_today_signals.py:2471` は `load_symbol_system_map()` だけを見ており、
   今回と同じ stale の影響を受ける（保護注文の system 帰属・Streamlit 表示）。
   一方 `scripts/export_alpaca_snapshot.py` は既に coid を優先解析している
   （`:184` `parse_system_from_client_order_id`、static は fallback）ため、
   ダッシュボードのスナップショットだけが正しい帰属を出せていた。
   **帰属ロジックが 3 箇所に別実装で散っている**（`core/final_allocation.py` と
   `common/alpaca_trading.py` に**同名の** `count_active_positions_by_system` が
   あり、前者は map のみ・後者は coid 優先）のが事故の温床。
3. **`trimmed_by_system` が無い。** どの system が枠で何本落ちたかが artifact に残らない。
   redesign doc（`96eae83`）§4.2-1。

---

## 6. デプロイ状況

**未デプロイ。** このブランチは `origin` に push しただけで、`main` にも
ランナー（`C:\tmp\qts-main-run`）にも入っていない。Vercel publish / deploy hook は
実行していない。次の定例 run（22:35 JST）は**従来どおりの挙動**のまま。

反映するには main への PR マージ後、ランナーを ff-only で前進させる必要がある。
