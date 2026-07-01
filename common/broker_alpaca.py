from __future__ import annotations

from collections.abc import Iterable
from concurrent.futures import ThreadPoolExecutor
import os
import time
from typing import Any

from dotenv import load_dotenv

try:  # pragma: no cover - SDK 未導入環境でも壊れないように
    from alpaca.trading.client import TradingClient

    try:
        from alpaca.trading.enums import (
            OrderClass,
            OrderSide,
            QueryOrderStatus,
            TimeInForce,
        )
    except ImportError:
        from alpaca.trading.models.enums import (
            OrderClass,
            OrderSide,
            QueryOrderStatus,
            TimeInForce,
        )
    from alpaca.trading.requests import (
        GetOrdersRequest,
        LimitOrderRequest,
        MarketOrderRequest,
        StopLossRequest,
        TakeProfitRequest,
        TrailingStopOrderRequest,
    )
    from alpaca.trading.stream import TradingStream
except Exception:  # pragma: no cover
    TradingClient = None
    OrderSide = OrderClass = TimeInForce = QueryOrderStatus = None
    MarketOrderRequest = None
    LimitOrderRequest = None
    GetOrdersRequest = None
    TakeProfitRequest = None
    StopLossRequest = None
    TrailingStopOrderRequest = None
    TradingStream = None


def _require_sdk() -> None:
    if TradingClient is None:
        raise RuntimeError(
            "alpaca-py がインストールされていません。requirements に追加/インストールしてください。"
        )


def _load_env_once() -> None:
    # 設定側で読み込み済みでも harm はない
    load_dotenv(dotenv_path=os.path.join(os.getcwd(), ".env"), override=False)


def get_client(
    *,
    paper: bool | None = None,
    api_key: str | None = None,
    secret_key: str | None = None,
):
    """TradingClient を生成して返す。

    - paper: None の場合は `ALPACA_PAPER` を '1/true/yes/on' として解釈（デフォルト True）
    - api_key/secret_key: 未指定なら .env から `APCA_API_KEY_ID`/`APCA_API_SECRET_KEY`
    """
    _require_sdk()
    _load_env_once()

    if paper is None:
        paper = os.getenv("ALPACA_PAPER", "true").lower() in (
            "1",
            "true",
            "yes",
            "y",
            "on",
        )
    api_key = api_key or os.getenv("APCA_API_KEY_ID")
    secret_key = secret_key or os.getenv("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        raise RuntimeError(
            "APCA_API_KEY_ID/APCA_API_SECRET_KEY が .env に設定されていません。",
        )

    return TradingClient(  # type: ignore[misc]
        api_key,
        secret_key,
        paper=bool(paper),
    )


def submit_order(
    client,
    symbol: str,
    qty: int,
    *,
    side: str = "buy",
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_percent: float | None = None,
    time_in_force: str = "GTC",
    client_order_id: str | None = None,
    log_callback=None,
):
    """注文を送信する共通関数。

    order_type: "market" | "limit" | "oco" | "trailing_stop"

    client_order_id: 指定すると Alpaca 側の冪等キーとして送信される。
        同一 client_order_id の再送は 422 (duplicate) となり、重複発注を防げる。
    """
    _require_sdk()

    if OrderSide is None:
        raise RuntimeError("Alpaca SDK is not installed or OrderSide is unavailable.")
    side_enum = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
    if TimeInForce is None:
        raise RuntimeError("Alpaca SDK is not installed or TimeInForce is unavailable.")
    tif = (
        getattr(TimeInForce, time_in_force.upper())
        if hasattr(TimeInForce, time_in_force.upper())
        else TimeInForce.GTC
    )

    # client_order_id は全 request 種別で共通の冪等キー。None の場合は付与しない。
    _coid: dict[str, Any] = (
        {"client_order_id": client_order_id} if client_order_id else {}
    )

    if order_type == "market":
        req = MarketOrderRequest(  # type: ignore[call-arg]
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=tif,
            **_coid,
        )
    elif order_type == "limit":
        if limit_price is None:
            raise ValueError("limit_price が必要です。")
        req = LimitOrderRequest(  # type: ignore[call-arg]
            symbol=symbol,
            qty=qty,
            side=side_enum,
            limit_price=limit_price,
            time_in_force=tif,
            **_coid,
        )
    elif order_type == "oco":
        if take_profit is None or stop_loss is None:
            raise ValueError("take_profit と stop_loss が必要です。")
        req = LimitOrderRequest(  # type: ignore[call-arg]
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=tif,
            order_class=OrderClass.OCO,  # type: ignore[attr-defined]
            take_profit=TakeProfitRequest(
                limit_price=take_profit,
            ),  # type: ignore[call-arg]
            stop_loss=StopLossRequest(
                stop_price=stop_loss,
            ),  # type: ignore[call-arg]
        )
    elif order_type == "trailing_stop":
        if trail_percent is None and stop_price is None:
            raise ValueError("trail_percent か trail_price のいずれかが必要です。")
        req = TrailingStopOrderRequest(  # type: ignore[call-arg]
            symbol=symbol,
            qty=qty,
            side=side_enum,
            time_in_force=tif,
            trail_percent=trail_percent,
            trail_price=stop_price,
        )
    else:
        raise ValueError(f"未知の order_type: {order_type}")

    order = client.submit_order(order_data=req)
    if log_callback:
        try:
            msg = f"Submitted {order_type} order {order.id} {symbol} qty={qty} side={side_enum.name}"
            log_callback(msg)
        except Exception:
            pass
    return order


