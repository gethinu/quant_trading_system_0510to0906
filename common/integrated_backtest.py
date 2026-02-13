from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import logging
import time

# ---------------
# 型と設定
# ---------------
from typing import Any, Protocol

import pandas as pd

from common.performance_optimization import PerformanceTimer


class StrategyProtocol(Protocol):
    def compute_pnl(
        self, entry_price: float, exit_price: float, shares: int
    ) -> float: ...

    def calculate_position_size(
        self,
        bucket_capital: float,
        entry_price: float,
        stop_price: float,
        risk_pct: float,
        max_pct: float,
    ) -> int: ...
    def prepare_data(self, raw_data): ...
    def generate_candidates(self, prepared_data, market_df=None): ...


@dataclass
class SystemState:
    name: str
    side: str  # "long" | "short"
    strategy: StrategyProtocol
    prepared: dict[str, pd.DataFrame]
    candidates_by_date: dict[pd.Timestamp, list[dict]]


AllocationMap = dict[str, float]


DEFAULT_ALLOCATIONS: AllocationMap = {
    # Long bucket (各25%)
    "System1": 0.25,
    "System3": 0.25,
    "System4": 0.25,
    "System5": 0.25,
    # Short bucket (2:40%, 6:40%, 7:20%)
    "System2": 0.40,
    "System6": 0.40,
    "System7": 0.20,
}


def _get_side(system_name: str) -> str:
    return "short" if system_name in {"System2", "System6", "System7"} else "long"


def _union_signal_dates(states: list[SystemState]) -> list[pd.Timestamp]:
    all_dates = set()
    for st in states:
        all_dates.update(pd.to_datetime(list(st.candidates_by_date.keys())).tolist())
    return sorted(pd.to_datetime(list(all_dates)))


def _symbol_open_in_active(active: list[dict], symbol: str) -> bool:
    return any(p.get("symbol") == symbol for p in active)


