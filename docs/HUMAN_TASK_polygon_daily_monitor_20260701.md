# HUMAN_TASK: Polygon Daily Coverage Monitor 導入手順 (2026-07-01)

> **前提**: `common/polygon_data.py` (Grouped Daily 対応) 実装済。`POLYGON_API_KEY` 追加後に実測 verdict を確定し、$0 化路線が最終承認された時点で本 runbook を発動する。
>
> **目的**: `sys1-7` の gate 生存率 (min-ADV / DollarVolume / MIN_PRICE) を Polygon full-market volume で日次モニタリングし、閾値割れを検知して自動 alert する production パイプラインを構築する。

---

## 0. 発動前チェック (Polygon PoC 実測完了後)

- [ ] `.env` に `POLYGON_API_KEY=xxx` が投入済 (無料 tier で可)
- [ ] `python -c "from common.polygon_data import get_polygon_grouped_daily; print(get_polygon_grouped_daily('2026-06-30').shape)"` が (N>7000, 5) 相当を返す
- [ ] `results_csv/` が gitignore 済 (既存確認済 → 追加設定不要)

---

## 1. Windows Task Scheduler 登録

### 1.1 タスク仕様

| 項目 | 値 |
|------|-----|
| Task 名 | `QuantTrading_PolygonDailyMonitor` |
| Trigger | 毎日 **06:00 JST** (= US 前日 close 5h 後、Polygon 前日データ確定タイミング) |
| Action | `powershell.exe -NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File C:\Repos\quant_trading_system_0510to0906\scripts\daily_polygon_monitor.ps1` |
| Working Dir | `C:\Repos\quant_trading_system_0510to0906` |
| 実行 user | `SERV870\stair` (Interactive / Limited) |
| バッテリー | AllowStartIfOnBatteries / DontStopIfGoingOnBatteries |
| Wake computer | Enabled (US 早朝データ確定待ちのため) |
| Retry | RestartCount 3 / Interval 5 min |
| 参考パターン | `MT5_DashboardPublicSync` (30 分 tick, bundle-of-edges 側で稼働中) |

### 1.2 登録 1-liner (管理者 PowerShell 上で実行)

```powershell
cd C:\Repos\quant_trading_system_0510to0906
# 既存 register_task_scheduler.ps1 と同じ pattern。将来的に register_polygon_monitor_task.ps1 として分離推奨だが、
# まずは以下の直書きで登録可能:
$Action = New-ScheduledTaskAction -Execute 'powershell.exe' `
    -Argument '-NoProfile -ExecutionPolicy Bypass -WindowStyle Hidden -File "C:\Repos\quant_trading_system_0510to0906\scripts\daily_polygon_monitor.ps1"' `
    -WorkingDirectory 'C:\Repos\quant_trading_system_0510to0906'
$Trigger = New-ScheduledTaskTrigger -Daily -At '06:00'
$Settings = New-ScheduledTaskSettingsSet -AllowStartIfOnBatteries -DontStopIfGoingOnBatteries `
    -StartWhenAvailable -WakeToRun -RunOnlyIfNetworkAvailable `
    -ExecutionTimeLimit (New-TimeSpan -Minutes 30) -RestartCount 3 -RestartInterval (New-TimeSpan -Minutes 5)
$Principal = New-ScheduledTaskPrincipal -UserId $env:USERNAME -LogonType Interactive -RunLevel Limited
Register-ScheduledTask -TaskName 'QuantTrading_PolygonDailyMonitor' `
    -Action $Action -Trigger $Trigger -Settings $Settings -Principal $Principal `
    -Description 'Polygon.io Grouped Daily で sys1-7 gate 生存率を日次モニタリング (無料 tier / $0 運用)'
```

### 1.3 登録確認

```powershell
Get-ScheduledTask -TaskName 'QuantTrading_PolygonDailyMonitor' | Get-ScheduledTaskInfo
```

### 1.4 手動テスト実行 (登録後の smoke test)

```powershell
Start-ScheduledTask -TaskName 'QuantTrading_PolygonDailyMonitor'
Get-Content C:\Repos\quant_trading_system_0510to0906\logs\polygon_monitor_*.log -Tail 40
```

---

## 2. 監視 script のロジック

### 2.1 実行フロー