def submit_order_with_retry(
    client,
    symbol: str,
    qty: int,
    *,
    side: str = "buy",
    order_type: str = "market",
    limit_price: float | None = None,
    stop_price: float | None = None,
    take_profit: float | None = None,
    stop_loss: float | None = None,
    trail_percent: float | None = None,
    time_in_force: str = "GTC",
    client_order_id: str | None = None,
    retries: int = 2,
    backoff_seconds: float = 1.0,
    rate_limit_seconds: float = 0.0,
    log_callback=None,
):
    """submit_order をリトライ付きで実行。
    - retries: 失敗時の再試行回数
    - backoff_seconds: 失敗毎に待機する秒数（指数ではなく線形）
    - rate_limit_seconds: 成功/失敗に関わらず各試行後に待機
    """
    last_exc: Exception | None = None
    for i in range(retries + 1):
        try:
            order = submit_order(
                client,
                symbol,
                qty,
                side=side,
                order_type=order_type,
                limit_price=limit_price,
                stop_price=stop_price,
                take_profit=take_profit,
                stop_loss=stop_loss,
                trail_percent=trail_percent,
                time_in_force=time_in_force,
                client_order_id=client_order_id,
                log_callback=log_callback,
            )
            if rate_limit_seconds > 0:
                time.sleep(rate_limit_seconds)
            return order
        except Exception as e:  # pragma: no cover - ネットワーク/SDK例外
            last_exc = e
            if log_callback:
                try:
                    msg = " ".join(
                        [
                            f"submit retry {i + 1}/{retries}: {symbol}",
                            f"qty={qty} error={e}",
                        ]
                    )
                    log_callback(msg)
                except Exception:
                    pass
            if i < retries:
                time.sleep(max(0.0, backoff_seconds))
            if rate_limit_seconds > 0:
                time.sleep(rate_limit_seconds)
    assert last_exc is not None
    raise last_exc


def get_orders_status_map(client, order_ids: Iterable[str]) -> dict[str, Any]:
    """order_id -> status の簡易マップを返す."""
    id_set = {oid for oid in order_ids if oid}
    out: dict[str, Any] = {}
    if not id_set:
        return out
    for oid in id_set:
        try:
            o = client.get_order_by_id(oid)
            out[oid] = getattr(o, "status", None)
        except Exception:
            out[oid] = None
    return out


def log_orders_positions(client) -> tuple[Any, Any]:
    """現在の注文とポジションを同時に取得する."""
    if GetOrdersRequest is None or QueryOrderStatus is None:
        raise RuntimeError("alpaca-py がインストールされていません。")

    # 並列取得で待ち時間を短縮
    with ThreadPoolExecutor(max_workers=2) as executor:
        orders_future = executor.submit(
            client.get_orders, GetOrdersRequest(status=QueryOrderStatus.ALL)
        )
        positions_future = executor.submit(client.get_all_positions)
        orders = orders_future.result()
        positions = positions_future.result()
    return orders, positions


def get_open_orders(client) -> Any:
    """未約定注文を取得する."""
    if GetOrdersRequest is None or QueryOrderStatus is None:
        raise RuntimeError("alpaca-py がインストールされていません。")

    return client.get_orders(GetOrdersRequest(status=QueryOrderStatus.OPEN))


def cancel_all_orders(client) -> None:
    """すべての未約定注文をキャンセルする."""
    try:
        client.cancel_orders()
    except Exception:
        # SDK のバージョン差異に対応
        client.cancel_all_orders()


def subscribe_order_updates(client, log_callback=None):
    """注文更新の WebSocket を購読して即時実行（ブロッキング）。"""
    _require_sdk()
    if TradingStream is None:
        raise RuntimeError("alpaca-py がインストールされていません。")

    stream = TradingStream(client.api_key, client.secret_key, paper=client.paper)  # type: ignore[attr-defined]
    stream_any: Any = stream  # type: ignore

    @stream_any.on_order_update
    async def _(data):  # noqa: ANN001 - SDK 固有
        if log_callback:
            try:
                msg = " ".join(
                    [
                        f"WS update {data.event} id={data.order.id}",
                        f"status={data.order.status}",
                    ]
                )
                log_callback(msg)
            except Exception:
                pass

    stream.run()


__all__ = [
    "get_client",
    "submit_order",
    "log_orders_positions",
    "cancel_all_orders",
    "subscribe_order_updates",
]


