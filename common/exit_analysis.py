"""Exit analysis for current positions.

This module provides the core logic for identifying exit candidates based on
current Alpaca positions and system rules. It is UI-agnostic and returns
dataframes that can be rendered by Streamlit or sent to order submitters.
"""

from __future__ import annotations

from dataclasses import dataclass
import json
from pathlib import Path
from typing import Any, Callable

import pandas as pd

from common import broker_alpaca as ba
from common.data_loader import load_price
from common.exit_planner import decide_exit_schedule
from common.position_age import (
    fetch_entry_dates_from_alpaca,
    load_entry_dates,
    save_entry_dates,
)
from common.utils_spy import get_latest_nyse_trading_day
from strategies.system1_strategy import System1Strategy
from strategies.system2_strategy import System2Strategy
from strategies.system3_strategy import System3Strategy
from strategies.system4_strategy import System4Strategy
from strategies.system5_strategy import System5Strategy
from strategies.system6_strategy import System6Strategy


@dataclass
class ExitAnalysisResult:
    exits_today: pd.DataFrame
    planned: pd.DataFrame
    exit_counts: dict[str, int]
    error: str | None = None


def _strategy_class_map() -> dict[str, Callable[[], Any]]:
    return {
        "system1": System1Strategy,
        "system2": System2Strategy,
        "system3": System3Strategy,
        "system4": System4Strategy,
        "system5": System5Strategy,
        "system6": System6Strategy,
    }


def analyze_exit_candidates(paper_mode: bool) -> ExitAnalysisResult:
    """Analyze current positions and determine exit candidates."""

    exits_today_rows: list[dict[str, Any]] = []
    planned_rows: list[dict[str, Any]] = []
    exit_counts: dict[str, int] = {f"system{i}": 0 for i in range(1, 8)}
    try:
        client_tmp = ba.get_client(paper=paper_mode)
        try:
            positions = list(client_tmp.get_all_positions())
        except Exception:
            positions = []

        # 1) load entry dates
        raw_entry_map = load_entry_dates()
        entry_map: dict[str, str] = {}
        for k, v in raw_entry_map.items():
            try:
                entry_map[str(k).upper()] = str(v)
            except Exception:
                continue

        # 2) fill missing entry dates via Alpaca
        missing = [
            str(getattr(p, "symbol", "")).upper()
            for p in positions
            if str(getattr(p, "symbol", "")).upper()
            and str(getattr(p, "symbol", "")).upper() not in entry_map
        ]
        if missing:
            try:
                fetched = fetch_entry_dates_from_alpaca(client_tmp, missing)
            except Exception:
                fetched = None
            if fetched:
                for sym, ts in fetched.items():
                    if sym not in entry_map:
                        try:
                            entry_map[sym] = pd.Timestamp(ts).strftime("%Y-%m-%d")
                        except Exception:
                            continue
                try:
                    save_entry_dates(entry_map)
                except Exception:
                    pass

        symbol_system_map = _load_symbol_system_map(Path("data/symbol_system_map.json"))
        latest_trading_day = _latest_trading_day()
        strategy_classes = _strategy_class_map()

        # 3) evaluate positions
        for pos in positions:
            result = _evaluate_position_for_exit(
                pos,
                entry_map,
                symbol_system_map,
                latest_trading_day,
                strategy_classes,
            )
            if result is None:
                continue
            system, _pos_side, _qty, exit_when, row_base, exit_today = result
            when_val = str(exit_when or "").strip()
            when_lower = when_val.lower()
            when_display = when_lower or when_val
            if exit_today:
                exit_counts[system] = exit_counts.get(system, 0) + 1
                if when_lower == "tomorrow_open":
                    planned_rows.append(row_base | {"when": when_display})
                else:
                    exits_today_rows.append(row_base | {"when": when_display})
            else:
                planned_rows.append(row_base | {"when": when_display})

        exits_today_df = pd.DataFrame(exits_today_rows)
        planned_df = pd.DataFrame(planned_rows)
        return ExitAnalysisResult(
            exits_today=exits_today_df, planned=planned_df, exit_counts=exit_counts
        )
    except Exception as exc:
        return ExitAnalysisResult(
            exits_today=pd.DataFrame(),
            planned=pd.DataFrame(),
            exit_counts=exit_counts,
            error=str(exc),
        )


def _load_symbol_system_map(path: Path) -> dict[str, str]:
    try:
        if path.exists():
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                return {str(k).upper(): str(v).lower() for k, v in data.items()}
    except Exception:
        pass
    return {}


def _latest_trading_day() -> pd.Timestamp | None:
    calendar_day: pd.Timestamp | None = None
    try:
        calendar_day = get_latest_nyse_trading_day()
    except Exception:
        calendar_day = None

    price_day: pd.Timestamp | None = None
    try:
        spy_df = load_price("SPY", cache_profile="rolling")
        if spy_df is not None and not spy_df.empty:
            try:
                price_raw = pd.Timestamp(spy_df.index[-1])
                if price_raw.tzinfo is not None:
                    try:
                        price_raw = price_raw.tz_convert(None)
                    except Exception:
                        price_raw = price_raw.tz_localize(None)
                price_day = pd.Timestamp(price_raw).normalize()
            except Exception:
                price_day = None
    except Exception:
        price_day = None

    if calendar_day is not None and price_day is not None:
        return max(calendar_day, price_day)
    return calendar_day or price_day


