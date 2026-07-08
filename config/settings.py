from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from functools import lru_cache
import json
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

validate_config_dict: Callable[[Mapping[str, object]], Any] | None
try:  # optional import for validation
    from .schemas import validate_config_dict  # noqa: F401
except Exception:  # pragma: no cover
    validate_config_dict = None

try:
    import yaml  # type: ignore[import-untyped]  # stubs 任意: types-PyYAML 導入で解除可
except Exception:  # pragma: no cover
    yaml = None  # PyYAML が未導入でも動くように（後述のガードで対応）

# プロジェクトルート推定
PROJECT_ROOT = Path(__file__).resolve().parents[1]

# .env を読み込み（存在すれば）
load_dotenv(dotenv_path=PROJECT_ROOT / ".env", override=False)


# -----------------------------
# Dataclasses (セクション別)
# -----------------------------
@dataclass(frozen=True)
class PortfolioRiskConfig:
    """portfolio-level 管理上限 (Phase 5, 2026-07-07)。

    デフォルトは既存ルール (per-system 10 / 50-50 / bucket) からの導出 no-op。
    詳細と根拠: docs/POSITION_MANAGEMENT_PHASE5_20260707.md。
    """

    max_total_positions: int = 70
    max_long_positions: int = 40
    max_short_positions: int = 30
    max_gross_exposure_pct: float = 1.0
    max_net_exposure_pct: float = 1.0
    # off-by-default (0 = 無効)
    drawdown_flatten_pct: float = 0.0
    max_positions_per_sector: int = 0


@dataclass(frozen=True)
class RiskConfig:
    risk_pct: float = 0.02
    max_positions: int = 10
    max_pct: float = 0.10
    portfolio: PortfolioRiskConfig = field(default_factory=PortfolioRiskConfig)


@dataclass(frozen=True)
class SizingConfig:
    """当日デプロイ予算のサイジング方式 (2026-07-09)。

    mode:
      - "equity_linked": deploy_budget = current_equity × equity_deploy_pct。
        notional_i = weight_i × deploy_budget。gross 目標 ≈ pct×equity。
        risk cap (per-name max_pct / gross / net / 件数) は *この予算の内側* で
        従来通り縛る (budget を上げても cap は緩まない)。既定。
      - "fixed_tier": 従来の tier 固定予算 (small=$1k/medium=$10k/large=$100k)。
        後方互換のため撤去せず残す。tier 経路には dollar cap は掛けない (従来挙動)。
    equity_deploy_pct:
      deploy_budget = equity × この係数。既定 1.0 (gross≤1.0×equity cap いっぱい)。
      後から 0.5 等の単一ノブで絞れる。0 以下/NaN は 1.0 に安全フォールバック。
    詳細: docs/EQUITY_LINKED_SIZING_20260709.md。
    """

    mode: str = "equity_linked"
    equity_deploy_pct: float = 1.0


@dataclass(frozen=True)
class DataConfig:
    vendor: str = "EODHD"
    eodhd_base: str = "https://eodhistoricaldata.com"
    api_key_env: str = "EODHD_API_KEY"
    cache_dir: Path = Path("data_cache")
    cache_recent_dir: Path = Path("data_cache_recent")
    max_workers: int = 20
    batch_size: int = 100
    request_timeout: int = 10
    download_retries: int = 3
    api_throttle_seconds: float = 1.5


@dataclass(frozen=True)
class BacktestConfig:
    start_date: str = "2018-01-01"
    end_date: str = "2024-12-31"
    max_symbols: int = 500
    top_n_rank: int = 50
    initial_capital: int = 100000


@dataclass(frozen=True)
class OutputConfig:
    results_csv_dir: Path = Path("results_csv")
    logs_dir: Path = Path("logs")
    signals_dir: Path = Path("data_cache") / "signals"


@dataclass(frozen=True)
class CsvConfig:
    """CSV locale settings."""

    decimal_point: str = "."
    thousands_sep: str | None = None
    field_sep: str = ","


