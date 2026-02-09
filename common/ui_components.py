"""
共通UIコンポーネント（UTF-8・日本語対応）。
既存の公開API（関数名・戻り値）は維持しつつ、各フェーズ（データ取得/インジ計算/候補抽出/バックテスト）で
UIManager（任意）に進捗とログを出力できるようにしている。
"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor, as_completed
import logging
import os
import time
from typing import Any, cast

import matplotlib as mpl
from matplotlib import font_manager as _font_manager
import matplotlib.pyplot as plt
import pandas as pd
import streamlit as st

from common.cache_format import round_dataframe
from common.utils import get_cached_data, safe_filename
from config.settings import get_settings

try:
    # 設定からUIフラグを参照（失敗時はデフォルト動作にフォールバック）
    from config.settings import get_settings

    _APP_SETTINGS = get_settings(create_dirs=True)
except Exception:
    _APP_SETTINGS = None
from common.cache_manager import base_cache_path, load_base_cache
from common.holding_tracker import generate_holding_matrix
import common.i18n as i18n
from common.logging_utils import log_with_progress
from scripts.tickers_loader import get_all_tickers

# 互換用エイリアス（既存コードの tr(...) 呼び出しを維持）
tr = i18n.tr

# 明示的公開API境界: tests/test_public_api_exports.py の EXPECTED_EXPORTS と同期すること。
__all__ = [
    "run_backtest_app",
    "prepare_backtest_data",
    "fetch_data",
    "show_results",
    "show_signal_trade_summary",
    "clean_date_column",
    "save_signal_and_trade_logs",
]


# ------------------------------
# Type overloads for static checkers
# ------------------------------
# overloads removed - keep concrete implementations only


# overloads removed - keep concrete implementations only


# overloads removed - keep concrete implementations only


# overloads removed - keep concrete implementations only


# overloads removed - keep concrete implementations only


# overloads removed - keep concrete implementations only


# 日本語表示のためのフォントフォールバック（Windows向け優先）
def _set_japanese_font_fallback() -> None:
    """日本語フォントをインストール済みのものだけに設定して警告を回避する。"""
    try:
        preferred = [
            "Noto Sans JP",
            "IPAexGothic",
            "Yu Gothic",
            "Meiryo",
            "MS Gothic",
            "Yu Gothic UI",
            "MS PGothic",
            "Hiragino Sans",
            "Hiragino Kaku Gothic ProN",
            "TakaoGothic",
            "DejaVu Sans",
        ]
        available = {f.name for f in _font_manager.fontManager.ttflist}
        chosen = [name for name in preferred if name in available]
        if not chosen:
            chosen = ["DejaVu Sans"]
        mpl.rcParams["font.family"] = chosen
        mpl.rcParams["axes.unicode_minus"] = False
    except Exception:
        pass


_set_japanese_font_fallback()

# matplotlib.font_manager の冗長な INFO を抑制
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)


# ------------------------------
# Small utilities
# ------------------------------
def clean_date_column(df: pd.DataFrame, col_name: str = "Date") -> pd.DataFrame:
    if col_name in df.columns:
        df = df.copy()
        df[col_name] = pd.to_datetime(df[col_name], errors="coerce")
        df = df.dropna(subset=[col_name])
    return df


def default_log_callback(
    processed: int, total: int, start_time: float, prefix: str = "📊 状況"
) -> str:
    elapsed = time.time() - start_time
    remain = (elapsed / processed) * (total - processed) if processed else 0
    return (
        f"{prefix}: {processed}/{total} 件 | 経過: {int(elapsed // 60)}分{int(elapsed % 60)}秒"
        f" / 残り目安: 約{int(remain // 60)}分{int(remain % 60)}秒"
    )


# ------------------------------
# Data fetch
# ------------------------------
def _mtime_or_zero(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except Exception:
        return 0.0


@st.cache_data(show_spinner=False)
def _load_symbol_cached(
    symbol: str, *, base_path: str, base_mtime: float, raw_path: str, raw_mtime: float
) -> tuple[str, pd.DataFrame | None]:
    """ファイルの更新時刻をキーに含めてキャッシュし、ファイル更新で自動無効化。
    戻り値は (symbol, DataFrame|None)
    """
    try:
        df = load_base_cache(
            symbol, rebuild_if_missing=True, prefer_precomputed_indicators=True
        )
        if df is not None and not df.empty:
            return symbol, df
    except Exception:
        pass
    if os.path.exists(raw_path):
        return symbol, get_cached_data(symbol)
    return symbol, None


def load_symbol(
    symbol: str, cache_dir: str = "data_cache"
) -> tuple[str, pd.DataFrame | None]:
    base_path = str(base_cache_path(symbol))
    raw_path = os.path.join(cache_dir, f"{safe_filename(symbol)}.csv")
    return _load_symbol_cached(
        symbol,
        base_path=base_path,
        base_mtime=_mtime_or_zero(base_path),
        raw_path=raw_path,
        raw_mtime=_mtime_or_zero(raw_path),
    )


def fetch_data(
    symbols, max_workers: int = 8, ui_manager=None, enable_debug_logs: bool = True
) -> dict[str, pd.DataFrame]:
    data_dict: dict[str, pd.DataFrame] = {}
    total = len(symbols)
    # UIManagerのフェーズ（fetch）があればそこへ出力
    phase = ui_manager.phase("fetch") if ui_manager else None
    if phase:
        progress_bar = phase.progress_bar
        log_area = phase.log_area
        # フェーズ配下に「no data」用の別スロットを確保（未作成なら生成）
        no_data_area = phase.no_data_area if hasattr(phase, "no_data_area") else None
        if no_data_area is None:
            try:
                no_data_area = phase.container.empty()
            except Exception:
                no_data_area = st.empty()
            try:
                phase.no_data_area = no_data_area
            except Exception:
                pass
        try:
            phase.info(tr("fetch: start | {total} symbols", total=total))
        except Exception:
            pass
    else:
        # UIManager なしの場合: 実行時に動的生成
        fetch_info_placeholder = st.empty()
        fetch_progress_placeholder = st.empty()
        fetch_log_placeholder = st.empty()
        no_data_placeholder = st.empty()

        fetch_info_placeholder.info(tr("fetch: start | {total} symbols", total=total))
        progress_bar = fetch_progress_placeholder.progress(0)
        log_area = fetch_log_placeholder
        # フェーズ未使用時は直下にno-data用スロットを用意
        no_data_area = no_data_placeholder
    buffer, skipped, start_time = [], [], time.time()

    with ThreadPoolExecutor(max_workers=max_workers) as executor:
        futures = {executor.submit(load_symbol, sym): sym for sym in symbols}
        for i, future in enumerate(as_completed(futures), 1):
            sym, df = future.result()
            if df is not None and not df.empty:
                data_dict[sym] = df
                buffer.append(sym)
            else:
                skipped.append(sym)

            if i % 50 == 0 or i == total:
                log_with_progress(
                    i,
                    total,
                    start_time,
                    prefix="データ取得",
                    batch=50,
                    log_func=(
                        (lambda msg: (log_area.text(msg), None)[1])
                        if hasattr(log_area, "text")
                        else None
                    ),
                    progress_func=(
                        (lambda val: (progress_bar.progress(val), None)[1])
                        if hasattr(progress_bar, "progress")
                        else None
                    ),
                    extra_msg=(f"銘柄: {', '.join(buffer)}" if buffer else None),
                    silent=not enable_debug_logs,
                )
                buffer.clear()

    try:
        progress_bar.empty()
    except Exception:
        pass
    if skipped:
        try:
            # use i18n message for skipped count, append symbols list
            # tr は kwargs を受けてフォーマット済み文字列を返すので .format は不要
            msg = tr("⚠️ no data: {n} symbols", n=len(skipped))
            # 長大なリストを避けるため、代表のみ（先頭10件）を表示
            _sample = list(skipped)[:10]
            msg = msg + "\n" + ", ".join(_sample)
            _rest = len(skipped) - len(_sample)
            if _rest > 0:
                msg += f"\n... (+{_rest} more)"
            # 取得ログを上書きせず、下の行に表示
            no_data_area.text(msg)
        except Exception:
            pass
    return data_dict


# ------------------------------
# Prepare + candidates
# ------------------------------


def prepare_backtest_data(
    strategy,
    symbols,
    system_name: str = "SystemX",
    spy_df: pd.DataFrame | None = None,
    ui_manager=None,
    use_process_pool: bool = False,
    enable_debug_logs: bool = True,
    fast_mode: bool = False,
    **kwargs,
):
    # 1) fetch
    if use_process_pool:
        data_dict = None
    else:
        data_dict = fetch_data(
            symbols, ui_manager=ui_manager, enable_debug_logs=enable_debug_logs
        )
        if not data_dict:
            st.error(tr("no valid data"))
            return None, None, None

    # 2) indicators (delegated to strategy)
    # indicators フェーズ
    ind_phase = ui_manager.phase("indicators") if ui_manager else None
    if ind_phase:
        try:
            ind_phase.info(tr("indicators: computing..."))
        except Exception:
            pass
        ind_progress = ind_phase.progress_bar
        ind_log = ind_phase.log_area
    else:
        # UIManager なしの場合: 実行時に動的生成
        ind_info_placeholder = st.empty()
        ind_progress_placeholder = st.empty()
        ind_log_placeholder = st.empty()

        ind_info_placeholder.info(tr("indicators: computing..."))
        ind_progress = ind_progress_placeholder.progress(0)
        ind_log = ind_log_placeholder

    if fast_mode and data_dict:
        # 履歴を末尾120行に短縮（存在する日付数に応じて）
        try:
            trimmed = {}
            for _sym, _df in data_dict.items():
                if hasattr(_df, "tail"):
                    trimmed[_sym] = _df.tail(120)
                else:
                    trimmed[_sym] = _df
            data_dict = trimmed
        except Exception:
            pass

    call_input = data_dict if not use_process_pool else symbols

    # 進捗カウンター（文字列メッセージをカウントに変換）
    progress_counter = {"count": 0}
    total_symbols = len(symbols)

    def _update_progress(msg_or_tuple):
        """進捗更新：文字列メッセージまたは(done, total)タプルの両方に対応"""
        try:
            if isinstance(msg_or_tuple, tuple) and len(msg_or_tuple) == 2:
                # (done, total) 形式
                done, total = msg_or_tuple
                if total > 0:
                    ind_progress.progress(done / total)
            else:
                # 文字列メッセージ形式（レガシー）
                progress_counter["count"] += 1
                if total_symbols > 0:
                    ind_progress.progress(progress_counter["count"] / total_symbols)
        except Exception:
            pass

    call_kwargs = dict(
        progress_callback=_update_progress,
        log_callback=lambda msg: ind_log.text(str(msg)),
        skip_callback=lambda msg: ind_log.text(str(msg)),
        fast_mode=fast_mode,
        **kwargs,
    )
    if use_process_pool:
        # cast to Any to satisfy narrow type checkers used in the repo
        call_kwargs["use_process_pool"] = cast(Any, True)

    try:
        prepared_dict = strategy.prepare_data(call_input, **call_kwargs)
    except TypeError:
        # 後方互換: 未対応パラメータを削除して再試行
        for _k in ["skip_callback", "use_process_pool", "fast_mode"]:
            call_kwargs.pop(_k, None)
        prepared_dict = strategy.prepare_data(call_input, **call_kwargs)
    try:
        ind_progress.empty()
    except Exception:
        pass

    # 3) candidates
    # candidates フェーズ
    cand_phase = ui_manager.phase("candidates") if ui_manager else None
    if cand_phase:
        try:
            cand_phase.info(tr("candidates: extracting..."))
        except Exception:
            pass
        cand_progress = cand_phase.progress_bar
    else:
        # UIManager なしの場合: 実行時に動的生成
        cand_info_placeholder = st.empty()
        cand_progress_placeholder = st.empty()

        cand_info_placeholder.info(tr("candidates: extracting..."))
        cand_progress = cand_progress_placeholder.progress(0)

    merged_df = None
    # すべてのシステムは strategy.generate_candidates を使う統一パス
    try:
        if system_name == "System4" and spy_df is not None:
            candidates_by_date = strategy.generate_candidates(
                prepared_dict,
                market_df=spy_df,
                **kwargs,
            )
        else:
            candidates_by_date = strategy.generate_candidates(
                prepared_dict,
                **kwargs,
            )
    except (TypeError, ValueError) as e:
        st.error(f"候補抽出エラー: {e}")
        return prepared_dict, None, None

    # 正常系でも (dict, df) を返す実装があるため後段で正規化
    if isinstance(candidates_by_date, tuple) and len(candidates_by_date) == 2:
        candidates_by_date, merged_df = candidates_by_date
    try:
        cand_progress.empty()
    except Exception:
        pass

    if not candidates_by_date:
        st.warning(tr("{system_name}: no candidates"))
        return prepared_dict, None, None

    return prepared_dict, candidates_by_date, merged_df


# ------------------------------
# Backtest execution (common wrapper)
# ------------------------------
def run_backtest_with_logging(
    strategy,
    prepared_dict,
    candidates_by_date,
    capital,
    system_name: str = "SystemX",
    ui_manager=None,
):
    bt_phase = ui_manager.phase("backtest") if ui_manager else None
    if bt_phase:
        try:
            bt_phase.info(tr("backtest: running..."))
        except Exception:
            pass
        progress = bt_phase.progress_bar
        log_area = bt_phase.log_area
        # 資金推移は最新行のみ、エクスパンダーは使わず単一プレースホルダに出力
        fund_log_area = (
            bt_phase.fund_log_area
            if hasattr(bt_phase, "fund_log_area")
            else bt_phase.container.empty()
        )
        try:
            bt_phase.fund_log_area = fund_log_area
        except Exception:
            pass
    else:
        # UIManager なしの場合: 実行時に動的生成
        bt_info_placeholder = st.empty()
        bt_progress_placeholder = st.empty()
        bt_log_placeholder = st.empty()
        bt_fund_log_placeholder = st.empty()

        bt_info_placeholder.info(tr("backtest: running..."))
        progress = bt_progress_placeholder.progress(0)
        log_area = bt_log_placeholder
        fund_log_area = bt_fund_log_placeholder

    debug_logs: list[str] = []

    def handle_log(msg):
        if isinstance(msg, str) and msg.startswith("💰"):
            # attempt to localize capital/active segments while preserving date
            import re

            s = str(msg)
            # Capital: 3812.31 USD -> 資金: 3812.31 USD
            s = re.sub(r"Capital:\s*([0-9\.,]+)\s*USD", r"資金: \1 USD", s)
            # Active: 0 -> 保有ポジション: 0
            s = re.sub(r"Active:\s*([0-9]+)", r"保有ポジション: \1", s)
            debug_logs.append(s)
            # 最新行のみを表示（差し替え）
            fund_log_area.text(s)
        else:
            log_area.text(str(msg))

    results_df = strategy.run_backtest(
        prepared_dict,
        candidates_by_date,
        capital,
        on_progress=lambda i, total, start: log_with_progress(
            i,
            total,
            start,
            prefix="bt",
            log_func=(
                (lambda msg: (log_area.text(msg), None)[1])
                if hasattr(log_area, "text")
                else None
            ),
            progress_func=(
                (lambda val: (progress.progress(val), None)[1])
                if hasattr(progress, "progress")
                else None
            ),
            unit="days",
        ),
        on_log=lambda msg: handle_log(msg),
    )

    try:
        progress.empty()
    except Exception:
        pass

    # ログをセッションへ保持（リランしても表示できるように）
    st.session_state[f"{system_name}_debug_logs"] = list(debug_logs)

    # システム固有のデバッグフラグをチェック
    debug_key = f"{system_name}_show_debug_logs"
    show_debug = st.session_state.get(debug_key, True)

    if show_debug and debug_logs:
        # ログはバックテスト・フェーズのコンテナ内に配置（システムごとにまとまるように）
        parent = bt_phase.container if bt_phase else st.container()
        # ユーザー要望: 取引ログはエクスパンダーで折りたたみ表示
        title = f"💰 {tr('trade logs')}"
        with parent.expander(title, expanded=False):
            # text_area の方が行間・スクロールで視認性が高い
            st.text_area(
                "Logs",
                "\n".join(debug_logs),
                height=300,
            )

    # 結果も併せてセッションに保存（UI層でも保存するが二重でも安全）
    st.session_state[f"{system_name}_results_df"] = results_df
    return results_df


# ------------------------------
# App entry for a single system tab
# ------------------------------


def run_backtest_app(
    strategy,
    system_name: str = "SystemX",
    limit_symbols: int = 10,
    system_title: str | None = None,
    spy_df: pd.DataFrame | None = None,
    ui_manager=None,
    **kwargs,
):
    st.title(system_title or f"{system_name} backtest")
    # モード表示用のプレースホルダー（タイトル直下）
    mode_caption_placeholder = st.empty()

    # --- サイドバーに設定UIを統合 ---
    with st.sidebar:
        st.subheader(tr("backtest settings"))

        debug_key = f"{system_name}_show_debug_logs"
        if debug_key not in st.session_state:
            st.session_state[debug_key] = True
        st.checkbox(tr("show debug logs"), key=debug_key)

        # 自動ユニバース利用フラグ (普通株ユニバース) 復元
        auto_key = f"{system_name}_auto"
        if auto_key not in st.session_state:
            st.session_state[auto_key] = True
        use_auto = st.checkbox(
            tr("auto symbols (common stocks)"),
            value=st.session_state[auto_key],
            key=auto_key,
        )
        # 普通株 全銘柄を一括利用するオプション（制限数入力を無視する）
        all_common_key = f"{system_name}_use_all_common"
        st.checkbox(
            tr("use full common stocks universe"), value=True, key=all_common_key
        )
        # Fast Preview / 挙動確認モード (MVP)
        fast_key = f"{system_name}_fast_mode"
        if fast_key not in st.session_state:
            st.session_state[fast_key] = False
        from common.i18n import get_language as _get_lang  # 遅延 import

        st.checkbox(
            (
                "Fast Preview Mode"
                if _get_lang() == "en"
                else tr("fast preview mode (mvp)")
            ),
            key=fast_key,
            help=(
                "Skip heavy indicators & shorten lookback (~120d) for quicker approximate preview"
                if _get_lang() == "en"
                else "重い指標を省略し履歴を約120日に短縮して高速に近似結果を表示"
            ),
        )

        _init_cap = int(st.session_state.get(f"{system_name}_capital_saved", 100000))
        capital = st.number_input(
            tr("capital (USD)"),
            min_value=1000,
            value=_init_cap,
            step=100,
            key=f"{system_name}_capital",
        )

        # 常に通常株（普通株のみ、~6200銘柄）を使用（11800全銘柄オプション廃止）
        # 簡易キャッシュ（プロセス内）: 毎回ロードを避ける
        global _COMMON_STOCKS_CACHE  # type: ignore
        if "_COMMON_STOCKS_CACHE" not in globals():  # 初回定義
            _COMMON_STOCKS_CACHE = None  # type: ignore

        if _COMMON_STOCKS_CACHE is None:
            try:
                from scripts.tickers_loader import get_common_stocks_only

                _COMMON_STOCKS_CACHE = list(get_common_stocks_only())  # type: ignore
            except ImportError:
                _COMMON_STOCKS_CACHE = list(get_all_tickers())  # type: ignore
            except Exception:
                _COMMON_STOCKS_CACHE = list(get_all_tickers())  # type: ignore

        all_tickers = _COMMON_STOCKS_CACHE  # type: ignore

        max_allowed = len(all_tickers)
        default_value = min(10, max_allowed)
        # 全銘柄選択時の銘柄総数表示 & パフォーマンス警告 + 過去ログから推定時間
        if st.session_state.get(all_common_key, False):
            st.caption(tr("using all common stocks: {n} symbols", n=max_allowed))
            if max_allowed > 3000:
                st.warning(
                    tr(
                        "large universe may slow processing (>{th} symbols)",
                        th=3000,
                    )
                )
            # 過去の実行時間ログ(JSONL)から推定（単純に同Universeサイズ近傍を対象）
            try:
                import json
                import math
                from pathlib import Path
                import statistics

                settings = get_settings(create_dirs=True)
                perf_dir = Path(settings.LOGS_DIR) / "perf_estimates"
                perf_dir.mkdir(parents=True, exist_ok=True)
                history_path = perf_dir / f"{system_name}_universe_times.jsonl"

                # 既存ログ読み込み
                sizes: list[int] = []
                durations: list[float] = []
                if history_path.exists():
                    with history_path.open("r", encoding="utf-8") as fh:
                        for line in fh:
                            try:
                                rec = json.loads(line)
                                sizes.append(int(rec.get("size", 0)))
                                durations.append(float(rec.get("seconds", 0.0)))
                            except Exception:
                                continue
                # 近傍 (±5%) のサイズサンプルを抽出
                est_seconds: float | None = None
                p25 = p75 = None
                if sizes:
                    target_min = max_allowed * 0.95
                    target_max = max_allowed * 1.05
                    filt = [
                        d
                        for s, d in zip(sizes, durations)
                        if target_min <= s <= target_max and d > 0
                    ]
                    if len(filt) >= 3:
                        filt.sort()
                        p25 = filt[max(0, math.floor(len(filt) * 0.25) - 1)]
                        p75 = filt[min(len(filt) - 1, math.floor(len(filt) * 0.75))]
                        est_seconds = statistics.median(filt)
                if est_seconds is not None and p25 is not None and p75 is not None:
                    st.caption(
                        tr(
                            "estimated processing time: median {m:.1f}s (p25={p25:.1f}s / p75={p75:.1f}s)",
                            m=est_seconds,
                            p25=p25,
                            p75=p75,
                        )
                    )
            except Exception:
                pass

        if system_name != "System7" and not st.session_state.get(all_common_key, False):
            limit_symbols = st.number_input(
                tr("symbol limit"),
                min_value=1,
                max_value=max_allowed,
                value=default_value,
                step=1,
                key=f"{system_name}_limit",
            )
            # 全銘柄使用オプションは廃止（上限指定のみ）

        symbols_input = None
        if not use_auto:
            symbols_input = st.text_input(
                tr("symbols (comma separated)"),
                "AAPL,MSFT,TSLA,NVDA,META",
                key=f"{system_name}_symbols_main",
            )

        # 通知トグル（サイドバーへ移動）
        if system_name in (
            "System1",
            "System2",
            "System3",
            "System4",
            "System5",
            "System6",
            "System7",
        ):
            _notify_key = f"{system_name}_notify_backtest"
            if _notify_key not in st.session_state:
                st.session_state[_notify_key] = True
            _label = tr("バックテスト結果を通知する（Webhook）")
            try:
                _use_toggle = hasattr(st, "toggle")
            except Exception:
                _use_toggle = False
            if _use_toggle:
                st.toggle(_label, key=_notify_key)
            else:
                st.checkbox(_label, key=_notify_key)
            try:
                import os as _os

                if not (
                    _os.getenv("SLACK_BOT_TOKEN")
                    or _os.getenv("DISCORD_WEBHOOK_URL")
                    or _os.getenv("DISCORD_WEBHOOK_URL_SIGNALS")
                    or _os.getenv("DISCORD_WEBHOOK_URL_EQUITY")
                    or _os.getenv("DISCORD_WEBHOOK_URL_LOGS")
                ):
                    st.caption(tr("Webhook/Bot 設定が未設定です（.env を確認）"))
            except Exception:
                pass

    # --- メイン領域: 前回実行結果の表示/クリア（セッション保持） ---
    key_results = f"{system_name}_results_df"
    key_prepared = f"{system_name}_prepared_dict"
    key_cands = f"{system_name}_candidates_by_date"
    key_capital = f"{system_name}_capital"
    key_capital_saved = f"{system_name}_capital_saved"
    key_merged = f"{system_name}_merged_df"
    key_debug = f"{system_name}_debug_logs"

    has_prev = any(
        k in st.session_state
        for k in [key_results, key_cands, f"{system_name}_capital_saved"]
    )
    if has_prev:
        with st.expander("前回の結果（リランでも保持）", expanded=False):
            prev_res = st.session_state.get(key_results)
            prev_cap = st.session_state.get(
                key_capital_saved, st.session_state.get(key_capital, 0)
            )
            if prev_res is not None and getattr(prev_res, "empty", False) is False:
                show_results(prev_res, prev_cap, system_name, key_context="prev")
            dbg = st.session_state.get(key_debug)
            if dbg:
                st.markdown("**保存済み 取引ログ**")
                st.text("\n".join(map(str, dbg)))
            if st.button(tr("保存済み結果をクリア"), key=f"{system_name}_clear_saved"):
                for k in [
                    key_results,
                    key_prepared,
                    key_cands,
                    key_capital_saved,
                    key_capital,
                    key_merged,
                    key_debug,
                ]:
                    if k in st.session_state:
                        del st.session_state[k]
                rerun = getattr(st, "experimental_rerun", None)
                if callable(rerun):
                    try:
                        rerun()
                    except Exception:
                        pass

    if st.button(tr("clear streamlit cache"), key=f"{system_name}_clear_cache"):
        st.cache_data.clear()
        st.success(tr("cache cleared"))

    # シンボル選択処理（サイドバーで定義済みの変数を使用）
    if system_name == "System7":
        symbols = ["SPY"]
    elif use_auto:
        if st.session_state.get(f"{system_name}_use_all_common", False):
            symbols = all_tickers  # 全普通株
        else:
            symbols = all_tickers[:limit_symbols]
    else:
        if not symbols_input:
            st.error(tr("please input symbols"))
            return None, None, None, None, None
        symbols = [s.strip().upper() for s in symbols_input.split(",")]

    run_clicked = st.button(tr("run"), key=f"{system_name}_run")
    fast_mode_flag = bool(st.session_state.get(f"{system_name}_fast_mode", False))
    # タイトル下に現在モードを表示（即時更新）
    try:
        from common.i18n import get_language as _get_lang2

        if fast_mode_flag:
            if _get_lang2() == "en":
                mode_caption_placeholder.caption("Mode: Fast Preview (approximate)")
            else:
                mode_caption_placeholder.caption(
                    tr("fast preview mode enabled (approximate results)")
                )
        else:
            if _get_lang2() == "en":
                mode_caption_placeholder.caption("Mode: Normal")
            else:
                mode_caption_placeholder.caption(tr("mode: normal"))
    except Exception:
        pass

    # 実行ボタンクリック後に動的にプレースホルダーを生成
    if run_clicked:
        # バックテスト実行領域を動的生成（実行前は表示されない）
        result_area = st.container()
        with result_area:
            # --- timing start (stdout) ---
            from datetime import datetime as _dt

            _t_start = _dt.now()
            try:
                import json
                import os
                import pathlib

                use_color = os.getenv("BACKTEST_COLOR", "1") != "0"
                use_json = os.getenv("BACKTEST_JSON", "1") != "0"
                mode_txt = "FAST" if fast_mode_flag else "NORMAL"
                # run_id: 1 回の UI 実行単位
                import uuid

                run_id = os.getenv("BACKTEST_RUN_ID") or uuid.uuid4().hex[:12]
                os.environ["BACKTEST_RUN_ID"] = run_id  # Downstream (Slack) 使用向け
                # ANSI colors
                C = {
                    "reset": "\u001b[0m",
                    "cyan": "\u001b[36m",
                    "green": "\u001b[32m",
                    "yellow": "\u001b[33m",
                    "magenta": "\u001b[35m",
                    "bold": "\u001b[1m",
                }
                if not use_color:
                    for k in list(C.keys()):
                        C[k] = ""
                start_lines = [
                    "",
                    f"{C['cyan']}=============================={C['reset']}",
                    f"{C['bold']}🚀 バックテスト開始{C['reset']}: {system_name}",
                    f"🕒 開始時刻: {_t_start:%Y-%m-%d %H:%M:%S}",
                    f"📊 対象シンボル数: {len(symbols)}",
                    f"🆔 Run ID: {run_id}",
                    f"モード: {mode_txt}",
                    f"{C['cyan']}=============================={C['reset']}",
                ]
                print("\n".join(start_lines), flush=True)
                try:
                    # UI 側にも Run ID を表示する小パネル
                    st.info(f"Run ID: {run_id}")
                except Exception:
                    pass
                if use_json:
                    try:
                        settings_local = get_settings(create_dirs=True)
                        log_dir = (
                            pathlib.Path(settings_local.LOGS_DIR) / "backtest_events"
                        )
                    except Exception:
                        log_dir = pathlib.Path("logs") / "backtest_events"
                    log_dir.mkdir(parents=True, exist_ok=True)
                    rec = {
                        "event": "start",
                        "system": system_name,
                        "timestamp": _t_start.isoformat(),
                        "symbols": len(symbols),
                        "mode": mode_txt,
                        "run_id": run_id,
                        "status": "running",
                        "exception": None,
                    }
                    with (log_dir / f"{system_name.lower()}_events.jsonl").open(
                        "a", encoding="utf-8"
                    ) as jf:
                        jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
            except Exception:
                pass
            prepared_dict, candidates_by_date, merged_df = prepare_backtest_data(
                strategy,
                symbols,
                system_name=system_name,
                spy_df=spy_df,
                ui_manager=ui_manager,
                enable_debug_logs=st.session_state.get(debug_key, True),
                fast_mode=fast_mode_flag,
                **kwargs,
            )
            if candidates_by_date is None:
                return None, None, None, None, None

            results_df = run_backtest_with_logging(
                strategy,
                prepared_dict,
                candidates_by_date,
                capital,
                system_name,
                ui_manager=ui_manager,
            )
            # fast_mode 列付与（後工程で利用者が識別できるように）
            try:
                if results_df is not None and not results_df.empty:
                    results_df = results_df.copy()
                    results_df["mode"] = "fast" if fast_mode_flag else "normal"
            except Exception:
                pass
            show_results(results_df, capital, system_name, key_context="curr")

            # fast_mode 実行後に通常モードでの再実行ボタンを提供
            try:
                if fast_mode_flag:
                    if st.button(
                        tr("re-run in normal mode"), key=f"{system_name}_rerun_normal"
                    ):
                        # フラグを False にして再実行。ユーザー操作簡略化。
                        st.session_state[f"{system_name}_fast_mode"] = False
                        rerun = getattr(st, "experimental_rerun", None)
                        if callable(rerun):
                            try:
                                rerun()
                            except Exception:
                                pass
            except Exception:
                pass

            # セッションへ保存（リラン対策）
            st.session_state[key_results] = results_df
            st.session_state[key_prepared] = prepared_dict
            st.session_state[key_cands] = candidates_by_date
            st.session_state[key_capital_saved] = capital
            if merged_df is not None:
                st.session_state[key_merged] = merged_df

            if system_name == "System1":
                return results_df, merged_df, prepared_dict, capital, candidates_by_date
            else:
                # --- timing end (stdout) ---
                try:
                    import json
                    import os
                    import pathlib

                    use_color = os.getenv("BACKTEST_COLOR", "1") != "0"
                    use_json = os.getenv("BACKTEST_JSON", "1") != "0"
                    run_id = os.getenv("BACKTEST_RUN_ID") or "unknown"
                    _t_end = _dt.now()
                    _elapsed = (_t_end - _t_start).total_seconds()
                    h = int(_elapsed // 3600)
                    m = int((_elapsed % 3600) // 60)
                    s = _elapsed % 60
                    trades_cnt = 0
                    try:
                        if (
                            results_df is not None
                            and hasattr(results_df, "empty")
                            and not results_df.empty
                        ):
                            trades_cnt = len(results_df)
                    except Exception:
                        trades_cnt = 0
                    C = {
                        "reset": "\u001b[0m",
                        "cyan": "\u001b[36m",
                        "green": "\u001b[32m",
                        "yellow": "\u001b[33m",
                        "magenta": "\u001b[35m",
                        "bold": "\u001b[1m",
                    }
                    if not use_color:
                        for k in list(C.keys()):
                            C[k] = ""
                    mode_txt = "FAST" if fast_mode_flag else "NORMAL"
                    # 視認性向上: 0埋め H:MM:SS と合計秒、両方を表示
                    total_fmt = f"{h}:{m:02d}:{int(s):02d}"  # 例 0:03:52
                    # 長時間色付け(> 15分)で注意を引く
                    warn = use_color and _elapsed > 900
                    elapsed_line = (
                        f"⏱️ 所要時間: {total_fmt} (合計 {_elapsed:.2f} 秒)"
                        if not warn
                        else f"⏱️ 所要時間: {C['yellow']}{total_fmt}{C['reset']} (合計 {_elapsed:.2f} 秒)"
                    )
                    end_lines = [
                        "",
                        f"{C['green']}=============================={C['reset']}",
                        f"{C['bold']}✅ バックテスト完了{C['reset']}: {system_name}",
                        f"🕒 終了時刻: {_t_end:%Y-%m-%d %H:%M:%S}",
                        elapsed_line,
                        f"📊 取引件数: {trades_cnt} / シンボル数: {len(symbols)}",
                        f"🆔 Run ID: {run_id}",
                        f"モード: {mode_txt}",
                        f"{C['green']}=============================={C['reset']}",
                        "",
                    ]
                    print("\n".join(end_lines), flush=True)
                    if use_json:
                        try:
                            settings_local = get_settings(create_dirs=True)
                            log_dir = (
                                pathlib.Path(settings_local.LOGS_DIR)
                                / "backtest_events"
                            )
                        except Exception:
                            log_dir = pathlib.Path("logs") / "backtest_events"
                        log_dir.mkdir(parents=True, exist_ok=True)
                        rec = {
                            "event": "end",
                            "system": system_name,
                            "timestamp": _t_end.isoformat(),
                            "elapsed_sec": round(_elapsed, 3),
                            "elapsed_hms": {"h": h, "m": m, "s": round(s, 3)},
                            "symbols": len(symbols),
                            "trades": trades_cnt,
                            "mode": mode_txt,
                            "run_id": run_id,
                            "status": "success",
                            "exception": None,
                        }
                        with (log_dir / f"{system_name.lower()}_events.jsonl").open(
                            "a", encoding="utf-8"
                        ) as jf:
                            jf.write(json.dumps(rec, ensure_ascii=False) + "\n")
                except Exception:
                    pass
                return results_df, None, prepared_dict, capital, candidates_by_date
    return None, None, None, None, None


# ------------------------------
# Heatmap silent export helper
# ------------------------------
def _export_holding_heatmap_silent(matrix, system_name: str) -> None:
    """Export holding matrix to CSV (and optionally PNG in future) without rendering.

    The UI no longer renders the heatmap directly to avoid vertical clutter and
    performance overhead. We still persist a CSV so users can download or use it
    for offline analysis. PNG export can be added later if required.
    """
    from pathlib import Path

    try:
        from .logging_utils import get_logger  # type: ignore
    except Exception:  # pragma: no cover - fallback when logger util unavailable
        get_logger = None  # type: ignore

    try:
        settings = get_settings(create_dirs=True)
        base_dir = Path(getattr(settings.cache, "base_dir", "data_cache"))  # type: ignore[arg-type]
        out_dir = base_dir / "exports" / "holding_heatmaps"
        out_dir.mkdir(parents=True, exist_ok=True)
        out_path = out_dir / f"holding_status_{system_name}.csv"
        matrix.to_csv(out_path)
        if get_logger:
            try:
                logger = get_logger()
                logger.info(
                    "heatmap csv exported",
                    extra={"system": system_name, "path": str(out_path)},
                )
            except Exception:
                pass
    except Exception:
        # 非致命: 失敗しても UI で warning 済み
        pass


# ------------------------------
# Rendering helpers
# ------------------------------
def summarize_results(results_df: pd.DataFrame, capital: float):
    # 防御: 必須列不足時は空サマリ返却（呼び出し側でinfo表示済）
    required_cols = {"entry_date", "exit_date"}
    if (
        results_df is None
        or results_df.empty
        or not required_cols.issubset(set(results_df.columns))
    ):
        return {
            "trades": 0,
            "total_return": 0.0,
            "win_rate": 0.0,
            "max_dd": 0.0,
        }, pd.DataFrame(
            columns=["cumulative_pnl"]
        )  # type: ignore
    df = results_df.copy()

    # 日付を確実に日時型に
    df["entry_date"] = pd.to_datetime(df["entry_date"])
    df["exit_date"] = pd.to_datetime(df["exit_date"])

    # 基本集計
    df = df.sort_values("exit_date").reset_index(drop=True)
    trades = len(df)
    total_return = float(df["pnl"].sum()) if "pnl" in df.columns else 0.0
    wins = int((df["pnl"] > 0).sum()) if "pnl" in df.columns else 0
    win_rate = (wins / trades * 100.0) if trades > 0 else 0.0

    # exit_date 基準で累積PnL を作成（グラフ用）
    df2 = df.copy()
    if "pnl" in df2.columns:
        df2["cumulative_pnl"] = df2["pnl"].cumsum()
    else:
        df2["cumulative_pnl"] = 0.0

    # 日次保有状態・エクイティ等（簡易版）
    # cumulative_pnl からドローダウンを計算
    try:
        cum = df2["cumulative_pnl"].astype(float)
        dd_series = cum - cum.cummax()
        max_dd = float(abs(dd_series.min()))
    except Exception:
        max_dd = 0.0

    summary = {
        "trades": int(trades),
        "total_return": float(total_return),
        "win_rate": float(win_rate),
        "max_dd": float(max_dd),
    }

    # 呼び出し元は (summary, df2) を期待しているため返す
    return summary, df2


def show_results(
    results_df: pd.DataFrame,
    capital: float,
    system_name: str = "SystemX",
    *,
    key_context: str = "main",
):
    # 追加防御: results_dfが期待列を欠く場合は早期returnでUI崩壊防止
    minimal_cols = {"entry_date", "exit_date"}
    if (
        results_df is None
        or results_df.empty
        or not minimal_cols.issubset(set(results_df.columns))
    ):
        st.info(i18n.tr("no trades"))
        return

    st.success(i18n.tr("backtest finished"))
    st.subheader(i18n.tr("results"))
    st.dataframe(results_df)

    # デバッグ: 列名・型・先頭数行を表示（max drawdown が0の原因確認用、確認後は削除してください）
    # removed debug: results_df.head()
    # removed debug: results_df.columns
    # removed debug: results_df.dtypes

    # 一部環境で summarize_results が 2 引数版でラップされていることがあるため、
    # system_name 固有のデバッグフラグを一時的に共通キーへコピーしてから呼び出す
    try:
        prev_flag = st.session_state.get("show_debug_logs", None)
        # system_name 固有フラグがあれば優先して一時的にセット
        sys_flag = st.session_state.get(f"{system_name}_show_debug_logs", None)
        if sys_flag is not None:
            st.session_state["show_debug_logs"] = sys_flag
    except Exception:
        prev_flag = None

    # 互換呼び出し（2 引数版でも動作するようにする）
    summary, df2 = summarize_results(results_df, capital)
    # 最大ドローダウンを再計算して summary に反映（表示のゼロを防止）
    try:
        cum = df2["cumulative_pnl"].astype(float)
        dd_series = cum - cum.cummax()
        max_dd_val = float(abs(dd_series.min()))
        try:
            summary["max_dd"] = max_dd_val
        except Exception:
            pass
    except Exception:
        pass

    # フラグを元に戻す
    try:
        if prev_flag is None:
            if "show_debug_logs" in st.session_state:
                del st.session_state["show_debug_logs"]
        else:
            st.session_state["show_debug_logs"] = prev_flag
    except Exception:
        pass

    # Series/Dict いずれにも安全に対応し、欠損キーは 0 扱い
    if isinstance(summary, pd.Series):
        summary = summary.to_dict()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("取引数", int(summary.get("trades", 0)))
    col2.metric("合計損益", f"{float(summary.get('total_return', 0.0)):.2f}")
    col3.metric("勝率 (%)", f"{float(summary.get('win_rate', 0.0)):.2f}")
    col4.metric("最大ドローダウン", f"{float(summary.get('max_dd', 0.0)):.2f}")

    st.subheader(i18n.tr("cumulative pnl"))
    # 日本語を軸ラベルに使う際のフォントフォールバック設定（環境にあるフォントを優先して選択）
    try:
        _set_japanese_font_fallback()
    except Exception:
        pass
    plt.figure(figsize=(10, 4))
    plt.plot(df2["exit_date"], df2["cumulative_pnl"], label="CumPnL")
    # Drawdown（累積損益のピークからの下落）を赤線で重ねる
    try:
        cum = df2["cumulative_pnl"].astype(float)
        dd = cum - cum.cummax()
        plt.plot(df2["exit_date"], dd, color="red", linewidth=1.2, label="Drawdown")
    except Exception:
        pass
    plt.xlabel(i18n.tr("date"))
    plt.ylabel(i18n.tr("pnl"))
    plt.legend()
    # streamlit.pyplot には Figure を渡す（plt モジュールそのものを渡さない）
    try:
        fig = plt.gcf()
        st.pyplot(fig)
    except Exception:
        # フォールバック: 直接渡すのは避けるがエラー時は無視
        pass

    st.subheader(i18n.tr("yearly summary"))
    if len(df2) > 0:
        yearly = (
            df2.groupby(df2["exit_date"].dt.to_period("Y"))["pnl"].sum().reset_index()
        )
        yearly["損益"] = yearly["pnl"].round(2)
        yearly["リターン(%)"] = yearly["pnl"] / (capital if capital else 1) * 100
        yearly = yearly.rename(columns={"exit_date": "年"})
        st.dataframe(
            yearly[["年", "損益", "リターン(%)"]].style.format(
                {"損益": "{:.2f}", "リターン(%)": "{:.1f}%"}
            )
        )
    else:
        st.info("トレードデータがありません")

    st.subheader(i18n.tr("monthly summary"))
    if len(df2) > 0:
        monthly = (
            df2.groupby(df2["exit_date"].dt.to_period("M"))["pnl"].sum().reset_index()
        )
        monthly["損益"] = monthly["pnl"].round(2)
        monthly["リターン(%)"] = monthly["pnl"] / (capital if capital else 1) * 100
        monthly = monthly.rename(columns={"exit_date": "月"})
        st.dataframe(
            monthly[["月", "損益", "リターン(%)"]].style.format(
                {"損益": "{:.2f}", "リターン(%)": "{:.1f}%"}
            )
        )
    else:
        st.info("トレードデータがありません")

    # (仕様変更) ヒートマップは UI へ描画せず、後続のサイレントエクスポートのみ実行
    # 利用者へは軽量な完了情報のみ提供。
    try:
        export_start = time.time()
        holding_matrix = generate_holding_matrix(df2)
        _export_holding_heatmap_silent(holding_matrix, system_name)
        st.caption(i18n.tr("heatmap generated"))
        st.session_state[f"{system_name}_heatmap_export_secs"] = round(
            time.time() - export_start, 3
        )
    except Exception as _e:
        # 失敗しても致命的でないため warning のみに留める
        st.warning(f"heatmap export skipped: {_e}")


def show_signal_trade_summary(
    source_df, trades_df, system_name: str, display_name: str | None = None
):
    if system_name == "System1" and isinstance(source_df, pd.DataFrame):
        signal_counts = source_df["symbol"].value_counts().reset_index()
        signal_counts.columns = ["symbol", "Signal_Count"]
    else:
        signal_counts = {
            sym: int(df.get("setup", pd.Series(dtype=int)).sum())
            for sym, df in (source_df or {}).items()
        }
        signal_counts = pd.DataFrame(
            signal_counts.items(), columns=["symbol", "Signal_Count"]
        )

    if trades_df is not None and not trades_df.empty:
        trade_counts = (
            trades_df.groupby("symbol").size().reset_index(name="Trade_Count")
        )
    else:
        trade_counts = pd.DataFrame(columns=["symbol", "Trade_Count"])

    summary_df = pd.merge(signal_counts, trade_counts, on="symbol", how="outer").fillna(
        0
    )
    summary_df["Signal_Count"] = summary_df["Signal_Count"].astype(int)
    summary_df["Trade_Count"] = summary_df["Trade_Count"].astype(int)

    label = f"{display_name or system_name} signal発生件数 / トレード発生件数"
    with st.expander(label, expanded=False):
        st.dataframe(summary_df.sort_values("Signal_Count", ascending=False))
    return summary_df


def extract_zero_reason_from_logs(logs: list[str] | None) -> str | None:
    """ログ配列から候補0件の理由を抽出して返す（見つからなければ None）。

    対応パターン:
    - "候補0件理由: ..."
    - "セットアップ不成立: ..."
    """
    if not logs:
        return None
    import re as _re

    for ln in reversed(list(logs)):
        if not ln:
            continue
        m = _re.search(r"候補0件理由[:：]\s*(.+)$", ln)
        if m:
            return m.group(1).strip()
        m2 = _re.search(r"セットアップ不成立[:：]\s*(.+)$", ln)
        if m2:
            return m2.group(1).strip()
    return None


def display_roc200_ranking(
    ranking_df: pd.DataFrame,
    years: int = 5,
    top_n: int = 10,
    title: str = "System1 ROC200ランキング",
):
    if ranking_df is None or ranking_df.empty:
        st.info(tr("ランキングデータがありません"))
        return
    df = ranking_df.copy()
    df["Date"] = (
        pd.to_datetime(df["Date"]) if "Date" in df.columns else pd.to_datetime(df.index)
    )
    df = df.reset_index(drop=True)
    if "ROC200_Rank" not in df.columns and "ROC200" in df.columns:
        df["ROC200_Rank"] = df.groupby("Date")["ROC200"].rank(
            ascending=False, method="first"
        )
    if years:
        start_date = pd.Timestamp.now() - pd.DateOffset(years=years)
        df = df[df["Date"] >= start_date]
    if top_n:
        df = df.groupby("Date").head(top_n)
    df = df.sort_values(["Date", "ROC200_Rank"], ascending=[True, True])
    with st.expander(f"{title} (直近{years}年 / 上位{top_n}件)", expanded=False):
        st.dataframe(
            df.reset_index(drop=True)[["Date", "ROC200_Rank", "symbol"]],
            hide_index=False,
        )


# ------------------------------
# Save helpers
# ------------------------------


def save_signal_and_trade_logs(signal_counts_df, results, system_name, capital):
    # download_button key衝突回避: key_contextを含め一意性強化
    key_suffix = f"{system_name}_{int(capital)}"
    today_str = pd.Timestamp.today().strftime("%Y-%m-%d_%H%M")
    save_dir = "results_csv"
    os.makedirs(save_dir, exist_ok=True)
    sig_dir = os.path.join(save_dir, "signals")
    os.makedirs(sig_dir, exist_ok=True)
    trade_dir = os.path.join(save_dir, "trades")
    os.makedirs(trade_dir, exist_ok=True)

    if signal_counts_df is not None and not signal_counts_df.empty:
        signal_path = os.path.join(
            sig_dir, f"{system_name}_signals_{today_str}_{int(capital)}.csv"
        )
        try:
            settings = get_settings(create_dirs=True)
            round_dec = getattr(settings.cache, "round_decimals", None)
        except Exception:
            round_dec = None
        try:
            out_df = round_dataframe(signal_counts_df, round_dec)
        except Exception:
            out_df = signal_counts_df
        out_df.to_csv(signal_path, index=False)
        st.write(tr("シグナルを保存しました: {signal_path}", signal_path=signal_path))
        # 即時ダウンロード
        st.download_button(
            label=f"{system_name} シグナルCSVをダウンロード",
            data=out_df.to_csv(index=False).encode("utf-8"),
            file_name=f"{system_name}_signals_{today_str}_{int(capital)}.csv",
            mime="text/csv",
            key=f"{key_suffix}_download_signals_csv",
        )

    trades_df = pd.DataFrame(results) if isinstance(results, list) else results
    if trades_df is not None and not trades_df.empty:
        # 画面内プレビュー（呼び出し元でエクスパンダー内にいる想定）
        try:
            preferred_cols = [
                "entry_date",
                "exit_date",
                "symbol",
                "action",
                "price",
                "qty",
                "pnl",
            ]
            cols = [c for c in preferred_cols if c in trades_df.columns]
            st.dataframe(trades_df[cols] if cols else trades_df)
        except Exception:
            pass
        trade_path = os.path.join(
            trade_dir, f"{system_name}_trades_{today_str}_{int(capital)}.csv"
        )
        try:
            try:
                settings = get_settings(create_dirs=True)
                round_dec = getattr(settings.cache, "round_decimals", None)
            except Exception:
                round_dec = None
            try:
                out_trades = round_dataframe(trades_df, round_dec)
            except Exception:
                out_trades = trades_df
            out_trades.to_csv(trade_path, index=False)
            st.write(tr("トレードを保存しました: {trade_path}", trade_path=trade_path))
            # 即時ダウンロード
            st.download_button(
                label=f"{system_name} トレードCSVをダウンロード",
                data=out_trades.to_csv(index=False).encode("utf-8"),
                file_name=f"{system_name}_trades_{today_str}_{int(capital)}.csv",
                mime="text/csv",
                key=f"{key_suffix}_download_trades_csv",
            )
        except Exception:
            # 書き込み/ダウンロード失敗しても処理を継続
            pass


###############################
# Deprecated/Removed Features #
###############################
# save_prepared_data_cache: 完全撤去 (2025-10). UI からも呼出し削除済み。


def display_cache_health_dashboard() -> None:
    """
    rolling cacheの健全性を表示するダッシュボードコンポーネント。
    """
    st.subheader("🩺 Cache Health Dashboard")

    from common.cache_manager import CacheManager
    from config.settings import get_settings

    try:
        settings = get_settings(create_dirs=True)
        cache_manager = CacheManager(settings)

        # 健全性サマリー取得
        health_summary = cache_manager.get_rolling_health_summary()

        # メタファイル状況
        st.write("### 📋 メタファイル状況")
        col1, col2 = st.columns(2)

        with col1:
            meta_status = "✅ 存在" if health_summary["meta_exists"] else "❌ 不在"
            st.metric("メタファイル", meta_status)

        with col2:
            st.metric("Rolling Files", f"{health_summary['rolling_files_count']}個")

        # SPY アンカー状況
        st.write("### ⚓ SPY アンカー状況")
        anchor_status = health_summary["anchor_symbol_status"]
        col1, col2, col3 = st.columns(3)

        with col1:
            anchor_exists = "✅ 存在" if anchor_status["exists"] else "❌ 不在"
            st.metric("SPY存在", anchor_exists)

        with col2:
            st.metric("データ行数", f"{anchor_status['rows']:,}")

        with col3:
            target_status = "✅ 十分" if anchor_status["meets_target"] else "⚠️ 不足"
            st.metric("目標達成", target_status)

        # 目標データ長
        st.write("### 🎯 目標設定")
        st.metric("目標データ長", f"{health_summary['target_length']}日")

        # メタファイル内容詳細
        if health_summary["meta_exists"] and health_summary["meta_content"]:
            st.write("### 📄 メタファイル詳細")
            st.json(health_summary["meta_content"])

        # アクションボタン
        st.write("### ⚡ アクション")
        col1, col2 = st.columns(2)

        with col1:
            if st.button("🔄 Rolling Cache 分析実行"):
                with st.spinner("分析中..."):
                    analysis_result = cache_manager.analyze_rolling_gaps()
                    _display_cache_analysis_results(analysis_result)

        with col2:
            if st.button("🧹 Rolling Cache Prune実行"):
                with st.spinner("Prune実行中..."):
                    prune_result = cache_manager.prune_rolling_if_needed()
                    st.success(
                        f"✅ Prune完了: {prune_result['pruned_files']}ファイル処理"
                    )

    except Exception as e:
        st.error(f"Cache health dashboard エラー: {str(e)}")
        logging.error(f"Cache health dashboard error: {e}")


def _display_cache_analysis_results(analysis_result: dict) -> None:
    """Cache分析結果を表示する内部ヘルパー関数。"""
    st.write("### 📊 Rolling Cache 分析結果")

    # サマリーメトリクス
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("総シンボル数", analysis_result["total_symbols"])

    with col2:
        st.metric("整備済み", analysis_result["available_in_rolling"])

    with col3:
        st.metric("未整備", analysis_result["missing_from_rolling"])

    with col4:
        coverage = analysis_result["coverage_percentage"]
        st.metric("カバレッジ", f"{coverage:.1f}%")

    # カバレッジ状況の視覚化
    if coverage >= 90:
        st.success("🎉 Rolling cache整備状況は良好です")
    elif coverage >= 70:
        st.warning("⚠️ Rolling cache整備率の改善を推奨します")
    else:
        st.error("🚨 Rolling cache整備が不十分です")

    # 未整備シンボルの表示
    missing_symbols = analysis_result.get("missing_symbols", [])
    if missing_symbols:
        st.write("### ❌ 未整備シンボル")

        if len(missing_symbols) <= 20:
            # 20個以下なら全て表示
            st.write(", ".join(missing_symbols))
        else:
            # 多い場合は展開可能にする
            with st.expander(f"未整備シンボル一覧 ({len(missing_symbols)}個)"):
                # 10個ずつ区切って表示
                for i in range(0, len(missing_symbols), 10):
                    chunk = missing_symbols[i : i + 10]
                    st.write(", ".join(chunk))


def display_system_cache_coverage() -> None:
    """
    システム別のcache coverage状況を表示するコンポーネント。
    """
    st.subheader("🎯 System別 Cache Coverage")

    from common.cache_manager import CacheManager
    from common.system_groups import analyze_system_symbols_coverage
    from config.settings import get_settings
    from scripts.tickers_loader import get_all_tickers

    try:
        settings = get_settings(create_dirs=True)
        cache_manager = CacheManager(settings)

        # 全ティッカーから各システム用のシンボルマップを構築
        # 実装では各システムに固有のフィルタリングロジックが必要だが、
        # ここでは簡略化して全シンボルを使用
        all_tickers = get_all_tickers()
        system_symbols_map = {}
        for system_num in range(1, 8):
            # 実際の実装では、各システム固有のフィルタリング条件を適用
            system_symbols_map[f"system{system_num}"] = all_tickers[:500]  # 簡略化

        # 全体のcache分析
        overall_analysis = cache_manager.analyze_rolling_gaps()

        # システム別カバレッジ分析
        coverage_analysis = analyze_system_symbols_coverage(
            system_symbols_map, overall_analysis
        )

        # グループ別サマリー表示
        st.write("### 📈 グループ別サマリー")
        group_data = coverage_analysis["by_group"]

        for group_name in ["long", "short"]:
            if group_name in group_data:
                group_stats = group_data[group_name]
                col1, col2, col3, col4 = st.columns(4)

                group_display = (
                    "Long Systems" if group_name == "long" else "Short Systems"
                )
                st.write(f"**{group_display}**")

                with col1:
                    st.metric("総シンボル", group_stats["total_symbols"])

                with col2:
                    st.metric("整備済み", group_stats["available"])

                with col3:
                    st.metric("未整備", group_stats["missing"])

                with col4:
                    coverage = group_stats["coverage_percentage"]
                    status = group_stats["status"]
                    st.metric("状況", f"{status} {coverage:.1f}%")

        # システム別詳細
        st.write("### 🔍 システム別詳細")
        system_data = coverage_analysis["by_system"]

        # データフレーム形式で表示
        df_data = []
        for system_name in [f"system{i}" for i in range(1, 8)]:
            if system_name in system_data:
                stats = system_data[system_name]
                df_data.append(
                    {
                        "システム": system_name.upper(),
                        "総シンボル": stats["total_symbols"],
                        "整備済み": stats["available"],
                        "未整備": stats["missing"],
                        "カバレッジ": f"{stats['coverage_percentage']:.1f}%",
                        "状況": stats["status"],
                    }
                )

        if df_data:
            df = pd.DataFrame(df_data)
            st.dataframe(df, width="stretch")

        # 詳細分析用の展開セクション
        with st.expander("📋 詳細分析結果"):
            st.json(coverage_analysis)

    except Exception as e:
        st.error(f"System cache coverage エラー: {str(e)}")
        logging.error(f"System cache coverage error: {e}")


def display_cache_recommendations(analysis_result: dict) -> None:
    """
    Cache分析結果に基づく推奨アクションを表示する。
    """
    from common.system_groups import format_cache_coverage_report

    # 分析結果をフォーマット
    report = format_cache_coverage_report(
        analysis_result["total_symbols"],
        analysis_result["available_in_rolling"],
        analysis_result["missing_from_rolling"],
        analysis_result["coverage_percentage"],
        analysis_result.get("missing_symbols", []),
    )

    # ステータス表示
    st.write(f"### {report['status']} 総合評価")
    st.write(f"**優先度**: {report['priority']}")

    # サマリー情報
    summary = report["summary"]
    col1, col2, col3, col4 = st.columns(4)

    with col1:
        st.metric("総数", summary["total"])
    with col2:
        st.metric("整備済み", summary["available"])
    with col3:
        st.metric("未整備", summary["missing"])
    with col4:
        st.metric("カバレッジ", summary["coverage"])

    # 推奨アクション
    st.write("### 💡 推奨アクション")
    for recommendation in report["recommendations"]:
        st.write(f"- {recommendation}")

    # 未整備シンボルプレビュー
    if report["missing_symbols_preview"]:
        st.write("### 🔍 未整備シンボル（プレビュー）")
        for symbol in report["missing_symbols_preview"]:
            st.write(f"- {symbol}")
