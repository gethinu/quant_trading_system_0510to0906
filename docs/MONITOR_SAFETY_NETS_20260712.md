# 監視「回して見守る」フェーズの生命線 2 本 (2026-07-12)

paper 専用・ライブ発注なし。branch `claude/monitor-safety-nets-20260712`
(base = `claude/open-auto-run`。production worktree `C:\tmp\qts-main-run` が実行中の branch)。

過去に踏んだ silent 失敗 (「0 シグナル」「ダッシュ 07-07 固着」) と、未検証だった
exit 発火 / 未配線だった drawdown flatten を塞ぐ。

---

## #1 自己監視アラート (silent-failure ガード)

**`scripts/self_monitor_check.py`** + `self_monitor_check.ps1`

毎朝 1 回、以下を検査し **1 通のサマリ ntfy** にする (NtfyPublisher = UTF-8-safe。
素の `Invoke-RestMethod` / str POST の latin-1 silent death を回避)。異常時は urgent(5)。

| check | 内容 | 異常判定 |
|-------|------|----------|
| `daily` | 06:00 デイリー (main 追従) が走ったか。`today_signals_YYYYMMDD.json` の mtime 鮮度 | 最新が `--max-age-hours`(既定26h) 超 → **CRIT** / 皆無 → CRIT |
| `signals` | シグナルが潤沢か。`portfolio.total_signals` | 0 → **CRIT** / `--min-signals`(既定10) 未満 → **WARN** |
| `publish` | Vercel publish 成功か。`monitor-webapp` ブランチの最新 commit 時刻 (git log) | 最新 commit が 26h 超 → **CRIT** (ダッシュ固着の疑い) |
| `open_run` | オープン自動発注が走り entry が fill したか。最新 `logs/open_run_<date>/completion_recon.json` | `market_closed` abort=良性(OK) / 他 abort・entry 0 → **WARN** / 96h 超 → WARN |

- source of truth は **primary repo** (`C:\Repos\...`)。`C:\tmp\qts-main-run` の
  `logs`/`results_csv`/`data_cache` は primary への junction なので `--repo-root` だけ見れば良い。
- durable: `logs/self_monitor_YYYYMMDD.json` (全 check の合否)。
- exit code: 0=OK / 2=WARN / 3=CRIT。
- 「全 OK」も 1 通送る = **dead-man's-switch** (通知が来ない=監視自体が死んでいる、が分かる)。
  静かにしたいなら `--only`... ではなく `--no-notify` で JSON だけ出せる。

疎通 (実データ, ntfy なし):
```
python scripts/self_monitor_check.py --repo-root C:\Repos\quant_trading_system_0510to0906 --dry-run
```
検証済 (2026-07-12): daily/signals(44)/publish/open_run すべて OK。

---

## #2a exit E2E 検証

**`scripts/exit_verify.py`** + `exit_verify.ps1`

`paper_exit_check` が生成する `exit_orders_YYYYMMDD.json` (planned exits + position
snapshot) を single source に、「本日 exit 予定 vs 実 fill」を突合する。

1. **expected**: position snapshot から time-based 満期 (`holding_days >= max_holding_days`,
   S2=2/S3=3/S5=6/S6=3) を **独立再計算** = paper_exit_check の漏れ検知。
2. **planned**: `exits[]` = paper_exit_check が実際に planned/submit した exit。
3. **fill**: 各 planned close の Alpaca order status を GET 照合 (`--no-alpaca` なら JSON 記録値)。
   resting protection (stop/limit/trailing) は fill 待ちが正常なので close 判定から除外。
4. **reconcile** → `due_not_planned` / `closes_rejected` / `closes_unfilled_nonpending` /
   `closes_pending`(市場休場の成行=良性) を分類。

- durable: `logs/exit_verify_YYYYMMDD.json`。月曜以降の建玉で time-exit が実発火するのを日次で追える。
- WARN 通知は「満期漏れ」「close reject/未 fill」があるときだけ。
- exit code: 0=乖離なし / 2=discrepancy。

### ⚠ 初回実行で検出した実バグ (要別対応)

2026-07-12 の実データで `exit_verify` が **system3 の 6 建玉 (AEHR/AIP/FORM/MXL/UCTT/VECO)**
を「満期(4d>=3d)だが未計画」と検出。根因は **端株 (qty<1) が exit 計画から丸ごと落ちる**:

- `PositionSnapshot.abs_qty = int(abs(qty))` → qty 0.18 などは **0 に切り捨て** →
  `build_exit_orders_from_positions` の `if abs_qty <= 0: continue` で **全 exit 種別(time/protect)
  から silent に除外**。equity 連動サイジングは端株を日常的に作るので、多数の建玉が
  **time-exit で決済できない** = まさに silent exit 失敗。
- 制約: Alpaca は端株 (notional/fractional) を **market DAY のみ**受け付け、stop/limit/trailing
  不可。→ 端株の time-exit は market close、protective は別扱い、という設計が要る。
- 本 branch では **修正せず検出に留める** (core 発注ロジック変更 + Alpaca 端株制約の設計は別タスク)。

---

## #2b drawdown サーキットブレーカ (配線)

判定関数 `portfolio_guard.evaluate_drawdown_flatten` は既存。今回 **自動 flatten を配線**:

- **`common/drawdown_breaker.py`** (共有): peak 解決 (`alpaca_equity_history.json` + 現 equity の最大)、
  `assess()` (誤発火ガード込み判定)、`flatten_all_paper()` (close_all_positions)。
- **`scripts/drawdown_circuit_breaker.py`** (standalone CLI): 状態確認 / 手動 / スケジュール可能。
- **`open_auto_run.py` の entry 前に配線**: config 有効 & 閾値超え & 全ガード通過なら
  全 flatten して **新規建てを中止** (ドローダウン中に建てない安全弁)。

**既定は完全 no-op** (`config.risk.portfolio.drawdown_flatten_pct = 0.0`)。有効化は user が
config に閾値を入れたときだけ。**保守的な提案値 = 0.15 (15%)**。

### 誤発火防止ガード (`assess`)
| ガード | 挙動 |
|--------|------|
| config 無効 (threshold<=0) | `armed=False` で即 no-op (最優先, 既定) |
| equity / peak 欠損・非正 | flatten しない (`no_equity`/`no_peak`) |
| 履歴点数 < `--min-history-points`(既定5) | 薄い履歴で peak 不確か → flatten しない |
| 絶対ドローダウン額 < `--min-abs-drawdown-usd`(既定0=無効) | 小口の flatten を抑止 |
| 本日 flatten 済 (DONE marker) | 冪等 skip |
| `--confirm` 未指定 | dry-run 等価 (WOULD FIRE 通知のみ) |
| flatten 実行前 | `assert_paper_env` (live 口座なら中止) |

新高値 (現 equity=peak) では drawdown=0 で絶対に発火しない。

dry-run 検証 (2026-07-12, `--no-alpaca --equity` で疑似):
- config=0 → `disabled` no-op (exit 0)
- 閾値0.15 & 履歴2点 → `breached_but_guarded:thin_history(2<5)` = flatten せず (exit 0)
- 閾値0.15 & `--min-history-points 1` & equity 80k(peak 106k, dd 24.7%) → `WOULD FLATTEN` (exit 11, 未執行)
- equity 200k(新高値) → `within_threshold` (exit 0)

exit code: 0=無為 / 10=flatten実行 / 11=WOULD(dry-run) / 2=エラー。

---

## スケジュール

**`scripts/register_safety_tasks.ps1`** (登録専用・checkは走らせない・冪等)。既存
`QuantTrading_OpenAutoRun` と同じ principal (Interactive/Limited)・hidden 実行:

- `QuantTrading_SelfMonitor` : 07:15 JST daily → `self_monitor_check.ps1`
- `QuantTrading_ExitVerify`  : 07:20 JST daily → `exit_verify.ps1`

06:00 デイリーと前夜 22:35/23:35 open-run の **後** に 1 パスで全サイクルを検査。
`-WorktreeRoot` 既定は production `C:\tmp\qts-main-run` (branch merge 後に有効)。
merge 前の暫定は `-WorktreeRoot C:\tmp\qts-safety-nets`。

> このスクリプトは **まだ未登録**。レビュー後にユーザーが 1 回実行する
> (`push は確認後` 方針に合わせ、ライブ通知を送るタスクを勝手に常駐させない)。

---

## 安全性まとめ
- 全スクリプト paper 前提。self_monitor / exit_verify は **read-only** (発注・cancel 一切なし)。
- circuit breaker のみ flatten するが、`config 有効 + 閾値超え + 全ガード通過 + --confirm`
  の 4 条件が揃うときだけ。既定は無効。live 口座は `assert_paper_env` で拒否。
- ntfy は全経路 NtfyPublisher (UTF-8-safe)。