@dataclass(frozen=True)
class CacheRollingConfig:
    base_lookback_days: int = 300
    buffer_days: int = 30
    max_staleness_days: int = 2
    prune_chunk_days: int = 30
    meta_file: str = "_meta.json"
    max_stale_days: int = 2
    max_symbols: int | None = None
    # 小数丸め桁数: None で無効、整数で有効
    round_decimals: int | None = 4
    # 並列ワーカー数: None で無効（スクリプト側の既定を使用）
    workers: int | None = 4
    # 適応制御パラメータ
    adaptive_window_count: int = 8
    adaptive_increase_threshold: float = 1.02
    adaptive_decrease_threshold: float = 0.98
    adaptive_step: int = 1
    adaptive_min_workers: int = 1
    # None の場合はスクリプト側の上限（CPU×2 など）を使用
    adaptive_max_workers: int | None = None
    # 進捗ステータスを書き出す間隔（秒）
    adaptive_report_seconds: int = 10
    # CSV ロケール設定
    csv: CsvConfig = field(default_factory=CsvConfig)
    # ローリング読み込み時に必須指標が欠けていたら自動再計算する（テスト／運用で無効化可能）
    recompute_indicators_on_read: bool = False


@dataclass(frozen=True)
class CacheConfig:
    full_dir: Path = Path("data_cache/full_backup")
    rolling_dir: Path = Path("data_cache/rolling")
    file_format: str = "auto"
    rolling: CacheRollingConfig = field(default_factory=CacheRollingConfig)
    # キャッシュ書き出し時の丸め桁数: None で無効
    round_decimals: int | None = 4
    # CSV locale defaults (applies when writing CSV files)
    csv: CsvConfig = field(default_factory=CsvConfig)
    # rolling cache無効化: TrueでrollingをbaseからのTail処理に変更
    disable_rolling_cache: bool = False


@dataclass(frozen=True)
class LoggingConfig:
    level: str = "INFO"
    rotation: str = "daily"
    filename: str = "app.log"


@dataclass(frozen=True)
class SchedulerJob:
    name: str
    cron: str
    task: str


@dataclass(frozen=True)
class SchedulerConfig:
    timezone: str = "America/New_York"
    jobs: tuple[SchedulerJob, ...] = tuple()


@dataclass(frozen=True)
class UIConfig:
    default_capital: int = 100000
    # 長短の既定資金配分比率（long の割合 0.0〜1.0）
    default_long_ratio: float = 0.5
    # システム別配分（long/short で独立に定義可能）。値は比率で合計1とは限らない（使用側で正規化）。
    long_allocations: dict[str, float] = field(
        default_factory=lambda: {
            "system1": 0.25,
            "system3": 0.25,
            "system4": 0.25,
            "system5": 0.25,
        }
    )
    short_allocations: dict[str, float] = field(
        default_factory=lambda: {
            "system2": 0.40,
            "system6": 0.40,
            "system7": 0.20,
        }
    )
    auto_tickers: tuple[str, ...] = tuple()
    debug_mode: bool = False
    show_download_buttons: bool = True


@dataclass(frozen=True)
class Settings:
    """アプリ全体で共有する設定値（YAML + .env を統合）
    優先度: .env > YAML > 既定値
    create_dirs=True で主要ディレクトリを作成
    """

    PROJECT_ROOT: Path

    # 既存互換（従来フィールド）
    DATA_CACHE_DIR: Path
    DATA_CACHE_RECENT_DIR: Path
    RESULTS_DIR: Path
    LOGS_DIR: Path

    # API/ネットワーク
    API_EODHD_BASE: str
    EODHD_API_KEY: str | None
    REQUEST_TIMEOUT: int
    DOWNLOAD_RETRIES: int
    API_THROTTLE_SECONDS: float

    # 実行パラメータ
    THREADS_DEFAULT: int
    MARKET_CAL_TZ: str

    # 新構成（YAMLセクション）
    risk: RiskConfig
    sizing: SizingConfig
    data: DataConfig
    cache: CacheConfig
    backtest: BacktestConfig
    outputs: OutputConfig
    logging: LoggingConfig
    scheduler: SchedulerConfig
    ui: UIConfig

    # システムごとの戦略パラメータ
    strategies: Mapping[str, Mapping[str, Any]]


