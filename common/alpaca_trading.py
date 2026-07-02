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


def _side_from_row(row: pd.Series) -> str:
    raw = str(row.get("side", "")).lower()
    if raw in ("buy", "sell"):
        return raw
    return "buy" if raw == "long" else "sell"


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

        side = _side_from_row(row)
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


__all__ = [
    "PreparedOrder",
    "LiveAccountGuardError",
    "OrderSubmitError",
    "TIER_NOTIONAL_USD",
    "assert_paper_env",
    "resolve_tier_notional",
    "submit_paper_order",
    "signals_to_orders",
    "signals_json_to_orders",
]