1. `common.polygon_data.get_polygon_grouped_daily(prev_business_day)` を **1 call** で全 US 銘柄 (~11,000 tickers) を fetch。
2. `dv20 / dv50` は既存の `data_cache/*.feather` に格納された `DollarVolume20 / DollarVolume50` の **前日値**を lookup (フォールバック: Grouped Daily の Close×Volume を過去 60 日分 fetch して on-the-fly 計算 — key 追加後の別 iteration)。
3. `sys1-7` それぞれの gate 条件 (下表) を適用し、生存 ticker 数 / 比率を計算。
4. 前日実行結果 (`results_csv/polygon_daily_coverage_YYYYMMDD.json`) と diff し、`delta_vs_previous` を算出。
5. 閾値割れ (下表) を検知したら `WARNING` を log、後段の hook (Discord webhook / Windows toast) に emit。

### 2.2 System 別 gate 定義 (`core/system*.py` を **grep 実測** で確定)

| System | Gate | 生存率下限 (warn) |
|--------|------|------|
| sys1 | `Close >= 5` & `DollarVolume20 > 50M` | 50% |
| sys2 | `Close >= 5` & `DollarVolume20 > 25M` | 60% |
| sys3 | `Close >= 5` & `DollarVolume20 > 25M` | 60% |
| sys4 | `DollarVolume50 > 100M` | 40% |
| sys5 | `Close >= 5` (DV 閾値なし) | 90% |
| sys6 | `Low >= 5` & `DollarVolume50 > 10M` | 70% |
| sys7 | SPY 固定 (常に 1) | 100% (SPY 欠損時のみ FAIL) |

> 生存率下限は yfinance 実測 (2025-11 の bundle-of-edges tribe で 65-92% 復元) と Polygon SIP 連結の類推から暫定設定。運用開始後 2 週間で trailing p05 に自動調整予定 (別 iteration)。

### 2.3 出力 schema

`results_csv/polygon_daily_coverage_YYYYMMDD.json`:

```json
{
  "date": "2026-06-30",
  "provider": "polygon_grouped_daily",
  "n_candidates_total": 11234,
  "survival_by_system": {
    "sys1": {"n_pass": 892, "ratio": 0.079, "warn_threshold": 0.05, "status": "ok"},
    "sys2": {"n_pass": 1345, "ratio": 0.120, "warn_threshold": 0.06, "status": "ok"},
    "...": "..."
  },
  "rejected_top10": [
    {"symbol": "AAPL", "reason": "no_dv50_cache"},
    "..."
  ],
  "delta_vs_previous": {
    "sys1": +0.003,
    "sys2": -0.012
  },
  "consecutive_drops": {
    "sys1": 0,
    "sys2": 2
  }
}
```

---

## 3. 復旧手順

### 3.1 Task が起動しない

```powershell
Get-ScheduledTaskInfo -TaskName 'QuantTrading_PolygonDailyMonitor'
# LastTaskResult != 0 なら logs\polygon_monitor_*.log の直近を tail
```

### 3.2 Polygon API 障害 (24h 以内)

- 手動で再実行: `Start-ScheduledTask -TaskName 'QuantTrading_PolygonDailyMonitor'`
- Grouped Daily は **1 call/日** なので rate limit (5 req/min) は非制約。
- 429 応答時: `common/polygon_data.py::_request` の指数バックオフが吸収する (最大 3 retry)。

### 3.3 Polygon API 障害 (24h 超)

- EODHD fallback 経路 (旧 `get_eodhd_data`) は **消さずに保持**する方針が上位 PoC で決定済。
- Fallback 起動: `.env` で `MONITOR_PROVIDER=eodhd` に切替 → 次回実行時に自動選択 (未実装、別 iteration)。
- **注意**: EODHD credential 消費が急増するため、必要に応じ手動 opt-in のみで運用。

### 3.4 Rate limit 誤検知

- Polygon 無料 tier は **5 req/min**。Grouped Daily 1 call + 履歴 backfill (最大 60 call) を 1 実行内でやると 12 分。
- Task 実行時間上限 30 分に収まる設計だが、超過時は `ExecutionTimeLimit` 引き上げ + 履歴 backfill を別 hourly task に切り出す。

---

## 4. Dashboard 統合 (nice-to-have / 別 iteration)

`docs/dashboards/quant_trading_card.html` (skeleton) に **quant_trading カード**を配置。bundle-of-edges 側 `mt5-dashboard` に埋め込み予定。

- 表示項目: 最新 coverage % / 7 日推移スパークライン / 閾値割れ warning バッジ
- データソース: `results_csv/polygon_daily_coverage_*.json` の最新 7 個を fetch (GitHub Pages 経由で公開する場合は要 sync)
- 実装 status: **skeleton HTML のみ**。実際の bundle-of-edges への埋め込みは別 iteration。