# -----------------------------
# 内部ユーティリティ
# -----------------------------


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except Exception:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, default))
    except Exception:
        return default


def _coerce_int(val: Any, default: int) -> int:
    """Best-effort int coercion with safe fallbacks for mypy clarity."""
    if isinstance(val, bool):  # bool is int subclass, but keep semantic clarity
        return int(val)
    if isinstance(val, int):
        return val
    if isinstance(val, float):
        try:
            return int(val)
        except Exception:
            return default
    if isinstance(val, str):
        try:
            return int(val.strip())
        except Exception:
            return default
    return default


def _positive_int_or_none(value: object | None) -> int | None:
    if value is None:
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return None
    if isinstance(value, bool):  # bool -> int 変換しない（正の整数条件外）
        return None
    if isinstance(value, int):
        parsed = value
    elif isinstance(value, float):
        iv = int(value)
        parsed = iv
    elif isinstance(value, str):  # str は上で strip 済み
        try:
            parsed = int(value)
        except Exception:
            return None
    else:
        return None
    return parsed if parsed > 0 else None


def _as_path(base: Path, p: str | os.PathLike) -> Path:
    pth = Path(p)
    return pth if pth.is_absolute() else (base / pth)


def _load_config_generic(env_var: str, default_path: Path, loader) -> dict[str, Any]:
    cfg_path_env = os.getenv(env_var, "")
    cfg_path = Path(cfg_path_env) if cfg_path_env else default_path
    if not cfg_path.exists():
        return {}
    try:
        with open(cfg_path, encoding="utf-8") as f:
            return loader(f) or {}
    except Exception:
        return {}


def _load_yaml_config(project_root: Path) -> dict[str, Any]:
    """config/config.yaml を読み込んで辞書を返す。未存在なら空。"""
    if yaml is None:
        raise RuntimeError(
            "PyYAML が見つかりません。requirements.txt に PyYAML を追加しインストールしてください。"
        )
    default_path = project_root / "config" / "config.yaml"
    return _load_config_generic(
        "APP_CONFIG",
        default_path,
        lambda f: yaml.safe_load(f) if yaml is not None else {},
    )


def _load_yaml_config_validated(project_root: Path) -> dict[str, Any]:
    """YAMLを読み込み、可能ならPydanticで検証・正規化して返す。"""
    data: dict[str, Any] = _load_yaml_config(project_root)
    if not data:
        return data
    if validate_config_dict is None:
        return data
    try:
        model = validate_config_dict(data)
        dumped = model.model_dump()
        return dict(dumped)
    except Exception:
        return data


def _load_json_config(project_root: Path) -> dict[str, Any]:
    """config/config.json を読み込んで辞書として返す。存在しなければ {}。"""
    default_path = project_root / "config" / "config.json"
    return _load_config_generic("APP_CONFIG_JSON", default_path, lambda f: json.load(f))


def _load_config_json_or_yaml_validated(project_root: Path) -> dict[str, Any]:
    """JSON > YAML の優先順位で読み込み、可能ならPydanticで検証して返す。"""
    data: dict[str, Any] = _load_json_config(project_root)
    if not data:
        data = _load_yaml_config(project_root)
    if not data:
        return data
    if validate_config_dict is None:
        return data
    try:
        model = validate_config_dict(data)
        dumped = model.model_dump()
        return dict(dumped)
    except Exception:
        return data


# -----------------------------
# 設定構築ヘルパー
# -----------------------------


