# Quant Trading System (Streamlit)

![CI Unified](https://github.com/r-estoer3588/quant_trading_system_0510to0906/workflows/CI%20Unified/badge.svg)
![Coverage Report](https://github.com/r-estoer3588/quant_trading_system_0510to0906/workflows/Coverage%20Report/badge.svg)
![Python Version](https://img.shields.io/badge/python-3.11-blue.svg)
![License](https://img.shields.io/badge/license-Private-red.svg)

Streamlit ベースのアプリで 7 つの売買システムを可視化・バックテストします。

## 📚 このプロジェクトについて

このリポジトリは、**教育目的**で開発された米国株自動売買システムのフレームワークです。

### ⚠️ 重要な注意事項

- **本プロジェクトは教育・学習目的です**
- **投資助言ではありません**
- **リアルマネー運用実績はありません**
- **ペーパートレード（仮想取引）での検証環境です**

投資は自己責任で行ってください。本プロジェクトを利用した結果について、開発者は一切の責任を負いません。

### 🎯 学習目標

このプロジェクトを通じて、以下のスキルを習得できます：

- Pythonでの株式データ分析
- API連携（EOD Historical Data, Alpaca）
- バックテストと戦略検証
- Streamlitでの可視化
- CI/CDとテスト駆動開発

### 💡 誰のための教材か

- Pythonで自動売買を学びたい方
- データ分析スキルを実践で磨きたい方
- ペーパートレードで安全に学習したい方
- 投資とプログラミングを同時に学びたい方

## 📚 ドキュメント

詳細なドキュメントは **[docs/](./docs/)** フォルダに整理されています：

- 🚀 **[クイックスタート](./docs/#quick-start)** - 初回セットアップから基本操作まで
- 📊 **[システム仕様](./docs/systems/INDEX.md)** - System1-7 の詳細仕様と資産配分
- 🔧 **[技術文書](./docs/technical/INDEX.md)** - アーキテクチャ・指標計算・処理フロー
- 🏃 **[運用ガイド](./docs/operations/INDEX.md)** - 自動実行・通知・監視設定
- 🤖 **[AI 連携](./docs/technical/mcp_integration_plan.md)** - MCP 統合と VS Code 連携

> **[📋 統合ドキュメント目次はこちら](./docs/README.md)**

---

## 🎉 新機能: Diagnostics API

Phase0-7 で導入された **Diagnostics API** により、各システムの候補生成プロセスを詳細に追跡できるようになりました。

### 主な診断キー

すべてのシステム(System1-7)で以下の統一キーが取得できます:

- `setup_predicate_count`: Setup 条件を満たした行数
- `ranked_top_n_count`: 最終候補件数(ランキング後)
- `ranking_source`: `"latest_only"` または `"full_scan"`

これにより、候補がどのように絞り込まれたかを明確に把握でき、トラブルシューティングや検証が容易になります。

### 使用例

```python
from core.system1 import generate_system1_candidates

candidates, diagnostics = generate_system1_candidates(df, current_date, latest_only=True)

print(diagnostics)
# {
#   "ranking_source": "latest_only",
#   "setup_predicate_count": 5,
#   "ranked_top_n_count": 3,
#   ...
# }
```

### Snapshot Export

Mini パイプライン実行後に診断情報を JSON 形式でエクスポート可能:

```bash
python scripts/run_all_systems_today.py --test-mode mini --skip-external
cat results_csv_test/diagnostics_snapshot_*.json | jq '.systems'
```

### 差分比較ツール

2 つのスナップショットを比較して、診断情報の変化を検出:

```bash
python tools/compare_diagnostics_snapshots.py \
  --baseline baseline.json \
  --current current.json \
  --output diff.json \
  --summary
```

**詳細は [docs/technical/diagnostics.md](./docs/technical/diagnostics.md) を参照してください。**

---

## 📊 テストカバレッジ

プロジェクトのコード品質を維持するため、テストカバレッジを継続的に計測・改善しています。

### カバレッジ目標と現状

| モジュール          | 目標   | 現状    | ステータス             |
| ------------------- | ------ | ------- | ---------------------- |
| `core/system7.py`   | 65%    | **66%** | ✅ 達成済み (41 tests) |
| `core/system1-6.py` | 60-65% | -       | 🎯 計画中              |
| `common/*.py`       | 70%    | -       | 🎯 計画中              |
| `strategies/*.py`   | 65%    | -       | 🎯 計画中              |

### System7 カバレッジ達成 (2025 年 10 月 11 日)

- **開始時**: 53% (132/247 lines)
- **最終**: 66% (162/247 lines)
- **改善**: +13 ポイント
- **テスト構成**: 4 つの公式テストファイル、合計 41 テスト
  - `test_system7_branches.py`: 16 tests, 89% coverage
  - `test_system7_latest_only.py`: 10 tests, 87% coverage
  - `test_system7_error_cases.py`: 9 tests, 96% coverage
  - `test_system7_full_scan.py`: 6 tests, 98% coverage

### カバレッジレポートの確認方法

#### ローカルでの確認

```bash
# HTMLレポート生成
pytest --cov=core --cov=common --cov=strategies \
  --cov-report=html:htmlcov \
  --cov-report=term-missing

# ブラウザで確認
open htmlcov/index.html  # macOS/Linux
start htmlcov/index.html # Windows
```

#### CI/CD での自動生成

PR 作成時に自動的にカバレッジレポートが生成され、以下が実行されます:

1. **カバレッジ計測**: pytest-cov で全モジュールを計測
2. **HTML レポート**: アーティファクトとしてアップロード（30 日間保持）
3. **PR コメント**: カバレッジサマリーを自動投稿
4. **しきい値チェック**: System7 が 66% 以上を維持しているか確認

#### GitHub Actions アーティファクト

各 PR ビルドで以下のアーティファクトをダウンロード可能:

- `coverage-report-html`: HTML 形式の詳細レポート
- `coverage-report-xml`: XML 形式（CI 統合用）
- `coverage-cache`: main ブランチのカバレッジ履歴

### カバレッジ設定

カバレッジの詳細設定は `pyproject.toml` の `[tool.coverage.*]` セクションで管理されています:

- **対象**: `core/`, `common/`, `strategies/`, `config/`, `schedulers/`
- **除外**: テストファイル、キャッシュ、生成ファイル
- **ブランチカバレッジ**: 有効
- **並列実行**: サポート

---

## 🤖 自動化機能

このプロジェクトは **AI 駆動開発** を重視し、以下の自動化機能を統合しています：

### 開発自動化

- ✅ **プロジェクトルールチェック**: CacheManager 経由のアクセス、System7 SPY 固定などを自動検証
- ⏱️ **パフォーマンス回帰検知**: mini テストのベンチマーク計測、10% 劣化で警告
- 📸 **スナップショットテスト**: 出力 CSV の変化を自動検出、Git commit と紐付け
- 🔍 **pre-commit/pre-push フック**: コミット・プッシュ時に自動品質チェック

### 運用自動化

- 🚀 **日次シグナル自動実行**: Windows タスクスケジューラで毎日 10:00 JST に自動生成
- 📊 **Slack 通知**: 成功/エラーを自動通知（`#trading-signals`, `#trading-errors`）
- ☁️ **GitHub Actions**: クラウドでのシグナル生成、ドキュメント更新、セキュリティ監査
- 💰 **Alpaca Paper Trading**: 仮想トレード実績の自動蓄積（詳細は下記）

### ドキュメント自動化

- 📝 **API ドキュメント生成**: docstring から markdown を自動生成（`docs/api/`）
- 📊 **システム仕様書生成**: System1-7 の仕様を自動抽出（`docs/systems_auto/`）
- 🔄 **GitHub Actions 連携**: コード変更時に自動でドキュメント更新

**詳細**: [docs/automation/AUTOMATION_GUIDE.md](./docs/automation/AUTOMATION_GUIDE.md)

---

## セットアップ

1. 仮想環境を作成し依存関係をインストール:

```bash
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
```

2. `.env` を用意し、日次データ取得用に `ALPACA_API_KEY` と `ALPACA_SECRET_KEY`
   を設定します（ブローカー連携を行う場合は `APCA_API_KEY_ID` /
   `APCA_API_SECRET_KEY` も設定）。

### 主要な環境変数

- `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`: 日次 OHLCV 取得用（**必須**、無料 IEX feed）
- `ALPACA_FEED`: データ feed（既定 `iex`。`sip` は有料）
- `EODHD_API_KEY`: EOD Historical Data API キー（**deprecated**、legacy スクリプト用に残置）
- `APCA_API_KEY_ID`, `APCA_API_SECRET_KEY`: Alpaca ブローカー連携用（**Paper Trading対応済み**）
- `ALPACA_PAPER`: `true` でペーパートレード、`false` で本番取引（デフォルト: `true`）
- `SLACK_WEBHOOK_URL` または `SLACK_BOT_TOKEN`+`SLACK_CHANNEL`: Slack 通知設定
- `DISCORD_WEBHOOK_URL`: Discord 通知設定

## データソース (Data source)

日次 OHLCV を **無料データソースに移行**し EODHD 有料 $20/月 → $0 化を進めています。
プロバイダは同一スキーマ（columns / index / dtypes）の drop-in replacement として実装され、
旧 `get_eodhd_data(symbol)` と透過的に差し替え可能です。

### 推奨: Polygon.io 無料 tier（本命・full-market volume）

- **取得関数**: `common/polygon_data.py`
  - `get_polygon_data(symbol)` — 単一銘柄の日次 OHLCV（EODHD 同一スキーマ）
  - `get_polygon_grouped_daily(date)` — **1 call で全 US 銘柄の日足**（dv20/dv50 pre-compute 用）
- **volume**: SIP 連結（全取引所 + FINRA/OTC/ATS）の **full-market**。min-ADV / 流動性フィルタが正常動作。
- **料金**: **$0**。レート制限 5 req/min（Grouped Daily は 1 call/日で全銘柄のため実質非制約）。
- **API キー取得先（無料）**: <https://polygon.io/dashboard/signup>
- **環境変数**: `POLYGON_API_KEY`（未設定なら `ValueError` で fail-fast）。
- **AdjClose**: `adjusted=true`（split/dividend 調整済）。Close は `adjusted=false`（raw）。

### 補助: Alpaca IEX feed（発注連携用途／非推奨・株データ本番不可）

- **取得関数**: `common/alpaca_data.py` の `get_alpaca_data(symbol)`。
- **環境変数**: `ALPACA_API_KEY`, `ALPACA_SECRET_KEY`。

> ⚠️ **既知の制限（IEX feed の出来高過小・実証済）**
> IEX feed の出来高は全市場の **約 2〜4% しか反映されません**（実測: 真値の約 1/26）。
> 確定候補 27 銘柄で min-ADV / 生株数フィルタ生存率が **12〜25%** まで低下することを実証済。
> このため **株の日次データ本番運用には非推奨**。full-market volume が要る用途では
> **Polygon.io（上記・無料）** を使用してください。SIP 切替（`ALPACA_FEED=sip`）は
> 有料 $99/月で EODHD より高コストになるため退行です。
>
> `common/stooq_data.py`（stub）は API 無し・daily hits quota・ティッカーマッピング困難のため却下。

### Bulk API 品質設定（2025-10-12 改善）

日次更新で使用する Bulk API のデータ品質を環境変数で調整できます：

- `BULK_API_VOLUME_TOLERANCE`: Volume 差異許容範囲（デフォルト: 5.0%）
- `BULK_API_PRICE_TOLERANCE`: 価格差異許容範囲（デフォルト: 0.5%）
- `BULK_API_MIN_RELIABILITY`: Bulk API 使用の最低信頼性スコア（デフォルト: 70.0%）

**📘 詳細**: [Bulk API クイックスタート](./docs/BULK_API_QUICK_START.md) を参照してください。

### ログ・進捗関連の環境変数

- `COMPACT_TODAY_LOGS`: 当日パイプラインの詳細ログを抑制（デフォルト: false）
- `ENABLE_PROGRESS_EVENTS`: 進捗イベント出力を有効化（デフォルト: false）
- `TODAY_SIGNALS_LOG_MODE`: ログファイル名形式（`single` または `dated`）
- `ROLLING_ISSUES_VERBOSE_HEAD`: Rolling キャッシュ問題の詳細表示件数
- `RUN_PLANNED_EXITS`: 自動手仕舞い実行モード（`off`/`open`/`close`/`auto`）

## 実行例

### 基本実行

- UI: `streamlit run apps/app_integrated.py`
- Alpaca ダッシュボード: `streamlit run app_alpaca_dashboard.py`
- 当日パイプライン: `python scripts/run_all_systems_today.py --parallel --save-csv`
- **🔥 Alpaca Paper Trading**: `python scripts/daily_paper_trade.py --dry-run` （詳細は下記）

### ログ最適化機能

当日パイプラインのログ出力を制御する環境変数：

```bash
# コンパクトログモード（詳細ログをDEBUGレベルに変更）
COMPACT_TODAY_LOGS=1 python scripts/run_all_systems_today.py

# 進捗イベント出力有効化（UIでリアルタイム監視可能）
ENABLE_PROGRESS_EVENTS=1 python scripts/run_all_systems_today.py

# Rolling キャッシュ問題の詳細表示件数制限
ROLLING_ISSUES_VERBOSE_HEAD=5 python scripts/run_all_systems_today.py

# 全て同時使用
COMPACT_TODAY_LOGS=1 ENABLE_PROGRESS_EVENTS=1 ROLLING_ISSUES_VERBOSE_HEAD=3 \
    python scripts/run_all_systems_today.py --parallel --save-csv
```

### その他の実行例

- 日次キャッシュ: `python scripts/cache_daily_data.py`
  - 並列度調整: `--max-workers 20` (デフォルト: 20)
  - API 取得並列度: `--fetch-workers 1` (デフォルト: 1、順次実行でレート制限遵守)
  - 保存並列度: `--save-workers` (デフォルト: max_workers)
  - スロットリング: `--throttle-seconds 0.0667` (デフォルト: 0.0667 秒、約 15req/sec、公式制限 1000req/min 以内に収まるよう調整)
  - 進捗表示間隔: `--progress-interval 300` (デフォルト: 300 件、指定件数ごとに進捗を表示)
  - 注意: 引数を指定しない `python scripts/cache_daily_data.py` の既定実行は
    全銘柄の全ヒストリカルデータを取得する（フル取得）。当日の一括（Bulk）更新を
    実行したい場合は `--bulk-today` を指定するか、`scripts/update_from_bulk_last_day.py`
    を直接実行してください。
- 簡易スケジューラ: `python -m schedulers.runner`

### リアルタイム進捗監視

`ENABLE_PROGRESS_EVENTS=1` で当日パイプラインを実行中、同時に Streamlit UI で進捗をリアルタイム監視できます：

1. ターミナル 1: 当日パイプライン実行

   ```bash
   ENABLE_PROGRESS_EVENTS=1 python scripts/run_all_systems_today.py --parallel
   ```

2. ターミナル 2: UI 起動

   ```bash
   streamlit run apps/app_integrated.py
   ```

3. UI の「当日シグナル」タブで「進捗ログを表示」をチェック
   - `logs/progress_today.jsonl` を 1 秒間隔でポーリング
   - システム実行開始、配分処理、通知完了などの主要イベントをリアルタイム表示
   - 各システムの候補数、エントリ数、現在ポジション数などの詳細情報も含む

### ログ最適化の効果

- **通常モード**: 全詳細ログが INFO レベルで出力され、大量のメッセージが表示される
- **コンパクトモード (`COMPACT_TODAY_LOGS=1`)**:
  - システム内訳や進捗詳細が DEBUG レベルに変更
  - レート制限により同種メッセージが適切な間隔（2-10 秒）で出力
  - 重要な情報（エラー、最終結果など）は引き続き INFO レベルで表示
  - ログファイルサイズが約 60-80%削減（実測値）

---

## 🔥 Alpaca Paper Trading 連携

システムが生成したシグナルを **Alpaca Paper Trading API** へ自動送信し、仮想トレード実績を蓄積できます。

### セットアップ

1. [Alpaca](https://alpaca.markets/) でアカウント作成（無料）
2. Paper Trading 用の API キーを取得
3. `.env` に以下を追加：

```bash
APCA_API_KEY_ID=your_key_id_here
APCA_API_SECRET_KEY=your_secret_key_here
ALPACA_PAPER=true  # ペーパートレード（仮想）モード
```

### 実行方法

```bash
# Dry-run（注文を送信せず確認のみ）
python scripts/daily_paper_trade.py --dry-run

# 実際にペーパートレード実行
python scripts/daily_paper_trade.py

# フルモード（全銘柄対象）
python scripts/daily_paper_trade.py --mode full --verbose
```

### 主な機能

- ✅ **全システム統合**: System1-7 のシグナルを自動送信
- ✅ **注文タイプ対応**: システム別に Market / Limit を自動選択
- ✅ **ログ記録**: `results_csv/paper_trade_log_*.csv` に注文履歴を保存
- ✅ **通知連携**: Slack/Discord へ注文結果を自動通知
- ✅ **エラーハンドリング**: リトライ機能とレート制限対応

### 自動化（オプション）

毎日市場終了前（15:45 ET）に自動実行する設定：

```powershell
# Windows タスクスケジューラ
$action = New-ScheduledTaskAction `
    -Execute "python" `
    -Argument "c:\Repos\quant_trading_system\scripts\daily_paper_trade.py" `
    -WorkingDirectory "c:\Repos\quant_trading_system"

$trigger = New-ScheduledTaskTrigger -Daily -At "15:45"

Register-ScheduledTask `
    -TaskName "AlpacaPaperTrade" `
    -Action $action `
    -Trigger $trigger
```

**📚 詳細ガイド**: [docs/ALPACA_PAPER_TRADING.md](./docs/ALPACA_PAPER_TRADING.md)

---

## テスト

### 単体テスト

```bash
# 全テスト実行
pytest -q

# カバレッジ測定
pytest --cov=core --cov=common --cov-report=term-missing

# System7のみ
pytest tests/test_*system7*.py --cov=core.system7 --cov-report=term-missing
```

**📚 詳細**: [tests/README.md](./tests/README.md) - テスト決定性、トラブルシューティング、ベストプラクティス

#### テスト品質保証

- ✅ **決定性保証**: `conftest.py`の自動フィクスチャによりテスト再現性 100%
- ✅ **並列実行対応**: pytest-xdist 互換（テスト間干渉なし）
- ✅ **カバレッジ目標**: core/system7.py = 53% (目標達成)

### パイプライン高速テスト

当日シグナル パイプライン (`scripts/run_all_systems_today.py`) には複数のテストモードがあります。最新の制御テスト環境は `docs/testing.md` に詳細をまとめています。

#### テストモード早見表

| モード         | 銘柄数 | 実行時間 | 再現性 | 主な用途                  |
| -------------- | ------ | -------- | ------ | ------------------------- |
| `test_symbols` | 113    | 約 1 分  | 100%   | 再現性重視の統合テスト    |
| `mini`         | 10     | 約 2 秒  | 中     | 超高速スモーク            |
| `quick`        | 50     | 約 10 秒 | 中     | 並列処理や CSV 保存の検証 |
| `sample`       | 100    | 約 30 秒 | 中     | 中規模データでの統合検証  |

共通オプションとして `--skip-external` (外部 API 省略) と `--benchmark` (実行時間記録) を組み合わせると、CI/CD でも安定した計測が可能です。

#### 制御テスト環境の使い方

```bash
# テスト銘柄生成 (初回のみ)
python tools/generate_test_symbols.py

# 制御データでパイプライン実行
python scripts/run_all_systems_today.py --test-mode test_symbols --skip-external --benchmark

# 生成、パイプライン、検証を一括で実行
python run_controlled_tests.py
```

制御テストではフィルター、セットアップ、ランキングの各分岐を網羅する 113 銘柄が自動生成され、TRDlist と Entry が必ず 10 銘柄ずつ出力されます。結果として得られる診断 JSON、ベンチマーク JSON、TRDlist CSV を `run_controlled_tests.py` が検証し、差異があれば終了コードを 1 にします。

その他のモードは、開発中のスモーク (`mini`)、並列処理や CSV 保存機能の検証 (`quick`)、より本番に近い負荷試験 (`sample`) に活用できます。

## 設定

優先順位は **JSON > YAML > .env**（`config/settings.py` 実装に準拠）。
推奨: `config.yaml` をベースに、秘匿値は `.env`、上書きは JSON で。

## ログ運用

`logging_utils` にてローテーション設定。容量上限と日次ローテの使い分けを明記し、
古いログのクリーンアップ方針を追加。

## ディレクトリ構成

```
├── apps/              # Streamlit UI
│   ├── app_integrated.py    # 統合ダッシュボード
│   └── dashboards/          # 専用ダッシュボード
├── strategies/        # 戦略ラッパークラス
├── core/             # システム純ロジック (system1.py - system7.py)
├── common/           # 共通ユーティリティ・バックテスト基盤
├── config/           # 設定管理 (settings.py, config.yaml)
├── docs/             # 📚 **統合ドキュメント** (詳細仕様・技術文書)
│   ├── systems/           # System1-7仕様書
│   ├── technical/         # アーキテクチャ・指標計算
│   ├── operations/        # 運用・自動実行・通知
│   └── today_signal_scan/ # シグナル処理8フェーズ
├── data/             # 設定ファイル・マッピング
├── scripts/          # 実行スクリプト
└── tests/            # テストスイート
```

> **詳細は [📋 docs/README.md](./docs/README.md) を参照**

## データストレージ最適化

### デュアルフォーマット対応

- `data_cache/rolling/`：CSV + Feather 同時保存
- パフォーマンス：Feather 優先読み取り、CSV 自動フォールバック
- ファイルサイズ：Feather は CSV の約 74%（重複列削除効果）
- 対応範囲：6,200+銘柄すべて

### 重複列クリーンアップ

- 削除対象：`open/Open/OPEN` 等の冗長列（約 7 列）
- 優先順位：PascalCase > ALL_CAPS > lowercase
- データ削減：列数を約 40%削減（58→35 列）

## キャッシュ階層（base / rolling / full_backup）

- base: 指標付与済みの長期データ（バックテスト・分析の既定）。
  - 読み込みが最速。欠損時は内部で再構築されます（full_backup/rolling から）。
- rolling: 直近 N 営業日（既定 300）の軽量データ（当日シグナル抽出用）。
  - 無ければ base から必要分を生成して保存します。
- full_backup: 取得元そのままの長期バックアップ（原本）。
  - 通常は読みません。復旧や base 再構築のソースとしてのみ使用します。

解決ポリシー（SPY 含む）:

- backtest: base → full_backup（rolling は参照しない）。
- today: rolling → base → full_backup（rolling が無ければ base から生成して保存）。

補足:

- 旧来の `data_cache/` 直下ファイルは参照しません（移行済みを前提）。
- SPY フル履歴の復旧は `scripts/recover_spy_cache.py` が `data_cache/full_backup/` のみへ保存します。

## 型チェックと静的解析 (Windows / UTF-8 対応)

Windows 環境 (cp932) で `mypy` 実行時にエンコード例外が発生するケースがあったため、UTF-8 強制ラッパーを用意しています。

### 使い方 (部分チェック)

```bash
python tools/mypy_utf8_runner.py core/system7.py core/system6.py --no-incremental
```

### 使い方 (主要システムまとめチェック)

```bash
python tools/mypy_utf8_runner.py core/system1.py core/system2.py core/system3.py core/system4.py core/system5.py core/system6.py core/system7.py --no-incremental
```

オプション `--no-incremental` はキャッシュ破損や KeyError 回避のため開発中は推奨。高速化したい場合は省略可能です。

### ruff (Lint)

```bash
ruff check .
```

修正を自動適用したい場合:

```bash
ruff check --fix .
```

### 品質チェックの自動化

プロジェクトは GitHub Actions で自動品質チェックを行います：

- **CI Unified**: lint(format)/test/coverage を一本化（`.github/workflows/ci-unified.yml`）
- **Coverage Report**: カバレッジ詳細レポートと PR コメント（`.github/workflows/coverage-report.yml`）
- 旧ワークフロー（CI / CI Quality Gate / Auto Fix Quality）は退役し、呼び出し専用に変更しました。

## 当日シグナル高速化 (latest_only 最適化)

Systems 1–7 全てで当日シグナル抽出 (`scripts/run_all_systems_today.py`) 時に `latest_only` 最適化を有効化しました。これにより:

- 当日処理: 最終行のみ参照してセットアップ成立可否を判定 (O(銘柄数))
- バックテスト: 従来通り全履歴走査 (ロジック不変)
- 返却スキーマ: すべて `{date: {symbol: payload}}` に統一

バックテスト等で従来挙動を強制したい場合は戦略 `generate_candidates` 呼び出しで `latest_only=False` を明示してください。

## 貢献ガイド

- コミットメッセージは命令形・現在形で 72 文字以内。
- 変更後は `pytest -q` を実行してテストを確認してください。

### コード品質管理

#### pre-commit フックのセットアップ（必須）

初回セットアップ時に必ず pre-commit フックをインストールしてください：

```bash
pip install pre-commit
pre-commit install
pre-commit install --hook-type pre-push
```

#### 自動フォーマット

VS Code 使用時は保存時に自動フォーマットが適用されます（`.vscode/settings.json` で設定済み）。他のエディタを使用する場合は、コミット前に以下を実行してください：

```bash
# 全ファイルをフォーマット
black .
ruff check --fix .
isort .
```

#### PR 前チェック（重要）

CI は `isort --check-only .` を実行します。ローカルで自動 isort フックを無効化している場合は、PR を作成する前に手動で import 整理を行ってください。そうしないと CI で import 順序の差分が原因で PR が落ちます。

```bash
# PR 前に実行
python -m isort .
git add -u
git commit -m "style: apply isort before PR"
```

この運用で、ローカルコミットは止まらず、CI でも import 順序の一貫性が保たれます。

#### コミット前チェック

pre-commit フックが以下を自動実行します：

- **pre-commit ステージ**: ruff、black、isort、基本チェック（trailing whitespace 等）
- **pre-push ステージ**:
  - mini パイプラインテスト（core/common 変更時）
  - 品質集計（ruff/mypy/pytest/bandit/radon）
  - **black フォーマット厳格チェック**（フォーマット漏れを防止）

#### 手動フックの使い分け（PowerShell）

用途別にローカルで個別のチェックを素早く回せます。

- Ruff（静的解析、軽微な修正の自動適用）

  ```powershell
  # チェックのみ
  ruff check .

  # 自動修正も適用
  ruff check --fix .
  ```

- mypy（UTF-8 強制ランナーで Windows のコードページ問題を回避）

  ```powershell
  # 指定ファイルのみ（例: core/system7.py）
  python tools/mypy_utf8_runner.py core/system7.py --no-incremental

  # 主要システムまとめチェック
  python tools/mypy_utf8_runner.py core/system1.py core/system2.py core/system3.py core/system4.py core/system5.py core/system6.py core/system7.py --no-incremental
  ```

- pytest（クイックサブセット、短いトレース）

  ```powershell
  C:\Repos\quant_trading_system\venv\Scripts\python.exe -m pytest -q --tb=short -k "not slow and not integration"
  ```

ヒント:

- まとまった変更の前後は `ruff --fix` → `black` → `isort` → `pytest -q` の順で回すとノイズが減ります。
- エディタの型警告は Pylance（basic）で軽めに、厳密チェックは mypy を使用します。

#### フォーマット問題の回避

大量のファイル編集後は以下の手順でコミットしてください：

1. 変更をステージ: `git add -u`
2. コミット実行: `git commit -m "message"`
3. pre-commit が black でフォーマット → 自動修正される
4. 再度ステージ: `git add -u`
5. 再コミット: `git commit -m "Apply black formatting"`

または、コミット前に手動フォーマット：

```bash
black .
git add -u
git commit -m "message"
```

#### pre-commit バイパスの禁止

`git commit --no-verify` は使用しないでください。品質チェックをバイパスすると、CI で失敗する可能性があります。

---

### 🆕 フォーマット・コミット運用の標準手順（2024 年 10 月更新）

#### 1. 自動フォーマットの推奨手順

コミット前に、**必ず下記コマンドで全ファイルを一括整形**してください。

```bash
make fmt
# または Windows PowerShell では
python -m ruff check --fix .
python -m black .
python -m isort .
```

`make fmt` は `ruff --fix` → `black` → `isort` の順で全ファイルを自動整形します。

#### 2. pre-commit フックの動作と注意点

- pre-commit フックは「**チェック専用モード**」で動作します（black/isort/ruff すべて check-only）。
- フォーマット漏れがあると「files were modified by this hook」と表示され、コミットがブロックされます。
- その場合は `make fmt` を再実行し、**必ず `git add -u` で再ステージ**してからコミットしてください。

#### 3. 典型的なコミット手順

```bash
# 変更を保存
make fmt
git add -u
git commit -m "メッセージ"
# フックで修正が入った場合
make fmt
git add -u
git commit -m "Apply formatting"
```

#### 4. トラブル時の対処

- 何度も「files were modified by this hook」が出る場合は、
  - 1. `make fmt` を 2 回以上実行
  - 2. `git add -u` で再ステージ
  - 3. それでも直らない場合はエディタの自動改行・BOM・改行コード設定を確認
- **pyproject.toml はルートのみ有効**（`common/`等に重複がないか確認）
- `.gitattributes` で `* text=auto eol=lf` を強制しています（BOM 混入や CRLF 混在を防止）

#### 5. 参考: Makefile の fmt ターゲット

```makefile
fmt:
	$(PYTHON) -m ruff check --fix .
	$(PYTHON) -m black .
	$(PYTHON) -m isort .
```

---

#### pre-push フックの動作確認

初回セットアップ後、pre-push フックが正しく動作するか確認してください：

```bash
# テストコミットを作成
echo "test" > test_file.txt
git add test_file.txt
git commit -m "Test pre-push hook"
git push  # ここで pre-push フックが実行されます
```

pre-push 時に以下のチェックが実行されます：

- Mini パイプラインテスト（core/common 変更時のみ）
- 品質集計（ruff/mypy/pytest/bandit/radon）
- **Black フォーマット厳格チェック**（全ファイル検証）

---

## ⚖️ 免責事項

### 教育目的の明示

本プロジェクト（Quant Trading System）は、プログラミングとデータ分析の教育を目的としています。

### 投資助言の否定

本プロジェクトで提供される情報、コード、戦略は、投資助言を構成するものではありません。

### リスクの認識

株式投資には元本割れのリスクがあります。本プロジェクトを参考にした投資判断は、すべて自己責任で行ってください。

### 実績の不存在

本プロジェクトは、リアルマネーでの運用実績を持ちません。すべてペーパートレード（仮想取引）での検証結果です。

### 責任の限定

本プロジェクトの利用によって生じたいかなる損害についても、開発者は一切の責任を負いません。

### 法令遵守

本プロジェクトは、金融商品取引法その他の関連法規を遵守し、投資助言業の登録を要しない範囲での教育活動として運営されています。

---

## 📖 学習リソース

### 無料で学べる資料

#### note記事
- [【完全無料】Pythonで学ぶ米国株自動売買の基礎](https://note.com/ai_narrative25)
- Python環境構築、ペーパートレード、実践コード例を解説

#### X（Twitter）での日次学習
- [@ai_narrative25](https://x.com/ai_narrative25) - 毎朝7時に投稿中
- ハッシュタグ: #Python自動売買 #投資教育

### 📚 よくある質問（FAQ）

**Q1: プログラミング初心者でも大丈夫ですか？**
A: Python基礎（変数、関数、ループ）の知識があれば大丈夫です。環境構築から丁寧に解説しています。

**Q2: 本当にお金は使わなくて大丈夫？**
A: はい。Alpaca Paper Tradingを使えば、仮想のお金で実際の株価を使った取引を体験できます。完全無料です。

**Q3: 実際に稼げますか？**
A: **本プロジェクトは教育目的です。**利益を保証するものではありません。学習を通じてスキルを身につけることが目的です。

**Q4: Mac/Linuxでも使えますか？**
A: はい。Windows/Mac/Linuxすべて対応しています。

**Q5: リアルマネーでの運用実績は？**
A: **ありません。**本プロジェクトは教育・学習目的であり、リアルマネーでの運用は行っていません。

---

**学習を楽しんでください！** 📚🤖

質問や提案は、IssuesまたはDiscussionsでお気軽にどうぞ。

**作成者**: r-estoer3588
**X**: [@ai_narrative25](https://x.com/ai_narrative25)
**note**: [https://note.com/ai_narrative25](https://note.com/ai_narrative25)
