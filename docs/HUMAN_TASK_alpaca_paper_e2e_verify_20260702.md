# HUMAN TASK: Alpaca Paper E2E verify (entry + exit) — 1 週間実 tick

**issue**: subscriber サービスイン基準 = 「S1〜S7 の entry **と exit** が Alpaca で自動で回る」。
Phase 2-3 で Case C hybrid (entry step + exit_check step) を配線。この runbook で 1 週間の paper 実 tick で contract を verify する。

**作成日**: 2026-07-03
**verify 期間**: 2026-07-06 (月) 〜 2026-07-10 (金)
**verify 対象 commit**: `claude/monitor-webapp` branch (Phase 2-3 push 後の HEAD)

---

## 絶対に守ること (safety 制約)

1. **live 口座に絶対に触らない**。`.env` の `ALPACA_PAPER=true` を毎回確認。`ALPACA_API_BASE_URL` に `paper-api.alpaca.markets` 以外の URL を絶対に入れない。
2. `-AutoSubmitPaper` を付けない限り **entry も exit も dry-run** (JSON 出力のみ、Alpaca に注文は投げない)。
3. `-AutoSubmitPaper` を付ける前に必ず前日の `paper_orders_YYYYMMDD.json` と `exit_orders_YYYYMMDD.json` を目視で eyeball する。
4. 想定外 (49 signals を大きく超える、SPY 以外の system7 signal など) を検知したら **即座に Task Scheduler を停止**し (`Disable-ScheduledTask -TaskName "quant_daily_pipeline"`)、状況を issue log に書き残す。
5. verify 期間中は **code 変更禁止**。bug 発見 → 記録 → 週明けに対応。

---

## 事前準備 (2026-07-06 05:00 JST 前)

### 1. Alpaca Paper 口座リセット

新 verify を始める前に Paper 口座を初期状態にする (前 verify の残 position が混じらないため)。

```powershell
# 現 position/order を確認
python scripts/alpaca_snapshot.py --paper

# Paper リセット (任意、equity=100000 推奨)
python scripts/reset_paper_account.py --equity 100000 --confirm
```

### 2. `.env` sanity check

```powershell
Select-String -Path .env -Pattern "^ALPACA_|^APCA_"
# 期待:
#   ALPACA_PAPER=true
#   ALPACA_PAPER_STRICT=1
#   ALPACA_API_BASE_URL=  (未設定 or paper-api.alpaca.markets)
#   APCA_API_KEY_ID=PK...
#   APCA_API_SECRET_KEY=***
```

### 3. Task Scheduler の Action 確認

daily_pipeline.ps1 の Action にちゃんと `-AutoSubmitPaper -Tier small` が入ってるか。無いと dry-run のみで実発注されず、verify 不能。

```powershell
Get-ScheduledTask -TaskName "quant_daily_pipeline" |
  Select-Object -ExpandProperty Actions |
  Format-List Execute, Arguments
# Arguments に -AutoSubmitPaper -Tier small が含まれる必要あり
```

### 4. position_tracker.json 初期化

```powershell
Remove-Item -Force data\position_tracker.json -ErrorAction SilentlyContinue
Remove-Item -Force data\position_entry_dates.json -ErrorAction SilentlyContinue
```

これで entry_orders_index (paper_orders_*.json) が唯一の system tag source になる。

---

## 週次 verify checklist

各営業日 (月〜金) の朝、pipeline が 06:00 JST tick で回った後に以下を確認する。

### Day N (2026-07-06 〜 2026-07-10) checklist

必ず以下の順で目視 + 記録。

#### A. 前夜の pipeline log を read

```powershell
$today = Get-Date -Format "yyyyMMdd"
Get-Content logs\daily_pipeline_${today}_*.log | Select-String -Pattern "\[cache\]|\[signals\]|\[coverage\]|\[publish\]|\[paper_orders\]|\[exit_check\]|\[vercel\]" | Select-Object -Last 40
```

**expected**: 全 step の 開始 / 終了 / exit code が出ている。`[exit_check] AutoSubmitPaper=ON` が出ていれば実発注 pass。

#### B. entry の verify

```powershell
$today = Get-Date -Format "yyyyMMdd"
$paperOrders = "results_csv\paper_orders_$today.json"
if (Test-Path $paperOrders) {
    $j = Get-Content $paperOrders | ConvertFrom-Json
    Write-Host "entry count: $($j.count) submitted: $($j.submitted) failed: $($j.failed)"
    $j.orders | Group-Object system | Format-Table Name, Count
}
```