def _build_portfolio_risk_config(cfg: dict[str, Any]) -> PortfolioRiskConfig:
    """risk.portfolio セクションから portfolio-level 上限を構築 (欠損は no-op default)。"""
    pf = cfg.get("portfolio", {}) or {}
    if not isinstance(pf, dict):
        pf = {}
    d = PortfolioRiskConfig()

    def _i(key: str, default: int) -> int:
        return _coerce_int(pf.get(key, default), default)

    def _f(key: str, default: float) -> float:
        try:
            return float(pf.get(key, default))
        except (TypeError, ValueError):
            return default

    return PortfolioRiskConfig(
        max_total_positions=_i("max_total_positions", d.max_total_positions),
        max_long_positions=_i("max_long_positions", d.max_long_positions),
        max_short_positions=_i("max_short_positions", d.max_short_positions),
        max_gross_exposure_pct=_f("max_gross_exposure_pct", d.max_gross_exposure_pct),
        max_net_exposure_pct=_f("max_net_exposure_pct", d.max_net_exposure_pct),
        drawdown_flatten_pct=_f("drawdown_flatten_pct", d.drawdown_flatten_pct),
        max_positions_per_sector=_i(
            "max_positions_per_sector", d.max_positions_per_sector
        ),
    )


def _build_risk_config(cfg: dict[str, Any]) -> RiskConfig:
    raw_max_pos = os.getenv("MAX_POSITIONS", cfg.get("max_positions", 10))
    max_pos_val = _coerce_int(raw_max_pos, 10)
    return RiskConfig(
        risk_pct=float(os.getenv("RISK_PCT", cfg.get("risk_pct", 0.02))),
        max_positions=max_pos_val,
        max_pct=float(os.getenv("MAX_PCT", cfg.get("max_pct", 0.10))),
        portfolio=_build_portfolio_risk_config(cfg),
    )


def _build_sizing_config(cfg: dict[str, Any]) -> SizingConfig:
    """sizing セクション (+ env override) から SizingConfig を構築。

    env: SIZING_MODE, EQUITY_DEPLOY_PCT が YAML を上書きする。
    mode は未知値なら既定 "equity_linked" に、equity_deploy_pct は 0 以下/非数なら
    1.0 に安全フォールバック (誤設定で発注が消える/過大になるのを防ぐ)。
    """
    if not isinstance(cfg, dict):
        cfg = {}
    d = SizingConfig()

    raw_mode = str(os.getenv("SIZING_MODE", cfg.get("mode", d.mode))).strip().lower()
    mode = raw_mode if raw_mode in ("equity_linked", "fixed_tier") else d.mode

    try:
        pct = float(os.getenv("EQUITY_DEPLOY_PCT", cfg.get("equity_deploy_pct", d.equity_deploy_pct)))
    except (TypeError, ValueError):
        pct = d.equity_deploy_pct
    import math as _math

    if (not _math.isfinite(pct)) or pct <= 0:
        pct = d.equity_deploy_pct

    return SizingConfig(mode=mode, equity_deploy_pct=pct)


def _build_data_config(cfg: dict[str, Any], root: Path) -> DataConfig:
    return DataConfig(
        vendor=str(cfg.get("vendor", "EODHD")),
        eodhd_base=str(
            os.getenv(
                "API_EODHD_BASE", cfg.get("eodhd_base", "https://eodhistoricaldata.com")
            )
        ),
        api_key_env=str(cfg.get("api_key_env", "EODHD_API_KEY")),
        cache_dir=_as_path(
            root, os.getenv("DATA_CACHE_DIR", cfg.get("cache_dir", "data_cache"))
        ),
        cache_recent_dir=_as_path(
            root,
            os.getenv(
                "DATA_CACHE_RECENT_DIR",
                cfg.get("cache_recent_dir", "data_cache_recent"),
            ),
        ),
        max_workers=_env_int(
            "THREADS_DEFAULT", _coerce_int(cfg.get("max_workers", 8), 8)
        ),
        batch_size=_env_int("BATCH_SIZE", _coerce_int(cfg.get("batch_size", 100), 100)),
        request_timeout=_env_int(
            "REQUEST_TIMEOUT", _coerce_int(cfg.get("request_timeout", 10), 10)
        ),
        download_retries=_env_int(
            "DOWNLOAD_RETRIES", _coerce_int(cfg.get("download_retries", 3), 3)
        ),
        api_throttle_seconds=_env_float(
            "API_THROTTLE_SECONDS", float(cfg.get("api_throttle_seconds", 1.5))
        ),
    )