def _compute_entry_exit(strategy, df: pd.DataFrame, candidate: dict, side: str):
    # entry/stop
    entry_idx = None
    try:
        entry_idx = df.index.get_loc(candidate["entry_date"])
    except Exception:
        return None

    # Strategy hook
    if hasattr(strategy, "compute_entry"):
        try:
            res = strategy.compute_entry(df, candidate, 0.0)
        except Exception:
            res = None
        if not res:
            return None
        entry_price, stop_loss_price = res
    else:
        try:
            entry_price = float(df.at[df.index[entry_idx], "Open"])  # next-day open
            # Ensure entry_idx is an integer
            if not isinstance(entry_idx, int):
                # slice型は除外
                if isinstance(entry_idx, slice):
                    return None
                # numpy.ndarray型は除外
                try:
                    import numpy as np

                    if isinstance(entry_idx, np.ndarray):
                        return None
                except ImportError:
                    pass
                # numpy scalarならitem()でint化
                if hasattr(entry_idx, "item"):
                    entry_idx = entry_idx.item()
                # bool型は除外
                if isinstance(entry_idx, bool):
                    return None
                # その他はint化を試みる
                try:
                    entry_idx = int(entry_idx)
                except Exception:
                    return None
            atr = (
                float(df.iloc[max(0, entry_idx - 1)]["ATR20"])
                if "ATR20" in df.columns
                else float(df.iloc[max(0, entry_idx - 1)]["ATR10"])
            )
            if side == "short":
                stop_loss_price = entry_price + 5 * atr
            else:
                stop_loss_price = entry_price - 5 * atr
        except Exception:
            return None

    # exit hook or fallback
    if hasattr(strategy, "compute_exit"):
        try:
            exit_price, exit_date = strategy.compute_exit(
                df, entry_idx, entry_price, stop_loss_price
            )
        except Exception:
            return None
    else:
        # simple trailing fallback
        trail_pct = 0.25
        exit_price, exit_date = entry_price, df.index[-1]
        if side == "short":
            low_since_entry = entry_price
            # Ensure entry_idx is an integer
            if not isinstance(entry_idx, int):
                # slice型は除外
                if isinstance(entry_idx, slice):
                    return None
                # numpy.ndarray型は除外
                try:
                    import numpy as np

                    if isinstance(entry_idx, np.ndarray):
                        return None
                except ImportError:
                    pass
                # numpy scalarならitem()でint化
                if hasattr(entry_idx, "item"):
                    entry_idx = entry_idx.item()
                # bool型は除外
                if isinstance(entry_idx, bool):
                    return None
                # その他はint化を試みる
                try:
                    entry_idx = int(entry_idx)
                except Exception:
                    return None
            if not isinstance(entry_idx, int):
                return None
            for j in range(entry_idx + 1, len(df)):
                low_since_entry = min(low_since_entry, float(df["Low"].iloc[j]))
                trailing_stop = low_since_entry * (1 + trail_pct)
                if float(df["High"].iloc[j]) > stop_loss_price:
                    exit_price, exit_date = stop_loss_price, df.index[j]
                    break
                elif float(df["High"].iloc[j]) > trailing_stop:
                    exit_price, exit_date = trailing_stop, df.index[j]
                    break
        else:
            high_since_entry = entry_price
            # Ensure entry_idx is an integer
            if not isinstance(entry_idx, int):
                # slice型は除外
                if isinstance(entry_idx, slice):
                    return None
                # numpy.ndarray型は除外
                try:
                    import numpy as np

                    if isinstance(entry_idx, np.ndarray):
                        return None
                except ImportError:
                    pass
                # numpy scalarならitem()でint化
                if hasattr(entry_idx, "item"):
                    entry_idx = entry_idx.item()
                # bool型は除外
                if isinstance(entry_idx, bool):
                    return None
                # その他はint化を試みる
                try:
                    entry_idx = int(entry_idx)
                except Exception:
                    return None
            if not isinstance(entry_idx, int):
                return None
            for j in range(entry_idx + 1, len(df)):
                high_since_entry = max(high_since_entry, float(df["High"].iloc[j]))
                trailing_stop = high_since_entry * (1 - trail_pct)
                if float(df["Low"].iloc[j]) < stop_loss_price:
                    exit_price, exit_date = stop_loss_price, df.index[j]
                    break
                elif float(df["Low"].iloc[j]) < trailing_stop:
                    exit_price, exit_date = trailing_stop, df.index[j]
                    break

    return (
        entry_idx,
        float(entry_price),
        float(stop_loss_price),
        float(exit_price),
        pd.Timestamp(exit_date),
    )


def _coerce_index_to_int(idx_obj, *, upper_bound: int) -> int | None:
    if isinstance(idx_obj, bool):
        return None
    if isinstance(idx_obj, slice):
        return None
    try:
        import numpy as np

        if isinstance(idx_obj, np.ndarray):
            return None
    except Exception:
        pass
    if hasattr(idx_obj, "item"):
        try:
            idx_obj = idx_obj.item()
        except Exception:
            return None
    try:
        idx = int(idx_obj)
    except Exception:
        return None
    if idx < 0 or idx > upper_bound:
        return None
    return idx


def _enforce_min_hold_days(
    df: pd.DataFrame,
    entry_idx: int,
    exit_date: pd.Timestamp,
    exit_price: float,
    *,
    min_hold_days: int,
) -> tuple[float, pd.Timestamp]:
    if min_hold_days <= 0 or df is None or df.empty:
        return float(exit_price), pd.Timestamp(exit_date)

    n = len(df)
    max_idx = n - 1
    if entry_idx < 0:
        entry_idx = 0
    if entry_idx > max_idx:
        entry_idx = max_idx

    min_exit_idx = min(max_idx, entry_idx + int(min_hold_days))
    try:
        exit_idx_obj = df.index.get_loc(pd.Timestamp(exit_date))
    except Exception:
        exit_idx_obj = min_exit_idx
    exit_idx = _coerce_index_to_int(exit_idx_obj, upper_bound=max_idx)
    if exit_idx is None:
        exit_idx = min_exit_idx

    if exit_idx >= min_exit_idx:
        return float(exit_price), pd.Timestamp(exit_date)

    row = df.iloc[min_exit_idx]
    if "Close" in row.index:
        forced_exit_price = float(row["Close"])
    elif "Open" in row.index:
        forced_exit_price = float(row["Open"])
    else:
        forced_exit_price = float(exit_price)
    forced_exit_date = pd.Timestamp(df.index[min_exit_idx])
    return forced_exit_price, forced_exit_date