def get_shortable_map(client, symbols: Iterable[str]) -> dict[str, bool]:
    """Return a map of symbol->shortable via Alpaca Assets API.

    - On any SDK/API error, returns empty map (caller should skip filtering).
    - Symbols are upper-cased for lookup consistency.
    """
    out: dict[str, bool] = {}
    try:
        syms = [str(s).upper() for s in symbols if str(s).strip()]
    except Exception:
        return out
    if not syms:
        return out
    for sym in syms:
        try:
            asset = client.get_asset(sym)
            shortable = bool(getattr(asset, "shortable", False))
            out[sym] = shortable
        except Exception:
            # On error per-symbol, leave it out (unknown)
            continue
    return out


# ----------------------------------------------------------------------------
# Paper cash reset (best-effort / unofficial)
# ----------------------------------------------------------------------------

from dataclasses import dataclass
import json
from typing import TypedDict

import requests


class PaperResetResult(TypedDict, total=False):
    ok: bool
    status_code: int
    error: str | None
    equity: float | None
    raw: dict[str, Any] | None


@dataclass(slots=True)
class PaperResetOptions:
    desired_equity: float | None = None  # 希望初期残高 (None なら API デフォルト)
    timeout: float = 10.0  # seconds
    dry_run: bool = False  # True なら実行せず検証のみ


def reset_paper_account(
    *,
    api_key: str | None = None,
    secret_key: str | None = None,
    desired_equity: float | None = None,
    dry_run: bool = False,
    timeout: float = 10.0,
    endpoint: str | None = None,
) -> PaperResetResult:
    """Alpaca Paper 口座をリセットするヘルパ。

    - Live キー / live base URL では動かない想定なので paper API 固定。
    - desired_equity を指定すると (サポートされている環境なら) 新しい初期残高を設定試行。
    - dry_run=True なら HTTP リクエストを送らず検証のみ。
    戻り値: 成功/失敗の簡易情報 (ok, status_code, equity 等)
    失敗しても例外を極力投げず呼び出し側で扱いやすい形にする。
    """
    _load_env_once()
    api_key = api_key or os.getenv("APCA_API_KEY_ID")
    secret_key = secret_key or os.getenv("APCA_API_SECRET_KEY")
    if not api_key or not secret_key:
        return PaperResetResult(
            ok=False,
            status_code=0,
            error="Missing API keys in params or .env",
            equity=None,
            raw=None,
        )

    base_url = "https://paper-api.alpaca.markets"
    # 環境変数で上書き可能 (テスト/将来変更検証用): ALPACA_PAPER_RESET_ENDPOINT
    ep_env = os.getenv("ALPACA_PAPER_RESET_ENDPOINT")
    endpoint = endpoint or ep_env or f"{base_url}/v2/account/reset"

    headers = {
        "APCA-API-KEY-ID": api_key,
        "APCA-API-SECRET-KEY": secret_key,
        "Content-Type": "application/json",
    }
    payload: dict[str, Any] = {}
    if desired_equity is not None:
        # API は文字列形式を許容、少数はサポート幅不明なため丸め
        payload["equity"] = str(int(desired_equity))

    if dry_run:
        return PaperResetResult(
            ok=True,
            status_code=0,
            error=None,
            equity=desired_equity,
            raw={"dry_run": True, "endpoint": endpoint, "payload": payload},
        )

    try:
        resp = requests.post(
            endpoint,
            headers=headers,
            data=json.dumps(payload) if payload else None,
            timeout=timeout,
        )
    except Exception as e:  # pragma: no cover - ネットワーク例外
        return PaperResetResult(
            ok=False, status_code=0, error=f"network_error:{e}", equity=None, raw=None
        )

    raw_text: str | None = None
    try:
        data = resp.json() if resp.content else {}
    except Exception:
        raw_text = resp.text[:500] if resp.text else None
        data = {}

    ok = 200 <= resp.status_code < 300
    # 成功時 data から新 equity を拾える場合だけ抽出
    new_equity: float | None = None
    for key in ("cash", "equity", "portfolio_value"):
        try:
            if key in data and data[key] is not None:
                new_equity = float(data[key])
                break
        except Exception:
            continue

    # 403/404 の典型原因ヒント
    error_msg: str | None = None
    if not ok:
        if resp.status_code in (403, 404):
            # 404 の場合: 仕様変更 / エンドポイント無効化 / 誤 URL / リージョン差異
            hint = "endpoint still enabled? correct paper key?"
            if resp.status_code == 404:
                hint += (
                    " (Possibly removed by Alpaca; check latest docs or dashboard UI)"
                )
            error_msg = f"reset failed status={resp.status_code} ({hint})"
        elif resp.status_code == 422:
            error_msg = "invalid equity value format"
        else:
            error_msg = f"reset failed status={resp.status_code}"

    result = PaperResetResult(
        ok=ok,
        status_code=resp.status_code,
        error=error_msg,
        equity=new_equity,
        raw=data if isinstance(data, dict) else None,
    )
    if raw_text and not result.get("raw"):
        # type: ignore[index]
        result["raw"] = {"_non_json": raw_text}
    return result


__all__.append("reset_paper_account")
