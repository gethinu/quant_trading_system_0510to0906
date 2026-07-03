# D5 精査 report: SYSTEM_SPECIFIC_CONFIG と S1 3-day 強制決済 bug (2026-07-02)

`tests/DIVERGENCE_ANALYSIS_20260702.md` の D5 項 (TradeManager max_holding_days) を
`strategies/constants.py` `SYSTEM_SPECIFIC_CONFIG` の観点で精査。前 audit の bug 範囲
が誇張されていた点を訂正しつつ、**真の bug (S1 のみ) の事業影響を定量化** し、
修正 3 case を trade-off table で比較する。

**この文書は判断材料で、実装 patch は含まない。**
推奨 case を user が決定した後、別 dispatch で patch 適用する。

---

## Executive Summary

- **真の bug は S1 のみ**。live signal 経路 (Config A = `SYSTEM_TRADE_RULES`) は spec 準拠。
  backtest 経路の `System1Strategy.compute_exit` が YAML の `max_hold_days` 未定義を拾わず、
  Python fallback `MAX_HOLD_DAYS_DEFAULT=3` で **long-only trend momentum を 3 日で強制決済**。
- **S4 / S6 / S7 の bug は前 audit の誤認**。S4 の compute_exit は trailing stop only で
  `max_hold_days` を参照しない。S6 は YAML の `profit_take_max_days: 3` で spec 準拠。
  S7 は独自 compute_exit で該当 config を使わない。
- **`SYSTEM_SPECIFIC_CONFIG` 自体が完全な死コード**。repo 全体 grep で参照元 0 件
  (constants.py 自身と report のみ)。
- **micro-benchmark の定量差** (7 synthetic scenario の合計 return): bug -5.09% vs spec +54.43%
  → 1 trade あたり平均 **+8.5% の逸失利益**、強モメンタム 1 件で **+36.6%** の差。
  S1 は long-bucket の 25% を占めるため、事業影響 (subscriber の期待リターン) 直接的。
- **推奨: Case 3 (hybrid)**。S1 の 3-day fallback を撤去し trailing stop に一元化 (=Case 1 と同挙動)、
  同時に **死コード `SYSTEM_SPECIFIC_CONFIG` を削除** して config surface を YAML 1 本に。
  Case 2 (現状維持 + docs) は subscriber リターン想定を歪めた状態で正当化することになり NG。

---

## 1. 三者比較 table (spec / Config A / Config B / YAML / 実挙動)

7 system を横断で **時間 exit** 観点で並べる。ここでは
- `spec` = `docs/systems/システムN.txt` (書籍準拠)
- Config A = `common/trade_management.py::SYSTEM_TRADE_RULES` (live signal 経路)
- Config B = `strategies/constants.py::SYSTEM_SPECIFIC_CONFIG` (死コード)
- YAML = `config/config.yaml::strategies.systemN` (backtest 経路の実 source)
- 実挙動 = `strategies/systemN_strategy.py::compute_exit` が返す結果

| System | Spec 時間 exit | Config A | Config B (dead) | YAML | 実 compute_exit | 判定 |
|---|---|---|---|---|---|---|
| **S1** long trend | **無し** (25% trail) | 未設定 → trail ✓ | `max_hold_days=3` | key **無し** | `self.config.get("max_hold_days", MAX_HOLD_DAYS_DEFAULT=3)` → **3 日強制決済** | 🔴 **真の bug** |
| **S2** short RSI thrust | 2 日 | `max_holding_days=2` ✓ | `max_hold_days=3` | `max_hold_days: 2` | 2 日 ✓ | ✅ |
| **S3** long MR sell-off | 3 日 | `max_holding_days=3` ✓ | `max_hold_days=3` | `max_hold_days: 3` | 3 日 ✓ | ✅ |
| **S4** long trend low-vol | **無し** (20% trail) | 未設定 → trail ✓ | `max_hold_days=3` | key 無し / `trailing_pct: 0.20` | compute_exit は `max_hold_days` **不参照**、trailing 20% のみ | ✅ (前 audit 誤認) |
| **S5** long MR ADX | 6 日 | `max_holding_days=6` ✓ | `fallback_exit_days=6` | `fallback_exit_after_days: 6` | 6 日 ✓ | ✅ |
| **S6** short 6-day surge | 3 日 | `max_holding_days=3` ✓ | (key 無し) | `profit_take_max_days: 3` | S6 は `profit_take_max_days` key を使う → 3 日 ✓ | ✅ (前 audit 誤認) |
| **S7** SPY hedge | 無し | 削除済 (2026-07-02 audit) ✓ | `max_hold_days=3` | key 無し | S7 独自 compute_exit、該当 config **不使用** | ✅ |