def _normalize_candidates_for_date(
    cands: object,
    current_date: pd.Timestamp,
) -> list[dict]:
    # 互換性確保: {date: {symbol: payload}} 形式にも対応
    if isinstance(cands, dict):
        normalized: list[dict] = []
        for sym, payload in cands.items():
            if not isinstance(sym, str) or not sym:
                continue
            item = {
                "symbol": sym,
                "entry_date": pd.Timestamp(current_date),
            }
            if isinstance(payload, dict):
                item.update(payload)
            normalized.append(item)
        return normalized
    if isinstance(cands, list):
        return cands
    return []


def _build_rust_payload(
    system_states: list[SystemState],
    *,
    initial_capital: float,
    allocations: AllocationMap,
    long_share: float,
    short_share: float,
    allow_gross_leverage: bool,
    min_hold_days: int,
) -> tuple[dict[str, Any], list[pd.Timestamp]]:
    name_to_state = {s.name: s for s in system_states}
    all_dates = _union_signal_dates(system_states)
    systems_order = [
        sys for sys in [f"System{k}" for k in range(1, 8)] if sys in name_to_state
    ]
    opportunities: list[dict[str, Any]] = []

    for current_date in all_dates:
        current_ts = pd.Timestamp(current_date)
        current_key = current_ts.strftime("%Y-%m-%d")
        for sys_name in systems_order:
            stt = name_to_state.get(sys_name)
            if stt is None:
                continue
            raw = stt.candidates_by_date.get(current_ts, [])
            cands = _normalize_candidates_for_date(raw, current_ts)
            if not cands:
                continue

            cfg = getattr(stt.strategy, "config", {}) or {}
            max_positions = int(cfg.get("max_positions", 10))
            risk_pct = float(cfg.get("risk_pct", 0.02))
            max_pct = float(cfg.get("max_pct", 0.10))

            for c in cands:
                sym = c.get("symbol")
                sym_text = "" if sym is None else str(sym)
                try:
                    entry_date_ts = pd.Timestamp(c.get("entry_date", current_ts))
                except Exception:
                    entry_date_ts = current_ts
                opp: dict[str, Any] = {
                    "date": current_key,
                    "system": sys_name,
                    "side": stt.side,
                    "symbol": sym_text,
                    "entry_date": entry_date_ts.strftime("%Y-%m-%d"),
                    "exit_date": entry_date_ts.strftime("%Y-%m-%d"),
                    "entry_price": 0.0,
                    "stop_price": 0.0,
                    "exit_price": 0.0,
                    "risk_pct": float(risk_pct),
                    "max_pct": float(max_pct),
                    "max_positions": int(max_positions),
                    "is_valid": False,
                }

                # Python path semantics: candidates inside `cands[:slots]` consume a slot
                # even when they are invalid. Keep them in payload as invalid placeholders
                # so Rust runtime can preserve the same behavior.
                if sym is None:
                    opportunities.append(opp)
                    continue
                df = stt.prepared.get(sym)
                if df is None or df.empty:
                    opportunities.append(opp)
                    continue

                comp = _compute_entry_exit(stt.strategy, df, c, stt.side)
                if not comp:
                    opportunities.append(opp)
                    continue
                entry_idx, entry_price, stop_price, exit_price, exit_date = comp
                if min_hold_days > 0:
                    exit_price, exit_date = _enforce_min_hold_days(
                        df,
                        int(entry_idx),
                        pd.Timestamp(exit_date),
                        float(exit_price),
                        min_hold_days=min_hold_days,
                    )

                opp.update(
                    {
                        "exit_date": pd.Timestamp(exit_date).strftime("%Y-%m-%d"),
                        "entry_price": float(entry_price),
                        "stop_price": float(stop_price),
                        "exit_price": float(exit_price),
                        "is_valid": True,
                    }
                )
                opportunities.append(opp)

    payload = {
        "dates": [pd.Timestamp(d).strftime("%Y-%m-%d") for d in all_dates],
        "systems_order": systems_order,
        "initial_capital": float(initial_capital),
        "allocations": {str(k): float(v) for k, v in (allocations or {}).items()},
        "long_share": float(long_share),
        "short_share": float(short_share),
        "allow_gross_leverage": bool(allow_gross_leverage),
        "opportunities": opportunities,
    }
    return payload, all_dates