def _evaluate_position_for_exit(
    pos: Any,
    entry_map: dict[str, Any],
    symbol_system_map: dict[str, str],
    latest_trading_day: pd.Timestamp | None,
    strategy_classes: dict[str, Callable[[], Any]],
    load_price_fn: Callable[[str, str], Any] | None = None,
) -> tuple[str, str, int, str, dict[str, Any], bool] | None:
    try:
        loader = load_price_fn or load_price
        sym = str(getattr(pos, "symbol", "")).upper()
        if not sym:
            return None
        qty = int(abs(float(getattr(pos, "qty", 0)) or 0))
        if qty <= 0:
            return None
        pos_side = str(getattr(pos, "side", "")).lower()
        system = symbol_system_map.get(sym, "").lower()
        if not system:
            if sym == "SPY" and pos_side == "short":
                system = "system7"
            else:
                return None
        if system == "system7":
            return None
        entry_date_str = entry_map.get(sym)
        if not entry_date_str:
            return None
        entry_dt = pd.to_datetime(entry_date_str).normalize()
        df_price = loader(sym, cache_profile="full")
        if df_price is None or df_price.empty:
            return None
        df = df_price.copy(deep=False)
        if "Date" in df.columns:
            df.index = pd.Index(pd.to_datetime(df["Date"]).dt.normalize())
        else:
            df.index = pd.Index(pd.to_datetime(df.index).normalize())
        if latest_trading_day is None and len(df.index) > 0:
            latest_trading_day = pd.to_datetime(df.index[-1]).normalize()
        entry_idx = _find_entry_index(df.index, entry_dt)
        if entry_idx < 0:
            return None
        strategy_cls = strategy_classes.get(system)
        if strategy_cls is None:
            return None
        strategy = strategy_cls()
        prev_close = float(df.iloc[int(max(0, entry_idx - 1))]["Close"])
        entry_price, stop_price = _entry_and_stop_prices(
            system, strategy, df, entry_idx, prev_close
        )
        if entry_price is None or stop_price is None:
            return None
        _apply_strategy_state(system, strategy, df, entry_idx, prev_close)
        exit_price, exit_date = strategy.compute_exit(
            df, int(entry_idx), float(entry_price), float(stop_price)
        )
        today_norm = pd.to_datetime(df.index[-1]).normalize()
        if latest_trading_day is not None:
            today_norm = latest_trading_day
        is_today_exit, when = decide_exit_schedule(system, exit_date, today_norm)
        row_base = {
            "symbol": sym,
            "qty": qty,
            "position_side": pos_side,
            "system": system,
            "entry_price": entry_price,
            "stop_price": stop_price,
            "exit_price": exit_price,
        }
        return system, pos_side, qty, when, row_base, is_today_exit
    except Exception:
        return None


def _find_entry_index(index: pd.Index, entry_dt: pd.Timestamp) -> int:
    try:
        if entry_dt in index:
            arr = index.get_indexer([entry_dt])
        else:
            arr = index.get_indexer([entry_dt], method="bfill")
        if len(arr) and arr[0] >= 0:
            return int(arr[0])
    except Exception:
        pass
    return -1


def _entry_and_stop_prices(
    system: str,
    strategy: Any,
    df: pd.DataFrame,
    entry_idx: int,
    prev_close: float,
) -> tuple[float | None, float | None]:
    try:
        if system == "system1":
            entry_price = float(df.iloc[int(entry_idx)]["Open"])
            atr20 = float(df.iloc[int(max(0, entry_idx - 1))]["ATR20"])
            stop_mult = float(strategy.config.get("stop_atr_multiple", 5.0))
            return entry_price, entry_price - stop_mult * atr20
        if system == "system2":
            entry_price = float(df.iloc[int(entry_idx)]["Open"])
            atr = float(df.iloc[int(max(0, entry_idx - 1))]["ATR10"])
            stop_mult = float(strategy.config.get("stop_atr_multiple", 3.0))
            return entry_price, entry_price + stop_mult * atr
        if system == "system6":
            ratio = float(strategy.config.get("entry_price_ratio_vs_prev_close", 1.05))
            entry_price = round(prev_close * ratio, 2)
            atr = float(df.iloc[int(max(0, entry_idx - 1))]["ATR10"])
            stop_mult = float(strategy.config.get("stop_atr_multiple", 3.0))
            return entry_price, entry_price + stop_mult * atr
        if system == "system3":
            ratio = float(strategy.config.get("entry_price_ratio_vs_prev_close", 0.93))
            entry_price = round(prev_close * ratio, 2)
            atr = float(df.iloc[int(max(0, entry_idx - 1))]["ATR10"])
            stop_mult = float(strategy.config.get("stop_atr_multiple", 2.5))
            return entry_price, entry_price - stop_mult * atr
        if system == "system4":
            entry_price = float(df.iloc[int(entry_idx)]["Open"])
            atr40 = float(df.iloc[int(max(0, entry_idx - 1))]["ATR40"])
            stop_mult = float(strategy.config.get("stop_atr_multiple", 1.5))
            return entry_price, entry_price - stop_mult * atr40
        if system == "system5":
            ratio = float(strategy.config.get("entry_price_ratio_vs_prev_close", 0.97))
            entry_price = round(prev_close * ratio, 2)
            atr = float(df.iloc[int(max(0, entry_idx - 1))]["ATR10"])
            stop_mult = float(strategy.config.get("stop_atr_multiple", 3.0))
            return entry_price, entry_price - stop_mult * atr
    except Exception:
        return None, None
    return None, None


def _apply_strategy_state(
    system: str,
    strategy: Any,
    df: pd.DataFrame,
    entry_idx: int,
    prev_close: float,
) -> None:
    if system == "system5":
        try:
            atr = float(df.iloc[int(max(0, entry_idx - 1))]["ATR10"])
            strategy._last_entry_atr = atr
        except Exception:
            pass
    if system in {"system3", "system5", "system6"}:
        try:
            strategy._last_prev_close = prev_close
        except Exception:
            pass


__all__ = ["ExitAnalysisResult", "analyze_exit_candidates"]