### 前 audit との差分 (誤認訂正)

| 前 audit 主張 | 真の状態 |
|---|---|
| S1 3-day exit bug | ✅ 事実 (**唯一の真の bug**) |
| S4 3-day exit bug | ❌ 誤認: S4 compute_exit は max_hold_days を **参照しない** (trailing 20% のみ) |
| S6 time exit 欠落 bug | ❌ 誤認: YAML `profit_take_max_days:3` で spec 準拠 (3 日で決済) |
| S7 dead time exit | ❌ 影響なし: S7 は独自 compute_exit 経路 |
| Config B に一元化 or 削除 | ✅ 削除方向 (**Config B は完全な死コード**、`SYSTEM_SPECIFIC_CONFIG` を import している symbol は 0) |

### `SYSTEM_SPECIFIC_CONFIG` が死コードである証拠

```bash
$ grep -rn "SYSTEM_SPECIFIC_CONFIG" .
strategies/constants.py:25:SYSTEM_SPECIFIC_CONFIG = {  # 定義箇所のみ
tests/DIVERGENCE_ANALYSIS_20260702.md:...             # 誤認前提の言及
```

runtime の `self.config` は `strategies/base_strategy.py:__init__` で
`config.settings.get_system_params(sys_name)` から populate される。これは
`config/config.yaml` の `strategies.<name>` mapping を読む
(`config/settings.py:654 get_system_params`)。`SYSTEM_SPECIFIC_CONFIG` は
どの code path からも lookup されない。

各 strategy が `from .constants import MAX_HOLD_DAYS_DEFAULT, FALLBACK_EXIT_DAYS_DEFAULT`
と個別 constant を import しているのみで、dict そのものは未使用。

### S1 bug の実行 path

```
common/backtest_utils.py:90     if hasattr(strategy, "compute_exit"):
  → strategy.compute_exit(df, entry_idx, entry_price, stop_loss_price)
    ↓
strategies/system1_strategy.py:212-229
  max_hold_days = int(self.config.get("max_hold_days", MAX_HOLD_DAYS_DEFAULT))
    ↓ self.config は YAML の system1 セクションのコピー
    ↓ YAML system1 に max_hold_days key が **無い** (`config/config.yaml:14-22`)
    ↓ → fallback = MAX_HOLD_DAYS_DEFAULT = 3 (`strategies/constants.py:15`)

  for offset in range(max_hold_days):  # 0..2
    ...stop check...
  exit_idx = min(entry_idx + max_hold_days, n - 1)  # 3 日目 close で forced exit
```

`integrated_backtest.py:132-138` の経路も同一 branch を通る。従って **全 backtest 経路
で S1 が 3 日で切られる**。

---

## 2. 修正 case 3 種 × trade-off matrix

### Case 1: spec 完全準拠

**変更内容**:
1. `strategies/system1_strategy.py::compute_exit` を書き換え。
   - S4 と同じく trailing stop + 5ATR stop-loss のみ、時間 exit ロジック撤去。
2. `strategies/constants.py::SYSTEM_SPECIFIC_CONFIG` を **削除**。
   - `MAX_HOLD_DAYS_DEFAULT` / `FALLBACK_EXIT_DAYS_DEFAULT` / `STOP_ATR_MULTIPLE_*` /
     `PROFIT_TAKE_PCT_DEFAULT_*` / `ENTRY_MIN_GAP_PCT_DEFAULT` は個別 constant
     として維持 (S2/S3 が fallback 用に import している)。
3. `config/config.yaml::strategies.system1` に `trailing_pct: 0.25` を明記
   (spec 通り、25% trail)。
4. `docs/D5_*` に「S1 は max_hold_days 無し・trailing 25%」を追記 (この文書自身)。

**touched files** (概算 diff):

| File | 変更 line 数 (add / del) |
|---|---|
| `strategies/system1_strategy.py` | +30 / -20 (compute_exit 差し替え) |
| `strategies/constants.py` | +5 / -35 (dict 削除、コメント追加) |
| `config/config.yaml` | +1 / 0 (trailing_pct 追加) |

**backtest 可能性**: 変更後の compute_exit は S4 と同型なので、既存
`tests/test_system4_strategy*.py` の pattern を移植して S1 unit test 追加可能。
End-to-end backtest は Windows 側で `scripts/bench_pipeline_today.py` などで再走可能
(sandbox は disk 満杯で不可)。