def _build_cache_config(cfg: dict[str, Any], root: Path) -> CacheConfig:
    rolling_cfg = cfg.get("rolling", {}) or {}

    # Determine final values before instantiation
    max_symbols_final = _positive_int_or_none(rolling_cfg.get("max_symbols"))
    env_override_raw = os.getenv("ROLLING_MAX_SYMBOLS")
    if env_override_raw is not None:
        override_val = _positive_int_or_none(env_override_raw)
        if not (override_val is None and env_override_raw.strip() not in {"", "0"}):
            max_symbols_final = override_val

    stale_days = _coerce_int(rolling_cfg.get("max_stale_days", 2), 2)
    staleness_days = _coerce_int(
        rolling_cfg.get("max_staleness_days", stale_days), stale_days
    )

    cache_round = _positive_int_or_none(
        os.getenv("CACHE_ROUND_DECIMALS", cfg.get("round_decimals"))
    )
    rolling_round = _positive_int_or_none(
        os.getenv("ROLLING_CACHE_ROUND_DECIMALS", rolling_cfg.get("round_decimals"))
    )

    return CacheConfig(
        full_dir=_as_path(root, cfg.get("full_dir", "data_cache/full_backup")),
        rolling_dir=_as_path(root, cfg.get("rolling_dir", "data_cache/rolling")),
        file_format=str(cfg.get("file_format", "auto")),
        round_decimals=cache_round,
        csv=CsvConfig(
            decimal_point=str(cfg.get("csv_decimal_point", ".")),
            thousands_sep=(
                str(cfg.get("csv_thousands_sep"))
                if cfg.get("csv_thousands_sep") is not None
                else None
            ),
            field_sep=str(cfg.get("csv_field_sep", ",")),
        ),
        rolling=CacheRollingConfig(
            base_lookback_days=_coerce_int(
                rolling_cfg.get("base_lookback_days", 300), 300
            ),
            buffer_days=_coerce_int(rolling_cfg.get("buffer_days", 30), 30),
            workers=_positive_int_or_none(rolling_cfg.get("workers")),
            max_staleness_days=staleness_days,
            prune_chunk_days=_coerce_int(rolling_cfg.get("prune_chunk_days", 30), 30),
            meta_file=str(rolling_cfg.get("meta_file", "_meta.json")),
            max_stale_days=stale_days,
            max_symbols=max_symbols_final,
            round_decimals=rolling_round,
            recompute_indicators_on_read=bool(
                rolling_cfg.get("recompute_indicators_on_read", True)
            ),
            adaptive_window_count=_coerce_int(
                rolling_cfg.get("adaptive_window_count", 8), 8
            ),
            adaptive_increase_threshold=float(
                rolling_cfg.get("adaptive_increase_threshold", 1.02)
            ),
            adaptive_decrease_threshold=float(
                rolling_cfg.get("adaptive_decrease_threshold", 0.98)
            ),
            adaptive_step=_coerce_int(rolling_cfg.get("adaptive_step", 1), 1),
            adaptive_min_workers=_coerce_int(
                rolling_cfg.get("adaptive_min_workers", 1), 1
            ),
            adaptive_max_workers=_positive_int_or_none(
                rolling_cfg.get("adaptive_max_workers")
            ),
            adaptive_report_seconds=_coerce_int(
                rolling_cfg.get("adaptive_report_seconds", 10), 10
            ),
            csv=CsvConfig(
                decimal_point=str(rolling_cfg.get("csv_decimal_point", ".")),
                thousands_sep=(
                    str(rolling_cfg.get("csv_thousands_sep"))
                    if rolling_cfg.get("csv_thousands_sep") is not None
                    else None
                ),
                field_sep=str(rolling_cfg.get("csv_field_sep", ",")),
            ),
        ),
    )


