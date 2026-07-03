# Windows 側 commit + push 手順 (position management docs-alignment audit)

**dispatch**: 2026-07-03 position management / capital allocation / risk override docs-alignment
**target branch**: `claude/monitor-webapp`
**base commit**: `29e25f5`

## 変更 files (4)

- `common/alpaca_trading.py` — `_DEFAULT_SYSTEM_ORDER_TYPE` の S3/S5/S7 是正 (docs 準拠)
- `tests/test_position_management_docs_alignment_20260703.py` — **新規** 21 test (5 cluster)
- `tests/test_signals_to_orders.py` — S3/S5/S7 docs-alignment assertion 更新
- `docs/POSITION_MANAGEMENT_AUDIT_20260703.md` — **新規** audit report

---

## Windows PowerShell command

```powershell
cd C:\Repos\quant_trading_system_0510to0906

# 0. 現在の branch と作業状態を確認
git status
git branch --show-current  # → claude/monitor-webapp のはず

# 1. Sandbox で編集した変更を差分確認
git diff --stat
git diff common/alpaca_trading.py | Select-String "_DEFAULT_SYSTEM_ORDER_TYPE" -Context 5

# 2. 新規 test を含めて全変更を stage
git add common/alpaca_trading.py `
        tests/test_position_management_docs_alignment_20260703.py `
        tests/test_signals_to_orders.py `
        docs/POSITION_MANAGEMENT_AUDIT_20260703.md `
        docs/POSITION_MANAGEMENT_AUDIT_20260703_COMMIT_INSTRUCTIONS.md

# 3. pre-commit フックを走らせる前に一括フォーマット
make fmt
# または個別:
# python -m ruff check --fix .
# python -m black .
# python -m isort .

# 4. 再 stage (fmt で変更があれば)
git add -u

# 5. commit (F1/F2 の慣例に従い docs-alignment scope で 1 commit)
git commit -m "fix(alpaca): docs-align S3/S5=limit / S7=market default order type map`
`
- common/alpaca_trading.py::_DEFAULT_SYSTEM_ORDER_TYPE`
  - system3: market -> limit (docs=前日終値-7%指値買)`
  - system5: market -> limit (docs=前日終値-3%指値買)`
  - system7: limit -> market (docs=翌日寄付成行 catastrophe hedge)`
- tests/test_position_management_docs_alignment_20260703.py: new`
  21 regression tests / 5 cluster (allocation / trade_rules /`
  order_type / signals_to_orders / dedup behavior lock-in)`
- tests/test_signals_to_orders.py: update S3/S5/S7 assertions`
- docs/POSITION_MANAGEMENT_AUDIT_20260703.md: audit report`
  (matrix, gap fill, future consideration for portfolio drawdown /`
   cross-system dedup / sector cap / total position cap)`
`
docs-driven fix。SYSTEM_TRADE_RULES / DEFAULT_LONG/SHORT_ALLOCATIONS /`
risk / max_pct / max_positions / trailing / profit target / stop /`
holding は既に docs 準拠 (verify only, regression test で lock-in)。`
`
runtime fallback (limit_price=None -> market) 維持で誤発注防止。`
signals_json_to_orders (tier notional 経路) は order_type=market 固定`
なので影響外。"

# 6. push (direct origin claude/monitor-webapp)
git push origin claude/monitor-webapp

# 7. push 後の verify
git log --oneline -5
```

---

## 期待挙動 (07-04 tick 後)

### 直後の verify (push 完了時)

```powershell
# 新規 test が全 pass
python -m pytest tests\test_position_management_docs_alignment_20260703.py -v --tb=short
# 期待: 21 passed

# 既存 test の regression 0
python -m pytest tests\test_signals_to_orders.py -v --tb=short
# 期待: 全 pass (S3/S5/S7 assertion 更新済み)

# 全 order type map が docs 通り
python -c "from common.alpaca_trading import _DEFAULT_SYSTEM_ORDER_TYPE as m; import json; print(json.dumps(m, indent=2))"
# 期待:
#   { "system1": "market", "system2": "limit", "system3": "limit",
#     "system4": "market", "system5": "limit", "system6": "limit",
#     "system7": "market" }
```

### 07-04 06:00 JST daily tick 後の verify

```powershell
# 1. daily_paper_trade.py で S3/S5/S7 の entry order type を確認
python scripts\daily_paper_trade.py --dry-run 2>&1 | Select-String "order_type|system[357]"

# 2. logs\alpaca_orders_YYYYMMDD.log で actual order_type を確認
Get-Content logs\alpaca_orders_20260704.log -Tail 30 | ConvertFrom-Json |
    Where-Object { $_.system -in @("system3","system5","system7") } |
    Select-Object symbol, system, order_type, limit_price, side

# 期待:
#   system3: order_type=limit (entry_price 付) or market (entry_price 無 = fallback)
#   system5: order_type=limit (entry_price 付) or market (entry_price 無 = fallback)
#   system7: order_type=market 常時 (limit_price=None)

# 3. Vercel dashboard で当日 signal を subscribers-facing view で確認
# https://quant-trading-monitor.vercel.app
```

---

## Rollback 手順

もし想定外の挙動 (S3/S5 で limit 未成立が多発など) が確認されたら:

```powershell
# 単一 commit revert
git revert HEAD
git push origin claude/monitor-webapp

# または hard reset (未 push 状態のみ)
git reset --hard 29e25f5
```

---

## D5 defer 中の bug (未対応、user 判断待ち)

`docs/D5_SYSTEM_SPECIFIC_CONFIG_bug_20260702.md` の S1 backtest 3-day 強制決済 bug は
本 dispatch でも defer。runtime signal 経路は spec 通りなので **live paper 影響なし**。
backtest 経路 (`strategies/system1_strategy.py::compute_exit`) の Case 3 hybrid 修正は
別 dispatch で user 承認後に実施予定。
