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

from common.backtest_context import backtest_context
from common.candidate_schema import normalize_candidates_for_date, resolve_entry_bar
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
    # Candidates carry either "entry_date" (System1/2/4/5/6/7) or only the
    # signal "date" (System3 full scan); resolve_entry_bar handles both and
    # raises CandidateSchemaError when neither is usable.
    resolved = resolve_entry_bar(df, candidate, system=None)
    if resolved is None:
        return None
    entry_idx, _entry_ts = resolved

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


def run_integrated_backtest(
    system_states: list[SystemState],
    initial_capital: float,
    allocations: AllocationMap | None = None,
    *,
    long_share: float = 0.5,
    short_share: float = 0.5,
    allow_gross_leverage: bool = False,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """統合バックテスト（backtest コンテキストを張って本体へ委譲）。"""
    with backtest_context():
        return _run_integrated_backtest(
            system_states,
            initial_capital,
            allocations,
            long_share=long_share,
            short_share=short_share,
            allow_gross_leverage=allow_gross_leverage,
            on_progress=on_progress,
        )


def _run_integrated_backtest(
    system_states: list[SystemState],
    initial_capital: float,
    allocations: AllocationMap | None = None,
    *,
    long_share: float = 0.5,
    short_share: float = 0.5,
    allow_gross_leverage: bool = False,
    on_progress: Callable[[int, int, float], None] | None = None,
) -> tuple[pd.DataFrame, dict[str, int]]:
    """
    統合バックテスト本体。
    - system_states: 各Systemの prepared/candidates を含む状態
    - initial_capital: 初期資金（共通）
    - allocations: システム別の最大投下資金比率（当日基準）。指定無ければ既定。
    - allow_gross_leverage: Trueなら総建玉のコスト合計が資金を超えても許容（既定False）

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
            # 互換性確保: {date: {symbol: payload}} と list 形式の双方に対応。
            # 認識できない形なら CandidateSchemaError で即座に落とす（黙って
            # 0 建玉になる旧挙動が System3 を全バックテストから消していた）。
            cands = normalize_candidates_for_date(cands, current_date, system=sys_name)
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

                resolved = resolve_entry_bar(df, c, system=sys_name)
                if resolved is None:
                    continue
                # 戦略フックが参照する entry_date を、この銘柄の実バーへ解決して渡す
                # （System3 のフルスキャン候補は "date" しか持たない）。
                c = {**c, "entry_date": resolved[1]}

                comp = _compute_entry_exit(stt.strategy, df, c, stt.side)
                if not comp:
                    continue
                entry_idx, entry_price, stop_price, exit_price, exit_date = comp

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

    候補抽出は backtest コンテキスト内で行う。today 実行専用の高速パス
    （System6 の latest_only 強制など）はここでは無効化され、履歴が潰れない。
    live（scripts/run_all_systems_today.py）はこの関数を通らないので無影響。
    """
    with backtest_context():
        return _build_system_states(
            symbols,
            spy_df,
            ui_bridge_prepare=ui_bridge_prepare,
            ui_manager=ui_manager,
        )


def _build_system_states(
    symbols: list[str],
    spy_df: pd.DataFrame | None = None,
    *,
    ui_bridge_prepare: Any = None,
    ui_manager: Any = None,
) -> list[SystemState]:
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