def _build_backtest_config(cfg: dict[str, Any]) -> BacktestConfig:
    max_symbols_val = _coerce_int(cfg.get("max_symbols", 500), 500)
    top_n_val = _coerce_int(cfg.get("top_n_rank", 50), 50)
    init_cap_val = _coerce_int(
        os.getenv("DEFAULT_CAPITAL", cfg.get("initial_capital", 100000)), 100000
    )
    return BacktestConfig(
        start_date=str(cfg.get("start_date", "2018-01-01")),
        end_date=str(cfg.get("end_date", "2024-12-31")),
        max_symbols=max_symbols_val,
        top_n_rank=top_n_val,
        initial_capital=init_cap_val,
    )


def _build_outputs_config(cfg: dict[str, Any], root: Path) -> OutputConfig:
    return OutputConfig(
        results_csv_dir=_as_path(
            root, os.getenv("RESULTS_DIR", cfg.get("results_csv_dir", "results_csv"))
        ),
        logs_dir=_as_path(root, os.getenv("LOGS_DIR", cfg.get("logs_dir", "logs"))),
        signals_dir=_as_path(root, cfg.get("signals_dir", "data_cache/signals")),
    )


def _build_logging_config(cfg: dict[str, Any]) -> LoggingConfig:
    return LoggingConfig(
        level=str(os.getenv("LOG_LEVEL", cfg.get("level", "INFO"))).upper(),
        rotation=str(cfg.get("rotation", "daily")),
        filename=str(os.getenv("LOG_FILENAME", cfg.get("filename", "app.log"))),
    )


def _build_scheduler_config(cfg: dict[str, Any]) -> SchedulerConfig:
    tz = cfg.get("timezone", "America/New_York")
    jobs_raw = cfg.get("jobs", []) or []
    jobs: list[SchedulerJob] = []
    for j in jobs_raw:
        try:
            jobs.append(SchedulerJob(name=j["name"], cron=j["cron"], task=j["task"]))
        except Exception:
            continue
    return SchedulerConfig(timezone=tz, jobs=tuple(jobs))


def _build_ui_config(cfg: dict[str, Any]) -> UIConfig:
    try:
        _dlr_raw = os.getenv(
            "DEFAULT_LONG_RATIO", str(cfg.get("default_long_ratio", 0.5))
        )
        _dlr = float(_dlr_raw)
    except Exception:
        _dlr = 0.5
    _dlr = max(0.0, min(1.0, _dlr))

    def _as_alloc_map(obj: Any, default_map: dict[str, float]) -> dict[str, float]:
        if not isinstance(obj, dict):
            return default_map
        out: dict[str, float] = {}
        for k, v in obj.items():
            try:
                out[str(k)] = float(v)
            except Exception:
                continue
        return out or default_map

    return UIConfig(
        default_capital=_coerce_int(
            os.getenv("DEFAULT_CAPITAL", cfg.get("default_capital", 100000)), 100000
        ),
        default_long_ratio=_dlr,
        long_allocations=_as_alloc_map(
            cfg.get("long_allocations"),
            {
                "system1": 0.25,
                "system3": 0.25,
                "system4": 0.25,
                "system5": 0.25,
            },
        ),
        short_allocations=_as_alloc_map(
            cfg.get("short_allocations"),
            {
                "system2": 0.40,
                "system6": 0.40,
                "system7": 0.20,
            },
        ),
        auto_tickers=tuple(cfg.get("auto_tickers", [])),
        debug_mode=str(
            os.getenv("DEBUG_MODE", str(cfg.get("debug_mode", False)))
        ).lower()
        in ("1", "true", "yes"),
        show_download_buttons=str(
            os.getenv(
                "SHOW_DOWNLOAD_BUTTONS", str(cfg.get("show_download_buttons", True))
            )
        ).lower()
        in ("1", "true", "yes"),
    )