**チェック**:
- [ ] `count` が 0 < N ≤ 60 (49 前後を想定、上振れは alert)
- [ ] `failed` = 0 (投資可能性チェック等 filter で自然に減るのは OK、submit_error は NG)
- [ ] system 別 count が spec 内 (S1 max 10, S2 max 10, S3 max 10, S4 max 10, S5 max 10, S6 max 10, S7 = 0 or 1)
- [ ] 各 order の `client_order_id` が `system{N}-{SYM}-{YYYYMMDD}` 形式

#### C. Alpaca Paper 側の実 fill を verify

```powershell
python scripts/alpaca_snapshot.py --paper --orders-only --limit 60
```

**チェック**:
- [ ] `paper_orders_$today.json` の各 order.client_order_id が Alpaca 側 orders リストに存在
- [ ] status が `filled` / `partially_filled` / `pending_new` のいずれか (rejected は NG)
- [ ] avg_fill_price が spec 通り (S1/S4 = 寄成、S2/S3/S5/S6 = limit)

#### D. exit_check の verify

```powershell
$today = Get-Date -Format "yyyyMMdd"
$exit = "results_csv\exit_orders_$today.json"
if (Test-Path $exit) {
    $j = Get-Content $exit | ConvertFrom-Json
    Write-Host "exit mode=$($j.mode) count=$($j.count) submitted=$($j.submitted) failed=$($j.failed)"
    Write-Host "spy: high=$($j.spy_high) max70=$($j.spy_max70)"
    $j.exits | Group-Object reason | Format-Table Name, Count
    $j.positions | Format-Table symbol, system, side, qty, avg_entry_price, entry_date, unrealized_pl
}
```

**Day 1 (07-06 月) expected**:
- `positions` = 0 (前夜 reset 済、Day 1 の entry 後には 40〜60 件になる)
- `exits` = 0 (position 無いので何も生成されない) or `protect_*` のみ (Day 1 に entry したものが翌 tick で protection 発注される)
- `mode = submitted` (`-AutoSubmitPaper` 時)

**Day 2 (07-07 火) expected**:
- `positions` に前日 entry した銘柄が 40〜60 件並ぶ
- 各 position に system tag, entry_date が hydrate されている (`system: null` はゼロ理想)
- `exits` に:
  - `protect_stop` / `protect_trail` / `protect_target` が各 position 分生成 (dedup 済なので Day 3 以降は減る)
  - `time_based` は 0 (S2/S3/S5/S6 は max_holding_days に達してない)
  - `spy_breakout` は 0 (SPY high < max_70 想定)

**Day 3 (07-08 水) expected**:
- **S2 の time-based exit が発火開始** (S2 entry_date = 07-06, holding=2 = max)
- `exits` に `reason: time_based`, `system: system2`, `side: buy` (short cover) が数件

**Day 4 (07-09 木) expected**:
- **S3, S6 の time-based も発火** (entry_date = 07-06, holding=3 = max)
- `exits` に `reason: time_based`, `system: system3`, `side: sell` と `system: system6`, `side: buy`

**Day 5 (07-10 金) expected**:
- **S5 の 6 日 exit はまだ来ない** (07-06 entry → 07-14 exit)
- entry で新規 40〜60 position が積み上がる
- 累計 position は 100〜200 前後 (S2/S3/S6 の一部は既に close 済)

#### E. Alpaca Paper 側の実 exit fill を verify

```powershell
python scripts/alpaca_snapshot.py --paper --orders-only --status filled --limit 60 |
  Select-String "exit-|protect-"
```

**チェック**:
- [ ] `exit-system{N}-{SYM}-*-exit-time` client_order_id が Alpaca 側で filled になっている
- [ ] `protect-system{N}-{SYM}-*-protect-{stop,trail,target}` が active (`accepted` / `new`)
- [ ] 停止 (stop) 発火の場合、`protect-stop` が `filled` になり対応 position が消えている

#### F. `paper_trading_status.py` で position 全体像を眺める

```powershell
python scripts\paper_trading_status.py --date (Get-Date -Format "yyyy-MM-dd")
Get-Content results_csv\paper_status_$today.json | ConvertFrom-Json |
  Select-Object -ExpandProperty positions |
  Select-Object symbol, system, side, holding_days, max_holding_days,
                unrealized_pl_pct, distance_to_stop_pct, distance_to_target_pct, exit_expected |
  Format-Table -AutoSize
```

**チェック**:
- [ ] `system` に null が無い
- [ ] `exit_expected` が翌日の exit trigger を予告 (S2/S3/S5/S6 で holding_days >= max_holding_days なら `time_based`)
- [ ] `distance_to_target_pct` が -1〜-5% (target 近くまで詰まってる) の position は要注視

---

## 1 週間後 (2026-07-10 金) の期待結果

### 定量指標

