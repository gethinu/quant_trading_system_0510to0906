"""Trade Management System - Entry/Exit Rules Implementation

This module implements the comprehensive trade management system that handles:
- Entry (仕掛け): Market/limit orders with system-specific pricing rules
- Stop Loss (損切): ATR-based stop-loss orders
- Profit Protection (利益の保護): Trailing stops for long-term trend following
- Profit Taking (利食い): Target-based profit taking with percentage or ATR rules
- Re-entry (再仕掛け): Re-entry after stop-loss with full condition validation
- Time-based Exit (時間ベース手仕舞い): Maximum holding period management

This system integrates with the existing allocation framework and supports
all 7 trading systems with their specific rule sets.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta
from enum import Enum
import logging
from typing import Any, Dict, List, Optional

import pandas as pd

logger = logging.getLogger(__name__)


class OrderType(Enum):
    """Order execution types."""

    MARKET = "market"
    LIMIT = "limit"
    STOP = "stop"
    TRAILING_STOP = "trailing_stop"


class TradeStatus(Enum):
    """Trade lifecycle status."""

    PENDING = "pending"  # 注文待ち
    ENTERED = "entered"  # エントリー済み
    STOPPED_OUT = "stopped_out"  # 損切り実行
    PROFIT_TAKEN = "profit_taken"  # 利食い実行
    TIME_EXIT = "time_exit"  # 時間ベース手仕舞い
    CANCELLED = "cancelled"  # キャンセル


class ExitReason(Enum):
    """Exit execution reasons."""

    STOP_LOSS = "stop_loss"  # 損切り
    PROFIT_TARGET = "profit_target"  # 利確目標到達
    TRAILING_STOP = "trailing_stop"  # トレーリングストップ
    TIME_BASED = "time_based"  # 時間ベース
    MANUAL = "manual"  # 手動決済
    RE_ENTRY_CONDITION = "re_entry"  # 再仕掛け


@dataclass
class SystemTradeRules:
    """System-specific trade management rules."""

    system_name: str
    side: str  # "long" or "short"

    # Entry rules (仕掛け)
    entry_type: OrderType
    entry_price_offset_pct: float = 0.0  # Percentage offset from reference price
    entry_reference: str = "close"  # Ref: "open", "close", "high", "low"

    # Stop loss rules (損切り)
    stop_atr_period: int = 20  # ATR calculation period
    stop_atr_multiplier: float = 5.0  # ATR multiplier for stop distance

    # Profit protection rules (利益の保護)
    use_trailing_stop: bool = False
    trailing_stop_pct: float = 0.0  # Trailing stop percentage

    # Profit taking rules (利食い)
    profit_target_type: str = "none"  # "percentage", "atr", "none"
    profit_target_value: float = 0.0  # Target value (percentage or ATR multiplier)
    profit_target_atr_period: int = 10  # ATR period for profit target calculation

    # Time-based exit rules (時間ベース手仕舞い)
    max_holding_days: int = 0  # 0 = no time limit

    # Re-entry rules (再仕掛け)
    allow_re_entry: bool = True  # Allow re-entry after stop-out
    re_entry_delay_days: int = 1  # Days to wait before re-entry

    # Position sizing
    risk_pct: float = 0.02  # Risk percentage of capital
    max_pct: float = 0.10  # Maximum position percentage of capital


@dataclass
class TradeEntry:
    """Trade entry information."""

    symbol: str
    system: str
    side: str
    entry_date: datetime
    entry_price: float
    shares: int
    position_value: float

    # Entry execution details
    entry_type: OrderType
    entry_order_price: Optional[float] = None  # Original order price (for limit orders)

    # Risk management prices
    stop_price: float = 0.0
    initial_stop_price: float = 0.0
    profit_target_price: Optional[float] = None
    trailing_stop_price: Optional[float] = None

    # System-specific rules
    rules: Optional[SystemTradeRules] = None

    # ATR context for dynamic calculations
    entry_atr: float = 0.0
    profit_target_atr: float = 0.0

    # Time-based management
    max_exit_date: Optional[datetime] = None

    # Metadata
    entry_signal_date: Optional[datetime] = None  # Original signal date
    ranking_score: Optional[float] = None
    system_budget: Optional[float] = None
    remaining_after: Optional[float] = None


@dataclass
class TradeExit:
    """Trade exit information."""

    exit_date: datetime
    exit_price: float
    exit_reason: ExitReason
    pnl: float
    pnl_pct: float
    days_held: int

    # Exit execution details
    exit_type: OrderType = OrderType.MARKET
    exit_order_price: Optional[float] = None

    # Performance metrics
    max_favorable_price: Optional[float] = None  # Best price reached during trade
    max_adverse_price: Optional[float] = None  # Worst price reached during trade
    mae: Optional[float] = None  # Maximum Adverse Excursion
    mfe: Optional[float] = None  # Maximum Favorable Excursion


@dataclass
class TradeRecord:
    """Complete trade record with entry and exit information."""

    trade_id: str
    entry: TradeEntry
    exit: Optional[TradeExit] = None
    status: TradeStatus = TradeStatus.PENDING

    # Dynamic tracking during trade lifecycle
    current_price: float = 0.0
    current_pnl: float = 0.0
    current_pnl_pct: float = 0.0

    # Updated prices for dynamic management
    current_stop_price: float = 0.0
    current_trailing_stop: Optional[float] = None
    highest_price_since_entry: float = 0.0  # For long positions
    lowest_price_since_entry: float = 0.0  # For short positions

    # Diagnostics and monitoring
    price_updates: List[Dict[str, Any]] = field(default_factory=list)
    rule_violations: List[str] = field(default_factory=list)

    def __post_init__(self):
        """Initialize derived fields after creation."""
        if self.current_price == 0.0:
            self.current_price = self.entry.entry_price
        if self.current_stop_price == 0.0:
            self.current_stop_price = self.entry.stop_price
        if self.highest_price_since_entry == 0.0:
            self.highest_price_since_entry = self.entry.entry_price
        if self.lowest_price_since_entry == 0.0:
            self.lowest_price_since_entry = self.entry.entry_price


# System-specific rule definitions based on design documents
SYSTEM_TRADE_RULES = {
    "system1": SystemTradeRules(
        system_name="system1",
        side="long",
        entry_type=OrderType.MARKET,
        entry_reference="open",  # 翌日の寄り付きで成り行き
        stop_atr_period=20,
        stop_atr_multiplier=5.0,  # 過去20日の5ATR
        use_trailing_stop=True,
        trailing_stop_pct=0.25,  # 25%のトレーリングストップ
        profit_target_type="none",  # 利益目標は設定しない
        allow_re_entry=True,
    ),
    "system2": SystemTradeRules(
        system_name="system2",
        side="short",
        entry_type=OrderType.LIMIT,
        entry_price_offset_pct=4.0,  # 前日の終値を4%以上上回る価格で売る
        entry_reference="close",
        stop_atr_period=10,
        stop_atr_multiplier=3.0,  # 過去10日の3ATR
        use_trailing_stop=False,  # 利益の保護使わない
        profit_target_type="percentage",
        profit_target_value=4.0,  # 4%の利益が出たら手仕舞う
        max_holding_days=2,  # 2日後に利益目標に到達しない場合は手仕舞い
        allow_re_entry=True,
    ),
    "system3": SystemTradeRules(
        system_name="system3",
        side="long",
        entry_type=OrderType.LIMIT,
        entry_price_offset_pct=-7.0,  # 前日の終値の7%下に指値
        entry_reference="close",
        stop_atr_period=10,
        stop_atr_multiplier=2.5,  # 過去10日の2.5ATR
        use_trailing_stop=False,  # 利益の保護使わない
        profit_target_type="percentage",
        profit_target_value=4.0,  # 終値ベースで4%以上の利益で手仕舞い
        max_holding_days=3,  # 3日たっても目標未達なら手仕舞い
        allow_re_entry=True,
    ),
    "system4": SystemTradeRules(
        system_name="system4",
        side="long",
        entry_type=OrderType.MARKET,
        entry_reference="open",  # 寄り付きで成り行き
        stop_atr_period=40,
        stop_atr_multiplier=1.5,  # 過去40日の1.5ATR
        use_trailing_stop=True,
        trailing_stop_pct=0.20,  # 20%のトレーリングストップ
        profit_target_type="none",  # 利食いはしない
        allow_re_entry=True,
    ),
    "system5": SystemTradeRules(
        system_name="system5",
        side="long",
        entry_type=OrderType.LIMIT,
        entry_price_offset_pct=-3.0,  # 前日の終値の3%下に指値
        entry_reference="close",
        stop_atr_period=10,
        stop_atr_multiplier=3.0,  # 過去10日の3ATR
        use_trailing_stop=False,  # 利益の保護使わない
        profit_target_type="atr",
        profit_target_value=1.0,  # 過去10日の1ATRに利益目標
        profit_target_atr_period=10,
        max_holding_days=6,  # 6日後になっても目標未達なら手仕舞い
        allow_re_entry=True,
    ),
    "system6": SystemTradeRules(
        system_name="system6",
        side="short",
        entry_type=OrderType.LIMIT,
        entry_price_offset_pct=5.0,  # 前日の終値を5%上回る位置に指値で売る
        entry_reference="close",
        stop_atr_period=10,
        stop_atr_multiplier=3.0,  # 過去10日の3ATR
        use_trailing_stop=False,  # 利益の保護使わない
        profit_target_type="percentage",
        profit_target_value=5.0,  # 5%の利益が出たら手仕舞う
        max_holding_days=3,  # 3日後には成り行きで手仕舞う
        allow_re_entry=True,
    ),
    # NOTE (audit-remediation 2026-07-02, finding P0/Part4):
    #   System7 (SPY カタストロフィーヘッジ) は SYSTEM_TRADE_RULES を **使用しない**。
    #   実運用の stop/exit は strategy 側 (strategies/system7_strategy.py) の
    #   カスタム run_backtest で完結する: stop = entry + 3×ATR50
    #   (STOP_ATR_MULTIPLE_DEFAULT, strategies/constants.py:19), exit = 70日高値更新→翌寄り。
    #   以前ここに存在した stub (stop_atr_period=20, stop_atr_multiplier=5.0,
    #   コメント「詳細は要確認」) は仕様 (50日3ATR) とも実路とも不一致な未使用
    #   プレースホルダで、将来 System7 に配線された瞬間に stop が 20日5ATR に化ける
    #   時限バグだったため削除した。SYSTEM_TRADE_RULES.get("system7") は None を返し、
    #   create_trade_entry / run_auto_rule 系は graceful に skip する (誤った 20日5ATR
    #   での enhancement を行うより安全)。
}


class TradeManager:
    """Trade lifecycle management system."""

    def __init__(self):
        self.active_trades: Dict[str, TradeRecord] = {}
        self.completed_trades: List[TradeRecord] = []
        self.trade_counter = 0

    def _get_row_for_date(
        self,
        market_data: pd.DataFrame,
        date: datetime,
    ) -> Optional[pd.Series]:
        """Return a row for the given date, with robust fallbacks.

        Preference order:
        1) exact date
        2) previous available (pad/ffill)
        3) next available (backfill/bfill)
        4) nearest
        """
        try:
            if market_data is None or market_data.empty:
                return None
            idx = market_data.index
            ts = pd.Timestamp(date).normalize()

            # Exact
            if ts in idx:
                row = market_data.loc[ts]
                return row.iloc[0] if isinstance(row, pd.DataFrame) else row

            # Try previous first
            for method in ("pad", "ffill", "backfill", "bfill", "nearest"):
                try:
                    pos_arr = idx.get_indexer([ts], method=method)
                    pos = int(pos_arr[0]) if len(pos_arr) > 0 else -1
                except Exception:
                    pos = -1
                if pos is not None and 0 <= pos < len(idx):
                    row = market_data.iloc[pos]
                    return row.iloc[0] if isinstance(row, pd.DataFrame) else row
        except Exception:
            return None
        return None

    def create_trade_entry(
        self,
        symbol: str,
        system: str,
        side: str,
        signal_date: datetime,
        entry_data: Dict[str, Any],
        market_data: pd.DataFrame,
    ) -> Optional[TradeEntry]:
        """Create a trade entry from allocation result and market data.

        Args:
            symbol: Trading symbol
            system: System name (system1, system2, etc.)
            side: Trading side ("long" or "short")
            signal_date: Date when signal was generated
            entry_data: Entry data from allocation (includes shares, prices, etc.)
            market_data: Market data for calculating entry and stop prices

        Returns:
            TradeEntry object with all necessary trade management information
        """
        try:
            rules = SYSTEM_TRADE_RULES.get(system.lower())
            if not rules:
                logger.error("No trade rules found for system: %s", system)
                return None

            # Extract basic information
            shares = int(entry_data.get("shares", 0))
            if shares <= 0:
                logger.warning("Invalid shares for %s: %s", symbol, shares)
                return None

            # Calculate entry price based on system rules
            # Fallback priority order:
            # 1. Market data (Open for MARKET orders, reference price for LIMIT)
            # 2. Allocation entry_price (from signal generation phase)
            # 3. Fail if neither available
            entry_price = self._calculate_entry_price(market_data, signal_date, rules)
            if entry_price is None or entry_price <= 0:
                # Fallback: use entry_price from allocation result (signal phase)
                fallback_raw = entry_data.get("entry_price")
                if fallback_raw is not None:
                    try:
                        fallback_price = float(fallback_raw)
                    except (TypeError, ValueError):
                        fallback_price = None
                else:
                    fallback_price = None
                if fallback_price is not None and fallback_price > 0:
                    entry_price = fallback_price
                    logger.debug(
                        "Using allocation fallback entry_price for %s: %s",
                        symbol,
                        entry_price,
                    )
                else:
                    logger.debug(
                        (
                            "Could not calculate entry price for %s (no market data fallback)"
                        ),
                        symbol,
                    )
                    return None

            # Calculate stop loss price
            stop_price = self._calculate_stop_price(
                market_data,
                signal_date,
                entry_price,
                rules,
            )
            if stop_price is None:
                logger.debug("Could not calculate stop price for %s", symbol)
                return None

            # Calculate profit target if applicable
            profit_target_price = self._calculate_profit_target_price(
                market_data, signal_date, entry_price, rules
            )

            # Get ATR values for reference (coerce Optional to float)
            entry_atr_opt = self._get_atr_value(
                market_data,
                signal_date,
                rules.stop_atr_period,
            )
            profit_target_atr_opt = (
                self._get_atr_value(
                    market_data,
                    signal_date,
                    rules.profit_target_atr_period,
                )
                if rules.profit_target_type == "atr"
                else 0.0
            )
            entry_atr = float(entry_atr_opt or 0.0)
            try:
                profit_target_atr = float(profit_target_atr_opt or 0.0)
            except Exception:
                profit_target_atr = 0.0

            # Calculate position value
            position_value = shares * abs(entry_price)

            # Calculate maximum exit date for time-based management
            max_exit_date = None
            if rules.max_holding_days > 0:
                max_exit_date = signal_date + timedelta(days=rules.max_holding_days)

            # Create entry record
            entry = TradeEntry(
                symbol=symbol,
                system=system,
                side=side,
                entry_date=signal_date,  # Will be updated to actual entry date
                entry_price=entry_price,
                shares=shares,
                position_value=position_value,
                entry_type=rules.entry_type,
                stop_price=stop_price,
                initial_stop_price=stop_price,
                profit_target_price=profit_target_price,
                rules=rules,
                entry_atr=entry_atr,
                profit_target_atr=profit_target_atr,
                max_exit_date=max_exit_date,
                entry_signal_date=signal_date,
                ranking_score=entry_data.get("score"),
                system_budget=entry_data.get("system_budget"),
                remaining_after=entry_data.get("remaining_after"),
            )

            # Set order price for limit orders
            if rules.entry_type == OrderType.LIMIT:
                entry.entry_order_price = self._calculate_limit_order_price(
                    market_data, signal_date, rules
                )

            return entry

        except Exception as e:
            logger.error("Error creating trade entry for %s: %s", symbol, e)
            return None

    def _calculate_entry_price(
        self,
        market_data: pd.DataFrame,
        signal_date: datetime,
        rules: SystemTradeRules,
    ) -> Optional[float]:
        """Calculate entry price based on system rules.

        Priority:
        1. Open price for MARKET orders (if available on signal_date)
        2. Close price as fallback when Open is missing
        3. Reference price + offset for LIMIT orders
        """
        try:
            # Determine reference row for the given signal_date with robust fallback
            ref_row = self._get_row_for_date(market_data, signal_date)
            if ref_row is None:
                return None

            ref_row_date = None
            try:
                ref_row_date = getattr(ref_row, "name", None)
                if isinstance(ref_row_date, (pd.Timestamp, datetime)):
                    ref_row_date = pd.Timestamp(ref_row_date).normalize()
            except Exception:
                ref_row_date = None
            target_date = pd.Timestamp(signal_date).normalize()

            if rules.entry_type == OrderType.MARKET:
                # Market orders: use entry day's open (ctx.today is entry date)
                if isinstance(ref_row, pd.DataFrame):
                    # duplicate index guard: use first row
                    ref_row = ref_row.iloc[0]

                def _coerce_float(x: Any) -> Optional[float]:
                    try:
                        if isinstance(x, pd.Series):
                            if x.empty:
                                return None
                            x = x.iloc[0]
                        f = float(x)
                        return f if f > 0 else None
                    except Exception:
                        return None

                open_price = _coerce_float(ref_row.get("Open", ref_row.get("open", 0)))
                if open_price and open_price > 0:
                    return open_price
                # Fallback: if open is missing, try using Close as an approximation
                close_price = _coerce_float(
                    ref_row.get("Close", ref_row.get("close", 0))
                )
                if close_price and close_price > 0:
                    logger.debug(
                        "Entry price fallback to Close for %s (target=%s, source=%s)",
                        rules.system_name,
                        target_date,
                        ref_row_date,
                    )
                    return close_price
                logger.debug(
                    "Entry price not available for %s on %s (fallback row=%s)",
                    rules.system_name,
                    target_date,
                    ref_row_date,
                )
                return None

            elif rules.entry_type == OrderType.LIMIT:
                # Limit orders: calculate based on reference price and offset
                if isinstance(ref_row, pd.DataFrame):
                    ref_row = ref_row.iloc[0]
                ref_val = ref_row.get(
                    rules.entry_reference.title(),
                    ref_row.get(rules.entry_reference.lower(), 0),
                )
                try:
                    if isinstance(ref_val, pd.Series):
                        ref_val = ref_val.iloc[0]
                    ref_price = float(ref_val)
                except Exception:
                    ref_price = 0.0
                if ref_price <= 0:
                    return None

                offset_multiplier = 1.0 + (rules.entry_price_offset_pct / 100.0)
                entry_price = ref_price * offset_multiplier
                return entry_price

            return None

        except Exception as e:
            logger.error("Error calculating entry price: %s", e)
            return None

    def _calculate_limit_order_price(
        self,
        market_data: pd.DataFrame,
        signal_date: datetime,
        rules: SystemTradeRules,
    ) -> Optional[float]:
        """Calculate the original limit order price."""
        try:
            ref_row = self._get_row_for_date(market_data, signal_date)
            if ref_row is None:
                return None
            if isinstance(ref_row, pd.DataFrame):
                ref_row = ref_row.iloc[0]
            ref_price = float(
                ref_row.get(
                    rules.entry_reference.title(),
                    ref_row.get(rules.entry_reference.lower(), 0),
                )
            )
            if ref_price <= 0:
                return None

            offset_multiplier = 1.0 + (rules.entry_price_offset_pct / 100.0)
            return ref_price * offset_multiplier

        except Exception as e:
            logger.error("Error calculating limit order price: %s", e)
            return None

    def _calculate_stop_price(
        self,
        market_data: pd.DataFrame,
        signal_date: datetime,
        entry_price: float,
        rules: SystemTradeRules,
    ) -> Optional[float]:
        """Calculate stop loss price based on ATR and system rules."""
        try:
            atr_value = self._get_atr_value(
                market_data,
                signal_date,
                rules.stop_atr_period,
            )
            if atr_value is None or atr_value <= 0:
                return None

            stop_distance = atr_value * rules.stop_atr_multiplier

            if rules.side == "long":
                # Long positions: stop below entry
                stop_price = entry_price - stop_distance
            else:
                # Short positions: stop above entry
                stop_price = entry_price + stop_distance

            return max(0.01, stop_price)  # Ensure positive price

        except Exception as e:
            logger.error("Error calculating stop price: %s", e)
            return None

    def _calculate_profit_target_price(
        self,
        market_data: pd.DataFrame,
        signal_date: datetime,
        entry_price: float,
        rules: SystemTradeRules,
    ) -> Optional[float]:
        """Calculate profit target price if applicable."""
        try:
            if rules.profit_target_type == "none":
                return None

            elif rules.profit_target_type == "percentage":
                target_multiplier = 1.0 + (rules.profit_target_value / 100.0)
                if rules.side == "long":
                    return entry_price * target_multiplier
                else:
                    # Short position: profit when price goes down
                    return entry_price / target_multiplier

            elif rules.profit_target_type == "atr":
                atr_value = self._get_atr_value(
                    market_data, signal_date, rules.profit_target_atr_period
                )
                if atr_value is None or atr_value <= 0:
                    return None

                target_distance = atr_value * rules.profit_target_value
                if rules.side == "long":
                    return entry_price + target_distance
                else:
                    return entry_price - target_distance

            return None

        except Exception as e:
            logger.error("Error calculating profit target price: %s", e)
            return None

    def _get_atr_value(
        self,
        market_data: pd.DataFrame,
        date: datetime,
        period: int,
    ) -> Optional[float]:
        """Get ATR value for the specified date and period."""
        try:
            # Look for ATR column with the specified period
            atr_col = f"atr{period}"
            alt_cols = [f"ATR{period}", f"atr_{period}", f"ATR_{period}"]

            row = self._get_row_for_date(market_data, date)
            if row is None:
                return None
            if isinstance(row, pd.DataFrame):
                row = row.iloc[0]

            # Try primary column name
            if atr_col in row.index:
                raw_val = row[atr_col]
                try:
                    val = raw_val.iloc[0] if isinstance(raw_val, pd.Series) else raw_val
                    if pd.notna(val) and float(val) > 0:
                        return float(val)
                except Exception:
                    pass

            # Try alternative column names
            for col in alt_cols:
                if col in row.index:
                    raw_val2 = row[col]
                    try:
                        val2 = (
                            raw_val2.iloc[0]
                            if isinstance(raw_val2, pd.Series)
                            else raw_val2
                        )
                        if pd.notna(val2) and float(val2) > 0:
                            return float(val2)
                    except Exception:
                        continue

            logger.warning("ATR%s not found for date %s", period, date)
            return None

        except Exception as e:
            logger.error("Error getting ATR value: %s", e)
            return None

    def enhance_allocation_with_trade_management(
        self,
        allocation_df: pd.DataFrame,
        market_data_dict: Dict[str, pd.DataFrame],
        signal_date: datetime,
    ) -> pd.DataFrame:
        """Enhance allocation DataFrame with comprehensive trade management information.

        Args:
            allocation_df: Result from finalize_allocation
            market_data_dict: Market data by symbol
            signal_date: Date when signals were generated

        Returns:
            Enhanced DataFrame with trade management columns
        """
        if allocation_df.empty:
            return allocation_df.copy()

        enhanced_records = []

        for _, row in allocation_df.iterrows():
            try:
                symbol = row.get("symbol", "")
                system = row.get("system", "")
                side = row.get("side", "")

                if not all([symbol, system, side]):
                    logger.warning(
                        "Missing required fields in allocation row: %s",
                        dict(row),
                    )
                    enhanced_records.append(dict(row))
                    continue

                # Get market data for this symbol
                market_data = market_data_dict.get(symbol)
                if market_data is None or market_data.empty:
                    logger.warning("No market data available for %s", symbol)
                    enhanced_records.append(dict(row))
                    continue

                # Create trade entry
                entry_data = dict(row)
                trade_entry = self.create_trade_entry(
                    symbol=symbol,
                    system=system,
                    side=side,
                    signal_date=signal_date,
                    entry_data=entry_data,
                    market_data=market_data,
                )

                if trade_entry is None:
                    logger.warning("Could not create trade entry for %s", symbol)
                    enhanced_records.append(dict(row))
                    continue

                # Create enhanced record with trade management fields
                enhanced_row: Dict[str, Any] = dict(row)

                # Entry information
                enhanced_row["entry_type"] = trade_entry.entry_type.value
                enhanced_row["entry_price_final"] = trade_entry.entry_price
                enhanced_row["entry_order_price"] = trade_entry.entry_order_price

                # Risk management
                enhanced_row["stop_price"] = trade_entry.stop_price
                enhanced_row["initial_stop_price"] = trade_entry.initial_stop_price
                enhanced_row["profit_target_price"] = trade_entry.profit_target_price

                # System rules
                if trade_entry.rules:
                    enhanced_row["use_trailing_stop"] = (
                        trade_entry.rules.use_trailing_stop
                    )
                    enhanced_row["trailing_stop_pct"] = (
                        trade_entry.rules.trailing_stop_pct
                    )
                    enhanced_row["max_holding_days"] = (
                        trade_entry.rules.max_holding_days
                    )
                    enhanced_row["allow_re_entry"] = trade_entry.rules.allow_re_entry

                # ATR context
                enhanced_row["entry_atr"] = trade_entry.entry_atr
                enhanced_row["profit_target_atr"] = trade_entry.profit_target_atr

                # Time management
                enhanced_row["max_exit_date"] = trade_entry.max_exit_date
                enhanced_row["signal_date"] = trade_entry.entry_signal_date

                # Risk metrics
                if trade_entry.entry_price > 0 and trade_entry.stop_price > 0:
                    risk_per_share = abs(
                        trade_entry.entry_price - trade_entry.stop_price
                    )
                    total_risk = risk_per_share * trade_entry.shares
                    enhanced_row["risk_per_share"] = risk_per_share
                    enhanced_row["total_risk"] = total_risk

                    if trade_entry.position_value > 0:
                        enhanced_row["risk_pct_position"] = (
                            total_risk / trade_entry.position_value
                        ) * 100

                enhanced_records.append(enhanced_row)

            except Exception as e:
                logger.error("Error enhancing allocation row: %s", e)
                enhanced_records.append(dict(row))
                continue

        # Create enhanced DataFrame
        if enhanced_records:
            enhanced_df = pd.DataFrame(enhanced_records)

            # Ensure original column order is preserved, new columns added at end
            original_cols = allocation_df.columns.tolist()
            new_cols = [col for col in enhanced_df.columns if col not in original_cols]
            column_order = original_cols + new_cols
            enhanced_df = enhanced_df[column_order]

            return enhanced_df
        else:
            return allocation_df.copy()


def get_system_trade_rules(system_name: str) -> Optional[SystemTradeRules]:
    """Get trade rules for a specific system."""
    return SYSTEM_TRADE_RULES.get(system_name.lower())


def validate_trade_management_data(df: pd.DataFrame) -> List[str]:
    """Validate trade management data completeness.

    Returns:
        List of validation error messages (empty if all valid)
    """
    errors = []

    if df.empty:
        return ["DataFrame is empty"]

    required_columns = [
        "symbol",
        "system",
        "side",
        "shares",
        "entry_price_final",
        "stop_price",
    ]

    missing_cols = [col for col in required_columns if col not in df.columns]
    if missing_cols:
        errors.append(f"Missing required columns: {missing_cols}")

    # Validate data quality for each row
    for idx, row in df.iterrows():
        symbol = row.get("symbol", "")
        if not symbol:
            errors.append(f"Row {idx}: Missing symbol")

        shares = row.get("shares", 0)
        if shares <= 0:
            errors.append(f"Row {idx} ({symbol}): Invalid shares: {shares}")

        entry_price = row.get("entry_price_final", 0)
        if entry_price <= 0:
            errors.append(f"Row {idx} ({symbol}): Invalid entry price: {entry_price}")

        stop_price = row.get("stop_price", 0)
        if stop_price <= 0:
            errors.append(f"Row {idx} ({symbol}): Invalid stop price: {stop_price}")

    return errors


__all__ = [
    "OrderType",
    "TradeStatus",
    "ExitReason",
    "SystemTradeRules",
    "TradeEntry",
    "TradeExit",
    "TradeRecord",
    "TradeManager",
    "SYSTEM_TRADE_RULES",
    "get_system_trade_rules",
    "validate_trade_management_data",
]
