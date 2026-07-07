# Execution Summary & Reconciliation (実発注サマリ)

**追加**: 2026-07-07。**目的**: 「N signals 出て、実際に何件発注・約定したか」を
signals→plan→entry→fill→exit で 1 通に集約し、既存ポジ・上限突合後の実像を見せる。

## データの流れ (daily_pipeline.ps1)

```
Step2  signals   → today_signals_YYYYMMDD.json   (per-system funnel + signals)
Step5b paper_orders → paper_orders_YYYYMMDD.json (entry: submitted/skipped/failed + skip_reason)
Step5c exit_check   → exit_orders_YYYYMMDD.json  (exit: close/protection)
Step5d exec_summary → recon_YYYYMMDD.json + ntfy 通知  ← NEW
```

Step5d は `scripts/publish_execution_summary.py`。3 つの JSON を
`scripts/build_execution_recon.py::build_recon` で system×side に join し、
`recon_YYYYMMDD.json` を書き出して ntfy へ整列サマリを送る。
`-AutoSubmitPaper` 時のみ実送信、それ以外は `--dry-run` (recon 生成 + 本文表示のみ)。

## ntfy 実行サマリの読み方

```
📊 07-08 exec sig49 entry27 exit14          ← X-Title (ASCII+emoji)
Tgt 4123 → sig 49 → gen 34 → entry 27 → fill 25
exit 14 (close 5 / protect 9)
LONG entry 18 / SHORT entry 9   資産 $10,120
─ system別 sig→entry fill/ex ─
s1L 12→7 fill7 ex2 (skip1)
s2S  9→3 (fail1)
⚠ drop: below_min_notional 6 · short 4 · wash 1 · fail 1
```

- **Tgt** 対象ユニバース → **sig** 当日シグナル → **gen** 発注生成 → **entry** 実 entry 送信 → **fill** 約定
- **gen→entry** の差 = skip (min_notional 未満 / wash / unsizable) と fail。`⚠ drop` に内訳。
- **exit** は entry と別プロセス (Step5c) 由来。close = time/breakout、protect = stop/trailing/target。
- **fill** は約定確認できた分のみ (成行は submit 直後は accepted、fill は非同期なので 0 のことがある)。

## 手動実行

```powershell
# recon だけ作る
python scripts/build_execution_recon.py --date 2026-07-08 --account-equity 10120

# サマリを送信せず本文確認 (recon も書き出す)
python scripts/publish_execution_summary.py --date 2026-07-08 --dry-run

# 実送信 (NTFY_TOPIC 設定要)
python scripts/publish_execution_summary.py --date 2026-07-08
```

## 関連する観測性の修正 (2026-07-07)

- **min_notional silent drop の可視化**: `common/alpaca_trading.py::signals_json_to_orders`
  が min_notional 未満/サイズ不能を silent `continue` せず skip_reason 付きで残す
  → recon の drop 内訳に必ず出る。全 skip の confirm 実行は `no_orders_submitted`
  (exit 3) で silent success を防ぐ。
- **pipeline funnel の JSON 化**: `common/signal_export.py::build_signals_json` が
  per-system funnel (Tgt/FIL/STU/TRD/Entry) を `today_signals_*.json` に serialize
  → Vercel dashboard の SIGNAL PIPELINE が「未計測」でなくなる。STUpass は全 system
  対応済 (旧 system3/5 限定 hardcode を撤廃)。

## 既存ポジ・上限突合 (plan の中身)

recon の "gen"/"entry" は下記突合を経た後の数:
- **既存ポジ突合**: 配分段で現保有を `available_slots` に反映 (docs today_signal_scan/6)、
  entry 段で同方向の既保有銘柄を skip (`already_held`)。→ [`../POSITION_MANAGEMENT_PHASE5_20260707.md`](../POSITION_MANAGEMENT_PHASE5_20260707.md)
- **portfolio 上限**: total/long/short 件数 + gross/net exposure (net は equity の 50%)。
  詳細と config は同 doc。