def _canonicalize_rust_trades_for_python_parity(
    trades_df: pd.DataFrame,
    *,
    payload: dict[str, Any],
    name_to_state: dict[str, SystemState],
) -> pd.DataFrame:
    if trades_df is None or trades_df.empty:
        return trades_df

    opportunities = payload.get("opportunities", [])
    if not isinstance(opportunities, list):
        return trades_df

    lookup: dict[tuple[str, str, str, str, str], list[dict[str, Any]]] = {}
    for opp in opportunities:
        if not isinstance(opp, dict):
            continue
        if not bool(opp.get("is_valid", True)):
            continue
        key = (
            str(opp.get("system", "")),
            str(opp.get("side", "")),
            str(opp.get("symbol", "")),
            str(opp.get("entry_date", "")),
            str(opp.get("exit_date", "")),
        )
        lookup.setdefault(key, []).append(opp)

    out = trades_df.copy()
    for idx, row in out.iterrows():
        try:
            entry_key = pd.Timestamp(row.get("entry_date")).strftime("%Y-%m-%d")
            exit_key = pd.Timestamp(row.get("exit_date")).strftime("%Y-%m-%d")
        except Exception:
            continue
        system_name = str(row.get("system", ""))
        side = str(row.get("side", ""))
        symbol = str(row.get("symbol", ""))
        key = (system_name, side, symbol, entry_key, exit_key)
        candidates = lookup.get(key)
        if not candidates:
            continue
        opp = candidates.pop(0)
        if not candidates:
            lookup.pop(key, None)

        try:
            entry_raw = float(opp.get("entry_price", row.get("entry_price")))
            exit_raw = float(opp.get("exit_price", row.get("exit_price")))
            shares = int(row.get("shares", 0))
        except Exception:
            continue

        out.at[idx, "entry_price"] = round(entry_raw, 2)
        out.at[idx, "exit_price"] = round(exit_raw, 2)

        pnl_raw: float
        stt = name_to_state.get(system_name)
        if stt is not None and hasattr(stt.strategy, "compute_pnl"):
            try:
                pnl_raw = float(stt.strategy.compute_pnl(entry_raw, exit_raw, shares))
            except Exception:
                pnl_raw = (
                    (entry_raw - exit_raw) * shares
                    if side == "short"
                    else (exit_raw - entry_raw) * shares
                )
        else:
            pnl_raw = (
                (entry_raw - exit_raw) * shares
                if side == "short"
                else (exit_raw - entry_raw) * shares
            )
        out.at[idx, "pnl"] = round(pnl_raw, 2)

    return out


