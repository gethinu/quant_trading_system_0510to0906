"""Alpaca **Paper** 自動売買のための高レベル発注レイヤ。

このモジュールは既存の低レベルプリミティブ (``common.broker_alpaca``) と
既存のアロケーションロジック (``core.final_allocation.finalize_allocation`` が
出力する ``final_df``) を **再利用** する薄いラッパーである。売買アルゴリズムや
ポジションサイジングを再実装するものではない。

提供する 2 つの公開 API:

``submit_paper_order``
    単一注文を Paper 口座へ送信する。``dry_run=True`` (デフォルト) では
    実発注せず :class:`PreparedOrder` を返すだけ。``client_order_id`` により
    冪等性 (同一シグナルの重複発注防止) を担保する。

``signals_to_orders``
    当日シグナル (``final_df`` 形式の DataFrame) を Alpaca 注文へ変換する。
    ``dry_run=True`` (デフォルト) では :class:`PreparedOrder` のリストのみ返す。

安全設計 (重要):
    - ``ALPACA_PAPER`` が真でない、または base URL が live を指す場合は
      :class:`LiveAccountGuardError` を送出して **live 口座への誤配信を防ぐ**。
    - ``dry_run`` がデフォルト True。実発注は明示的に ``dry_run=False`` を
      指定した場合のみ。
    - 送信内容は ``logs/alpaca_orders_YYYYMMDD.log`` に追記される (監査証跡)。
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

# システム別のデフォルト order type。common/alpaca_order.submit_orders_df と一致させる
# (system1/3/4/5: 寄り成行, system2/6/7: 指値)。
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


class LiveAccountGuardError(RuntimeError):
    """Paper 前提の処理が live 口座設定を検出した場合に送出される。"""


class OrderSubmitError(RuntimeError):
    """発注が明示的な理由 (資金不足/市場休場/無効シンボル 等) で失敗した場合。"""


@dataclass(slots=True)
class PreparedOrder:
    """送信予定 (または送信済) の注文を表す軽量 DTO。

    dry_run では送信せずこのオブジェクトのみ返す。実発注時は ``order_id`` /
    ``status`` が Alpaca レスポンスで埋められる。
    """

    symbol: str
    qty: int
    side: str  # "buy" | "sell"
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None
    time_in_force: str = "day"
    client_order_id: str | None = None
    system: str | None = None
    entry_date: str | None = None
    # 実発注後に埋まる
    order_id: str | None = None
    status: str | None = None
    error: str | None = None
    extra: dict[str, Any] = field(default_factory=dict)

    def to_row(self) -> dict[str, Any]:
        """print / DataFrame 表示向けの平坦な dict。"""
        d = asdict(self)
        d.pop("extra", None)
        return d


# ---------------------------------------------------------------------------
# 安全ガード
# ---------------------------------------------------------------------------
def _is_paper_env() -> bool:
    """``ALPACA_PAPER`` を真偽解釈する (デフォルト True)。"""
    return os.getenv("ALPACA_PAPER", "true").strip().lower() in (
        "1",
        "true",
        "yes",
        "y",
        "on",
    )


def assert_paper_env() -> None:
    """live 口座への誤発注を防ぐ fail-fast ガード。

    - ``ALPACA_PAPER`` が偽 → 例外
    - ``ALPACA_API_BASE_URL`` が paper ホスト以外を指す → 例外
    実発注経路 (``dry_run=False``) の直前でのみ呼ばれる。
    """
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


# ---------------------------------------------------------------------------
# 監査ログ
# ---------------------------------------------------------------------------
def _audit_log(record: dict[str, Any]) -> None:
    """発注内容を ``logs/alpaca_orders_YYYYMMDD.log`` に JSON 1 行で追記する。

    ログ書き込み失敗が発注フローを壊さないよう best-effort。
    """
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(timezone.utc)
        path = _LOG_DIR / f"alpaca_orders_{stamp:%Y%m%d}.log"
        record = {"ts": stamp.isoformat(), **record}
        with path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    except Exception as exc:  # pragma: no cover - ログ失敗は無視
        logger.warning("監査ログ書き込み失敗: %s", exc)


def _classify_error(exc: Exception) -> OrderSubmitError:
    """Alpaca 例外メッセージから明示的な原因に分類する。"""
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


# ---------------------------------------------------------------------------
# 公開 API 1: 単一注文
# ---------------------------------------------------------------------------
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
    """Paper 口座へ単一注文を送信する。

    ``dry_run=True`` (デフォルト) では送信せず :class:`PreparedOrder` のみ返す。

    Parameters
    ----------
    symbol, qty, side
        ティッカー / 株数 / "buy"|"sell"。
    order_type
        "market" | "limit"。
    limit_price
        limit 注文時に必須。
    time_in_force
        "day" | "gtc" | "cls" 等 (大文字小文字問わず)。
    client_order_id
        冪等キー。同一値の再送は Alpaca 側で拒否され重複発注を防ぐ。
    dry_run
        True で実発注しない (デフォルト)。
    client
        既存の TradingClient を注入 (未指定なら paper client を生成)。

    Raises
    ------
    LiveAccountGuardError
        ``dry_run=False`` かつ live 環境を検出した場合。
    OrderSubmitError
        資金不足 / 市場休場 / 無効シンボル等で発注が失敗した場合。
    """
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

    # ---- ここから実発注 (dry_run=False) ----
    assert_paper_env()  # live 口座 fail-fast

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
    except Exception as exc:  # noqa: BLE001
        prepared.error = str(exc)
        _audit_log({"event": "submit_error", **prepared.to_row()})
        raise _classify_error(exc) from exc

    prepared.order_id = str(getattr(order, "id", "") or "")
    prepared.status = str(getattr(order, "status", "") or "")
    _audit_log({"event": "submitted", **prepared.to_row()})
    logger.info(
        "Paper order submitted: %s %s x%d id=%s status=%s",
        prepared.side,
        prepared.symbol,
        prepared.qty,
        prepared.order_id,
        prepared.status,
    )
    return prepared


# ---------------------------------------------------------------------------
# 公開 API 2: シグナル → 注文変換
# ---------------------------------------------------------------------------
def _side_from_row(row: pd.Series) -> str:
    """final_df の side/position 列から buy/sell を決定する。

    ``submit_orders_df`` と同じ規約: side=="long" → buy, それ以外 → sell。
    """
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
    """(symbol, system, entry_date) から決定論的な冪等キーを生成する。

    同一シグナルを再実行しても同じ id になり、Alpaca 側で重複発注が拒否される。
    """
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
    """当日シグナル (``final_df`` 形式) を Alpaca 注文へ変換する。

    ポジションサイジングは行わない — ``shares`` 列 (既存 ``finalize_allocation``
    が算出したアロケーション) をそのまま数量として使用する。

    Parameters
    ----------
    signals
        少なくとも ``symbol``, ``system``, ``side``, ``shares`` を含む DataFrame。
        ``entry_price`` があれば limit 注文の limit_price に使う。
    account_equity
        参考情報 (ログ用)。数量算出は shares 列に委譲するため計算には使わない。
    dry_run
        True (デフォルト) では :class:`PreparedOrder` のリストのみ返す。
        False では各注文を Paper 口座へ送信する。
    open_positions
        ``{symbol: signed_qty}`` の既存ポジション。重複買い/売りの抑制に使う。
        未指定かつ非 dry_run 時は Alpaca から取得する。

    Returns
    -------
    list[PreparedOrder]
    """
    if signals is None or signals.empty:
        return []
    if "shares" not in signals.columns:
        logger.warning("signals に shares 列がありません (資金配分モードで生成してください)。")
        return []

    # 実発注時は open positions を取得して重複抑制
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

        # 重複シグナル (symbol, system, entry_date) を除去
        dedup_key = (sym, system, str(entry_date))
        if dedup_key in seen:
            continue
        seen.add(dedup_key)

        side = _side_from_row(row)

        # 既存ポジションとの照合: 同方向を既に十分保有していればスキップ
        held = open_positions.get(sym, 0.0)
        if side == "buy" and held > 0:
            logger.info("%s: 既にロング保有 (%.0f株) のため買い増しスキップ", sym, held)
            continue
        if side == "sell" and held < 0:
            logger.info("%s: 既にショート保有 (%.0f株) のため売り増しスキップ", sym, held)
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
                ot = "market"  # limit 価格が無ければ成行にフォールバック

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
        len(prepared),
        account_equity,
        dry_run,
    )

    if dry_run:
        for po in prepared:
            _audit_log({"event": "dry_run", **po.to_row()})
        return prepared

    # 実発注 (client は上で確定済み)
    submitted: list[PreparedOrder] = []
    for po in prepared:
        result = submit_paper_order(
            po.symbol,
            po.qty,
            po.side,
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
    """Alpaca から ``{symbol: signed_qty}`` を取得する (ロング+/ショート-)。"""
    out: dict[str, float] = {}
    try:
        positions = client.get_all_positions()
    except Exception as exc:  # pragma: no cover - ネットワーク例外
        logger.warning("open positions 取得失敗 (重複抑制なしで続行): %s", exc)
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


__all__ = [
    "PreparedOrder",
    "LiveAccountGuardError",
    "OrderSubmitError",
    "assert_paper_env",
    "submit_paper_order",
    "signals_to_orders",
]
