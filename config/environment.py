"""環境変数の統一管理モジュール。

全プロジェクトで使用される環境変数を EnvironmentConfig クラスで一元管理。
デフォルト値、型変換、バリデーションを提供。

使用例:
    >>> from config.environment import get_env_config
    >>> env = get_env_config()
    >>> if env.compact_logs:
    ...     print("Compact mode enabled")
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache
import os


def _get_bool_env(key: str, default: bool = False) -> bool:
    """環境変数をboolとして取得。

    Args:
        key: 環境変数のキー
        default: デフォルト値

    Returns:
        環境変数の真偽値
    """
    val = os.environ.get(key, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


def _get_int_env(key: str, default: int) -> int:
    """環境変数をintとして取得。

    Args:
        key: 環境変数のキー
        default: デフォルト値

    Returns:
        環境変数の整数値（変換失敗時はデフォルト）
    """
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _get_float_env(key: str, default: float) -> float:
    """環境変数をfloatとして取得。

    Args:
        key: 環境変数のキー
        default: デフォルト値

    Returns:
        環境変数の浮動小数点値（変換失敗時はデフォルト）
    """
    val = os.environ.get(key, "").strip()
    if not val:
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _get_str_env(key: str, default: str = "") -> str:
    """環境変数を文字列として取得。

    Args:
        key: 環境変数のキー
        default: デフォルト値

    Returns:
        環境変数の文字列値
    """
    return os.environ.get(key, default).strip()


def _basic_data_parallel_default() -> bool | None:
    """Parse BASIC_DATA_PARALLEL into tri-state bool.

    Returns:
        True if env=="1", False if env=="0", otherwise None (auto)
    """
    val = _get_str_env("BASIC_DATA_PARALLEL", "").strip().lower()
    if val == "1":
        return True
    if val == "0":
        return False
    return None


@dataclass
class EnvironmentConfig:
    """環境変数の統一管理クラス。

    全環境変数をプロパティとして提供。
    型安全・デフォルト値・自動変換を保証。
    """

    # ===== 1. ログ制御 =====
    compact_logs: bool = field(
        default_factory=lambda: _get_bool_env("COMPACT_TODAY_LOGS", False)
    )
    """DEBUGログを抑制し、簡潔なログ出力。本番推奨。"""

    enable_progress_events: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_PROGRESS_EVENTS", False)
    )
    """進捗イベントをlogs/progress_today.jsonlに出力。"""

    today_signals_log_mode: str = field(
        default_factory=lambda: _get_str_env("TODAY_SIGNALS_LOG_MODE", "")
    )
    """シグナルログモード: 'compact' / 'verbose' / 'single' / 'dated'"""

    structured_ui_logs: bool = field(
        default_factory=lambda: _get_bool_env("STRUCTURED_UI_LOGS", False)
    )
    """UI向け構造化ログ（JSON）を出力。"""

    debug_mode: bool = field(default_factory=lambda: _get_bool_env("DEBUG_MODE", False))
    """開発・デバッグ用の詳細UI出力を有効化。DEBUG_MODE=1 でオン。"""

    structured_log_ndjson: bool = field(
        default_factory=lambda: _get_bool_env("STRUCTURED_LOG_NDJSON", False)
    )
    """NDJSON形式の構造化ログ出力。"""

    skip_external_apis: bool = field(
        default_factory=lambda: _get_bool_env("SKIP_EXTERNAL_APIS", False)
    )
    """外部API呼び出しをスキップ（短縮実行向け）。"""

    show_indicator_logs: bool = field(
        default_factory=lambda: _get_bool_env("SHOW_INDICATOR_LOGS", False)
    )
    """指標計算進捗ログを表示。"""

    enable_step_timings: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_STEP_TIMINGS", False)
    )
    """各処理ステップの実行時間を測定・出力。"""

    enable_substep_logs: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_SUBSTEP_LOGS", False)
    )
    """サブステップの詳細ログを有効化。"""

    # ===== 1.5. 機能フラグ（安全な段階的ロールアウト用） =====
    enable_option_b_system3: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_OPTION_B_SYSTEM3", False)
    )
    """System3 の候補最終化ユーティリティ（Option B）を有効化。

    目的:
        - System3 で新しい共通ユーティリティ finalize_ranking_and_diagnostics を
          安全に段階導入するためのトグル。

    仕様:
        - True: core/system3.py が finalize_ranking_and_diagnostics を使用（kwargs の
                use_option_b_utils=True も引き続き有効）。
        - False(既定): 既存の set_diagnostics_after_ranking を使用。

    備考:
        - テストやスポット検証での比較を容易にするため、環境変数
          ENABLE_OPTION_B_SYSTEM3=1 で切り替え可能。
    """

    enable_option_b_system5: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_OPTION_B_SYSTEM5", False)
    )
    """System5 の候補最終化ユーティリティ（Option B）を有効化。

    System3 と同様に、 finalize_ranking_and_diagnostics の段階導入を
    安全に行うためのフラグ。既定は False（従来動作）。
    """

    enable_option_b_system6: bool = field(
        default_factory=lambda: _get_bool_env("ENABLE_OPTION_B_SYSTEM6", False)
    )
    """System6 の候補最終化ユーティリティ（Option B）を有効化。

    目的:
        - System6 のランキング最終化・診断付与を共通ヘルパーで行うための段階導入フラグ。

    仕様:
        - True: core/system6.py が finalize_ranking_and_diagnostics を使用（kwargs の
                use_option_b_utils=True も引き続き有効）。
        - False(既定): 既存の set_diagnostics_after_ranking を使用。

    備考:
        - パリティ検証のため、環境変数 ENABLE_OPTION_B_SYSTEM6=1 で切り替え可能。
        - 既定は安全側（False）。
    """

    trd_log_ok: bool = field(default_factory=lambda: _get_bool_env("TRD_LOG_OK", False))
    """TRDlist生成成功時のログを表示。"""

    rolling_issues_verbose_head: int = field(
        default_factory=lambda: _get_int_env("ROLLING_ISSUES_VERBOSE_HEAD", 5)
    )
    """rollingキャッシュ問題の詳細表示行数。"""

    # 進捗表示（UI/CLI 共通）
    today_progress_chunk: int = field(
        default_factory=lambda: _get_int_env("TODAY_PROGRESS_CHUNK", 500)
    )
    """進捗ログを出す件数間隔（既定: 500）。"""

    today_progress_thousands: bool = field(
        default_factory=lambda: _get_bool_env("TODAY_PROGRESS_THOUSANDS", False)
    )
    """進捗件数表示を3桁区切りにする。"""

    today_progress_style: str = field(
        default_factory=lambda: _get_str_env("TODAY_PROGRESS_STYLE", "both")
    )
    """進捗表示スタイル: 'elapsed' | 'eta' | 'both'（既定）。"""

    no_emoji: bool = field(
        default_factory=lambda: _get_bool_env("NO_EMOJI", False)
        or _get_bool_env("DISABLE_EMOJI", False)
    )
    """ログからEmoji（絵文字）を削除。CI/CD用。"""

    rolling_missing_verbose: bool = field(
        default_factory=lambda: _get_bool_env("ROLLING_MISSING_VERBOSE", False)
    )
    """個別銘柄ごとの rolling 未整備ログを詳細表示（既定は抑制）。"""

    # ===== 2. System3固有（テスト用閾値） =====
    min_drop3d_for_test: float | None = field(
        default_factory=lambda: (
            _get_float_env("MIN_DROP3D_FOR_TEST", float("nan"))
            if "MIN_DROP3D_FOR_TEST" in os.environ
            else None
        )
    )
    """⚠️テスト専用。System3の3日間下落率閾値を上書き。本番環境では絶対に設定しないこと。"""

    min_atr_ratio_for_test: float | None = field(
        default_factory=lambda: (
            _get_float_env("MIN_ATR_RATIO_FOR_TEST", float("nan"))
            if "MIN_ATR_RATIO_FOR_TEST" in os.environ
            else None
        )
    )
    """⚠️テスト専用。System3のATR比率閾値を上書き。本番環境では絶対に設定しないこと。"""

    # ===== 3. パフォーマンス・並列処理 =====
    use_process_pool: bool = field(
        default_factory=lambda: _get_bool_env("USE_PROCESS_POOL", False)
    )
    """プロセスプールでの並列処理を有効化。"""

    process_pool_workers: int | None = field(
        default_factory=lambda: _get_int_env("PROCESS_POOL_WORKERS", 0) or None
    )
    """プロセスプールのワーカー数。Noneで自動決定。"""

    system6_use_process_pool: bool = field(
        default_factory=lambda: _get_bool_env("SYSTEM6_USE_PROCESS_POOL", False)
    )
    """System6専用のプロセスプール使用フラグ。"""

    system6_force_latest_only: bool = field(
        default_factory=lambda: _get_bool_env("SYSTEM6_FORCE_LATEST_ONLY", True)
    )
    """System6 を当日シグナル実行（バックテスト以外）で latest_only 強制する。

    目的:
        - System6 のフルスキャン（全日付走査）で時間が掛かるケースを回避し、
          当日候補抽出用途では O(symbols) の fast-path を常時利用する。
    仕様:
        - True (既定): generate_candidates_system6(latest_only=False 指定でも) 実行時に
          today 実行コンテキストと判断できれば latest_only パスへ強制切替。
        - False: 呼び出し側指定をそのまま尊重。
    注意:
        - バックテストや履歴検証では full_scan_today / 明示 latest_only=False を優先。
        - 将来 System6 の過去日ランキング分析をする際はこのフラグを False に設定。"""

    basic_data_parallel: bool | None = field(
        default_factory=lambda: _basic_data_parallel_default()
    )
    """基本データ読み込みの並列処理。True=強制並列、False=強制直列、None=自動。"""

    basic_data_parallel_threshold: int = field(
        default_factory=lambda: _get_int_env("BASIC_DATA_PARALLEL_THRESHOLD", 200)
    )
    """並列処理を開始する銘柄数の閾値。"""

    basic_data_max_workers: int | None = field(
        default_factory=lambda: _get_int_env("BASIC_DATA_MAX_WORKERS", 0) or None
    )
    """基本データ読み込みの最大ワーカー数。Noneで自動決定。"""

    lookback_margin: float = field(
        default_factory=lambda: _get_float_env("LOOKBACK_MARGIN", 0.15)
    )
    """ルックバック期間のマージン（既定15%）。"""

    lookback_min_days: int = field(
        default_factory=lambda: _get_int_env("LOOKBACK_MIN_DAYS", 80)
    )
    """ルックバック期間の最小日数。"""

    # ===== 4. テスト・デバッグ =====
    test_mode: str | None = field(
        default_factory=lambda: _get_str_env("TEST_MODE", "") or None
    )
    """テストモード: 'mini' (10銘柄) | 'quick' (50銘柄) | 'sample' (100銘柄) | None。"""

    validate_setup_predicate: bool = field(
        default_factory=lambda: _get_bool_env("VALIDATE_SETUP_PREDICATE", False)
    )
    """setup列とpredicate関数の一致検証を有効化。開発・デバッグ用。"""

    streamlit_server_enabled: bool = field(
        default_factory=lambda: _get_bool_env("STREAMLIT_SERVER_ENABLED", False)
    )
    """Streamlitサーバーモード実行中かどうか。"""

    today_symbol_limit: int | None = field(
        default_factory=lambda: _get_int_env("TODAY_SYMBOL_LIMIT", 0) or None
    )
    """当日シグナルスキャンの対象銘柄数を制限。テスト・デバッグ用。"""

    basic_data_test_freshness_tolerance: int = field(
        default_factory=lambda: _get_int_env("BASIC_DATA_TEST_FRESHNESS_TOLERANCE", 365)
    )
    """テストモード時のデータ鮮度許容日数。"""

    full_scan_today: bool = field(
        default_factory=lambda: _get_bool_env("FULL_SCAN_TODAY", False)
    )
    """全履歴をスキャン（latest_only=False）。デバッグ用。"""

    # ===== 3.5. 並列競合制御（テスト/CI向け） =====
    use_run_lock: bool = field(
        default_factory=lambda: _get_bool_env("PIPELINE_USE_RUN_LOCK", False)
    )
    """True の場合、共通出力を行うフェーズでプロセス間ロックを取得します。"""

    use_run_subdir: bool = field(
        default_factory=lambda: _get_bool_env("PIPELINE_USE_RUN_SUBDIR", False)
    )
    """True の場合、出力を run_<namespace> サブディレクトリへ分離します。"""

    run_namespace: str = field(
        default_factory=lambda: _get_str_env("RUN_NAMESPACE", "")
    )
    """デフォルトのラン名前空間。CLI 引数や環境変数で上書き可能。"""

    # 戦略レイヤー出力スキーマの標準化（安全な外向き互換）
    standardize_strategy_output: bool = field(
        default_factory=lambda: _get_bool_env("STANDARDIZE_STRATEGY_OUTPUT", False)
    )
    """戦略の generate_candidates() 返却形式を list-of-dicts に標準化するフラグ。

    目的:
        - 歴史的に一部の戦略が {date: {symbol: payload}} を返すが、
          消費側の大半は {date: [record, ...]} を前提としているため、外向きを統一する。

    仕様:
        - True: 戦略レイヤで {date: {symbol: payload}} を {date: [record, ...]} へ変換
                （順序は rank または return_6d に基づき安定化）。
        - False (既定): 既存動作を維持（コアの返却形を尊重）。

    注意:
        - コア（core/systemX.py）の返却形は変更しない。
        - 既に list[dict] の場合は変更なし（無害変換）。
    """

    allow_critical_changes: bool = field(
        default_factory=lambda: _get_bool_env("ALLOW_CRITICAL_CHANGES", False)
    )
    """重要ファイルの変更を許可。通常は設定しない。"""

    run_planned_exits: bool = field(
        default_factory=lambda: _get_bool_env("RUN_PLANNED_EXITS", False)
    )
    """計画的エグジット処理を実行。"""

    max_verbose_lines: int | None = field(
        default_factory=lambda: _get_int_env("MAX_VERBOSE_LINES", 0) or None
    )
    """ログ圧縮検証時の最大行数（verbose）。"""

    max_compact_lines: int | None = field(
        default_factory=lambda: _get_int_env("MAX_COMPACT_LINES", 0) or None
    )
    """ログ圧縮検証時の最大行数（compact）。"""

    allocation_debug: bool = field(
        default_factory=lambda: _get_bool_env("ALLOCATION_DEBUG", False)
    )
    """配分処理の詳細デバッグログを有効化。開発・デバッグ用。"""

    export_diagnostics_snapshot_always: bool = field(
        default_factory=lambda: _get_bool_env(
            "EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS", False
        )
    )
    """本番実行（test_mode なし）でも diagnostics スナップショットを出力する。

    既定は False（テストモード時のみ出力）。True の場合は `results_csv/diagnostics_test/` 配下に
    `diagnostics_snapshot_*.json` を出力し、UI フル実行時の 3 点同期（JSONL × スクショ × 診断）に利用できる。
    """

    slot_dedup_enabled: bool = field(
        default_factory=lambda: _get_bool_env("SLOT_DEDUP_ENABLED", False)
    )
    """スロット配分前にラウンドロビン方式でシンボル重複を解消する。"""

    slot_max_rank_depth: int = field(
        default_factory=lambda: _get_int_env("SLOT_MAX_RANK_DEPTH", 6)
    )
    """重複解消の候補探索で見る最大ランク深度。0以下なら無制限。"""

    # ===== 4.5. latest_only 鮮度ガード（カレンダー日ベース） =====
    latest_only_max_date_lag_days: int | None = field(
        default_factory=lambda: _get_int_env("LATEST_ONLY_MAX_DATE_LAG_DAYS", 0) or None
    )
    """latest_only 時に許容する最新バーとターゲット日の日付乖離（カレンダー日）。

    None: 設定未指定（settings.cache.rolling.max_staleness_days を使用）
    0以上の整数: 明示上書き（0 は同日一致のみ許容）
    """

    # ===== 5. API認証（機密情報） =====
    apca_api_key_id: str = field(
        default_factory=lambda: _get_str_env("APCA_API_KEY_ID", "")
    )
    """⚠️機密情報。Alpaca APIキーID。.envで管理。"""

    apca_api_secret_key: str = field(
        default_factory=lambda: _get_str_env("APCA_API_SECRET_KEY", "")
    )
    """⚠️機密情報。Alpaca APIシークレットキー。.envで管理。"""

    alpaca_api_base_url: str = field(
        default_factory=lambda: _get_str_env("ALPACA_API_BASE_URL", "")
    )
    """Alpaca APIのベースURL。"""

    alpaca_paper: bool = field(
        default_factory=lambda: _get_bool_env("ALPACA_PAPER", True)
    )
    """ペーパートレーディングモード。本番ではfalse。"""

    slack_bot_token: str = field(
        default_factory=lambda: _get_str_env("SLACK_BOT_TOKEN", "")
    )
    """⚠️機密情報。Slack Bot Token。.envで管理。"""

    slack_channel_logs: str = field(
        default_factory=lambda: _get_str_env("SLACK_CHANNEL_LOGS", "")
    )
    """Slack通知先チャンネル（ログ）。"""

    slack_channel_equity: str = field(
        default_factory=lambda: _get_str_env("SLACK_CHANNEL_EQUITY", "")
    )
    """Slack通知先チャンネル（エクイティ）。"""

    slack_channel_signals: str = field(
        default_factory=lambda: _get_str_env("SLACK_CHANNEL_SIGNALS", "")
    )
    """Slack通知先チャンネル（シグナル）。"""

    slack_channel_exit_radar: str = field(
        default_factory=lambda: _get_str_env("SLACK_CHANNEL_EXIT_RADAR", "")
    )
    """Slack通知先チャンネル（Exit Radar）。"""

    discord_webhook_url: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL", "")
    )
    """⚠️機密情報。Discord Webhook URL。.envで管理。"""

    discord_webhook_url_signals: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SIGNALS", "")
    )
    """⚠️機密情報。Discord Webhook URL（シグナル）。.envで管理。"""

    discord_webhook_url_exit_radar: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_EXIT_RADAR", "")
    )
    """⚠️機密情報。Discord Webhook URL（Exit Radar）。.envで管理。"""

    discord_webhook_url_summary: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SUMMARY", "")
    )
    """⚠️機密情報。Discord Webhook URL（日次サマリー）。.envで管理。"""

    discord_webhook_url_system1: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM1", "")
    )
    """⚠️機密情報。Discord Webhook URL（system1）。.envで管理。"""

    discord_webhook_url_system2: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM2", "")
    )
    """⚠️機密情報。Discord Webhook URL（system2）。.envで管理。"""

    discord_webhook_url_system3: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM3", "")
    )
    """⚠️機密情報。Discord Webhook URL（system3）。.envで管理。"""

    discord_webhook_url_system4: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM4", "")
    )
    """⚠️機密情報。Discord Webhook URL（system4）。.envで管理。"""

    discord_webhook_url_system5: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM5", "")
    )
    """⚠️機密情報。Discord Webhook URL（system5）。.envで管理。"""

    discord_webhook_url_system6: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM6", "")
    )
    """⚠️機密情報。Discord Webhook URL（system6）。.envで管理。"""

    discord_webhook_url_system7: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_SYSTEM7", "")
    )
    """⚠️機密情報。Discord Webhook URL（system7）。.envで管理。"""

    discord_webhook_url_equity: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_EQUITY", "")
    )
    """⚠️機密情報。Discord Webhook URL（バックテスト/売買結果）。.envで管理。"""

    discord_webhook_url_backtest: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_BACKTEST", "")
    )
    """⚠️機密情報。Discord Webhook URL（バックテスト）。.envで管理。"""

    discord_webhook_url_logs: str = field(
        default_factory=lambda: _get_str_env("DISCORD_WEBHOOK_URL_LOGS", "")
    )
    """⚠️機密情報。Discord Webhook URL（ログ/汎用）。.envで管理。"""

    position_tracker_auto_update: bool = field(
        default_factory=lambda: _get_bool_env("POSITION_TRACKER_AUTO_UPDATE", True)
    )
    """シグナル通知後に position_tracker.json を自動更新するか。"""

    position_tracker_confirm_auto_apply: bool = field(
        default_factory=lambda: _get_bool_env("POSITION_TRACKER_CONFIRM_AUTO_APPLY", True)
    )
    """確認CSVの自動適用を有効化するか。"""

    position_tracker_confirm_dir: str = field(
        default_factory=lambda: _get_str_env("POSITION_TRACKER_CONFIRM_DIR", "data")
    )
    """確認CSVの配置ディレクトリ。"""

    eodhd_api_key: str = field(
        default_factory=lambda: _get_str_env("EODHD_API_KEY", "")
    )
    """⚠️機密情報。EODHD APIキー。.envで管理。"""

    # ===== 6. 通知・ダッシュボード =====
    notify_use_rich: bool = field(
        default_factory=lambda: _get_bool_env("NOTIFY_USE_RICH", False)
    )
    """通知をリッチカード形式で送信。"""

    signals_platform: str = field(
        default_factory=lambda: _get_str_env("SIGNALS_PLATFORM", "slack").lower()
    )
    """シグナル通知の送信先プラットフォーム（slack / discord / auto / both）。"""

    cache_health_silent: bool = field(
        default_factory=lambda: _get_bool_env("CACHE_HEALTH_SILENT", False)
    )
    """キャッシュ健康診断のCLI通知を抑制。"""

    # ===== 7. Bulk APIデータ品質検証 =====
    bulk_api_volume_tolerance: float = field(
        default_factory=lambda: _get_float_env("BULK_API_VOLUME_TOLERANCE", 5.0)
    )
    """Bulk API Volume差異の許容範囲（パーセント）。デフォルト: 5.0%"""

    bulk_api_price_tolerance: float = field(
        default_factory=lambda: _get_float_env("BULK_API_PRICE_TOLERANCE", 0.5)
    )
    """Bulk API価格差異の許容範囲（パーセント）。デフォルト: 0.5%"""

    bulk_api_min_reliability: float = field(
        default_factory=lambda: _get_float_env("BULK_API_MIN_RELIABILITY", 70.0)
    )
    """Bulk API使用の最低信頼性スコア（パーセント）。デフォルト: 70.0%"""

    # ===== 8. その他 =====
    scheduler_workers: int = field(
        default_factory=lambda: _get_int_env("SCHEDULER_WORKERS", 4)
    )
    """スケジューラーのワーカー数。"""

    bulk_update_workers: int = field(
        default_factory=lambda: _get_int_env("BULK_UPDATE_WORKERS", 4)
    )
    """バルク更新のワーカー数。"""

    data_cache_dir: str = field(
        default_factory=lambda: _get_str_env("DATA_CACHE_DIR", "data_cache")
    )
    """データキャッシュディレクトリ。"""

    results_dir: str = field(
        default_factory=lambda: _get_str_env("RESULTS_DIR", "results_csv")
    )
    """結果CSV出力先ディレクトリ。"""

    logs_dir: str = field(default_factory=lambda: _get_str_env("LOGS_DIR", "logs"))
    """ログファイル出力先ディレクトリ。"""

    def validate(self) -> list[str]:
        """環境変数の妥当性を検証。

        Returns:
            エラーメッセージのリスト（空なら妥当）
        """
        errors: list[str] = []

        # テスト用閾値が本番環境で設定されていないかチェック
        if not self.compact_logs:  # 本番環境の可能性
            if self.min_drop3d_for_test is not None:
                errors.append(
                    "⚠️ MIN_DROP3D_FOR_TEST が設定されています。本番環境では絶対に使用しないでください。"
                )
            if self.min_atr_ratio_for_test is not None:
                errors.append(
                    "⚠️ MIN_ATR_RATIO_FOR_TEST が設定されています。本番環境では絶対に使用しないでください。"
                )

        # Alpacaペーパートレード警告
        if not self.alpaca_paper and self.apca_api_key_id:
            # 本番取引モード
            errors.append(
                "⚠️ ALPACA_PAPER=false（本番取引モード）が設定されています。本番取引を実行する場合のみ使用してください。"
            )

        return errors

    def is_production(self) -> bool:
        """本番環境かどうかを判定。

        Returns:
            本番環境の場合True
        """
        return self.compact_logs and not self.alpaca_paper

    def is_test_mode(self) -> bool:
        """テストモードかどうかを判定。

        Returns:
            テストモードの場合True
        """
        return (
            self.min_drop3d_for_test is not None
            or self.min_atr_ratio_for_test is not None
            or self.today_symbol_limit is not None
        )


@lru_cache(maxsize=1)
def get_env_config() -> EnvironmentConfig:
    """環境変数設定をシングルトンとして取得。

    初回呼び出し時に EnvironmentConfig を生成し、以降は同一インスタンスを返す。

    Returns:
        環境変数設定オブジェクト

    Examples:
        >>> env = get_env_config()
        >>> if env.compact_logs:
        ...     print("Compact mode")
        >>> if env.is_production():
        ...     print("Running in production")
    """
    return EnvironmentConfig()


def reset_env_config_cache() -> None:
    """環境変数設定のキャッシュをクリア。

    os.environ を動的に変更した後に呼び出すことで、
    get_env_config() が最新の環境変数を読み込むようにする。

    Examples:
        >>> import os
        >>> os.environ["TEST_MODE"] = "mini"
        >>> reset_env_config_cache()
        >>> env = get_env_config()  # 新しい環境変数で再初期化
    """
    get_env_config.cache_clear()


def print_env_summary() -> None:
    """環境変数設定のサマリーを出力（デバッグ用）。"""
    env = get_env_config()
    errors = env.validate()

    print("=" * 60)
    print("環境変数設定サマリー")
    print("=" * 60)
    mode = (
        "本番" if env.is_production() else ("テスト" if env.is_test_mode() else "開発")
    )
    print(f"モード: {mode}")
    print(f"Compact Logs: {env.compact_logs}")
    print(f"Alpaca Paper Trading: {env.alpaca_paper}")
    print(f"Process Pool: {env.use_process_pool}")
    print(f"Validate Setup Predicate: {env.validate_setup_predicate}")
    print("=" * 60)

    if errors:
        print("⚠️ 警告:")
        for err in errors:
            print(f"  - {err}")
        print("=" * 60)


__all__ = [
    "EnvironmentConfig",
    "get_env_config",
    "print_env_summary",
]


if __name__ == "__main__":
    # デバッグ実行: python -m config.environment
    print_env_summary()