def run_integrated_backtest(
    system_states: list[SystemState],
    initial_capital: float,
    allocations: AllocationMap | None = None,
    *,
    long_share: float = 0.5,
    short_share: float = 0.5,
    allow_gross_leverage: bool = False,
    min_hold_days: int = 0,
    engine: str | None = None,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    統合バックテスト本体。
    - system_states: 各Systemの prepared/candidates を含む状態
    - initial_capital: 初期資金（共通）
    - allocations: システム別の最大投下資金比率（当日基準）。指定無ければ既定。
    - allow_gross_leverage: Trueなら総建玉のコスト合計が資金を超えても許容（既定False）
    - min_hold_days: 0より大きい場合、全システム共通で最小保有日数を強制
    - engine: `python` / `rust` / `auto`（None時は環境変数 `INTEGRATED_BACKTEST_ENGINE` を参照）

    戻り値: (trades_df, signal_counts_by_system)
    """
    with PerformanceTimer("integrated_backtest_total", verbose=True):
        allocations = dict(allocations or DEFAULT_ALLOCATIONS)
        logging.getLogger(__name__).info(
            "[integrated] start | states=%d, long_share=%.2f, short_share=%.2f, gross=%s",
            len(system_states),
            float(long_share),
            float(short_share),
            bool(allow_gross_leverage),
        )
        name_to_state = {s.name: s for s in system_states}
        # シグナル件数
        signal_counts = {
            s.name: int(sum(len(v) for v in s.candidates_by_date.values()))
            for s in system_states
        }

        # 長短の初期資金
        if long_share < 0 or short_share < 0 or (long_share + short_share) == 0:
            long_share, short_share = 0.5, 0.5
        total = float(initial_capital)
        long_capital = total * (long_share / (long_share + short_share))
        short_capital = total * (short_share / (long_share + short_share))

        results: list[dict] = []
        active_positions: list[dict] = (
            []
        )  # {symbol, system, side, exit_date, pnl, cost}
        system_used_value: dict[str, float] = {s.name: 0.0 for s in system_states}
        bucket_used_value: dict[str, float] = {"long": 0.0, "short": 0.0}

        # 全営業日の集合（シグナルのある日ベース）
        all_dates = _union_signal_dates(system_states)
        logging.getLogger(__name__).info(
            "[integrated] trading days: %d", len(all_dates)
        )

    # Optional Rust core dispatch.
    try:
        from common.integrated_backtest_rust_bridge import (
            run_rust_backtest_core,
            should_use_rust_engine,
        )

        if should_use_rust_engine(engine=engine):
            payload, all_dates = _build_rust_payload(
                system_states,
                initial_capital=initial_capital,
                allocations=allocations,
                long_share=long_share,
                short_share=short_share,
                allow_gross_leverage=allow_gross_leverage,
                min_hold_days=min_hold_days,
            )
            rust_df = run_rust_backtest_core(
                payload,
                engine=engine,
                log_callback=lambda msg: logging.getLogger(__name__).warning(msg),
            )
            if rust_df is not None:
                rust_df = _canonicalize_rust_trades_for_python_parity(
                    rust_df,
                    payload=payload,
                    name_to_state=name_to_state,
                )
                if on_progress is not None:
                    try:
                        on_progress(len(all_dates), len(all_dates), time.time())
                    except Exception:
                        pass
                return rust_df, signal_counts
    except Exception:
        # `engine=rust` の場合は例外を上位へ返す。`auto` はpythonにフォールバックする。
        if (engine or "").strip().lower() == "rust":
            raise

    start_time = time.time()
    for i, current_date in enumerate(all_dates, 1):
        # UI側のプログレスバー更新（あれば）
        try:
            if on_progress is not None:
                on_progress(i, len(all_dates), start_time)
        except Exception:
            pass
        # 1) 当日決済を反映
        realized = [p for p in active_positions if p["exit_date"] == current_date]
        if realized:
            # バケットごとに資金へ反映
            for p in realized:
                sysname = p["system"]
                cost = float(p.get("cost", 0.0))
                side = p.get("side", "long")
                system_used_value[sysname] = max(0.0, system_used_value[sysname] - cost)
                bucket_used_value[side] = max(0.0, bucket_used_value[side] - cost)
                if side == "short":
                    short_capital += float(p["pnl"])
                else:
                    long_capital += float(p["pnl"])
        # remove exited
        active_positions = [
            p for p in active_positions if p["exit_date"] > current_date
        ]

        # 2) 当日の各Systemシグナルを順番に処理
        for sys_name in [f"System{k}" for k in range(1, 8)]:
            stt = name_to_state.get(sys_name)
            if stt is None:
                continue
            cands = stt.candidates_by_date.get(pd.Timestamp(current_date), [])
            # 互換性確保: {date: {symbol: payload}} 形式にも対応
            try:
                if isinstance(cands, dict):
                    cands = [
                        {
                            "symbol": str(sym),
                            "entry_date": pd.Timestamp(current_date),
                            **(payload or {}),
                        }
                        for sym, payload in cands.items()
                        if isinstance(sym, str) and sym
                    ]
            except Exception:
                # 正規化に失敗した場合は元の構造のまま進める（後段で弾かれる）
                pass
            if not cands:
                continue

            cfg = getattr(stt.strategy, "config", {}) or {}
            max_positions = int(cfg.get("max_positions", 10))
            risk_pct = float(cfg.get("risk_pct", 0.02))
            max_pct = float(cfg.get("max_pct", 0.10))

            # 既存の同システム建玉数
            active_same = [p for p in active_positions if p.get("system") == sys_name]
            slots = max(0, max_positions - len(active_same))
            if slots <= 0:
                continue

            for c in cands[:slots]:
                sym = c.get("symbol")
                # 統合管理: 同銘柄は重複して持たない
                if sym is None:
                    continue
                if _symbol_open_in_active(active_positions, sym):
                    continue
                df = stt.prepared.get(sym)
                if df is None or df.empty:
                    continue

                comp = _compute_entry_exit(stt.strategy, df, c, stt.side)
                if not comp:
                    continue
                entry_idx, entry_price, stop_price, exit_price, exit_date = comp
                if min_hold_days > 0:
                    exit_price, exit_date = _enforce_min_hold_days(
                        df,
                        int(entry_idx),
                        pd.Timestamp(exit_date),
                        float(exit_price),
                        min_hold_days=min_hold_days,
                    )

                # 既定ポジションサイズ
                try:
                    # バケット資金を使用
                    bucket_capital = (
                        short_capital if stt.side == "short" else long_capital
                    )
                    shares_std = stt.strategy.calculate_position_size(
                        bucket_capital,
                        entry_price,
                        stop_price,
                        risk_pct=risk_pct,
                        max_pct=max_pct,
                    )
                except Exception:
                    shares_std = 0
                if shares_std <= 0:
                    continue

                # 資金配分（当日資金×割当）
                bucket_capital = short_capital if stt.side == "short" else long_capital
                alloc_cap = float(allocations.get(sys_name, 0.0)) * bucket_capital
                alloc_rem = max(0.0, alloc_cap - system_used_value[sys_name])
                # バケット総量（ノンレバなら資金 - 既使用）
                if allow_gross_leverage:
                    global_rem = float("inf")
                else:
                    global_rem = max(0.0, bucket_capital - bucket_used_value[stt.side])

                max_by_alloc = int(alloc_rem // abs(entry_price)) if entry_price else 0
                max_by_global = (
                    int(global_rem // abs(entry_price)) if entry_price else 0
                )

                shares_cap = max(0, min(shares_std, max_by_alloc, max_by_global))
                if shares_cap <= 0:
                    continue

                # PnL算出（hook優先）
                if hasattr(stt.strategy, "compute_pnl"):
                    try:
                        pnl = float(
                            stt.strategy.compute_pnl(
                                entry_price, exit_price, int(shares_cap)
                            )
                        )
                    except Exception:
                        pnl = (exit_price - entry_price) * int(shares_cap)
                else:
                    if stt.side == "short":
                        pnl = (entry_price - exit_price) * int(shares_cap)
                    else:
                        pnl = (exit_price - entry_price) * int(shares_cap)

                results.append(
                    {
                        "system": sys_name,
                        "side": stt.side,
                        "symbol": sym,
                        "entry_date": pd.Timestamp(c["entry_date"]),
                        "exit_date": pd.Timestamp(exit_date),
                        "entry_price": round(float(entry_price), 2),
                        "exit_price": round(float(exit_price), 2),
                        "shares": int(shares_cap),
                        "pnl": round(float(pnl), 2),
                        # 参考用：トレード時点のバケット資金に対する比率
                        "return_%": round(
                            (float(pnl) / (bucket_capital if bucket_capital else 1.0))
                            * 100,
                            4,
                        ),
                    }
                )

                cost = float(abs(entry_price) * int(shares_cap))
                active_positions.append(
                    {
                        "system": sys_name,
                        "side": stt.side,
                        "symbol": sym,
                        "exit_date": pd.Timestamp(exit_date),
                        "pnl": float(pnl),
                        "cost": cost,
                    }
                )
                system_used_value[sys_name] += cost
                bucket_used_value[stt.side] += cost

        # 進捗ログ（呼び出し側UIで使う想定）
        # 呼び出し側で i/len(all_dates) を扱う
        _ = i, len(all_dates), start_time  # place holder to keep signature compat idea

    trades_df = pd.DataFrame(results)
    return trades_df, signal_counts


def build_system_states(
    symbols: list[str],
    spy_df: pd.DataFrame | None = None,
    *,
    ui_bridge_prepare=None,
    ui_manager=None,
) -> list[SystemState]:
    """
    各Systemのデータ準備＋候補抽出を実行して SystemState のリストを返す。
    - ui_bridge_prepare: common.ui_bridge.prepare_backtest_data_ui を渡すとUI連携付きで進捗表示可能
    """
    states: list[SystemState] = []

    logging.getLogger(__name__).info("[integrated] preparing per-system data...")
    for i in range(1, 8):
        sys_name = f"System{i}"
        mod = __import__(
            f"strategies.system{i}_strategy",
            fromlist=[f"System{i}Strategy"],
        )
        cls = getattr(mod, f"System{i}Strategy")
        strat = cls()

        # System7 は SPY のみ
        syms = ["SPY"] if sys_name == "System7" else symbols
        try:
            logging.getLogger(__name__).info(
                "[prepare] %s | symbols=%d", sys_name, len(syms)
            )
        except Exception:
            pass

        if ui_bridge_prepare is None:
            # UI非依存のフォールバック読み込み
            from common.ui_components import fetch_data

            raw = fetch_data(syms)
            prepared = strat.prepare_data(raw)
            try:
                cands, _ = strat.generate_candidates(prepared, market_df=spy_df)
            except Exception:
                cands = strat.generate_candidates(prepared)
        else:
            # UI が指定されていればシステムごとのコンテキストを渡す
            sys_ui = ui_manager.system(sys_name) if ui_manager is not None else None
            prepared, cands, _merged = ui_bridge_prepare(
                strat,
                syms,
                system_name=sys_name,
                spy_df=spy_df,
                ui_manager=sys_ui,
            )

        if not prepared:
            prepared = {}
        if not cands:
            cands = {}

        # サマリーをCLIに出力
        try:
            total_prepared = len(prepared or {})
            total_cand_dates = len(cands or {})
            total_cands = int(sum(len(v) for v in (cands or {}).values()))
            logging.getLogger(__name__).info(
                "[prepare.done] %s | prepared=%d | cand_dates=%d | candidates=%d",
                sys_name,
                total_prepared,
                total_cand_dates,
                total_cands,
            )
        except Exception:
            pass

        states.append(
            SystemState(
                name=sys_name,
                side=_get_side(sys_name),
                strategy=strat,
                prepared=prepared,
                candidates_by_date={
                    pd.Timestamp(k): v for k, v in (cands or {}).items()
                },
            )
        )

    return states