# -----------------------------
# 公開 API
# -----------------------------


@lru_cache(maxsize=1)
def get_settings(create_dirs: bool = False) -> Settings:
    """設定を生成して返す。必要に応じて出力系ディレクトリを作成。"""
    root = PROJECT_ROOT

    try:
        cfg = _load_config_json_or_yaml_validated(root)
    except Exception:
        cfg = _load_yaml_config_validated(root)

    # 各セクションの設定を構築
    risk = _build_risk_config(cfg.get("risk", {}))
    sizing = _build_sizing_config(cfg.get("sizing", {}))
    data = _build_data_config(cfg.get("data", {}), root)
    cache_cfg_raw = cfg.get("cache") or (
        cfg.get("data", {}).get("cache") if isinstance(cfg.get("data"), dict) else {}
    )
    cache = _build_cache_config(cache_cfg_raw or {}, root)
    backtest = _build_backtest_config(cfg.get("backtest", {}))
    outputs = _build_outputs_config(cfg.get("outputs", {}), root)
    logging = _build_logging_config(cfg.get("logging", {}))
    scheduler = _build_scheduler_config(cfg.get("scheduler", {}))
    ui = _build_ui_config(cfg.get("ui", {}))
    strategies = cfg.get("strategies", {})

    # EODHD API キー（env 優先）
    api_key = os.getenv(data.api_key_env)

    settings = Settings(
        PROJECT_ROOT=root,
        DATA_CACHE_DIR=data.cache_dir,
        DATA_CACHE_RECENT_DIR=data.cache_recent_dir,
        RESULTS_DIR=outputs.results_csv_dir,
        LOGS_DIR=outputs.logs_dir,
        API_EODHD_BASE=data.eodhd_base,
        EODHD_API_KEY=api_key,
        REQUEST_TIMEOUT=data.request_timeout,
        DOWNLOAD_RETRIES=data.download_retries,
        API_THROTTLE_SECONDS=data.api_throttle_seconds,
        THREADS_DEFAULT=data.max_workers,
        MARKET_CAL_TZ=os.getenv("MARKET_CAL_TZ", "America/New_York"),
        risk=risk,
        sizing=sizing,
        data=data,
        cache=cache,
        backtest=backtest,
        outputs=outputs,
        logging=logging,
        scheduler=scheduler,
        ui=ui,
        strategies=strategies,
    )

    if create_dirs:
        for p in (
            settings.DATA_CACHE_DIR,
            settings.DATA_CACHE_RECENT_DIR,
            settings.RESULTS_DIR,
            settings.LOGS_DIR,
            settings.outputs.signals_dir,
        ):
            try:
                Path(p).mkdir(parents=True, exist_ok=True)
            except Exception:
                pass

    return settings


# 便利関数: システム固有パラメータ取得


def get_system_params(system_name: str) -> Mapping[str, Any]:
    """YAML の strategies.<system_name> を返す。未定義なら空辞書。"""
    s = get_settings(create_dirs=False)
    return s.strategies.get(system_name, {})


__all__ = [
    "Settings",
    "RiskConfig",
    "SizingConfig",
    "DataConfig",
    "CacheConfig",
    "CacheRollingConfig",
    "BacktestConfig",
    "OutputConfig",
    "LoggingConfig",
    "SchedulerConfig",
    "SchedulerJob",
    "UIConfig",
    "get_settings",
    "get_system_params",
]
