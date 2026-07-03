"""Alpaca **Paper** 自動売買のための高レベル発注レイヤ。

提供する 3 つの公開 API: submit_paper_order / signals_to_orders / signals_json_to_orders.

安全設計:
    - ALPACA_PAPER が真でない、または base URL が live を指す場合は
      LiveAccountGuardError を送出して live 口座への誤配信を防ぐ。
    - ALPACA_PAPER_STRICT=1 で ALPACA_PAPER の明示設定を強制。
    - dry_run がデフォルト True。実発注は明示的に dry_run=False を指定した場合のみ。
    - 送信内容は logs/alpaca_orders_YYYYMMDD.log に追記される (監査証跡)。
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
from typing import Any

import pandas as pd

from common import broker_alpaca as ba

logger = logging.getLogger(__name__)

_DEFAULT_SYSTEM_ORDER_TYPE = {
    "system1": "market",
    "system3": "market",
    "system4": "market",
    "system5": "market",
    "system2": "limit",
    "system6": "limit",
    "system7": "limit",
}

_LOG_DIR = Path(os.getenv("ALPACA_ORDER_LOG_DIR", "logs"))
_PAPER_HOST = "paper-api.alpaca.markets"

# Tier ごとの日次デプロイ notional
TIER_NOTIONAL_USD: dict[str, float] = {
    "small": 1_000.0,
    "medium": 10_000.0,
    "large": 100_000.0,
}


def resolve_tier_notional(tier: str) -> float:
    key = (tier or "").strip().lower()
    return TIER_NOTIONAL_USD.get(key, TIER_NOTIONAL_USD["small"])


class LiveAccountGuardError(RuntimeError):
    pass


class OrderSubmitError(RuntimeError):
    pass


@dataclass(slots=True)
class PreparedOrder:
    symbol: str
    qty: int
    side: str
    order_type: str = "market"
    limit_price: float | None = None
    time_in_force: str = "day"
    client_order_id: str | None = None
    system: str | None = None
    entry_date: str | None = None
    order_id: str | None = None
    status: str | None = None
    error: str | None = None
    notional_usd: float | None = None
    tier: str | None = None
    dry_run: bool = False
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        d = asdict(self)
        d.pop("extra", None)
        return d


# --- Safety guard --------------------------------------------------------
_TRUTHY = ("1", "true", "yes", "y", "on")


def _is_paper_env() -> bool:
    return os.getenv("ALPACA_PAPER", "true").strip().lower() in _TRUTHY


def _is_strict_mode() -> bool:
    return os.getenv("ALPACA_PAPER_STRICT", "").strip().lower() in _TRUTHY


def assert_paper_env() -> None:
    raw = os.getenv("ALPACA_PAPER")
    if _is_strict_mode() and (raw is None or raw.strip() == ""):
        raise LiveAccountGuardError(
            "ALPACA_PAPER_STRICT=1 のため ALPACA_PAPER の明示設定が必要です。"
            " .env に ALPACA_PAPER=true を設定してから再実行してください。"
        )
    if not _is_paper_env():
        raise LiveAccountGuardError(
            "ALPACA_PAPER が true ではありません。live 口座への誤発注を防ぐため中止します。"
            " Paper 取引のみ許可されています (.env の ALPACA_PAPER=true を確認)。"
        )
    base_url = os.getenv("ALPACA_API_BASE_URL", "")
    if base_url and _PAPER_HOST not in base_url:
        raise LiveAccountGuardError(
            f"ALPACA_API_BASE_URL が paper エンドポイント ({_PAPER_HOST}) を指していません: "
            f"{base_url!r}。live 口座への誤発注を防ぐため中止します。"
        )


def _audit_log(record: dict[str, Any]) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        path = _LOG_DIR / f"alpaca_orders_{stamp:%Y%m%d}.log"
        record = {"ts": stamp.isoformat(), **record}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover
        logger.warning("監査ログ書き込み失敗: %s", exc)


def _classify_error(exc: Exception) -> OrderSubmitError:
    msg = str(exc).lower()
    if "insufficient" in msg or "buying power" in msg:
        reason = "資金不足 (insufficient buying power)"
    elif "market is closed" in msg or "not open" in msg or "closed" in msg:
        reason = "市場休場 (market closed)"
    elif "not found" in msg or "invalid" in msg or "not tradable" in msg:
        reason = "無効シンボル (symbol invalid / not tradable)"
    else:
        reason = "発注失敗"
    return OrderSubmitError(f"{reason}: {exc}")


def submit_paper_order(
    symbol: str,
    qty: int,
    side: str,
    order_type: str = "market",
    limit_price: float | None = None,
    time_in_force: str = "day",
    client_order_id: str | None = None,
    *,
    dry_run: bool = True,
    client: Any | None = None,
    retries: int = 2,
    backoff_seconds: float = 1.0,
    rate_limit_seconds: float = 0.35,
) -> PreparedOrder:
    side = side.lower().strip()
    if side not in ("buy", "sell"):
        raise ValueError(f"side は 'buy' か 'sell': {side!r}")
    order_type = order_type.lower().strip()
    if order_type == "limit" and limit_price is None:
        raise ValueError("limit 注文には limit_price が必要です。")
    qty = int(qty)
    if qty <= 0:
        raise ValueError(f"qty は正の整数: {qty}")

    prepared = PreparedOrder(
        symbol=symbol.upper(),
        qty=qty,
        side=side,
        order_type=order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
    )

    if dry_run:
        _audit_log({"event": "dry_run", **prepared.to_row()})
        return prepared

    assert_paper_env()

    if client is None:
        client = ba.get_client(paper=True)

    try:
        order = ba.submit_order_with_retry(
            client,
            prepared.symbol,
            prepared.qty,
            side=prepared.side,
            order_type=prepared.order_type,
            limit_price=prepared.limit_price,
            time_in_force=prepared.time_in_force,
            client_order_id=prepared.client_order_id,
            retries=retries,
            backoff_seconds=backoff_seconds,
            rate_limit_seconds=rate_limit_seconds,
        )
    except Exception as exc:
        prepared.error = str(exc)
        _audit_log({"event": "submit_error", **prepared.to_row()})
        raise _classify_error(exc) from exc

    prepared.order_id = str(getattr(order, "id", "") or "")
    prepared.status = str(getattr(order, "status", "") or "")
    _audit_log({"event": "submitted", **prepared.to_row()})
    logger.info(
        "Paper order submitted: %s %s x%d id=%s status=%s",
        prepared.side, prepared.symbol, prepared.qty,
        prepared.order_id, prepared.status,
    )
    return prepared


# NOTE(F2 P0#2 audit fix, 2026-07-03): silent `sell` default on unknown/missing
# `side` values previously caused every schema drift (missing column, typo, new
# system id) to submit an unintended short. Now we require an explicit mapping;
# unknown values raise InvalidSideError and the batch loop skips the row so a
# single bad row doesn't blow up the whole run but also never silently shorts.
_SIDE_ALIASES: dict[str, str] = {
    "buy": "buy",
    "long": "buy",
    "sell": "sell",
    "short": "sell",
    "sell_short": "sell",
}


class InvalidSideError(ValueError):
    """Raised when a signals row has a missing or unrecognized ``side`` value.

    We refuse to guess: the previous silent default-to-sell caused unintended
    short submissions when the upstream signals frame drifted. Failing loudly
    lets the operator see the row.
    """


def _side_from_row(row: pd.Series) -> str:
    raw = str(row.get("side", "")).strip().lower()
    if not raw:
        raise InvalidSideError(
            f"signals row has no 'side' (symbol={row.get('symbol')}, "
            f"system={row.get('system')})"
        )
    try:
        return _SIDE_ALIASES[raw]
    except KeyError as exc:
        raise InvalidSideError(
            f"unrecognized side {raw!r} for symbol={row.get('symbol')} "
            f"(system={row.get('system')})"
        ) from exc


def _order_type_from_row(row: pd.Series, override: str | None) -> str:
    if override:
        return override
    system = str(row.get("system", "")).lower()
    return _DEFAULT_SYSTEM_ORDER_TYPE.get(system, "market")


def _build_client_order_id(row: pd.Series) -> str:
    sym = str(row.get("symbol", "")).upper()
    system = str(row.get("system", "")).lower()
    date = str(row.get("entry_date", "")).replace("-", "").replace(" ", "")[:8]
    return f"{system}-{sym}-{date}" if date else f"{system}-{sym}"


def signals_to_orders(
    signals: pd.DataFrame,
    account_equity: float,
    dry_run: bool = True,
    *,
    order_type: str | None = None,
    time_in_force: str = "day",
    open_positions: dict[str, float] | None = None,
    client: Any | None = None,
) -> list[PreparedOrder]:
    if signals is None or signals.empty:
        return []
    if "shares" not in signals.columns:
        logger.warning("signals に shares 列がありません。")
        return []

    if not dry_run:
        assert_paper_env()
        if client is None:
            client = ba.get_client(paper=True)
        if open_positions is None:
            open_positions = _fetch_open_positions(client)
    open_positions = open_positions or {}

    prepared: list[PreparedOrder] = []
    seen: set[tuple[str, str, str]] = set()

    for _, row in signals.iterrows():
        sym = str(row.get("symbol", "")).upper()
        qty = int(row.get("shares") or 0)
        if not sym or qty <= 0:
            continue
        system = str(row.get("system", "")).lower()
        entry_date = str(row.get("entry_date", "")) if row.get("entry_date") else None

        dedup_key = (sym, system, str(entry_date))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        try:
            side = _side_from_row(row)
        except InvalidSideError as exc:
            # Fail loudly per-row but keep the batch alive. A single bad row
            # (missing/unknown `side`) must NOT silently become a short and
            # must NOT kill the whole run.
            logger.error("skip signals row: %s", exc)
            _audit_log(
                {
                    "event": "skip_invalid_side",
                    "detail": str(exc),
                    "symbol": sym,
                    "system": system,
                    "entry_date": entry_date,
                }
            )
            continue
        held = open_positions.get(sym, 0.0)
        if side == "buy" and held > 0:
            continue
        if side == "sell" and held < 0:
            continue

        ot = _order_type_from_row(row, order_type)
        limit_price: float | None = None
        if ot == "limit":
            raw_px = row.get("entry_price")
            try:
                if raw_px not in (None, ""):
                    limit_price = float(raw_px)
            except (TypeError, ValueError):
                limit_price = None
            if limit_price is None:
                ot = "market"

        po = PreparedOrder(
            symbol=sym,
            qty=qty,
            side=side,
            order_type=ot,
            limit_price=limit_price,
            time_in_force=time_in_force,
            client_order_id=_build_client_order_id(row),
            system=system or None,
            entry_date=entry_date,
        )
        prepared.append(po)

    logger.info(
        "signals_to_orders: %d 注文を生成 (equity=$%.0f, dry_run=%s)",
        len(prepared), account_equity, dry_run,
    )

    if dry_run:
        for po in prepared:
            _audit_log({"event": "dry_run", **po.to_row()})
        return prepared

    submitted: list[PreparedOrder] = []
    for po in prepared:
        result = submit_paper_order(
            po.symbol, po.qty, po.side,
            order_type=po.order_type,
            limit_price=po.limit_price,
            time_in_force=po.time_in_force,
            client_order_id=po.client_order_id,
            dry_run=False,
            client=client,
        )
        result.system = po.system
        result.entry_date = po.entry_date
        submitted.append(result)
    return submitted


def _fetch_open_positions(client: Any) -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        positions = client.get_all_positions()
    except Exception as exc:  # pragma: no cover
        logger.warning("open positions 取得失敗: %s", exc)
        return out
    for p in positions:
        try:
            sym = str(getattr(p, "symbol", "")).upper()
            qty = float(getattr(p, "qty", 0) or 0)
            if sym:
                out[sym] = qty
        except Exception:
            continue
    return out


# --- Public API 3: JSON signals to orders --------------------------------
def _flatten_json_signals(json_data: dict[str, Any]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    systems = (json_data or {}).get("systems") or {}
    if not isinstance(systems, dict):
        return out
    for sys_key, sys_block in systems.items():
        if not isinstance(sys_block, dict):
            continue
        signals = sys_block.get("signals") or []
        if not isinstance(signals, list):
            continue
        norm_sys = str(sys_key).lower().replace("sys", "system")
        if not norm_sys.startswith("system"):
            norm_sys = f"system_{sys_key}"
        for s in signals:
            if not isinstance(s, dict):
                continue
            sym = str(s.get("symbol", "")).upper()
            if not sym:
                continue
            side_raw = str(s.get("side", "buy")).lower()
            if side_raw in ("buy", "long"):
                side = "buy"
            elif side_raw in ("sell", "short"):
                side = "sell"
            else:
                side = "buy"
            try:
                price = float(s.get("entry_price") or 0.0)
            except (TypeError, ValueError):
                price = 0.0
            try:
                weight = float(s.get("weight") or 0.0)
            except (TypeError, ValueError):
                weight = 0.0
            out.append({
                "symbol": sym, "side": side, "entry_price": price,
                "weight": weight, "system": norm_sys,
            })
    return out


def signals_json_to_orders(
    json_data: dict[str, Any],
    tier: str,
    *,
    dry_run: bool = True,
    account_equity: float = 10_000.0,
    min_notional_usd: float = 5.0,
    prefer_fractional: bool = True,
    entry_date: str | None = None,
    client: Any | None = None,
) -> list[PreparedOrder]:
    """today_signals JSON を tier 別 notional で配分し Alpaca 注文へ変換する。"""
    signals = _flatten_json_signals(json_data)
    if not signals:
        return []

    tier_notional = resolve_tier_notional(tier)
    total_weight = sum(max(0.0, s["weight"]) for s in signals)
    if total_weight <= 0:
        per_signal_default = tier_notional / len(signals)
    else:
        per_signal_default = 0.0

    if not dry_run:
        assert_paper_env()
        if client is None:
            client = ba.get_client(paper=True)

    if entry_date is None:
        entry_date = str(json_data.get("date") or "")
    date_compact = entry_date.replace("-", "").replace(" ", "")[:8]

    prepared: list[PreparedOrder] = []
    seen: set[tuple[str, str]] = set()

    for s in signals:
        sym = s["symbol"]
        side = s["side"]
        price = s["entry_price"]
        weight = max(0.0, s["weight"])
        system = s["system"]

        dedup = (sym, system)
        if dedup in seen:
            continue
        seen.add(dedup)

        if total_weight > 0:
            notional = weight / total_weight * tier_notional
        else:
            notional = per_signal_default

        if notional < min_notional_usd:
            continue

        qty: int = 0
        if not prefer_fractional:
            if price <= 0:
                continue
            qty = int(notional / price)
            if qty <= 0:
                continue

        client_order_id = (
            f"{system}-{sym}-{date_compact}" if date_compact else f"{system}-{sym}"
        )
        po = PreparedOrder(
            symbol=sym,
            qty=qty,
            side=side,
            order_type="market",
            time_in_force="day",
            client_order_id=client_order_id,
            system=system,
            entry_date=entry_date or None,
            notional_usd=round(notional, 2),
            tier=tier,
            dry_run=dry_run,
        )
        prepared.append(po)

    logger.info(
        "signals_json_to_orders: %d 注文 tier=%s tier_notional=$%.0f dry_run=%s equity=$%.0f",
        len(prepared), tier, tier_notional, dry_run, account_equity,
    )

    if dry_run:
        for po in prepared:
            _audit_log({"event": "dry_run_json", **po.to_row()})
        return prepared

    submitted: list[PreparedOrder] = []
    for po in prepared:
        try:
            if prefer_fractional and po.notional_usd:
                from alpaca.trading.requests import MarketOrderRequest
                req = MarketOrderRequest(
                    symbol=po.symbol,
                    notional=float(po.notional_usd),
                    side="buy" if po.side == "buy" else "sell",
                    time_in_force="day",
                    client_order_id=po.client_order_id,
                )
                order = client.submit_order(order_data=req)
                po.order_id = str(getattr(order, "id", "") or "")
                po.status = str(getattr(order, "status", "") or "")
                _audit_log({"event": "submitted_notional", **po.to_row()})
                submitted.append(po)
            else:
                result = submit_paper_order(
                    po.symbol, po.qty, po.side,
                    order_type=po.order_type,
                    time_in_force=po.time_in_force,
                    client_order_id=po.client_order_id,
                    dry_run=False,
                    client=client,
                )
                result.system = po.system
                result.entry_date = po.entry_date
                result.notional_usd = po.notional_usd
                result.tier = po.tier
                result.dry_run = False
                submitted.append(result)
        except Exception as exc:
            po.error = str(exc)
            _audit_log({"event": "submit_error_json", **po.to_row()})
            logger.warning("submit 失敗 %s: %s", po.symbol, exc)
            submitted.append(po)
            continue
    return submitted


# =========================================================================
# Exit wiring (Phase 2-3, 2026-07-03)
# =========================================================================
# subscriber サービスイン基準: 「S1〜S7 の entry と exit が Alpaca で自動運用できる」。
# entry step (signals_json_to_orders + paper_trading_submit) は市場成行のみを発注する。
# ここに追加する exit layer は、現 positions を Alpaca から pull し:
#   (a) Alpaca 側の protection 発注 (stop / trailing_stop / take_profit) が未登録なら発注
#   (b) Python 側 time-based / breakout exit の判定 → 成行 close order 生成
#   (c) dry_run default、AutoSubmitPaper flag が入ったときのみ実発注
# を担当する。SYSTEM_TRADE_RULES (common/trade_management.py) は本 module では 1 度しか
# 参照せず、rule 変更は spec 側に閉じる。
# =========================================================================

from common.trade_management import SYSTEM_TRADE_RULES  # noqa: E402

# S1/S4 の trailing stop、S2/S3/S5/S6 の stop+target、S7 の stop-only を
# entry と対で発注するときの client_order_id 命名規則
_PROTECT_STOP_SUFFIX = "protect-stop"
_PROTECT_TRAIL_SUFFIX = "protect-trail"
_PROTECT_TARGET_SUFFIX = "protect-target"
_EXIT_TIME_SUFFIX = "exit-time"
_EXIT_BREAKOUT_SUFFIX = "exit-breakout"


class ExitReasonCode:
    """paper_exit_check が生成する exit order の reason enum (string)."""

    TIME = "time_based"
    BREAKOUT = "spy_breakout"
    PROTECT_STOP = "protect_stop"
    PROTECT_TRAIL = "protect_trailing"
    PROTECT_TARGET = "protect_target"


@dataclass(slots=True)
class PositionSnapshot:
    """paper 口座から取得した 1 position の scrub 済みビュー。

    Alpaca Position を直接持ち回すと SDK API 変更に弱いので、必要最小の
    フィールドだけ切り出す。system tag は client_order_id or position_tracker
    から派生させる (別関数 responsibility)。
    """

    symbol: str
    qty: float  # long なら +、short なら - (Alpaca は 常に float 文字列)
    side: str  # "long" or "short"
    avg_entry_price: float
    market_value: float | None = None
    unrealized_pl: float | None = None
    system: str | None = None
    entry_date: str | None = None  # ISO date "YYYY-MM-DD"

    @property
    def abs_qty(self) -> int:
        return int(abs(self.qty))


@dataclass(slots=True)
class PreparedExit:
    """exit_check step が生成する 1 exit order 案。

    dry_run/submit を切り替えても schema が変わらないよう、to_row() で JSON に落ちる。
    """

    symbol: str
    system: str
    qty: int
    side: str  # "buy" (short cover) or "sell" (long close)
    order_type: str  # "market" / "stop" / "trailing_stop" / "limit"
    reason: str  # ExitReasonCode.*
    entry_date: str | None = None
    limit_price: float | None = None
    stop_price: float | None = None
    trail_percent: float | None = None
    holding_days: int | None = None
    max_holding_days: int | None = None
    client_order_id: str | None = None
    order_id: str | None = None
    status: str | None = None
    error: str | None = None
    dry_run: bool = True
    time_in_force: str = "day"

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


# -----------------------------------------------------------------------
# system tag parsing
# -----------------------------------------------------------------------


def parse_system_from_client_order_id(coid: str | None) -> str | None:
    """entry order の client_order_id ('system{N}-{SYM}-{YYYYMMDD}') から system tag を切り出す。

    parse できない場合は None。exit_check の primary path で使う。
    """
    if not coid:
        return None
    s = str(coid).strip().lower()
    # allow prefix like "exit-..." from re-submissions; skip if not a raw entry coid
    if s.startswith("exit-") or s.startswith("protect-"):
        return None
    head = s.split("-", 1)[0]
    if head.startswith("system") and head[6:].isdigit():
        return head
    return None


def parse_entry_date_from_client_order_id(coid: str | None) -> str | None:
    """entry order の client_order_id から YYYYMMDD → 'YYYY-MM-DD' を抽出。

    形式は '{system}-{SYM}-{YYYYMMDD}'。parse 失敗時は None。
    """
    if not coid:
        return None
    parts = str(coid).strip().split("-")
    if len(parts) < 3:
        return None
    tail = parts[-1]
    if len(tail) == 8 and tail.isdigit():
        return f"{tail[0:4]}-{tail[4:6]}-{tail[6:8]}"
    return None


def _snapshot_from_alpaca_position(p: Any) -> PositionSnapshot | None:
    """Alpaca Position obj → PositionSnapshot。tolerant parser."""
    try:
        sym = str(getattr(p, "symbol", "") or "").upper()
        if not sym:
            return None
        qty = float(getattr(p, "qty", 0) or 0)
        side_raw = str(getattr(p, "side", "") or "").lower()
        if side_raw in ("long", "short"):
            side = side_raw
        else:
            side = "long" if qty >= 0 else "short"
        avg = float(getattr(p, "avg_entry_price", 0) or 0)
        mv_raw = getattr(p, "market_value", None)
        upl_raw = getattr(p, "unrealized_pl", None)
        try:
            mv = float(mv_raw) if mv_raw is not None else None
        except (TypeError, ValueError):
            mv = None
        try:
            upl = float(upl_raw) if upl_raw is not None else None
        except (TypeError, ValueError):
            upl = None
        return PositionSnapshot(
            symbol=sym,
            qty=qty,
            side=side,
            avg_entry_price=avg,
            market_value=mv,
            unrealized_pl=upl,
        )
    except Exception as exc:  # pragma: no cover - defensive
        logger.warning("position snapshot parse 失敗: %s", exc)
        return None


def fetch_position_snapshots(client: Any) -> list[PositionSnapshot]:
    """Alpaca client から現 positions を取得し、PositionSnapshot list を返す。"""
    out: list[PositionSnapshot] = []
    try:
        raw = client.get_all_positions()
    except Exception as exc:  # pragma: no cover
        logger.warning("get_all_positions 失敗: %s", exc)
        return out
    for p in raw or []:
        snap = _snapshot_from_alpaca_position(p)
        if snap is not None:
            out.append(snap)
    return out


def hydrate_system_tags(
    snapshots: list[PositionSnapshot],
    *,
    tracker: dict[str, Any] | None = None,
    entry_orders_index: dict[str, dict[str, Any]] | None = None,
) -> list[PositionSnapshot]:
    """system / entry_date を tracker or entry order index から埋める。

    優先順位:
      1. entry_orders_index[symbol] = {"system": ..., "entry_date": ...}
         (paper_orders_*.json や fetch_entry_orders から)
      2. tracker[symbol] = {"system": ..., "entry_date": ...}
         (data/position_tracker.json、common/position_tracker.py 由来)

    どちらも無い symbol は system=None のまま返す (exit_check 側で skip される)。
    """
    idx = entry_orders_index or {}
    tr = tracker or {}
    for snap in snapshots:
        info = idx.get(snap.symbol) or tr.get(snap.symbol)
        if not isinstance(info, dict):
            continue
        sys_tag = info.get("system")
        if sys_tag and not snap.system:
            snap.system = str(sys_tag).lower()
        ed = info.get("entry_date")
        if ed and not snap.entry_date:
            # accept both ISO 'YYYY-MM-DD' or 'YYYY-MM-DDT...'
            snap.entry_date = str(ed)[:10]
    return snapshots


# -----------------------------------------------------------------------
# holding days
# -----------------------------------------------------------------------


def compute_holding_days(entry_date: str | None, today: str | None = None) -> int | None:
    """entry_date (ISO 'YYYY-MM-DD') と today から holding days を計算。

    parse 失敗時は None。
    """
    if not entry_date:
        return None
    try:
        d0 = datetime.fromisoformat(str(entry_date)[:10])
        if today is None:
            d1 = datetime.now(timezone.utc).date()
            d1 = datetime(d1.year, d1.month, d1.day)
        else:
            d1 = datetime.fromisoformat(str(today)[:10])
        return int((d1 - d0).days)
    except Exception:
        return None


# -----------------------------------------------------------------------
# exit order plan builders (per system, pure functions)
# -----------------------------------------------------------------------


def _build_time_exit(
    snap: PositionSnapshot,
    rules: Any,
    today: str,
    holding_days: int,
) -> PreparedExit | None:
    if rules is None or getattr(rules, "max_holding_days", 0) <= 0:
        return None
    if holding_days < int(rules.max_holding_days):
        return None
    close_side = "sell" if snap.side == "long" else "buy"
    date_compact = today.replace("-", "")
    coid = f"exit-{snap.system}-{snap.symbol}-{date_compact}-{_EXIT_TIME_SUFFIX}"
    return PreparedExit(
        symbol=snap.symbol,
        system=snap.system or "unknown",
        qty=snap.abs_qty,
        side=close_side,
        order_type="market",
        reason=ExitReasonCode.TIME,
        entry_date=snap.entry_date,
        holding_days=holding_days,
        max_holding_days=int(rules.max_holding_days),
        client_order_id=coid,
        dry_run=True,
    )


def _build_spy_breakout_exit(
    snap: PositionSnapshot,
    today: str,
    *,
    spy_high: float | None,
    spy_max70: float | None,
) -> PreparedExit | None:
    """system7 (SPY hedge) の 70日高値 breakout exit。

    spy_high が spy_max70 以上なら翌寄成行 close を提案。SPY データが無い場合は
    None (exit skip、safety fallback = 何もしない)。
    """
    if snap.symbol.upper() != "SPY":
        return None
    if spy_high is None or spy_max70 is None:
        return None
    if float(spy_high) < float(spy_max70):
        return None
    close_side = "sell" if snap.side == "long" else "buy"
    date_compact = today.replace("-", "")
    coid = f"exit-{snap.system}-{snap.symbol}-{date_compact}-{_EXIT_BREAKOUT_SUFFIX}"
    return PreparedExit(
        symbol=snap.symbol,
        system=snap.system or "system7",
        qty=snap.abs_qty,
        side=close_side,
        order_type="market",
        reason=ExitReasonCode.BREAKOUT,
        entry_date=snap.entry_date,
        client_order_id=coid,
        dry_run=True,
    )


def _build_protection_orders(
    snap: PositionSnapshot,
    rules: Any,
    *,
    atr_value: float | None,
    existing_protect_coids: set[str],
) -> list[PreparedExit]:
    """S1/S4 の trailing、S1〜S6 の stop-loss、S2/S3/S5/S6/S7 の take_profit の
    protection order を Alpaca に対して発注する提案を返す。

    既に同 client_order_id で発注済 (existing_protect_coids に含まれる) なら skip。
    """
    if rules is None or snap.system is None:
        return []
    proposals: list[PreparedExit] = []
    close_side = "sell" if snap.side == "long" else "buy"
    entry_date_compact = (snap.entry_date or "").replace("-", "")

    # trailing stop (S1: 25%, S4: 20%)
    if getattr(rules, "use_trailing_stop", False) and rules.trailing_stop_pct > 0:
        coid = (
            f"protect-{snap.system}-{snap.symbol}-{entry_date_compact}-"
            f"{_PROTECT_TRAIL_SUFFIX}"
        )
        if coid not in existing_protect_coids:
            proposals.append(
                PreparedExit(
                    symbol=snap.symbol,
                    system=snap.system,
                    qty=snap.abs_qty,
                    side=close_side,
                    order_type="trailing_stop",
                    reason=ExitReasonCode.PROTECT_TRAIL,
                    entry_date=snap.entry_date,
                    trail_percent=float(rules.trailing_stop_pct) * 100.0,
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="gtc",
                )
            )

    # stop-loss (全 system): ATR ベース。ATR 値が無いと計算できないので skip。
    if atr_value is not None and atr_value > 0:
        stop_dist = float(atr_value) * float(rules.stop_atr_multiplier)
        if snap.side == "long":
            stop_price = max(0.01, snap.avg_entry_price - stop_dist)
        else:
            stop_price = snap.avg_entry_price + stop_dist
        coid = (
            f"protect-{snap.system}-{snap.symbol}-{entry_date_compact}-"
            f"{_PROTECT_STOP_SUFFIX}"
        )
        if coid not in existing_protect_coids:
            proposals.append(
                PreparedExit(
                    symbol=snap.symbol,
                    system=snap.system,
                    qty=snap.abs_qty,
                    side=close_side,
                    order_type="stop",
                    reason=ExitReasonCode.PROTECT_STOP,
                    entry_date=snap.entry_date,
                    stop_price=round(stop_price, 4),
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="gtc",
                )
            )

    # profit target (S2/S3/S6 = %, S5 = ATR)
    target_price: float | None = None
    ttype = getattr(rules, "profit_target_type", "none")
    if ttype == "percentage" and rules.profit_target_value > 0:
        mult = 1.0 + (float(rules.profit_target_value) / 100.0)
        if snap.side == "long":
            target_price = snap.avg_entry_price * mult
        else:
            target_price = snap.avg_entry_price / mult
    elif ttype == "atr" and atr_value is not None and atr_value > 0:
        dist = float(atr_value) * float(rules.profit_target_value)
        if snap.side == "long":
            target_price = snap.avg_entry_price + dist
        else:
            target_price = snap.avg_entry_price - dist
    if target_price is not None and target_price > 0:
        coid = (
            f"protect-{snap.system}-{snap.symbol}-{entry_date_compact}-"
            f"{_PROTECT_TARGET_SUFFIX}"
        )
        if coid not in existing_protect_coids:
            proposals.append(
                PreparedExit(
                    symbol=snap.symbol,
                    system=snap.system,
                    qty=snap.abs_qty,
                    side=close_side,
                    order_type="limit",
                    reason=ExitReasonCode.PROTECT_TARGET,
                    entry_date=snap.entry_date,
                    limit_price=round(target_price, 4),
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="gtc",
                )
            )

    return proposals


# -----------------------------------------------------------------------
# top-level: build all exit proposals from positions
# -----------------------------------------------------------------------


def build_exit_orders_from_positions(
    snapshots: list[PositionSnapshot],
    *,
    today: str,
    tracker: dict[str, Any] | None = None,
    entry_orders_index: dict[str, dict[str, Any]] | None = None,
    existing_protect_coids: set[str] | None = None,
    spy_high: float | None = None,
    spy_max70: float | None = None,
    atr_by_symbol: dict[str, dict[int, float]] | None = None,
) -> list[PreparedExit]:
    """position snapshots から exit 発注案を build する pure function。

    - time-based (S2/S3/S5/S6): holding_days >= max_holding_days なら 成行 close
    - SPY breakout (S7): spy_high >= spy_max70 なら 翌寄成行 close
    - protection: 未発注 (existing_protect_coids に無い) なら trailing/stop/target を発注
    - S1/S4 は time-based 無いので protection のみ

    副作用なし。dry_run=True で返す。実発注 / dry_run flag は呼び出し側が差し替える。
    """
    hydrate_system_tags(
        snapshots,
        tracker=tracker,
        entry_orders_index=entry_orders_index,
    )
    existing_coids = existing_protect_coids or set()
    atr_lookup = atr_by_symbol or {}

    out: list[PreparedExit] = []
    for snap in snapshots:
        if snap.abs_qty <= 0:
            continue
        if not snap.system:
            logger.debug(
                "exit skip: %s system tag 不明 (tracker/entry_orders_index 未登録)",
                snap.symbol,
            )
            continue
        rules = SYSTEM_TRADE_RULES.get(snap.system)

        # (1) time-based / breakout の判定
        hd = compute_holding_days(snap.entry_date, today) or 0
        time_exit = _build_time_exit(snap, rules, today, hd) if rules else None
        breakout_exit: PreparedExit | None = None
        if snap.system == "system7":
            breakout_exit = _build_spy_breakout_exit(
                snap, today, spy_high=spy_high, spy_max70=spy_max70
            )

        # (2) protection の判定 (time/breakout が既に発火してる場合は不要)
        atr_value = None
        if rules is not None:
            per_atr = atr_lookup.get(snap.symbol, {})
            atr_value = per_atr.get(int(rules.stop_atr_period))
        protection: list[PreparedExit] = []
        if rules is not None and time_exit is None and breakout_exit is None:
            protection = _build_protection_orders(
                snap,
                rules,
                atr_value=atr_value,
                existing_protect_coids=existing_coids,
            )

        # (3) 優先順位: time/breakout の close order > protection 発注
        if time_exit is not None:
            out.append(time_exit)
        if breakout_exit is not None:
            out.append(breakout_exit)
        out.extend(protection)

    return out


# -----------------------------------------------------------------------
# submit exit order (dry_run default, paper enforce)
# -----------------------------------------------------------------------


def submit_paper_exit_order(
    po: PreparedExit,
    *,
    dry_run: bool = True,
    client: Any | None = None,
    retries: int = 2,
    backoff_seconds: float = 1.0,
    rate_limit_seconds: float = 0.35,
) -> PreparedExit:
    """1 件の PreparedExit を Alpaca Paper に発注する。dry_run=True で送信 skip。"""
    if po.qty <= 0:
        raise ValueError(f"exit qty は正の整数: {po.qty}")
    if po.side not in ("buy", "sell"):
        raise ValueError(f"exit side は 'buy'/'sell': {po.side!r}")

    po.dry_run = dry_run
    if dry_run:
        _audit_log({"event": "exit_dry_run", **po.to_row()})
        return po

    assert_paper_env()
    if client is None:
        client = ba.get_client(paper=True)

    try:
        order = ba.submit_order_with_retry(
            client,
            po.symbol,
            po.qty,
            side=po.side,
            order_type=po.order_type,
            limit_price=po.limit_price,
            stop_price=po.stop_price,
            trail_percent=po.trail_percent,
            time_in_force=po.time_in_force,
            client_order_id=po.client_order_id,
            retries=retries,
            backoff_seconds=backoff_seconds,
            rate_limit_seconds=rate_limit_seconds,
        )
    except Exception as exc:
        po.error = str(exc)
        _audit_log({"event": "exit_submit_error", **po.to_row()})
        raise _classify_error(exc) from exc

    po.order_id = str(getattr(order, "id", "") or "")
    po.status = str(getattr(order, "status", "") or "")
    _audit_log({"event": "exit_submitted", **po.to_row()})
    logger.info(
        "Paper exit submitted: %s %s x%d %s id=%s status=%s reason=%s",
        po.side, po.symbol, po.qty, po.order_type,
        po.order_id, po.status, po.reason,
    )
    return po


def submit_paper_exit_orders(
    exits: list[PreparedExit],
    *,
    dry_run: bool = True,
    client: Any | None = None,
) -> list[PreparedExit]:
    """複数 exit の submit convenience wrapper。dry_run default。"""
    if not exits:
        return []
    if not dry_run:
        assert_paper_env()
        if client is None:
            client = ba.get_client(paper=True)
    out: list[PreparedExit] = []
    for po in exits:
        try:
            result = submit_paper_exit_order(po, dry_run=dry_run, client=client)
        except OrderSubmitError as exc:
            po.error = str(exc)
            out.append(po)
            continue
        out.append(result)
    return out


# -----------------------------------------------------------------------
# helper: fetch open protection orders from Alpaca (dedup 用)
# -----------------------------------------------------------------------


def fetch_existing_protect_coids(client: Any) -> set[str]:
    """Alpaca の open orders から protection order の client_order_id を集める。

    再実行時の重複発注 (同一 symbol に stop/trail/target を毎日追加してしまう) を
    防ぐ。エラー時は空集合 (safe fallback = 発注を試みる)。
    """
    out: set[str] = set()
    try:
        orders = ba.get_open_orders(client)
    except Exception as exc:  # pragma: no cover
        logger.warning("open orders 取得失敗: %s", exc)
        return out
    for o in orders or []:
        try:
            coid = str(getattr(o, "client_order_id", "") or "")
            if coid.startswith("protect-"):
                out.add(coid)
        except Exception:
            continue
    return out


__all__ = [
    "PreparedOrder",
    "PreparedExit",
    "PositionSnapshot",
    "ExitReasonCode",
    "LiveAccountGuardError",
    "OrderSubmitError",
    "TIER_NOTIONAL_USD",
    "assert_paper_env",
    "resolve_tier_notional",
    "submit_paper_order",
    "signals_to_orders",
    "signals_json_to_orders",
    "parse_system_from_client_order_id",
    "parse_entry_date_from_client_order_id",
    "fetch_position_snapshots",
    "hydrate_system_tags",
    "compute_holding_days",
    "build_exit_orders_from_positions",
    "submit_paper_exit_order",
    "submit_paper_exit_orders",
    "fetch_existing_protect_coids",
]
