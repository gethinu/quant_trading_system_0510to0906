---
description: Project context and reference documentation for AI coding assistance
---

# Quant Trading System - Project Reference

## 自動参照

このワークフローはチャット開始時に参照する主要ドキュメントを定義します。

## 主要ドキュメント

1. **プロジェクト概要**: `docs/README.md` - システム構成、テスト、運用ガイド
2. **技術仕様**: `docs/TECHNICAL_SPECS.md` - System1-7 の詳細仕様
3. **環境変数**: `docs/technical/environment_variables.md`
4. **LLM Spec**: `docs/llm/SPEC.md` - Product intent and invariants
5. **LLM Architecture**: `docs/llm/ARCHITECTURE.md` - System structure and data flow
6. **LLM Interfaces**: `docs/llm/INTERFACES.md` - CLI, UI, and API contracts
7. **LLM State**: `docs/llm/STATE.md` - State locations and lifecycle

## プロジェクト構造

```
├── apps/
│   ├── api/main.py           # FastAPI バックエンド (port 8000)
│   ├── dashboards/
│   │   ├── alpaca-next/      # Next.js ダッシュボード (port 3000)
│   │   └── app_integrated.py # Streamlit 統合 UI (port 8501)
├── core/                      # システム純ロジック (system1-7.py)
├── scripts/
│   ├── run_all_systems_today.py  # 当日パイプライン
│   └── daily_paper_trade.py      # Alpaca ペーパートレード
├── common/                    # 共通ユーティリティ
├── config/                    # 設定管理
└── tests/                     # テストスイート
```

## テストモード早見表

| モード         | 銘柄数 | 実行時間 | 再現性 | 主な用途                  |
| -------------- | ------ | -------- | ------ | ------------------------- |
| `test_symbols` | 113    | 約 1 分  | 100%   | 再現性重視の統合テスト    |
| `mini`         | 10     | 約 2 秒  | 中     | 超高速スモーク            |
| `quick`        | 50     | 約 10 秒 | 中     | 並列処理や CSV 保存の検証 |
| `sample`       | 100    | 約 30 秒 | 中     | 中規模データでの統合検証  |

## パイプライン実行

```powershell
# テスト用（推奨）
python -m scripts.run_all_systems_today --test-mode test_symbols --skip-external --save-csv

# 本番用
python -m scripts.run_all_systems_today --parallel --save-csv
```

## ダッシュボード起動

```powershell
# 統合起動スクリプト
.\Start-Dashboard.ps1

# または個別起動
# FastAPI
python -m uvicorn apps.api.main:app --reload --port 8000

# Next.js
cd apps\dashboards\alpaca-next
npm run dev -- --port 3000
```

## CSV 出力先

- **シグナル CSV**: `results_csv/signals_systemX_YYYY-MM-DD.csv`
- **日次メトリクス**: `results_csv/daily_metrics.csv`
- **ペーパートレードログ**: `results_csv/paper_trade_log_*.csv`
- **進捗ログ**: `logs/progress_today.jsonl`

## AI コーディングガイド

1. 変更前に Context Note（ファイル先頭）を確認
2. テスト: `python scripts/run_controlled_tests.py`
3. 検証ループ: `python tools/auto_refine_loop.py`