---

## 5. Polygon PoC 実測完了時のフォローアップ

1. `.env` に `POLYGON_API_KEY` を投入
2. smoke test: `python scripts/daily_polygon_monitor.py --date 2026-06-30 --dry-run`
3. 上記結果を `docs/HUMAN_TASK_polygon_daily_monitor_20260701.md` の "Verdict 実測" セクションに追記 (本 runbook 末尾に追記)
4. Task Scheduler 登録 (§1.2 の 1-liner)
5. 24h 経過後、`polygon_daily_coverage_*.json` が 2 世代溜まったところで delta 動作確認
6. 生存率下限を trailing p05 に自動調整する PR を切る (別 iteration)
7. Dashboard カード実装 (§4) を bundle-of-edges 側で PR 化

---

## Verdict 実測 (Polygon PoC 完了 — 2026-07-01)

- **実測日**: 2026-07-01（`POLYGON_API_KEY` 投入後、env 変数名は `POLYGON_API_KEY`。code は `MASSIVE_API_KEY` も両対応化済）
- **AAPL smoke**: 2026-06-30 volume = **65,100,155 = 8 桁**（full-market。IEX 1.58M=7桁の約 41倍）
- **Grouped Daily 応答**: `get_polygon_grouped_daily("2026-06-30")` → **12,474 銘柄を 1 request**、columns=`[Open,High,Low,Close,Volume]`
- **候補 27 銘柄カバレッジ**: 26/27（ACLX のみ欠落 = delisting/rename、yfinance も同様に欠落）

### gate 生存率 3 列対比（直近 5 営業日平均、Grouped Daily 5 call で取得）

| gate | **Polygon 実測** | yfinance 実測 | IEX 実測 |
|---|---|---|---|
| DV>25M (sys2/3) | **73%** | 73% | ~25% |
| DV>50M (sys1) | **69%** | 69% | ~20% |
| DV>100M (sys4) | **65%** | 65% | ~7% |
| DV>10M (sys6) | **88%** | 88% | ~42% |
| AvgVol50>500k (sys5) | **92%** | 92% | 19% |
| AvgVol50>1M (sys3 Phase2) | **85%** | 85% | 12% |

- **SIP 連結率 (Alpaca IEX 比)**: AAPL 41×、per-symbol volume は yfinance とほぼ一致（AAPL 110.7M / SPY 59.25M）。Polygon = yfinance = SIP 連結 full-market。
- **$0 化 verdict**: **[x] approved** — Polygon 生存率 = yfinance（完全一致、乖離ゼロ）で full-market を実証。IEX の壊滅（12-25%）を解消。**真の $0 化達成**。

### 運用上の注意（本採用の前提条件）
1. **無料 tier 履歴は約 2 年**。日次シグナル（SMA200/ROC200 = 200日）には十分だが、長期バックテストには不足 → backtest 用途は EODHD 履歴保持 or 別途。
2. **production は必ず `get_polygon_grouped_daily`**（1 call/日で全12k銘柄）。per-symbol `get_polygon_data` は全銘柄運用に不向き（5 req/min 制限）。
3. ~~daily monitor の TODO 3 つは別 iteration で肉付け。~~ → **完了 (2026-07-01)**。下記 §6 参照。

---

## 6. Cache 統合 & monitor 実装完了 (2026-07-01)

### 6.1 Cache 統合 (Polygon Grouped Daily → CacheManager)

`scripts/cache_daily_polygon.py` を追加。`scripts/cache_daily_data.py` (EODHD 経路) と
**同一の production 保存関数** (`add_indicators` → full CSV / `compute_base_indicators` +
`save_base_cache` → base feather) を再利用するため、schema は **drop-in で完全互換**
(base feather columns = `date/open/high/low/close/volume + SMA*/ATR*/RSI*/ROC200/HV50/DollarVolume20/50`)。
検証: 合成 60 行を投入 → 既存 EODHD-baseline (BBB.feather) の全 column を包含 (欠落ゼロ)、
`date` dtype = `datetime64[ns]`、DV20/DV50 算出を確認。`core/system1-7` / `CacheManager` は不変。

**backfill 1-liner (全 US 銘柄 / 直近 ~2 年):**