### Case 2: 現状維持 + docs update

**変更内容**:
1. `docs/systems/システム1.txt` に「実装は 3 日で時間 exit する (書籍とは意図的に乖離)」
   を追記。
2. `config/config.yaml::strategies.system1` に `max_hold_days: 3` を **明示** して
   fallback 依存を可視化。
3. `SYSTEM_SPECIFIC_CONFIG` は死コード状態のまま維持 (削除するかどうかは別議論)。

**touched files**:

| File | 変更 line 数 |
|---|---|
| `docs/systems/システム1.txt` | +3 / 0 |
| `config/config.yaml` | +1 / 0 |

**リスク**: subscriber リターン想定を書籍 (25% trail 前提の中期モメンタム) から
「3 日強制決済版」に変更することを暗に認める。**書籍と剥離した戦略になり
"trend-momentum" の name と実挙動が矛盾**。事業側の説明が困難。

**採用条件**: 過去 backtest で「3 日 exit の方が book 準拠より Sharpe が高い」
歴史的裏付けがあるなら初めて正当化可能。この文書時点で証拠なし。

### Case 3: hybrid (推奨)

**変更内容**:
1. Case 1 と同じく `System1Strategy.compute_exit` を trailing-only に書き換え
   (書籍 spec 準拠の 25% trail + 5ATR stop)。
2. `SYSTEM_SPECIFIC_CONFIG` を **完全削除** (死コード掃除)。個別 constant は残す。
3. `docs/systems/システム1.txt` は現状維持 (spec 記述と実装が一致するため追記不要)。
4. `config/config.yaml::strategies.system1` に `trailing_pct: 0.25` を明記。
5. **追加テスト**: `tests/test_system1_time_exit_regression.py` を新規作成し、
   S1 は 30+ 日保有され得ることと、trailing 25% が下降時に発火することを assert
   (再発予防 regression test)。

**touched files**:

| File | 変更 line 数 |
|---|---|
| `strategies/system1_strategy.py` | +30 / -20 |
| `strategies/constants.py` | +5 / -35 |
| `config/config.yaml` | +1 / 0 |
| `tests/test_system1_time_exit_regression.py` | +80 (new) |

**Case 1 との違い**: Case 3 は死コード掃除と regression test を明示的に含む。
Case 1 は最小 patch でこれらを含まない。**Case 3 は Case 1 の superset。**

### trade-off matrix

| 観点 | Case 1 spec | Case 2 現状 | Case 3 hybrid |
|---|---|---|---|
| S1 backtest リターン | 大幅改善 (下記 §3) | 現状 (bug 継続) | 大幅改善 (Case 1 と同じ) |
| Subscriber の期待値との整合 | ✅ | ❌ (書籍と乖離) | ✅ |
| 死コード掃除 | ⚠️ 未実施 | ⚠️ 未実施 | ✅ 実施 |
| Regression test | ⚠️ 未 | ⚠️ 未 | ✅ 追加 |
| Docs 更新の要否 | 不要 | 必要 (spec 書き換え) | 不要 |
| 実装リスク | 小 | 極小 | 小 |
| 事業影響 (subscriber リターン)| 直接的 up | 継続的 down (現状) | 直接的 up |
| **推奨度** | ○ | ✗ | **◎** |

---

## 3. backtest 差分 (sandbox micro-benchmark + Windows 側 full 手順)

### 3.1 sandbox micro-benchmark (実行済)

`outputs/s1_bug_microbench.py` で `System1Strategy.compute_exit` を独立再現し、
7 synthetic scenario で **bug 版 vs spec 版** の 1-trade return を比較。
Stop = entry × 0.88 (5ATR ≈ -12%)、trailing = 25%、entry = 100.

| Scenario | Case A bug (3-day) | Case B/C spec (trail) | Δ |
|---|---|---|---|
| momentum_strong (+40% in 30d) | +3.42% @ d3 | +40.00% @ d30 | **+36.58%** |
| momentum_mild (+12% in 30d) | +1.14% @ d3 | +12.00% @ d30 | **+10.86%** |
| up_then_reverse (+20%→-30%) | +5.62% @ d3 | -9.55% @ d26 | -15.17% |
| flat (±0.3%/日) | +0.20% @ d3 | +1.81% @ d30 | +1.61% |
| pullback_then_recover | -5.88% @ d3 | +30.16% @ d30 | **+36.05%** |
| failed_trend (-8% in 30d) | -0.83% @ d3 | -8.00% @ d30 | -7.17% |
| drawdown_-30%_then_flat | -8.76% @ d3 | -12.00% @ d4 | -3.24% |
| **合計** | **-5.09%** | **+54.43%** | **+59.52%** |