| 項目 | 期待値 |
|------|--------|
| 総 entry 数 | 200〜300 件 (49 × 5 日 - dedup) |
| 総 exit 数 | 100〜200 件 |
| avg holding days (全 system) | 2.5〜4.0 日 |
| S2 avg holding | 1.5〜2.0 日 |
| S3 / S6 avg holding | 2.5〜3.0 日 |
| S5 avg holding | 3.5〜6.0 日 (6 日制限、途中 target hit 含む) |
| S1 / S4 avg holding | 4〜10 日 (trailing まで保有) |
| 総 return % | 0 ±3% (5 日じゃ trend 出ない、赤字でも致命ではない) |
| 累計 failed 発注 | 全 pipeline で ≤ 5 件 (自然な filter で fail は 0 が理想) |

### 定性チェック

- [ ] `exit-` client_order_id で重複発注が発生していない (Alpaca 側で見て同 symbol に同 date の time exit が 1 回のみ)
- [ ] `protect-` client_order_id が Alpaca 側で 1 symbol につき **最大 3 個** (stop / trail / target のうち該当のみ) しかない
- [ ] S7 の SPY position が生成されない日 (SPY down day) と S7 short が pending の日で挙動が一致
- [ ] Task Scheduler が 5 回連続で `exit code = 0` を返している

### 定量集計 script (verify 週末)

```powershell
python -c @"
import json
from pathlib import Path
paper = list(Path('results_csv').glob('paper_orders_2026*.json'))
exit_files = list(Path('results_csv').glob('exit_orders_2026*.json'))
entry_total = 0
exit_total = 0
by_system_entry = {}
by_system_exit_reason = {}
for f in paper:
    j = json.loads(f.read_text(encoding='utf-8'))
    for o in j.get('orders', []):
        entry_total += 1
        s = o.get('system') or 'unknown'
        by_system_entry[s] = by_system_entry.get(s, 0) + 1
for f in exit_files:
    j = json.loads(f.read_text(encoding='utf-8'))
    for e in j.get('exits', []):
        exit_total += 1
        key = (e.get('system') or 'unknown', e.get('reason') or 'unknown')
        by_system_exit_reason[key] = by_system_exit_reason.get(key, 0) + 1
print(f'entry total: {entry_total}')
print(f'exit total:  {exit_total}')
print('entry by system:', by_system_entry)
print('exit by (system, reason):')
for k, v in sorted(by_system_exit_reason.items()):
    print(f'  {k}: {v}')
"@
```

---

## 中断条件 (即停止すべき signal)

| 現象 | 対応 |
|------|------|
| Alpaca 側の live URL に注文が飛んだ痕跡 | **即 Disable-ScheduledTask** + issue、`.env` 再確認、full audit |
| exit_check で同一 symbol に protection が 2 個以上ダブって発注 | dedup 破綻。Disable + code 修正 |
| S7 が SPY 以外の銘柄で発注 | signal logic bug or client_order_id 誤 parse。Disable + issue |
| paper_orders_*.json の count が 100 超え | signal 側の bug or filter 抜け。Disable + audit |
| exit_orders_*.json に `system: null` が過半数 | tracker 破損 or hydrate 失敗。Disable + tracker restore |

停止コマンド:

```powershell
Disable-ScheduledTask -TaskName "quant_daily_pipeline"
# Task Scheduler の GUI からも Disabled になってることを確認
```

---

## verify 完了後の判定

**pass** = 全 checklist の `[ ]` が埋まり、定量指標が期待レンジ、中断条件に触れず。この状態を「subscriber サービスイン可能」と定義。

**fail** = いずれか 1 つでも中断条件に触れる、または `system: null` / dedup 崩壊 / 未想定 order type が発生。Phase 5 の修正 iteration に戻る。

verify pass 時の作業:

1. 本 file の下部に「PASS 判定 (2026-07-10)、subscriber サービスイン可」を追記して commit
2. `docs/RELEASE_NOTES_alpaca_paper_wired.md` を新設して user-facing changelog を書く
3. subscriber 募集ページ (別 issue) を有効化

---

## 参照

- 実装: `common/alpaca_trading.py` (exit wiring section)、`common/broker_alpaca.py::submit_order(order_type='bracket')`、`scripts/paper_exit_check.py`、`scripts/paper_trading_status.py`
- pipeline: `scripts/daily_pipeline.ps1` Step 5c `[exit_check]`
- rules: `common/trade_management.py::SYSTEM_TRADE_RULES`
- test: `tests/test_alpaca_exit_orders.py`, `tests/test_alpaca_bracket_order.py`, `tests/test_paper_exit_check_output.py`, `tests/test_paper_trading_status_output.py`, `tests/system/test_daily_pipeline_exit_check_step.py`
