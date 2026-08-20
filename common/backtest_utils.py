import time
from typing import Any, Optional, Tuple

import pandas as pd

from common.backtest_context import backtest_context
from common.candidate_schema import normalize_candidates_for_date, resolve_entry_bar


def _compute_entry(
    strategy: Any,
    df: pd.DataFrame,
    candidate: dict,
    current_capital: float,
    side: Optional[str],
) -> Tuple[Optional[float], Optional[float]]:
    """Compute entry price and stop loss for a candidate.

    If the strategy exposes ``compute_entry`` use that, otherwise infer a
    default entry from the candidate index and a simple ATR-based stop.
    """
    if hasattr(strategy, "compute_entry"):
        try:
            computed = strategy.compute_entry(df, candidate, current_capital)
        except Exception:
            return None, None
        try:
            if not computed:
                return None, None
            a, b = computed

            def _to_num(x: Any) -> Optional[float]:
                try:
                    return float(x)
                except Exception:
                    return None

            return _to_num(a), _to_num(b)
        except Exception:
            return None, None

    try:
        from typing import cast

        entry_idx = cast(int, df.index.get_loc(candidate["entry_date"]))
        entry_price = float(df.iloc[entry_idx]["Open"])
        atr = float(df.iloc[max(0, entry_idx - 1)]["ATR20"])
        if (side or "long") == "short":
            stop_loss_price = entry_price + 5 * atr
        else:
            stop_loss_price = entry_price - 5 * atr
        return entry_price, stop_loss_price
    except Exception:
        return None, None


def _calculate_shares(
    strategy: Any,
    capital: float,
    entry_price: float,
    stop_loss_price: float,
    *,
    risk_pct: float,
    max_pct: float,
) -> int:
    try:
        return int(
            strategy.calculate_position_size(
                capital,
                entry_price,
                stop_loss_price,
                risk_pct=risk_pct,
                max_pct=max_pct,
            )
        )
    except Exception:
        return 0


def _compute_exit(
    strategy: Any,
    df: pd.DataFrame,
    entry_idx: int,
    entry_price: float,
    stop_loss_price: float,
    side: Optional[str],
) -> Tuple[Optional[float], Optional[pd.Timestamp]]:
    """Compute exit price and date.

    Prefer strategy.compute_exit if present, otherwise use a trailing-stop
    scan that returns a price and the index timestamp.
    """
    if hasattr(strategy, "compute_exit"):
        try:
            exit_calc = strategy.compute_exit(
                df, entry_idx, entry_price, stop_loss_price
            )
        except Exception:
            return None, None
        return exit_calc if exit_calc else (None, None)

    trail_pct = 0.25
    exit_price: float = entry_price
    exit_date: pd.Timestamp = df.index[-1]

    if (side or "long") == "short":
        low_since_entry = entry_price
        for j in range(entry_idx + 1, len(df)):
            low_since_entry = min(low_since_entry, float(df["Low"].iloc[j]))
            trailing_stop = low_since_entry * (1 + trail_pct)
            if float(df["High"].iloc[j]) > stop_loss_price:
                return stop_loss_price, df.index[j]
            if float(df["High"].iloc[j]) > trailing_stop:
                return trailing_stop, df.index[j]
    else:
        high_since_entry = entry_price
        for j in range(entry_idx + 1, len(df)):
            high_since_entry = max(high_since_entry, float(df["High"].iloc[j]))
            trailing_stop = high_since_entry * (1 - trail_pct)
            if float(df["Low"].iloc[j]) < stop_loss_price:
                return stop_loss_price, df.index[j]
            if float(df["Low"].iloc[j]) < trailing_stop:
                return trailing_stop, df.index[j]
    return exit_price, exit_date


def _compute_pnl(
    strategy: Any,
    entry_price: float,
    exit_price: float,
    shares: int,
    side: Optional[str],
) -> float:
    if hasattr(strategy, "compute_pnl"):
        try:
            return float(strategy.compute_pnl(entry_price, exit_price, shares))
        except Exception:
            pass
    if (side or "long") == "short":
        return (entry_price - exit_price) * shares
    return (exit_price - entry_price) * shares