**解釈**:

- S1 の core thesis (中期モメンタム持続) を最も評価できる `momentum_strong` /
  `momentum_mild` / `pullback_then_recover` で **bug は 10-36% の逸失利益**。
- `up_then_reverse` `failed_trend` `drawdown_flat` では bug が偶然早期切りで
  小さな損失で済むが、これらは spec が意図する戦略機会 (トレンド銘柄) ではない。
  短期的な誤脱出であって、統計的な "エッジ" とは呼べない。
- **7 scenario 単純合計で 59.52% の spec 優位**。実運用 universe (数百銘柄 × 数百日)
  では、モメンタム持続 scenario 側の頻度と絶対利益が支配的で、差はさらに拡大する見込み。

**サンプル出力**:
```
$ python3 outputs/s1_bug_microbench.py
Entry price = 100.00, Stop (5ATR ≈ -12%) = 88.00
Trailing pct (Case B/C) = 25%, max_hold_days (Case A bug) = 3
...
Sum of returns:
  Case A (bug 3-day):     -5.09%
  Case B/C (spec/hybrid): +54.43%
  Δ (spec - bug):         +59.52%
```

### 3.2 sandbox full backtest が走らせられなかった理由

- `data_cache/base/*.feather` は 15,692 銘柄分ある (十分)
- しかし `/sessions` partition が **9.8GB 中 9.2GB 使用 (残 50MB)** で
  `pyarrow` (48.8MB) の user-install が途中で切断。以降のインストール試行も
  disk pressure により中断。
- `data_cache/rolling/*.csv` は 373 バイト = ヘッダ + 直近 1 日のみで、
  時系列 backtest には不十分。

### 3.3 Windows 側 full backtest 再走手順

user 側 Windows で以下の順に走らせれば、pre/post の Sharpe / CAGR / MaxDD を
実データで測れる。**修正 patch は user 承認後に別 dispatch で当てる前提**。

```powershell
# 前提: repo root に居る、venv activate 済
cd C:\Repos\quant_trading_system_0510to0906

# 0. 現状 (bug 込み) baseline
python -m pytest tests/test_system1_strategy.py -v --no-header 2>&1 | tee _s1_pre_tests.log
# ↑ 現状 unit test の緑保存

# 1. 実データ backtest (現状 bug 版)
# integrated_backtest 経路 — 6 ヶ月 (~130 営業日) を想定
python -c "
from datetime import date
from common.integrated_backtest import IntegratedBacktester
from strategies.system1_strategy import System1Strategy
bt = IntegratedBacktester(start=date(2025,12,1), end=date(2026,6,30))
bt.add(System1Strategy())
res = bt.run(capital=100000)
print(res.summary())
res.trades.to_csv('bt_s1_pre_bug.csv', index=False)
"

# 2. Case 3 patch を dry-apply (別 dispatch で受け取った patch を local に当てる)
git apply --check outputs/d5_case3_hybrid.patch
git apply outputs/d5_case3_hybrid.patch

# 3. 修正後 backtest
python -c "
from datetime import date
from common.integrated_backtest import IntegratedBacktester
from strategies.system1_strategy import System1Strategy
bt = IntegratedBacktester(start=date(2025,12,1), end=date(2026,6,30))
bt.add(System1Strategy())
res = bt.run(capital=100000)
print(res.summary())
res.trades.to_csv('bt_s1_post_fix.csv', index=False)
"

# 4. 差分 summary
python -c "
import pandas as pd
pre = pd.read_csv('bt_s1_pre_bug.csv')
post = pd.read_csv('bt_s1_post_fix.csv')
def stats(df):
    tot = df['pnl'].sum()
    wr = (df['pnl'] > 0).mean() if len(df) else 0
    avg_hold = (pd.to_datetime(df['exit_date']) - pd.to_datetime(df['entry_date'])).dt.days.mean()
    return dict(trades=len(df), total_pnl=tot, win_rate=wr, avg_hold=avg_hold)
print('PRE (bug 3-day)  :', stats(pre))
print('POST (spec trail):', stats(post))
"

# 5. 3-scenario Monte Carlo (optional)
# 過去 3 年 × walk-forward 6 ヶ月 で 5 window を回す
python scripts/bench_pipeline_today.py --system system1 --start 2023-06-01 --end 2026-06-30 --window 180
```

