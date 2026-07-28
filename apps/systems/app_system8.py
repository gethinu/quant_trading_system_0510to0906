from __future__ import annotations

from pathlib import Path
import time
from typing import Any, cast

import pandas as pd
import streamlit as st

from common.i18n import language_selector, load_translations_from_dir, tr
from common.notifier import Notifier, get_notifiers_from_env, now_jst_str
from common.performance_summary import summarize as summarize_perf
from common.price_chart import save_price_chart
from common.ui_components import (
    run_backtest_app,
    save_signal_and_trade_logs,
    show_signal_trade_summary,
)
from common.ui_manager import UIManager
import common.ui_patch  # noqa: F401
from strategies import get_strategy

# Load translations and (optionally) show language selector
load_translations_from_dir(Path(__file__).parent / "translations")
if not st.session_state.get("_integrated_ui", False):
    language_selector()

SYSTEM_NAME = "System8"
DISPLAY_NAME = "システム8"


def _strategy():
    return get_strategy("system8")


notifiers: list[Notifier] = get_notifiers_from_env()


def run_tab(
    single_mode: bool | None = None,
    ui_manager: UIManager | None = None,
) -> None:
    """System8 タブを描画し、バックテストを実行する。

    System8 は SPY オーバーナイト FOMC プレドリフト（イベント駆動・ロング専用）。
    予定 FOMC 声明日の前営業日に引けで買い、声明日の寄りで手仕舞う 1 泊保有。
    """
    st.header(
        tr(
            "{display_name} バックテスト（FOMC オーバーナイト・ドリフト / SPYのみ）",
            display_name=DISPLAY_NAME,
        )
    )
    st.caption(
        tr(
            "予定 FOMC 声明日の前営業日 T-1 の引け（MOC）でロング、声明日 T の寄り"
            "（MOO）で手仕舞い。等ノーショナル・ストップなし・往復2bp。"
            "（出所: n0150_fomc_macro_event_drift_spy, GO_CANDIDATE）"
        )
    )

    ui_base: UIManager = (
        ui_manager.system(SYSTEM_NAME)
        if ui_manager
        else UIManager().system(SYSTEM_NAME)
    )
    fetch_phase = ui_base.phase("fetch", title=tr("データ取得"))
    ind_phase = ui_base.phase("indicators", title=tr("カレンダー適用"))
    cand_phase = ui_base.phase("candidates", title=tr("候補選定"))
    notify_key = f"{SYSTEM_NAME}_notify_backtest"
    run_start = time.time()
    strategy = _strategy()
    _rb = cast(
        tuple[
            pd.DataFrame | None,
            pd.DataFrame | None,
            dict[str, pd.DataFrame] | None,
            float,
            object | None,
        ],
        run_backtest_app(
            strategy,
            system_name=SYSTEM_NAME,
            limit_symbols=1,
            ui_manager=ui_base,
        ),
    )
    elapsed = time.time() - run_start
    results_df, _, data_dict, capital, candidates_by_date = _rb
    fetch_phase.log_area.write(tr("データ取得完了"))
    ind_phase.log_area.write(tr("FOMC カレンダー適用完了"))
    cand_phase.log_area.write(tr("候補選定完了"))

    if results_df is not None and candidates_by_date is not None:
        summary_df = show_signal_trade_summary(
            data_dict,
            results_df,
            SYSTEM_NAME,
            display_name=DISPLAY_NAME,
        )
        with st.expander(tr("取引ログ・保存ファイル"), expanded=False):
            save_signal_and_trade_logs(
                summary_df,
                results_df,
                SYSTEM_NAME,
                capital,
            )
        summary, df2 = summarize_perf(results_df, capital)
        try:
            _max_dd = float(df2["drawdown"].min())
        except Exception:
            _max_dd = float(getattr(summary, "max_drawdown", 0.0))
        stats: dict[str, Any] = {
            "総リターン": f"{summary.total_return:.2f}",
            "最大DD": f"{_max_dd:.2f}",
            "Sharpe": f"{summary.sharpe:.2f}",
            "実施日時": now_jst_str(),
            "銘柄数": len(data_dict) if data_dict else 0,
            "開始資金": int(capital),
            "処理時間": f"{elapsed:.2f}s",
        }

        try:
            from common.ui_components import show_results

            show_results(results_df, capital, SYSTEM_NAME, key_context="live")
        except Exception:
            pass

        period: str = ""
        if "entry_date" in results_df.columns and "exit_date" in results_df.columns:
            start = pd.to_datetime(results_df["entry_date"]).min()
            end = pd.to_datetime(results_df["exit_date"]).max()
            period = f"{start:%Y-%m-%d}〜{end:%Y-%m-%d}"
        ranking: list[str] = (
            [str(s) for s in results_df["symbol"].head(10)]
            if "symbol" in results_df.columns
            else []
        )
        chart_url = None
        if not results_df.empty and "symbol" in results_df.columns:
            try:
                top_sym = results_df.sort_values("pnl", ascending=False)["symbol"].iloc[
                    0
                ]
                _, chart_url = save_price_chart(str(top_sym), trades=results_df)
            except Exception:
                chart_url = None
        if st.session_state.get(notify_key, False):
            sent = False
            for n in notifiers:
                try:
                    _mention: str | None = (
                        "channel" if getattr(n, "platform", None) == "slack" else None
                    )
                    if hasattr(n, "send_backtest_ex"):
                        n.send_backtest_ex(
                            SYSTEM_NAME.lower(),
                            period,
                            stats,
                            ranking,
                            image_url=chart_url,
                            mention=_mention,
                        )
                    else:
                        n.send_backtest(
                            SYSTEM_NAME.lower(),
                            period,
                            stats,
                            ranking,
                        )
                    sent = True
                except Exception:
                    continue
            if sent:
                st.success(tr("通知を送信しました"))
            else:
                st.warning(tr("通知の送信に失敗しました"))
    else:
        # Fallback view from session state
        prev_res = st.session_state.get(f"{SYSTEM_NAME}_results_df")
        prev_data = st.session_state.get(f"{SYSTEM_NAME}_prepared_dict")
        prev_cap = st.session_state.get(f"{SYSTEM_NAME}_capital_saved")
        if prev_res is not None:
            _ = show_signal_trade_summary(
                prev_data,
                prev_res,
                SYSTEM_NAME,
                display_name=DISPLAY_NAME,
            )
            try:
                from common.ui_components import show_results

                show_results(
                    prev_res,
                    prev_cap or 0.0,
                    SYSTEM_NAME,
                    key_context="prev",
                )
            except Exception:
                pass


if __name__ == "__main__":
    import sys

    if "streamlit" not in sys.argv[0]:
        run_tab()