def simulate_trades_with_risk(
    candidates_by_date: dict,
    data_dict: dict,
    capital: float,
    strategy: Any,
    on_progress=None,
    on_log=None,
    *,
    side: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    """Run a simple position-sized backtest over candidates grouped by date.

    Returns: (trades_df, logs_df)
    """
    with backtest_context():
        return _simulate_trades_with_risk(
            candidates_by_date,
            data_dict,
            capital,
            strategy,
            on_progress,
            on_log,
            side=side,
        )


def _simulate_trades_with_risk(
    candidates_by_date: dict,
    data_dict: dict,
    capital: float,
    strategy: Any,
    on_progress: Any = None,
    on_log: Any = None,
    *,
    side: Optional[str] = None,
) -> Tuple[pd.DataFrame, pd.DataFrame]:
    results = []
    log_records = []
    active_positions = []
    current_capital = capital

    total_days = len(candidates_by_date)
    start_time = time.time()

    cfg: dict[str, Any] = getattr(strategy, "config", {}) or {}
    max_positions = int(cfg.get("max_positions", 10))
    risk_pct = float(cfg.get("risk_pct", 0.02))
    max_pct = float(cfg.get("max_pct", 0.10))

    system_name = str(getattr(strategy, "SYSTEM_NAME", "") or "") or None

    for i, (date, candidates) in enumerate(sorted(candidates_by_date.items()), start=1):
        # Candidate shapes differ per system ({symbol: payload} vs list-of-dicts
        # keyed by "entry_date" or "date"). An unrecognised shape raises
        # CandidateSchemaError instead of silently booking nothing.
        candidates = normalize_candidates_for_date(
            candidates, date, system=system_name
        )

        # update exits
        current_capital, active_positions = strategy.update_capital_with_exits(
            current_capital, active_positions, date
        )

        # filter active positions
        active_positions = [p for p in active_positions if p["exit_date"] >= date]
        available_slots = max(0, max_positions - len(active_positions))

        if available_slots > 0:
            day_candidates = [
                c
                for c in candidates
                if c["symbol"] not in {p["symbol"] for p in active_positions}
            ][:available_slots]

            for c in day_candidates:
                df = data_dict.get(c["symbol"])
                if df is None or df.empty:
                    continue
                resolved = resolve_entry_bar(df, c, system=system_name)
                if resolved is None:
                    # No tradeable bar in this frame (e.g. signal on the last
                    # available bar) - a data condition, not a schema problem.
                    continue
                entry_idx, entry_ts = resolved
                # Hand the strategy hooks a candidate whose entry_date is a real
                # bar of this frame (System3 full-scan only carries "date").
                c = {**c, "entry_date": entry_ts}

                entry_price, stop_loss_price = _compute_entry(
                    strategy, df, c, current_capital, side
                )
                if entry_price is None or stop_loss_price is None:
                    continue

                shares = _calculate_shares(
                    strategy,
                    current_capital,
                    entry_price,
                    stop_loss_price,
                    risk_pct=risk_pct,
                    max_pct=max_pct,
                )
                if shares <= 0:
                    continue

                if shares * abs(entry_price) > current_capital:
                    continue

                exit_price, exit_date = _compute_exit(
                    strategy, df, entry_idx, entry_price, stop_loss_price, side
                )
                if exit_price is None or exit_date is None:
                    continue

                pnl = _compute_pnl(strategy, entry_price, exit_price, shares, side)

                results.append(
                    {
                        "symbol": c["symbol"],
                        "entry_date": c["entry_date"],
                        "exit_date": exit_date,
                        "entry_price": round(entry_price, 2),
                        "exit_price": round(exit_price, 2),
                        "shares": int(shares),
                        "pnl": round(pnl, 2),
                        "return_%": round((pnl / max(1.0, current_capital)) * 100, 2),
                    }
                )

                active_positions.append(
                    {
                        "symbol": c["symbol"],
                        "exit_date": pd.Timestamp(exit_date),
                        "pnl": pnl,
                    }
                )

        # logging
        log_records.append(
            {
                "date": date,
                "capital": current_capital,
                "active_positions": len(active_positions),
                "message": (
                    f"💰 {date} | Capital: {current_capital:.2f} USD | Active: {len(active_positions)}"
                ),
            }
        )

        if on_log and (i % 10 == 0 or i == total_days):
            try:
                on_log(log_records[-1]["message"])
            except Exception:
                pass

        if on_progress:
            on_progress(i, total_days, start_time)

    return pd.DataFrame(results), pd.DataFrame(log_records)