期待挙動:
- `avg_hold` は PRE ≈ 3.0 日 → POST は 数十日 (spec の trend 保有目安)
- `trades` は POST が少なくなる (1 trade あたりの保有が伸びる)
- `total_pnl` は POST が **大幅に高くなる** はず (micro-bench の +59% 論理と一致)

---

## 4. 推奨判断

### 推奨 case: **Case 3 (hybrid)**

**理由**:
1. **Case 2 は subscriber 期待値と乖離した状態を追認する** ことになり、事業観点で NG。
   書籍 (25% trail) を売りにしている strategy を「実は 3 日で切ります」と説明する
   のは商品価値を毀損。
2. **Case 1 は最小 patch だが死コード掃除と regression test を欠く**。同じ bug が
   将来別 code path で復活する可能性を残す。
3. **Case 3 は Case 1 の全メリット + 死コード掃除 + regression test**。実装コスト差は
   `tests/test_system1_time_exit_regression.py` の +80 行程度で吸収可能。

### 実装優先度

前 audit の priority table では D5 が top 1 (backtest 破壊 bug、事業影響最大)。
今回の精査で **bug 範囲は S1 のみに絞られたが、S1 は long-bucket の 25% 配分**
なので影響は変わらず甚大。**優先度: 最高**。

### 実装順の推奨

1. Case 3 の patch を別 dispatch で作成 (touched files: 4 個、うち 1 個は新規 test)
2. patch を Windows 側で `git apply --check` → apply
3. 上記 §3.3 の Windows command で pre / post backtest 実施
4. `docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md` (本文書) に実データ backtest
   結果を追記 (Sharpe / CAGR / MaxDD の pre-post 数字)
5. 数字が想定通り (POST > PRE の CAGR、平均保有 3 日 → 20+ 日) なら merge

---

## 5. 補足: `SYSTEM_SPECIFIC_CONFIG` 削除で他のシステムに影響が出ないか

grep で参照元 0 件を確認 (§1)。個別 constant 
(`MAX_HOLD_DAYS_DEFAULT` / `FALLBACK_EXIT_DAYS_DEFAULT` / `STOP_ATR_MULTIPLE_*` /
`PROFIT_TAKE_PCT_DEFAULT_*` / `ENTRY_MIN_GAP_PCT_DEFAULT`) は S2/S3/S5/S6 の
`compute_exit` / `_compute_entry` で fallback として import しているため保持。

**削除するのは `SYSTEM_SPECIFIC_CONFIG` dict のみ**。副作用なし。

---

## 6. 制約 (この audit 中の遵守事項)

- 変更 patch は本文書に含めない (提案のみ)。実装は user 承認後、別 dispatch。
- `core/system1-7`, `common/trade_management.py::SYSTEM_TRADE_RULES`, signal logic は
  変更禁止 (audit 済 baseline)。触れるのは `strategies/system1_strategy.py`,
  `strategies/constants.py`, `config/config.yaml`, `tests/test_*` のみ。
- `git push` 禁止。sandbox 内の read/edit のみで、commit 経由での push は行っていない。

---

## Appendix A: micro-benchmark script

- `outputs/s1_bug_microbench.py` (211 行)
- `outputs/s1_bug_microbench_output.txt` (実行結果)

Case A (現状 bug) と Case B (spec) の compute_exit を独立に実装、7 scenario の
1-trade return を並べる。**実データ backtest ではなく、bug の directional 影響を
scenario 別に隔離する用途** の思考実験 script。

## Appendix B: 前 audit report との照合

| 前 audit 項目 | 前 audit 判定 | 本 audit 訂正判定 |
|---|---|---|
| S1 3-day exit bug | 事実 | ✅ 事実 (`compute_exit` fallback) |
| S4 3-day exit bug | 事実 | ❌ 誤認 (S4 は trailing のみ) |
| S6 time exit 欠落 | 事実 | ❌ 誤認 (YAML で 3 日設定済み) |
| S2 spec: 2 日 → SYSTEM_SPECIFIC_CONFIG: 3 日 mismatch | mismatch | ⚠️ SYSTEM_SPECIFIC_CONFIG が死コードなので mismatch 自体は runtime 影響なし。YAML は 2 日で spec 準拠。 |
| S7 dead time exit | mismatch | ⚠️ 死コードで影響なし。 |
| SYSTEM_SPECIFIC_CONFIG を Config A に一元化 | 推奨 | ✅ Config A 側は既に正しい。B を **削除** (統合ではなく撤去) が最小 patch。 |
