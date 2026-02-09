# Quant Trading System - ドキュメント総覧

このドキュメントは、7 つの売買システムを統合したクオンツトレーディングシステムの包括的なガイドです。

## 🌟 はじめに

**このドキュメントの使い方**:

- **初めて使う方**: [📘 初心者向けガイド](#-初心者向けガイド) から始めてください（環境構築 →UI 起動 → 基本操作の順）
- **開発者の方**: [🔧 開発者向けガイド](#-開発者向けガイド) で技術詳細・テスト・カスタマイズ方法を確認してください
- **特定の課題を解決したい**: 目次から該当セクションに直接ジャンプできます

---

## 📘 初心者向けガイド

このセクションは、システムを初めて使う方向けの基本的な操作ガイドです。

### 🚀 クイックスタート

1. **環境構築**: [セットアップ](../README.md#セットアップ) で Python 環境と依存パッケージをインストール
2. **UI 起動**: [基本実行](../README.md#実行例) で Streamlit アプリを起動
3. **動作確認**: UI から「Generate Signals」ボタンをクリックしてシグナル生成を試す

### 📊 システム概要

- [システム構成と資産配分](#システム構成と資産配分) - 7 つのシステムの役割と配分比率
- [各システム詳細](./systems/INDEX.md) - System1-7 の個別仕様（ロング/ショート、エントリー条件など）
- [パフォーマンス指標](#kpi) - バックテストの見方と評価基準

### 🏃 運用ガイド

- [運用ガイド一覧](./operations/INDEX.md) - 自動実行・通知・監視設定
- [文字化け対策ガイド（Windows）](./operations/mojibake_guide.md) - UTF-8/NO_EMOJI の設定

---

## 🔧 開発者向けガイド

このセクションは、コードを変更・拡張・テストする開発者向けの技術資料です。

### 📖 技術仕様書

- **[技術仕様書完全版](./TECHNICAL_SPECS.md)** ⭐ **← ここから開始**
  - System1-7 各システムの詳細仕様
  - エントリー・エグジット・ランキングロジック
  - 診断標準化と Option-B フレームワーク解説
  - トラブルシューティング

### 🧪 テストと検証

- [テスト実行](./testing.md) - システム動作確認の基本

  - **統合制御テスト**（systems 1-6）: `tests/test_systems_controlled_all.py` を実行する短い検証。開発中や変更適用後に素早くランク付けと最終エントリ数の整合を確認するために使います。

    **Windows (PowerShell):**

    ```powershell
    python scripts/run_controlled_tests.py
    # または直接 pytest を使う場合
    python -m pytest -q tests/test_systems_controlled_all.py
    ```

    **Unix/Linux/Mac:**

    ```bash
    python3 scripts/run_controlled_tests.py
    # または直接 pytest を使う場合
    python3 -m pytest -q tests/test_systems_controlled_all.py
    # または Makefile を使う場合
    make test-controlled
    ```

### 🔧 技術文書

- [技術文書一覧](./technical/INDEX.md) - 指標・実装・仕様の詳細資料
- [キャッシュインデックス要件](./technical/cache_index_requirements.md) - Feather 形式の制約と日付インデックス変換
- [候補数ゼロガイド](./technical/zero_candidates_guide.md) - System6 等で候補が出ない理由（正常動作）
- [環境変数一覧](./technical/environment_variables.md) - 既定値と用途
- [SPY/取引日ユーティリティ](./technical/spy_utils.md) - 営業日ヘルパの仕様
- [Playwright E2E テスト統合](./technical/playwright_integration.md) - Streamlit UI 自動テスト

### 📖 Context Note 形式

このプロジェクトでは、各ソースファイルの先頭に **「Context Note」** という設計意図・注意点をコメント形式で記載しています。以下の場面で参照してください：

- **コード変更前**: ファイルの役割・前提条件・禁止事項を確認して、設計をズレさせない
- **Copilot と相談するとき**: Context Note で Copilot が文脈を把握し、より正確な提案を得られる
- **レビュー・デバッグ時**: README.md の抽象情報を補足する具体的なガイドとして機能

---

## 📚 共通リファレンス

以下のセクションは、初心者・開発者共通で参照する詳細資料です。

### 📖 Context Note 形式の詳細

#### 📋 Context Note の構成

```python
# 🧠 Context Note
# このファイルは【役割・責務】
#
# 前提条件：
#   - 【設計前提1】
#   - 【設計前提2】
#
# ロジック単位：
#   function_name() → 【役割】
#
# Copilot へ：
#   → 【重点領域・禁止事項】
```

#### 📂 主要ファイルの Context Note 一覧

| ファイル                           | 役割                                                     |
| ---------------------------------- | -------------------------------------------------------- |
| `core/systemX.py`                  | エントリー・ランキング・フィルタロジック（ロジック重視） |
| `strategies/systemX_strategy.py`   | core のラッパー層（UI 連携）                             |
| `scripts/run_all_systems_today.py` | 当日パイプライン全体（フロー管理）                       |
| `common/cache_manager.py`          | キャッシュ層（I/O 統一、直接アクセス禁止）               |
| `apps/app_integrated.py`           | Streamlit UI（進捗表示・タブ管理）                       |

詳細は各ファイルの先頭 Context Note を参照してください。

**新規ファイル追加・リファクタリング時の Context Note 記載ガイド**: [Context Note 追記ガイド](./context_note_guide.md)

### 🤖 LLM Reference

- [LLM Reference Index](./llm/README.md) - 常時参照の入口
- [LLM Spec](./llm/SPEC.md) - 目的・スコープ・不変条件
- [LLM Architecture](./llm/ARCHITECTURE.md) - 構成とデータフロー
- [LLM Interfaces](./llm/INTERFACES.md) - CLI/UI/API/出力の契約
- [LLM State](./llm/STATE.md) - 状態管理と保存場所

### 🔗 関連リンク

- [メイン README](../README.md) - プロジェクト全体概要
- [変更履歴](../CHANGELOG.md) - リリースノート
- [GitHub Instructions](../.github/copilot-instructions.md) - AI 開発ガイド

---

## 🌐 Next.js ダッシュボード（新規）

Streamlit UI に代わる、モダンな Next.js ベースのダッシュボードです。

### 起動方法

```powershell
# 統合起動スクリプト（FastAPI + Next.js を同時起動）
.\Start-Dashboard.ps1

# または個別起動
# FastAPI バックエンド (port 8000)
python -m uvicorn apps.api.main:app --reload --port 8000

# Next.js フロントエンド (port 3000)
cd apps\dashboards\alpaca-next
npm run dev -- --port 3000
```

### アクセス URL

| ページ             | URL                              | 説明               |
| ------------------ | -------------------------------- | ------------------ |
| ホーム             | http://localhost:3000            | ポートフォリオ概要 |
| 統合ダッシュボード | http://localhost:3000/integrated | シグナル生成 UI    |
| バックテスト       | http://localhost:3000/backtest   | パフォーマンス分析 |
| FastAPI Docs       | http://localhost:8000/docs       | API ドキュメント   |

### 技術スタック

- **フロントエンド**: Next.js 16, React 19, TailwindCSS, Shadcn/UI
- **バックエンド**: FastAPI, WebSocket (リアルタイム進捗)
- **データ**: Alpaca API, ローカルキャッシュ

### 主な機能

- ✅ ポートフォリオ残高・ポジション表示
- ✅ System 1-7 シグナル生成（WebSocket 進捗表示）
- ✅ バックテスト結果のグラフ表示
- ✅ ダークモード対応

## 📊 システム構成と資産配分

### 4 つの買いシステム

- システム 1 ーロング・トレンド・ハイ・モメンタム（トレード資産の 25%を配分）
- システム 4 ーロング・トレンド・ロー・ボラティリティ（トレード資産の 25%を配分）
- システム 3 ーロング・ミーン・リバージョン・セルオフ（トレード資産の 25%を配分）
- システム 5 ーロング・ミーン・リバージョン・ハイ ADX・リバーサル（トレード資産の 25%を配分）

### 3 つの売りシステム

- システム 2 ーショート RSI スラスト（トレード資産の 40%を配分）
- システム 6 ーショート・ミーン・リバージョン・ハイ・シックスデイサージ（トレード資産の 40%を配分）
- システム 7 ーカタストロフィーヘッジ（トレード資産の 20%を配分）

### モニタリング（daily_metrics.csv）

- 出力先: `results_csv/daily_metrics.csv`
- 生成タイミング: `scripts/run_all_systems_today.py` 実行時（当日シグナル抽出の終盤）
- カラム:
  - `date`: NYSE 最新営業日
  - `system`: `system1`〜`system7`
  - `prefilter_pass`: 事前フィルター通過銘柄数（system7 は SPY の有無で 1/0）
  - `setup_pass`: セットアップ条件通過銘柄数（最新行ベース）
  - `candidates`: 当日候補数（最終スコアリング前のシステム別集計）
  - `entries`: 配分後のエントリー件数（system別）
- 用途: 事前フィルターの通過数と候補数の推移を日次で可視化し、データ品質やシグナル強度の変動を監視する。

### UI: Metrics タブ

- `app_integrated.py` のタブに `Metrics` を追加。`results_csv/daily_metrics.csv` を読み込み、システム別に `prefilter_pass` と `candidates` の推移をライン／バーで表示できる。

### 検証レポート（任意）

- `tools/build_metrics_report.py` が最新日のメトリクスと各システムのシグナル CSV（`signals_systemX_YYYY-MM-DD.csv`）を突き合わせ、`results_csv/daily_metrics_report.csv` を生成する。前日差分や合計サマリの確認に使う。

### 📂 CSV・ログ出力先一覧

パイプライン実行時の出力ファイルと保存場所：

| ファイル種別     | パス                                               | 生成タイミング                |
| ---------------- | -------------------------------------------------- | ----------------------------- |
| シグナル CSV     | `results_csv/signals_systemX_YYYY-MM-DD.csv`       | `--save-csv` 指定時           |
| 配分結果 CSV     | `results_csv/final_allocation_YYYYMMDD_HHMMSS.csv` | `--save-csv` 指定時           |
| 日次メトリクス   | `results_csv/daily_metrics.csv`                    | 毎回追記                      |
| 進捗ログ (JSONL) | `logs/progress_today.jsonl`                        | `ENABLE_PROGRESS_EVENTS=1` 時 |
| 実行ログ         | `logs/today_signals_YYYYMMDD_HHMM.log`             | 毎回生成                      |
| ペーパートレード | `results_csv/paper_trade_log_*.csv`                | `daily_paper_trade.py` 実行時 |
| 除外銘柄         | `logs/excluded_symbols_YYYYMMDD.csv`               | --skip-external 時            |

### パイプライン実行例

```powershell
# テストモード（推奨: 再現性100%）
python -m scripts.run_all_systems_today --test-mode test_symbols --skip-external --save-csv

# 本番モード（全銘柄）
python -m scripts.run_all_systems_today --parallel --save-csv

# 進捗イベント有効化（UI リアルタイム表示用）
$env:ENABLE_PROGRESS_EVENTS=1; python -m scripts.run_all_systems_today --parallel
```

## 🔄 半自動の検証ループ運用

AI と一緒にコード修正を進めるときは、テストと画像確認を毎回セットで行います。ここでは必ず確認すべき流れをまとめます。

1. **初回の基準づくり**: `make verify` もしくは PowerShell で `./tools/verify.ps1` を実行して、テストとスナップショットと画像差分をそろえます。画像が別フォルダに出力される場合は `make verify IMGDIR=outputs/images` のように指定します。
2. **半自動ループの開始**: `python tools/auto_refine_loop.py` を実行すると、テスト → スナップショット → 画像差分まで自動で進みます。差分が残った場合は Copilot に貼り付けるプロンプトが表示され、AI が修正案を示します。
3. **人の判断ポイント**: AI が提案した修正を確認し、適用するかスキップするかをコンソールで選びます。適用後はファイルを保存し、Enter を押すだけで次のサイクルへ進みます。
4. **終了条件**: 差分がなくなると自動で終了します。回数を制限したい場合は `python tools/auto_refine_loop.py --max-iterations 3` のように指定します。PowerShell の補助スクリプト `tools/auto_refine.ps1` でも同じ指定が可能です。
5. **成果の保管**: スナップショットは `snapshots/<timestamp>/` に保存され、`imgdiff_report.html` で差分の有無を振り返れます。後から比較したいときは `tools/imgdiff.py --snap-a ... --snap-b ...` を手動で呼び出しても構いません。

完全自動ではありませんが、AI が差分を読み取り、自律的に提案を出してくれるので、開発者は提案の良し悪しを判断するだけでループを回せます。差分が減らない場合はフォントや乱数など再現性の要因を疑い、必要であれば `common/testing.py::set_test_determinism()` で固定化します。

### チャット用プロンプト名

ループ操作を会話で共有するときは「検証ループプロンプト」という呼び名を使います。以下の定型文をチャットに貼れば、チーム全員が同じ流れで対応できます。

```text
[検証ループプロンプト]
目的: 変更コードをテストし、スナップショットと画像差分で確認する
手順:
1. make verify （または .\tools\verify.ps1）
2. python tools/auto_refine_loop.py
3. 差分が無ければ終了。差分があれば Copilot への提案と判断を繰り返す
出力: snapshots/<timestamp>/ と imgdiff_report.html を保管
```

---

## 📸 UI スクリーンショット取得（自動実行対応）

Streamlit UI の画面キャプチャを **ボタンクリックから完了画面の撮影まで完全自動化** できます。Playwright を使用してブラウザを自動操作し、フルページスクリーンショットを取得します。

### クイックスタート

**最も簡単な方法（Windows）:**

```powershell
# 初回のみ: Playwright インストール
pip install playwright
playwright install chromium

# 実行（ボタンクリック→撮影→スナップショット作成）
.\tools\run_and_snapshot.ps1
```

**Unix/Linux/Mac:**

```bash
# 初回のみ: Playwright インストール
pip install playwright
playwright install chromium

# 実行
make run-and-snapshot
```

### 1. Playwright セットアップ（詳細）

**基本インストール:**

仮想環境を有効化してから実行してください。

**Windows (PowerShell):**

```powershell
# 仮想環境の有効化
.\venv\Scripts\Activate.ps1

# Playwright インストール（約5分、300MBダウンロード）
pip install playwright
playwright install chromium
```

**Unix/Linux/Mac:**

```bash
# 仮想環境の有効化
source venv/bin/activate

# Playwright インストール
pip install playwright
playwright install chromium

# ネットワーク制限がある場合
playwright install chromium --with-deps
```

**トラブルシューティング:**

- インストール失敗: [Playwright 公式ドキュメント](https://playwright.dev/python/docs/intro) を参照
- プロキシ環境: `HTTPS_PROXY` 環境変数を設定

**VSCode 拡張機能（オプション）:**

"Playwright Test for VSCode" をインストールすると、以下の機能が使えます：

- Test Explorer（サイドバーからテスト実行）
- Pick Locator（UI 要素のセレクター自動生成）
- Trace Viewer（実行履歴のタイムライン表示）

詳細: [Playwright VSCode Extension](https://playwright.dev/docs/getting-started-vscode)

### 2. 自動実行

統合スクリプトを使うのが最も簡単です。以下の処理を自動実行します：

1. Streamlit アプリを開く (`http://localhost:8501`)
2. 「Generate Signals」ボタンをクリック
3. 実行完了を待機（進行状況バーの消失を検出）
4. フルページスクリーンショットを保存
5. CSV/ログ/画像をスナップショット

**Windows (PowerShell):**

```powershell
# PowerShell スクリプト（ワンコマンド）
.\tools\run_and_snapshot.ps1

# オプション: 実行完了まで60秒待機
.\tools\run_and_snapshot.ps1 -WaitAfterClick 60

# オプション: スクリーンショットのみ（スナップショット作成をスキップ）
.\tools\run_and_snapshot.ps1 -SkipSnapshot
```

**Unix/Linux/Mac:**

```bash
# 基本実行
make run-and-snapshot
```

**注意**: Streamlit アプリが `http://localhost:8501` で起動している必要があります。

### 3. カスタマイズ実行

**Windows (PowerShell):**

```powershell
# ボタンクリック + スクリーンショット
python tools/capture_ui_screenshot.py `
    --url http://localhost:8501 `
    --output results_images/today_signals_complete.png `
    --click-button "Generate Signals" `
    --wait-after-click 30

# スクリーンショットのみ（ボタンクリックなし）
python tools/capture_ui_screenshot.py --url http://localhost:8501 --output screenshots/ui_snapshot.png

# カスタムディレクトリに保存（親ディレクトリが自動作成される）
python tools/capture_ui_screenshot.py --url http://localhost:8501 --output my_reports/2024-11/ui_final.png
```

**Unix/Linux/Mac:**

```bash
# ボタンクリック + スクリーンショット
python3 tools/capture_ui_screenshot.py \
    --url http://localhost:8501 \
    --output results_images/today_signals_complete.png \
    --click-button "Generate Signals" \
    --wait-after-click 30

# スクリーンショットのみ（ボタンクリックなし）
python3 tools/capture_ui_screenshot.py --url http://localhost:8501 --output screenshots/ui_snapshot.png

# カスタムディレクトリに保存（親ディレクトリが自動作成される）
python3 tools/capture_ui_screenshot.py --url http://localhost:8501 --output my_reports/2024-11/ui_final.png
```

**主要オプション:**

| オプション           | 説明                             | デフォルト              |
| -------------------- | -------------------------------- | ----------------------- |
| `--url`              | Streamlit アプリの URL           | `http://localhost:8501` |
| `--output`           | 保存先パス（相対パス）           | 必須                    |
| `--click-button`     | クリックするボタンのテキスト     | なし                    |
| `--wait-after-click` | ボタンクリック後の待機時間（秒） | 15                      |
| `--no-scroll`        | 最下部へのスクロールを無効化     | 無効                    |
| `--wait`             | ページ読み込み後の待機時間（秒） | 3                       |

**画像パスについて:**

- 推奨ディレクトリ: `results_images/` または `screenshots/`（`.gitignore` で Git 管理外）
- カスタムパス: 親ディレクトリは自動作成されます（例: `my_reports/2024-11/ui.png`）
- PR 用画像: `docs/images/` にコピーして Git 管理下に置いてください

### 4. AI コーディング用プロンプト

会話で「検証ループプロンプト」と言えば、AI が自動的に以下を実行します：

```text
[検証ループプロンプト - UI版]
目的: Streamlit UI の完了画面を自動撮影し、スナップショット比較
手順:
1. .\tools\run_and_snapshot.ps1 でボタンクリック→撮影→スナップショット
2. python tools/imgdiff.py で差分確認
3. 差分があれば Copilot に修正提案を依頼
```

### 5. 参考情報

**重要な注意点:**

- **事前条件**: Streamlit アプリを `http://localhost:8501` で起動しておく
- **待機時間**: 処理が重い場合は `--wait-after-click` を延長（例: 60 秒）
- **ボタン名**: 日本語 UI の場合は絵文字含む正確な表記を指定（例: `"▶ 本日のシグナル実行"`）
- **環境依存**: フォント・レンダリングの微差が発生する場合は `common/testing.py::set_test_determinism()` で対応

**Makefile ターゲット（Unix/Linux/Mac）:**

```bash
make run-and-snapshot  # capture_ui_screenshot.py + snapshot.py を連続実行
```

**Image Diff Report（ダークモード対応）:**

`tools/imgdiff.py` で生成される `imgdiff_report.html` は、VSCode スタイルのダークモード配色で表示されます。画像ホバー時の拡大表示やレスポンシブデザインに対応しています。

- 生成場所: `snapshots/<最新>/imgdiff_report.html`
- カスタマイズ: `tools/imgdiff.py` の `_build_report()` 関数内の CSS を編集