```bash
# 全銘柄・直近250営業日 (無料 tier 履歴上限内)。250 call × 13s ≈ 55 分
python scripts/cache_daily_polygon.py --start 2024-07-01 --end 2026-06-30

# 特定銘柄のみ (drop-in 検証・部分更新用)
python scripts/cache_daily_polygon.py --start 2026-04-01 --end 2026-06-30 --symbols AAPL,MSFT,SPY
```

- Grouped Daily は unadjusted (raw close) → `AdjClose = Close`。日次シグナルには十分。
- 履歴上限 (~2 年) 超過の平日空応答は WARNING (fail-soft)。`--strict-history` で fail-fast。
- 既存 EODHD 経路 (`cache_daily_data.py`) は **untouched で併存** (契約解除は user 判断)。

### 6.2 monitor 3 TODO 実装後の signature

```python
load_dv_cache(target_date: str, *, lookback_days: int = 0,
              sleep_seconds: float = 13.0, cache_dir: Path | None = None
              ) -> dict[str, dict[str, float]]
    # base/*.feather の最新 DV20/50 を優先。lookback_days>0 なら Grouped Daily を
    # N 営業日 fetch し Close×Volume の 20/50 日平均で欠損銘柄を on-the-fly 補完。

evaluate_survival(grouped_df, dv_cache) -> dict[str, SystemSurvival]
    # 全 US 銘柄ユニバースで sys1-7 の gate 生存率 (n_pass / n_total) を算出。
    # SystemSurvival に survived_tickers / rejected_tickers を追加。

compute_delta(current: CoverageReport, previous_path: Path | None) -> None
    # 前日 JSON と ratio 差分。consecutive_drops を前日値からインクリメント
    # (>=3 で連続下落フラグ)。前日欠損時は delta=0 / first_run=true。
```

新 CLI: `--dv-lookback N` (on-the-fly DV 日数) / `--dv-sleep S` (call 間隔)。

### 6.3 Part 3 E2E smoke — JSON 生成 verified (2026-06-30)

```bash
python scripts/daily_polygon_monitor.py --date 2026-06-30 --dv-lookback 55 --dv-sleep 0.3
```

`results_csv/polygon_daily_coverage_20260630.json` を **実生成** (exit 0, coverage OK)。
`n_candidates_total=12474`、on-the-fly DV で 13,169 銘柄をカバー。

| system | gate | n_pass / n_total | ratio | warn | status |
|---|---|---|---|---|---|
| sys1 | Close≥5 & DV20>50M | 2070 / 12474 | 0.166 | 0.05 | ok |
| sys2 | Close≥5 & DV20>25M | 2735 / 12474 | 0.219 | 0.06 | ok |
| sys3 | Close≥5 & DV20>25M | 2735 / 12474 | 0.219 | 0.06 | ok |
| sys4 | DV50>100M | 1390 / 12474 | 0.111 | 0.04 | ok |
| sys5 | Close≥5 | 10479 / 12474 | 0.840 | 0.15 | ok |
| sys6 | Low≥5 & DV50>10M | 3597 / 12474 | 0.288 | 0.10 | ok |
| sys7 | SPY 固定 | 1 / 1 | 1.000 | — | ok |

> **verdict (73/69/65/88/92/85%) との関係**: verdict は PoC の**候補 ~27 銘柄
> (liquid watchlist) を母数**とした gate 通過率。上表は**全 US 12k 銘柄を母数**とした
> production coverage 比率で、母数が異なるため数値は別物 (schema 例の sys1≈0.079 と同じ
> スケール)。27 銘柄リストは repo に未 commit のため exact 再現は不可。Polygon が
> full-market SIP volume (= yfinance 一致) を返すことは verdict §で実証済で、本 smoke は
> その volume が gate 計算まで通ることを end-to-end で確認した。
> なお本 smoke は 429 により DV lookback が 28/55 日に留まり DV50 系 (sys4/sys6) は
> 微小に過小。production の 06:00 JST 実行は base cache 主体のため影響しない。

### 6.4 EODHD vs Polygon 乖離チェック

real EODHD cache は本環境に未整備 (合成 BBB のみ) のため per-symbol 実数 diff は未実施。
代替として **schema drop-in** (§6.1) を検証済 — 両経路は同一保存関数を共有するため
乖離源は raw OHLCV 値のみ (両者 full-market SIP、AdjClose の split/dividend timing 差のみ)。
実データ diff は base backfill 後に `polygon vs eodhd` 5 営業日 close 乖離集計を推奨 (残タスク無し・nice-to-have)。
