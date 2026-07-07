# 環境変数一覧（完全版）

プロジェクト全体で使用される環境変数を網羅的に一覧化し、各変数の目的・デフォルト値・影響範囲を明示します。

**最終更新**: 2025-10-10
**対象バージョン**: branch0906

> **真偽値の判定ルール**: "1" / "true" / "yes" / "on" を真、それ以外を偽と見なします（大文字小文字不問）。

---

## 📖 目次

1. [ログ制御](#1-ログ制御)
2. [System3 固有（テスト用閾値）](#2-system3-固有テスト用閾値)
3. [パフォーマンス・並列処理](#3-パフォーマンス並列処理)
4. [テスト・デバッグ](#4-テストデバッグ)
5. [API 認証（機密情報）](#5-api-認証機密情報)
6. [通知・ダッシュボード](#6-通知ダッシュボード)
7. [Bulk API データ品質検証](#7-bulk-api-データ品質検証)
8. [その他](#8-その他)
9. [設定方法とベストプラクティス](#設定方法とベストプラクティス)

---

## 1. ログ制御

### `COMPACT_TODAY_LOGS`

- **デフォルト**: `0` (詳細ログ)
- **設定値**: `0` / `1`
- **対象**: 全システム、特に `scripts/run_all_systems_today.py`
- **説明**: `1` で DEBUG レベルのログを抑制し、INFO 以上のみ出力。ログファイルサイズを削減。
- **使用例**: `COMPACT_TODAY_LOGS=1 python scripts/run_all_systems_today.py`
- **参照**: `scripts/run_all_systems_today.py` L3948-3950

### `ENABLE_PROGRESS_EVENTS`

- **デフォルト**: `false`
- **設定値**: `true` / `false`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: `true` で進捗イベントを `logs/progress_today.jsonl` に出力。Streamlit UI でリアルタイム進捗表示が可能。
- **参照**: `scripts/run_all_systems_today.py` L146

### `TODAY_SIGNALS_LOG_MODE`

- **デフォルト**: (空文字)
- **設定値**: `compact` / `verbose` / `single` / `dated`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: シグナル生成時のログモード。`compact`=簡潔、`verbose`=詳細、`dated`=日付別ファイル。
- **参照**: `scripts/run_all_systems_today.py` L648, L2513, L3495, L5416

### `STRUCTURED_UI_LOGS`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: UI 向け構造化ログ（JSON 文字列）を出力。
- **参照**: `scripts/run_all_systems_today.py` L716

### `STRUCTURED_LOG_NDJSON`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: NDJSON 形式の構造化ログを `logs/` へ出力。
- **参照**: `scripts/run_all_systems_today.py` L724

### `EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 本番実行（test*mode なし）でも Diagnostics スナップショット（`results_csv/diagnostics_test/diagnostics_snapshot*\*.json`）を出力する。UI フル実行時の「3 点同期（JSONL × スクショ × 診断）」で同一ランの診断情報を参照したい場合に有効化する。
- **注意**: 生成ファイルは `results_csv/diagnostics_test/` 配下に出力され、運用の結果 CSV と混ざらない。
- **参照**: `scripts/run_all_systems_today.py` の `_export_diagnostics_snapshot()`

### `SHOW_INDICATOR_LOGS`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 指標計算進捗ログを表示。
- **参照**: `scripts/run_all_systems_today.py` L1018

### `ENABLE_STEP_TIMINGS`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 各処理ステップの実行時間を測定・DEBUG 出力。
- **参照**: `scripts/run_all_systems_today.py` L1094

### `ENABLE_SUBSTEP_LOGS`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `core/system1.py`, `strategies/__init__.py`
- **説明**: サブステップの詳細ログを有効化。
- **参照**: `core/system1.py` L365, `strategies/__init__.py` L22

### `TRD_LOG_OK`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: TRDlist 生成成功時のログを表示。
- **参照**: `scripts/run_all_systems_today.py` L4493

### `ROLLING_ISSUES_VERBOSE_HEAD`

- **デフォルト**: `5`
- **設定値**: 整数（表示行数）
- **対象**: `CacheManager` (キャッシュ問題の詳細表示)
- **説明**: rolling キャッシュ問題を詳細表示する際の先頭行数。`COMPACT_TODAY_LOGS=1` と併用。
- **参照**: `tests/test_cache_manager_final.py` L101

### `NO_EMOJI` / `DISABLE_EMOJI`

- **デフォルト**: (未設定)
- **設定値**: `1` / (空)
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: ログから Emoji（絵文字）を削除。CI/CD 環境や文字化け対策。
- **参照**: `scripts/run_all_systems_today.py` L116

---

## 2. System3 固有（テスト用閾値）

⚠️ **警告**: これらは**テスト専用**です。本番環境では絶対に設定しないでください。

### `MIN_DROP3D_FOR_TEST`

- **デフォルト**: (未設定、本番は `0.125` = 12.5%下落)
- **設定値**: 浮動小数点数（例: `0.05`）
- **対象**: `core/system3.py`
- **説明**: **テスト専用**。System3 の 3 日間下落率（drop3d）の閾値を上書き。通常は 12.5%以上の下落が必要だが、テスト時に候補を増やすため低く設定可能。
- **使用例**: `MIN_DROP3D_FOR_TEST=0.05` で 5%以上の下落でも候補に含める
- **参照**: `core/system3.py` L276
- **⚠️ 本番厳禁**: 誤ってこの値を設定すると、不適格な銘柄が取引対象になります。

### `MIN_ATR_RATIO_FOR_TEST`

- **デフォルト**: (未設定、本番は `0.05` = 5%)
- **設定値**: 浮動小数点数（例: `0.01`）
- **対象**: `core/system3.py`
- **説明**: **テスト専用**。System3 の ATR 比率（atr_ratio）の閾値を上書き。通常は 5%以上が必要。
- **使用例**: `MIN_ATR_RATIO_FOR_TEST=0.01`
- **参照**: `core/system3.py` L51, L118
- **⚠️ 本番厳禁**: 誤ってこの値を設定すると、ボラティリティ不足の銘柄が取引対象になります。

---

## 3. パフォーマンス・並列処理

### `USE_PROCESS_POOL`

- **デフォルト**: (未設定、無効)
- **設定値**: `1` / `true` / (空)
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: プロセスプールでの並列処理を有効化。CPU 集約的な処理で有効。
- **参照**: `scripts/run_all_systems_today.py` L4964

### `PROCESS_POOL_WORKERS`

- **デフォルト**: (未設定、自動決定)
- **設定値**: 整数（ワーカー数）
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: プロセスプールのワーカー数を明示指定。
- **参照**: `scripts/run_all_systems_today.py` L4980

### `SYSTEM6_USE_PROCESS_POOL`

- **デフォルト**: `false`
- **設定値**: `true` / `false`
- **対象**: `strategies/system6_strategy.py`
- **説明**: System6 専用のプロセスプール使用フラグ。
- **参照**: `strategies/system6_strategy.py` L39

### `SYSTEM6_FORCE_LATEST_ONLY`

- **デフォルト**: `true`
- **設定値**: `true` / `false`
- **対象**: `core/system6.py`
- **説明**: 当日シグナル実行（`FULL_SCAN_TODAY` 未指定）で System6 の候補生成を必ず `latest_only` 高速パスに切り替える。System6 はショートのモメンタム逆張り特性で過去複数日のセットアップ走査がコスト高になりやすいため、日次用途では最終足のみを O(symbols) で評価する方が実用的。
- **挙動詳細**:
  - `SYSTEM6_FORCE_LATEST_ONLY=true` かつ `FULL_SCAN_TODAY` が `false` → 呼び出し側が `latest_only=False` を渡しても内部で強制上書き。
  - `FULL_SCAN_TODAY=1` を指定するとフルスキャン（履歴全日付）に戻る。
  - バックテストや過去日パターン分析で multi-day 候補が必要な場合は `SYSTEM6_FORCE_LATEST_ONLY=0` を併用。
- **副作用**: fast-path では 1 日分候補のみ生成するため、過去複数日のランキング検証には不向き。Diagnostics の `ranking_source` は `latest_only` になる。
- **ログ**: 強制適用時に `System6: forcing latest_only (system6_force_latest_only=1, full_scan_today=0)` を INFO 出力（compact ログでも 1 行）。
- **メトリクス**: `_metrics.record_metric("system6_forced_latest_only", 1, "count")` が記録され、パフォーマンス観測や回帰診断に利用可能。

### `STANDARDIZE_STRATEGY_OUTPUT`

- **デフォルト**: `false`
- **設定値**: `true` / `false`
- **対象**: `strategies/system6_strategy.py`, `strategies/system7_strategy.py`（今後拡大予定）
- **説明**: 戦略レイヤの `generate_candidates()` 返却形式を外向きに統一。歴史的に一部戦略は `{date: {symbol: payload}}` を返してきたが、消費側は `{date: [record, ...]}`（list-of-dicts）を前提とする箇所が多い。`true` にすると戦略内で安全に list-of-dicts に変換する。
- **挙動**:
  - `true`: 返却直前に `{date: {symbol: payload}}` → `{date: [record, ...]}` へ変換。順序は `rank` 昇順、なければ `return_6d` 降順、どちらも無ければ安定（挿入）順。
  - `false`: 既存の返却形式を維持（互換目的）。
- **備考**: コア層（`core/systemX.py`）の返却形式は変更しない。変換は無害で、すでに list-of-dicts の場合は変化しない。

### `BASIC_DATA_PARALLEL`

- **デフォルト**: (自動判定)
- **設定値**: `1` / `0` / (空)
- **対象**: `scripts/run_all_systems_today.py`, `core/today_pipeline/phase02_basic_data.py`
- **説明**: 基本データ読み込みの並列処理。`1`=強制並列、`0`=強制直列、未設定=自動。
- **参照**: `scripts/run_all_systems_today.py` L1687, `core/today_pipeline/phase02_basic_data.py` L729

### `BASIC_DATA_PARALLEL_THRESHOLD`

- **デフォルト**: `200`
- **設定値**: 整数（銘柄数）
- **対象**: `scripts/run_all_systems_today.py`, `core/today_pipeline/phase02_basic_data.py`
- **説明**: 並列処理を開始する銘柄数の閾値。この数以上の場合のみ並列化。
- **参照**: `scripts/run_all_systems_today.py` L1689, `core/today_pipeline/phase02_basic_data.py` L736

### `BASIC_DATA_MAX_WORKERS`

- **デフォルト**: (未設定、自動決定)
- **設定値**: 整数（ワーカー数）
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 基本データ読み込みの最大ワーカー数。
- **参照**: `scripts/run_all_systems_today.py` L1702

### `LOOKBACK_MARGIN`

- **デフォルト**: `0.15` (15%)
- **設定値**: 浮動小数点数
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: ルックバック期間のマージン（余裕）。
- **参照**: `scripts/run_all_systems_today.py` L5017

### `LOOKBACK_MIN_DAYS`

- **デフォルト**: `80`
- **設定値**: 整数（日数）
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: ルックバック期間の最小日数。
- **参照**: `scripts/run_all_systems_today.py` L5053

---

## 4. テスト・デバッグ

### `VALIDATE_SETUP_PREDICATE`

- **デフォルト**: `0` (無効)
- **設定値**: `0` / `1`
- **対象**: 全システム（Setup 列 vs Predicate 関数の一致検証）
- **説明**: `1` で setup 列と`system_setup_predicates.py`の関数が一致しているか検証。不一致時は詳細ログを出力。開発・デバッグ用。
- **使用例**: `VALIDATE_SETUP_PREDICATE=1 python scripts/run_all_systems_today.py --test-mode mini`
- **参照**: `common/system_setup_predicates.py` (validate_predicate_equivalence)

### `STREAMLIT_SERVER_ENABLED`

- **デフォルト**: (未設定)
- **設定値**: `1` / (空)
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: Streamlit サーバーモード実行中かどうかを示すフラグ。
- **参照**: `scripts/run_all_systems_today.py` L2522, L3522

### `TODAY_SYMBOL_LIMIT`

- **デフォルト**: (未設定、制限なし)
- **設定値**: 整数（銘柄数）
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 当日シグナルスキャンの対象銘柄数を制限。テスト・デバッグ用。
- **参照**: `scripts/run_all_systems_today.py` L2341

### `BASIC_DATA_TEST_FRESHNESS_TOLERANCE`

- **デフォルト**: `365` (日)
- **設定値**: 整数（日数）
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: テストモード時のデータ鮮度許容日数。
- **参照**: `scripts/run_all_systems_today.py` L2446

### `FULL_SCAN_TODAY`

- **デフォルト**: (未設定)
- **設定値**: `1` / `true` / (空)
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 全履歴をスキャン（`latest_only=False`）。デバッグ用。
- **参照**: `scripts/run_all_systems_today.py` L4419

### `ALLOW_CRITICAL_CHANGES`

- **デフォルト**: (未設定)
- **設定値**: `1` / (空)
- **対象**: `tools/guard_critical_files.py`
- **説明**: 重要ファイルの変更を許可するフラグ。通常は設定しない。
- **参照**: `tools/guard_critical_files.py` L25

### `RUN_PLANNED_EXITS`

- **デフォルト**: (未設定)
- **設定値**: `true` / (空)
- **対象**: `scripts/run_all_systems_today.py`
- **説明**: 計画的エグジット処理を実行。
- **参照**: `scripts/run_all_systems_today.py` L5578

### `MAX_VERBOSE_LINES` / `MAX_COMPACT_LINES`

- **デフォルト**: (未設定)
- **設定値**: 整数（行数）
- **対象**: `tools/validate_log_compactness.py`
- **説明**: ログ圧縮検証時の最大行数。
- **参照**: `tools/validate_log_compactness.py` L145, L151

---

### `LATEST_ONLY_MAX_DATE_LAG_DAYS`

- **デフォルト**: (未設定、`settings.cache.rolling.max_staleness_days` を使用)
- **設定値**: 整数（日数、0 以上）
- **対象**: `scripts/run_all_systems_today.py`（latest_only 用の鮮度ガード）
- **説明**: latest_only のターゲット日（latest_mode_date）に対して、最新バー日付が古すぎる銘柄を除外する許容乖離（日数、カレンダー日基準）。未設定時は設定ファイルの `max_staleness_days` を用いる。
- **使用例**: `LATEST_ONLY_MAX_DATE_LAG_DAYS=1` で当日 ≒ 同日のみ許容、`2` で週末・祝日跨ぎを広めに許容。
- **参照**: `config/environment.py` の `EnvironmentConfig.latest_only_max_date_lag_days`、`core/system1.py` の latest_only ステールネスチェック。

---

## 5. API 認証（機密情報）

⚠️ **重要**: これらの変数は `.env` ファイルで管理し、**絶対に Git 追跡対象にしないこと**！

### `APCA_API_KEY_ID`

- **デフォルト**: (未設定、必須)
- **設定値**: 文字列（Alpaca API キー）
- **対象**: Alpaca API 連携
- **説明**: Alpaca 取引 API のキー ID。
- **参照**: `tools/debug/alpaca_fetchtest.py` L10
- **⚠️ 機密情報**: `.env` ファイルで管理、Git 追跡対象外

### `APCA_API_SECRET_KEY`

- **デフォルト**: (未設定、必須)
- **設定値**: 文字列（Alpaca API シークレット）
- **対象**: Alpaca API 連携
- **説明**: Alpaca 取引 API のシークレットキー。
- **参照**: `tools/debug/alpaca_fetchtest.py` L11
- **⚠️ 機密情報**: `.env` ファイルで管理、Git 追跡対象外

### `ALPACA_API_BASE_URL`

- **デフォルト**: (未設定)
- **設定値**: URL（例: `https://paper-api.alpaca.markets`）
- **対象**: Alpaca API 連携
- **説明**: Alpaca API のベース URL。
- **参照**: `tools/debug/alpaca_fetchtest.py` L12

### `ALPACA_PAPER`

- **デフォルト**: `true`
- **設定値**: `true` / `false`
- **対象**: Alpaca API 連携
- **説明**: ペーパートレーディング（デモ）モードを使用。
- **参照**: `tools/debug/alpaca_fetchtest.py` L13
- **⚠️ 警告**: **本番環境では`false`に設定すること**

### `SLACK_BOT_TOKEN`

- **デフォルト**: (未設定、必須)
- **設定値**: 文字列（Slack Bot Token）
- **対象**: Slack 通知
- **説明**: Slack Bot API トークン。
- **参照**: `scripts/slack_bot_test.py` L9
- **⚠️ 機密情報**: `.env` ファイルで管理、Git 追跡対象外

### `SLACK_CHANNEL_LOGS` / `SLACK_CHANNEL_EQUITY` / `SLACK_CHANNEL_SIGNALS`

- **デフォルト**: (未設定)
- **設定値**: 文字列（Slack チャンネル ID）
- **対象**: Slack 通知
- **説明**: 各種通知の送信先チャンネル。
- **参照**: `scripts/slack_bot_test.py` L12-14

### `DISCORD_WEBHOOK_URL`

- **デフォルト**: (未設定)
- **設定値**: URL
- **対象**: Discord 通知
- **説明**: Discord Webhook URL。
- **⚠️ 機密情報**: `.env` ファイルで管理

### `EODHD_API_KEY`

- **デフォルト**: (未設定)
- **設定値**: 文字列（EODHD API キー）
- **対象**: EODHD API 連携
- **説明**: EODHD データプロバイダーの API キー。
- **参照**: `tests/test_config_settings_enhanced.py` L158
- **⚠️ 機密情報**: `.env` ファイルで管理

---

## 6. 通知・ダッシュボード

### `NOTIFY_USE_RICH`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: 通知システム
- **説明**: 通知をリッチカード形式で送信。

### `CACHE_HEALTH_SILENT`

- **デフォルト**: `0`
- **設定値**: `0` / `1`
- **対象**: キャッシュ健康診断
- **説明**: `1` で CLI 通知を抑制。

---

## 7. Bulk API データ品質検証

### `BULK_API_VOLUME_TOLERANCE`

- **デフォルト**: `5.0`
- **設定値**: 数値（パーセンテージ）
- **対象**: `scripts/verify_bulk_accuracy.py`
- **説明**: Volume（出来高）データの許容差異。デフォルト 5.0% で速報値の誤差を許容。
- **使用例**: `BULK_API_VOLUME_TOLERANCE=3.0` で 3% 以内の差異を許容
- **参照**: `scripts/verify_bulk_accuracy.py`
- **注意**: 市場データの特性上、Volume は確定値までに数%の変動が発生します。厳格すぎる設定は Bulk API の利点を損ないます。

### `BULK_API_PRICE_TOLERANCE`

- **デフォルト**: `0.5`
- **設定値**: 数値（パーセンテージ）
- **対象**: `scripts/verify_bulk_accuracy.py`
- **説明**: 価格データ（OHLC）の許容差異。デフォルト 0.5% で厳格に検証。
- **使用例**: `BULK_API_PRICE_TOLERANCE=1.0` で 1% 以内の差異を許容
- **参照**: `scripts/verify_bulk_accuracy.py`
- **注意**: 価格データの差異は戦略に直接影響するため、緩和は慎重に。

### `BULK_API_MIN_RELIABILITY`

- **デフォルト**: `70.0`
- **設定値**: 数値（パーセンテージ）
- **対象**: `scripts/verify_bulk_accuracy.py`
- **説明**: Bulk API 使用の最低信頼性スコア。デフォルト 70% 以上で Bulk API を使用可能と判定。
- **使用例**: `BULK_API_MIN_RELIABILITY=80.0` で 80% 以上に引き上げ
- **参照**: `scripts/verify_bulk_accuracy.py`
- **注意**: 低すぎる設定は品質の低いデータを許容し、高すぎる設定は過剰に個別 API にフォールバックします。

---

## 8. その他

### `SCHEDULER_WORKERS` / `BULK_UPDATE_WORKERS`

- **デフォルト**: `4`
- **設定値**: 整数（ワーカー数）
- **対象**: `scripts/scheduler_update_with_healthcheck.py`
- **説明**: スケジューラー/バルク更新のワーカー数。
- **参照**: `scripts/scheduler_update_with_healthcheck.py` L59-60

### `DATA_CACHE_DIR`

- **デフォルト**: `data_cache`
- **設定値**: パス文字列
- **対象**: キャッシュシステム
- **説明**: データキャッシュディレクトリのパス。
- **参照**: `tests/test_format_migration.py` L16

### `RESULTS_DIR`

- **デフォルト**: `results_csv`
- **設定値**: パス文字列
- **対象**: 出力システム
- **説明**: 結果 CSV 出力先ディレクトリ。

### `LOGS_DIR`

- **デフォルト**: `logs`
- **設定値**: パス文字列
- **対象**: ログシステム
- **説明**: ログファイル出力先ディレクトリ。

---

## 設定方法とベストプラクティス

### 1. コマンドライン（一時的）

```powershell
# PowerShell
$env:COMPACT_TODAY_LOGS="1"
python scripts/run_all_systems_today.py --test-mode mini

# Bash
COMPACT_TODAY_LOGS=1 python scripts/run_all_systems_today.py --test-mode mini
```

### 2. `.env` ファイル（永続的、推奨）

プロジェクトルートに `.env` ファイルを作成：

```ini
# ログ制御
COMPACT_TODAY_LOGS=1
ENABLE_PROGRESS_EVENTS=true

# API認証（絶対に公開しない）
APCA_API_KEY_ID=your_key_here
APCA_API_SECRET_KEY=your_secret_here
ALPACA_PAPER=true

# パフォーマンス
USE_PROCESS_POOL=1
PROCESS_POOL_WORKERS=4
```

⚠️ **注意**: `.env` ファイルは `.gitignore` に含めること！

### 3. コードからの参照（型安全・推奨）

環境変数は直接 `os.environ.get()` で参照せず、型安全なアクセサを使用してください。

```python
from config.environment import get_env_config

env = get_env_config()  # シングルトン
if env.validate_setup_predicate:
  # Setup/Predicate 検証を有効にする処理
  ...
```

理由:

- 値のパース（真偽・数値・None 可など）とデフォルトの一元管理
- ドキュメント整合（真偽値は "1"/"true"/"yes"/"on" などを許容）
- 将来の設定項目追加時の影響範囲を最小化

### 3. VS Code `launch.json`（デバッグ時）

```json
{
  "configurations": [
    {
      "name": "Run Today Signals (Compact)",
      "type": "python",
      "request": "launch",
      "program": "${workspaceFolder}/scripts/run_all_systems_today.py",
      "args": ["--test-mode", "mini", "--skip-external"],
      "env": {
        "COMPACT_TODAY_LOGS": "1",
        "VALIDATE_SETUP_PREDICATE": "1"
      }
    }
  ]
}
```

---

## 優先度別推奨設定

### 本番環境（Production）

```ini
COMPACT_TODAY_LOGS=1
ENABLE_PROGRESS_EVENTS=true
ALPACA_PAPER=false  # ⚠️ 本番取引
# MIN_DROP3D_FOR_TEST は絶対に設定しない
# MIN_ATR_RATIO_FOR_TEST は絶対に設定しない
```

### 開発環境（Development）

```ini
COMPACT_TODAY_LOGS=0  # 詳細ログ
VALIDATE_SETUP_PREDICATE=1  # Setup検証
ENABLE_STEP_TIMINGS=1  # パフォーマンス測定
ALPACA_PAPER=true  # ペーパートレード
```

### テスト環境（Test/CI）

```ini
COMPACT_TODAY_LOGS=1
NO_EMOJI=1  # CI/CD用
BASIC_DATA_TEST_FRESHNESS_TOLERANCE=999
MIN_DROP3D_FOR_TEST=0.05  # テスト専用閾値
MIN_ATR_RATIO_FOR_TEST=0.01  # テスト専用閾値
TODAY_SYMBOL_LIMIT=10  # 高速テスト
```

---

## トラブルシューティング

### Q: ログが多すぎる

**A**: `COMPACT_TODAY_LOGS=1` を設定してください。

### Q: System3 の候補が少なすぎる（テスト時）

**A**: `MIN_DROP3D_FOR_TEST=0.05` と `MIN_ATR_RATIO_FOR_TEST=0.01` を設定。**本番では絶対に使わないこと！**

### Q: Setup 列と Predicate が一致しない

**A**: `VALIDATE_SETUP_PREDICATE=1` でデバッグログを有効化し、不一致箇所を特定。

### Q: 並列処理が動作しない

**A**: `USE_PROCESS_POOL=1` と `PROCESS_POOL_WORKERS=4` を設定。銘柄数が閾値（`BASIC_DATA_PARALLEL_THRESHOLD`）未満の場合は並列化されない。

---

## 関連ドキュメント

- **設定管理**: [config/settings.py](../../config/settings.py)
- **ログ設定**: [common/logging_utils.py](../../common/logging_utils.py)
- **System3 実装**: [core/system3.py](../../core/system3.py)
- **今日シグナルスキャン**: [scripts/run_all_systems_today.py](../../scripts/run_all_systems_today.py)

---

## 更新ガイド

新しい環境変数を導入する場合は、以下の手順に従ってください：

1. **このドキュメントに追記**（カテゴリ別に整理）
2. **実装部にデフォルト値の根拠コメントを追加**
3. **テストケースを作成**（必要に応じて）
4. **PR で変更を明示**

---

**最終更新**: 2025-10-10
**メンテナー**: プロジェクトチーム
