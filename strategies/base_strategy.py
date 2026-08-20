from abc import ABC, abstractmethod
import logging
import math
from typing import Any, Dict

import pandas as pd


class StrategyBase(ABC):
    """
    全戦略共通の抽象基底クラス。

    各戦略はこのクラスを継承し、必要メソッドを実装する。
    また、YAML のリスク設定とシステム固有設定を self.config に取り込む。

    Attributes:
        config: システム設定とリスク管理パラメータを含む辞書
    """

    config: Dict[str, Any]

    def __init__(self) -> None:
        """戦略の初期化を行い、設定を読み込む。"""
        logger = logging.getLogger(__name__)
        try:
            from config.settings import get_settings, get_system_params
        except ImportError as exc:
            logger.error("設定モジュールの読み込みに失敗: %s", exc)
            self.config = {}
            return

        try:
            settings = get_settings(create_dirs=False)
        except Exception as exc:
            logger.error("設定の初期化に失敗: %s", exc)
            self.config = {}
            return

        module = getattr(self.__class__, "__module__", "")
        sys_name = getattr(self, "SYSTEM_NAME", None)
        if not sys_name:
            parts = module.split(".")
            cand = next(
                (
                    p
                    for p in parts
                    if p.startswith("system") and any(ch.isdigit() for ch in p)
                ),
                None,
            )
            sys_name = cand or ""

        system_params: Dict[str, Any] = {}
        if sys_name:
            try:
                system_params = dict(get_system_params(sys_name))
            except Exception as exc:
                logger.warning("システム固有パラメータ取得失敗: %s", exc)

        cfg: Dict[str, Any] = {
            "risk_pct": settings.risk.risk_pct,
            "max_positions": settings.risk.max_positions,
            "max_pct": settings.risk.max_pct,
        }
        try:
            cfg.update(system_params or {})
        except Exception as exc:
            logger.warning("config update failed: %s", exc)
        self.config = cfg

        # Diagnostics payload populated by subclasses after candidate generation
        self.last_diagnostics: dict[str, Any] | None = None

    @abstractmethod
    def prepare_data(
        self,
        raw_data_or_symbols: dict,
        reuse_indicators: bool | None = None,
        **kwargs,
    ) -> dict:
        """
        生データからインジケーターやシグナルを計算。

        Args:
            raw_data_or_symbols: OHLCV生データまたはシンボル辞書
            reuse_indicators: インジケーターキャッシュの再利用フラグ
            **kwargs: その他のオプション引数

        Returns:
            インジケーター付きデータ辞書
        """

    @abstractmethod
    def generate_candidates(
        self, data_dict: dict, market_df: pd.DataFrame | None = None, **kwargs
    ) -> tuple[dict, pd.DataFrame | None]:
        """
        日別仕掛け候補を生成。

        Args:
            data_dict: インジケーター付きデータ辞書
            market_df: 市場データ（SPY等）
            **kwargs: その他のオプション引数

        Returns:
            (日別候補辞書, 市場データ) のタプル
        """

    def get_trading_side(self) -> str:
        """戦略の取引方向を返す（サブクラスでオーバーライド）"""
        return "long"  # デフォルトはロング

    def run_backtest(
        self, data_dict: dict, candidates_by_date: dict, capital: float, **kwargs
    ) -> pd.DataFrame:
        """
        仕掛け候補に基づくバックテストを実施（共通実装）。

        Args:
            data_dict: インジケーター付きデータ辞書
            candidates_by_date: 日別仕掛け候補
            capital: 初期資本
            **kwargs: その他のオプション引数

        Returns:
            トレード結果DataFrame
        """
        from common.backtest_utils import simulate_trades_with_risk

        on_progress = kwargs.get("on_progress", None)
        on_log = kwargs.get("on_log", None)
        side = self.get_trading_side()

        # side が "long" の場合は引数から除外（デフォルト動作）
        extra_kwargs = {}
        if side != "long":
            extra_kwargs["side"] = side

        trades_df, _ = simulate_trades_with_risk(
            candidates_by_date,
            data_dict,
            capital,
            self,
            on_progress=on_progress,
            on_log=on_log,
            **extra_kwargs,
        )
        return trades_df

    # ----------------------------
    # 共通ユーティリティ: 資金管理 & ポジションサイズ計算
    # ----------------------------
    def update_capital_with_exits(
        self, capital: float, active_positions: list, current_date
    ):
        """
        exit_date == current_date のポジションを決済して損益を反映。
        戻り値: (更新後capital, 未決済active_positions)
        """
        realized_pnl = sum(
            p["pnl"] for p in active_positions if p["exit_date"] == current_date
        )
        capital += realized_pnl
        active_positions = [
            p for p in active_positions if p["exit_date"] > current_date
        ]
        return capital, active_positions

    def calculate_position_size(
        self,
        capital: float,
        entry_price: float,
        stop_price: float,
        *,
        risk_pct: float | None = None,
        max_pct: float | None = None,
        **_unused,
    ) -> int:
        """リスク割合と最大投資割合から株数を算出する共通実装。"""

        risk = self._resolve_pct(risk_pct, "risk_pct", 0.02)
        max_alloc = self._resolve_pct(max_pct, "max_pct", 0.10)
        return self._calculate_position_size_core(
            capital,
            entry_price,
            stop_price,
            risk,
            max_alloc,
        )

    def _calculate_position_size_core(
        self,
        capital: float,
        entry_price: float,
        stop_price: float,
        risk_pct: float,
        max_pct: float,
    ) -> int:
        capital_val = self._safe_float(capital)
        entry_val = self._safe_float(entry_price)
        stop_val = self._safe_float(stop_price)
        if capital_val <= 0 or entry_val <= 0 or stop_val <= 0:
            return 0

        risk_pct = max(0.0, float(risk_pct))
        max_pct = max(0.0, float(max_pct))
        if risk_pct == 0.0 or max_pct == 0.0:
            return 0

        risk_per_trade = capital_val * risk_pct
        max_position_value = capital_val * max_pct

        risk_per_share = abs(entry_val - stop_val)
        if risk_per_share <= 0:
            return 0

        shares_by_risk = math.floor(risk_per_trade / risk_per_share)
        shares_by_capital = math.floor(max_position_value / entry_val)
        shares = min(shares_by_risk, shares_by_capital)

        if shares <= 0 and risk_per_trade >= risk_per_share:
            shares = 1

        return max(shares, 0)

    def _resolve_pct(
        self,
        override: float | None,
        config_key: str,
        default: float,
    ) -> float:
        try:
            if override is not None:
                value = float(override)
                if math.isfinite(value) and value >= 0:
                    return value
        except (TypeError, ValueError):
            pass

        try:
            if isinstance(self.config, dict) and config_key in self.config:
                value = float(self.config.get(config_key, default))
                if math.isfinite(value) and value >= 0:
                    return value
        except (TypeError, ValueError):
            pass
        return default

    @staticmethod
    def _safe_float(value: float | int | str | None) -> float:
        if value is None:
            return 0.0
        try:
            result = float(value)
        except (TypeError, ValueError):
            return 0.0
        if not math.isfinite(result):
            return 0.0
        return result

    # --- Perf Snapshot Helpers -------------------------------------------------
    @staticmethod
    def _compute_candidate_count(result: object) -> int | None:
        """generate_candidates 戻り値から候補件数を推定。

        パターン:
        - dict -> len(dict)
        - list/set/tuple -> len(collection)
        - (dict, DataFrame|None) -> 先頭要素が dict なら len(dict)
        - DataFrame -> len(df) （空なら 0）
        - 上記以外/None/例外時 -> None (不明扱い)
        呼び出し側は None をそのまま記録し、可視化層で区別できるようにする。
        """
        try:  # noqa: SIM105
            if result is None:
                return None
            if isinstance(result, tuple) and result and isinstance(result[0], dict):
                return len(result[0])
            if isinstance(result, dict):
                return len(result)
            if isinstance(result, (list, set, tuple)):
                return len(result)
            # DataFrame 直接返却パターン（戦略実装が DataFrame 単体を返すケースを補足）
            try:
                import pandas as _pd  # 局所 import（依存循環回避 & 軽量）

                if isinstance(result, _pd.DataFrame):
                    # 空 DataFrame も 0 として明示 (None ではなく 0) → UI 上は 0 件として表示
                    return int(len(result))
            except Exception:
                # pandas 未インポートや型変換失敗時は従来どおり None へフォールバック
                pass
        except Exception:  # pragma: no cover
            return None
        return None

    # ----------------------------
    # 当日シグナル抽出（共通関数）
    # ----------------------------
    def get_today_signals(
        self,
        raw_data_dict: dict,
        *,
        market_df: pd.DataFrame | None = None,
        today: pd.Timestamp | None = None,
        progress_callback=None,
        log_callback=None,
        stage_progress=None,
        use_process_pool: bool = False,
        max_workers: int | None = None,
        lookback_days: int | None = None,
    ) -> pd.DataFrame:
        """
        各 strategy の `prepare_data`/`generate_candidates` を流用し、
        最新営業日のみのシグナルを DataFrame で返す。

        戻り値カラム: symbol, system, side, signal_type, entry_date,
        entry_price, stop_price, score_key, score
        """
        from common.today_signals import get_today_signals_for_strategy

        return get_today_signals_for_strategy(
            self,
            raw_data_dict,
            market_df=market_df,
            today=today,
            progress_callback=progress_callback,
            log_callback=log_callback,
            stage_progress=stage_progress,
            use_process_pool=use_process_pool,
            max_workers=max_workers,
            lookback_days=lookback_days,
        )

    # ----------------------------
    # リファクタリング用共通メソッド（追加）
    # ----------------------------

    def _resolve_data_params(
        self,
        raw_data_or_symbols,
        use_process_pool: bool = False,
        **_kwargs,
    ):
        """
        データパラメータの共通解決処理

        Args:
            raw_data_or_symbols: 生データ辞書またはシンボルリスト
            use_process_pool: プロセスプール使用フラグ
            **_kwargs: 未使用の追加引数（前方互換性のため）

        Returns:
            (symbols, raw_dict): シンボルリストと生データ辞書のタプル
        """
        if isinstance(raw_data_or_symbols, dict):
            symbols = list(raw_data_or_symbols.keys())
            raw_dict = None if use_process_pool else raw_data_or_symbols
        else:
            symbols = list(raw_data_or_symbols)
            raw_dict = None
        return symbols, raw_dict

    # ----------------------------
    # 共通PnL計算メソッド
    # ----------------------------

    def compute_pnl_long(
        self,
        entry_price: float,
        exit_price: float,
        shares: int,
    ) -> float:
        """
        ロングポジションのPnL計算。

        Args:
            entry_price: エントリー価格
            exit_price: 決済価格
            shares: 株数

        Returns:
            損益
        """
        return (exit_price - entry_price) * shares

    def compute_pnl_short(
        self,
        entry_price: float,
        exit_price: float,
        shares: int,
    ) -> float:
        """
        ショートポジションのPnL計算。

        Args:
            entry_price: エントリー価格
            exit_price: 決済価格
            shares: 株数

        Returns:
            損益
        """
        return (entry_price - exit_price) * shares

    def _get_top_n_setting(self, top_n_override=None):
        """
        top_n設定の共通取得処理
        """
        if top_n_override is not None:
            try:
                return max(0, int(top_n_override))
            except Exception:
                return 10

        try:
            from config.settings import get_settings

            s = get_settings(create_dirs=False)
            # システム固有の top_n_rank を優先（未設定なら backtest.top_n_rank）
            sys_name = getattr(self, "SYSTEM_NAME", None)
            if sys_name and isinstance(s.strategies, dict):
                try:
                    per = s.strategies.get(sys_name, {})
                    if isinstance(per, dict) and "top_n_rank" in per:
                        return max(0, int(per.get("top_n_rank", s.backtest.top_n_rank)))
                except Exception:
                    pass
            return s.backtest.top_n_rank
        except Exception:
            return 10

    def _get_market_df(self, data_dict, market_df=None):
        """
        Market DataFrame（SPY）の共通取得処理
        """
        if market_df is None:
            market_df = data_dict.get("SPY")
            if market_df is None:
                raise ValueError("SPY data not found in data_dict.")
        return market_df

    def _get_batch_size_setting(self, data_size):
        """
        batch_size設定の共通取得処理
        """
        try:
            from common.utils import resolve_batch_size
            from config.settings import get_settings

            batch_size = get_settings(create_dirs=False).data.batch_size
            return resolve_batch_size(data_size, batch_size)
        except Exception:
            return min(100, max(10, data_size // 10))

    def _compute_entry_common(
        self,
        df,
        candidate,
        atr_column="atr20",
        stop_multiplier=None,
    ):
        """
        共通エントリー計算（ATRベースのストップロス）
        戻り値: (entry_price, stop_price, entry_idx) または None
        """
        try:
            entry_ts = pd.to_datetime(candidate["entry_date"]).normalize()
            # Use get_indexer to tolerate non-unique or non-exact match behavior
            idxer = pd.to_datetime(df.index).normalize().get_indexer([entry_ts])
            entry_idx = int(idxer[0]) if idxer.size > 0 else -1
        except Exception:
            return None

        if entry_idx <= 0 or entry_idx >= len(df):
            return None

        entry_price = float(df.iloc[entry_idx]["Open"])

        atr_value = None
        column_candidates = [atr_column]
        if isinstance(atr_column, str):
            column_candidates.append(atr_column.upper())
            column_candidates.append(atr_column.lower())

        for col in dict.fromkeys(column_candidates):
            try:
                atr_value = float(df.iloc[entry_idx - 1][col])
                break
            except Exception:
                continue

        if atr_value is None:
            return None

        if stop_multiplier is None:
            stop_multiplier = float(self.config.get("stop_atr_multiple", 3.0))

        stop_price = entry_price - stop_multiplier * atr_value
        # エントリー直後の想定リスク幅が 0 以下なら無効（データ異常/ATR異常）
        if entry_price - stop_price <= 0:
            return None

        return entry_price, stop_price, entry_idx

    @staticmethod
    def _bar_value(df, bar_idx: int, column: str) -> float | None:
        """バーの OHLC 値を大文字小文字を問わず取得する（取れなければ None）。"""
        row = None
        try:
            row = df.iloc[int(bar_idx)]
        except Exception:
            return None
        for col in (column, column.upper(), column.lower()):
            try:
                value = float(row[col])
            except Exception:
                continue
            if math.isfinite(value):
                return value
            return None
        return None

    def _limit_entry_filled(
        self,
        df,
        entry_idx: int,
        limit_price: float,
        side: str,
    ) -> bool:
        """指値エントリーが当日バーで実際に約定し得たかを判定する。

        System3/5/6 は前日終値から離した **指値** で仕掛ける。指値は板が
        そこまで来なければ約定しないため、バックテストでも「当日バーが指値を
        通過したか」を確認しなければ、実際には約定しなかった候補まで建玉として
        数えてしまう（勝率の過大評価）。

        判定規約は本エンジンが既に exit 側で使っているものと同一:
        ``compute_exit`` は ``Low <= stop`` / ``High >= target`` で到達を判定し、
        約定値はその指定価格そのものとする（窓開けした場合の有利約定は
        モデル化しない）。エントリーも同じ規約に揃える:

        - long  (buy limit) : ``Low[entry_idx]  <= limit_price``
        - short (sell limit): ``High[entry_idx] >= limit_price``

        窓開けで指値より有利に寄り付いた場合、実際の約定値は寄値
        (long なら limit 未満 / short なら limit 超) になるため、指値で
        約定したものとして扱う本規約は **保守的**（リターンを過小評価する）
        側に倒れる。

        Low/High が取得できない・NaN の場合は「約定しなかった」として扱う
        （fail-closed。存在しない建玉を作らない）。

        Args:
            df: 価格データ（エントリー日を含む）
            entry_idx: エントリー日の行番号
            limit_price: 指値
            side: ``"long"`` または ``"short"``

        Returns:
            約定し得たなら True
        """
        try:
            limit = float(limit_price)
        except (TypeError, ValueError):
            return False
        if not math.isfinite(limit):
            return False

        if str(side).lower() == "short":
            bar_high = self._bar_value(df, entry_idx, "High")
            if bar_high is None:
                return False
            return bar_high >= limit

        bar_low = self._bar_value(df, entry_idx, "Low")
        if bar_low is None:
            return False
        return bar_low <= limit

    def _prepare_data_with_fallback(
        self,
        core_prepare_func,
        raw_dict,
        symbols,
        **kwargs,
    ):
        """
        System1用のフォールバック付きprepare_data処理
        """
        try:
            return core_prepare_func(
                raw_dict,
                symbols=symbols,
                **kwargs,
            )
        except Exception as e:
            # フォールバック: プロセスプールを使わず・指標を再計算
            log_callback = kwargs.get("log_callback")
            if log_callback:
                try:
                    log_callback(
                        f"⚠️ {getattr(self, 'SYSTEM_NAME', 'unknown')}: "
                        "prepare_data 失敗のためフォールバック再試行"
                        "（非プール・再計算）: "
                        f"{e}"
                    )
                except Exception:
                    pass
            fb_kwargs = dict(kwargs)
            fb_kwargs["reuse_indicators"] = False
            fb_kwargs["use_process_pool"] = False
            return core_prepare_func(
                raw_dict,
                symbols=symbols,
                **fb_kwargs,
            )

    def _prepare_data_template(self, raw_data_or_symbols, core_prepare_func, **kwargs):
        """
        prepare_data の共通テンプレートメソッド
        """
        symbols, raw_dict = self._resolve_data_params(
            raw_data_or_symbols, kwargs.get("use_process_pool", False)
        )

        # batch_size の設定（use_process_pool=Falseの時のみ）
        use_process_pool = kwargs.get("use_process_pool", False)
        if (
            not use_process_pool
            and raw_dict is not None
            and kwargs.get("batch_size") is None
        ):
            kwargs["batch_size"] = self._get_batch_size_setting(len(raw_dict))

        # System1のフォールバック処理
        if (
            hasattr(self, "SYSTEM_NAME")
            and getattr(self, "SYSTEM_NAME", None) == "system1"
        ):
            return self._prepare_data_with_fallback(
                core_prepare_func,
                raw_dict,
                symbols,
                **kwargs,
            )
        else:
            return core_prepare_func(raw_dict, symbols=symbols, **kwargs)
