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
    qty: float  # 整数株なら int 相当、fractional なら小数 (参考値)
    side: str  # "buy" | "sell"
    order_type: str = "market"  # "market" | "limit"
    limit_price: float | None = None
    time_in_force: str = "day"
    client_order_id: str | None = None
    system: str | None = None
    entry_date: str | None = None
    # account_equity scale sizing (signals_json_to_orders) で埋まる項目
    notional: float | None = None  # dollar 建て発注額 (fractional 発注時に使用)
    fractional: bool = False  # 分数株 (notional) 発注か否か
    price: float | None = None  # 参照価格 (entry_price)
    weight: float | None = None  # 元シグナルの weight
    rank: int | None = None  # 元シグナルの rank
    reason: str | None = None  # 元シグナルの reason
    tier: str | None = None  # small | medium | large
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


@dataclass(slots=True)
class SkippedSignal:
    """sizing/フィルタで発注対象外になったシグナルとその理由。"""

    symbol: str
    reason: str
    system: str | None = None
    weight: float | None = None

    def to_row(self) -> dict[str, Any]:
        return asdict(self)


# tier 境界 (account_equity, USD)。resolve_tier / signals_json_to_orders で共有。
TIER_SMALL_MAX = 10_000.0
TIER_MEDIUM_MAX = 100_000.0
# large tier で SPY hedge (system7) の weight に掛ける係数 (hedge 強化)。
_LARGE_HEDGE_BOOST = 1.5
_HEDGE_SYSTEM = "system7"


def resolve_tier(account_equity: float, tier: str = "auto") -> str:
    """account_equity から運用 tier を決定する。

    - small  (< $10k):   top pick 集中、fractional 必須
    - medium ($10k–100k): 標準 weight、全 sys の signals
    - large  (>= $100k):  分散、hedge 強化 (SPY weight up)
    明示指定 (small|medium|large) はそのまま採用する。
    """
    t = (tier or "auto").strip().lower()
    if t in ("small", "medium", "large"):
        return t
    if account_equity < TIER_SMALL_MAX:
        return "small"
    if account_equity < TIER_MEDIUM_MAX:
        return "medium"
    return "large"


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
    notional: float | None = None,
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

    # notional (dollar 建て / fractional) 発注は market のみ、qty は無視される。
    use_notional = notional is not None
    if use_notional:
        if order_type != "market":
            raise ValueError("notional (fractional) 発注は market のみ対応です。")
        if float(notional) <= 0:
            raise ValueError(f"notional は正の値: {notional}")
        qty_val: float = float(qty or 0)
    else:
        qty_val = int(qty)
        if qty_val <= 0:
            raise ValueError(f"qty は正の整数: {qty}")

    prepared = PreparedOrder(
        symbol=symbol.upper(),
        qty=qty_val,
        side=side,
        order_type=order_type,
        limit_price=limit_price,
        time_in_force=time_in_force,
        client_order_id=client_order_id,
        notional=float(notional) if use_notional else None,
        fractional=use_notional,
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
            notional=prepared.notional,
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


# ---------------------------------------------------------------------------
# 公開 API 3: signals JSON (Phase 1 pack) → account_equity scale sizing
# ---------------------------------------------------------------------------
def _norm_system(sys_key: str) -> str:
    """"sys1" / "system1" を "system1" に正規化する。"""
    k = str(sys_key).strip().lower()
    if k.startswith("system"):
        return k
    if k.startswith("sys"):
        return "system" + k[3:]
    return k


def _side_from_json(raw: str) -> str:
    """signals JSON の "BUY"/"SELL" を Alpaca の "buy"/"sell" に変換。"""
    s = str(raw).strip().lower()
    return "buy" if s in ("buy", "long") else "sell"


def _json_client_order_id(system: str, symbol: str, date: str) -> str:
    """signals JSON 用の決定論的な冪等キー ``sysN_SYMBOL_YYYYMMDD``。"""
    compact = str(date).replace("-", "").replace(" ", "")[:8]
    short = system.replace("system", "sys")
    return f"{short}_{symbol.upper()}_{compact}" if compact else f"{short}_{symbol.upper()}"


def _fractionable(
    symbol: str,
    *,
    client: Any | None,
    cache: dict[str, bool],
    fractionable_map: dict[str, bool] | None,
    default: bool,
) -> bool:
    """銘柄が分数株 (fractional) 対応かを判定する。

    優先順: 明示 ``fractionable_map`` → キャッシュ → Alpaca ``get_asset`` → ``default``。
    API 呼び出し結果はキャッシュして重複コールを避ける。offline/dry-run で
    client が無い場合は ``default`` (通常 ``prefer_fractional``) を返す。
    """
    sym = symbol.upper()
    if fractionable_map is not None and sym in fractionable_map:
        return bool(fractionable_map[sym])
    if sym in cache:
        return cache[sym]
    if client is None:
        return default
    try:
        asset = client.get_asset(sym)
        val = bool(getattr(asset, "fractionable", default))
    except Exception as exc:  # pragma: no cover - ネットワーク例外
        logger.warning("%s: get_asset 失敗、fractionable=%s と仮定: %s", sym, default, exc)
        val = default
    cache[sym] = val
    return val


@dataclass(slots=True)
class OrderPlan:
    """signals_json_to_orders の結果コンテナ (発注対象 + skip + 集計)。"""

    date: str
    account_equity: float
    tier: str
    orders: list[PreparedOrder] = field(default_factory=list)
    skipped: list[SkippedSignal] = field(default_factory=list)

    def _order_notional(self, po: PreparedOrder) -> float:
        if po.notional is not None:
            return float(po.notional)
        if po.price is not None:
            return float(po.qty) * float(po.price)
        return 0.0

    def summary(self) -> dict[str, Any]:
        total = sum(self._order_notional(o) for o in self.orders)
        hedge = sum(
            self._order_notional(o)
            for o in self.orders
            if _norm_system(o.system or "") == _HEDGE_SYSTEM
        )
        return {
            "total_notional": round(total, 2),
            "n_orders": len(self.orders),
            "n_skipped": len(self.skipped),
            "hedge_notional": round(hedge, 2),
        }

    def to_preview_dict(self) -> dict[str, Any]:
        """Vercel dashboard / 突合用の preview JSON schema。"""
        return {
            "date": self.date,
            "account_equity": self.account_equity,
            "tier": self.tier,
            "orders": [
                {
                    "symbol": o.symbol,
                    "side": o.side,
                    "notional_usd": round(self._order_notional(o), 2),
                    "qty": round(float(o.qty), 6),
                    "fractional": o.fractional,
                    "order_type": o.order_type,
                    "system": o.system,
                    "weight": o.weight,
                    "rank": o.rank,
                    "client_order_id": o.client_order_id,
                }
                for o in self.orders
            ],
            "skipped": [s.to_row() for s in self.skipped],
            "summary": self.summary(),
        }


def signals_json_to_orders(
    signals_json: dict[str, Any],
    account_equity: float,
    tier: str = "auto",
    min_notional_usd: float = 5.0,
    prefer_fractional: bool = True,
    dry_run: bool = True,
    *,
    fractionable_map: dict[str, bool] | None = None,
    client: Any | None = None,
    time_in_force: str = "day",
) -> OrderPlan:
    """当日 signals JSON (Phase 1 pack) を account_equity scale で発注へ変換する。

    schema: ``{version, date, systems: {sysN: {signals: [...]}}, portfolio, ...}``

    tier logic (``auto`` 時、:func:`resolve_tier` で判定):
        - small  (< $10k):   各 sys の rank==1 のみ (top pick 集中)、fractional 必須
        - medium ($10k–100k): 標準 weight、全 signals
        - large  (>= $100k):  全 signals、SPY hedge (system7) weight を強化

    position sizing::

        target_notional = weight * account_equity
        (large tier の hedge は weight ×1.5)
        target_notional < min_notional_usd            -> skip
        fractional 対応 & prefer_fractional            -> notional 発注 (market)
        非対応 or prefer_fractional=False              -> whole share (round)、0株なら skip

    ``dry_run`` はここでは常に発注しない (変換のみ)。実発注は
    :func:`submit_paper_order` / ``paper_trading_submit.py`` 側で行う。

    Returns
    -------
    OrderPlan
        ``orders`` (PreparedOrder) + ``skipped`` (SkippedSignal) + ``summary``。
    """
    resolved_tier = resolve_tier(account_equity, tier)
    date = str(signals_json.get("date", ""))
    systems = signals_json.get("systems", {}) or {}

    plan = OrderPlan(date=date, account_equity=float(account_equity), tier=resolved_tier)
    frac_cache: dict[str, bool] = {}
    seen: set[str] = set()

    for sys_key, sysdata in systems.items():
        system = _norm_system(sys_key)
        raw_signals = (sysdata or {}).get("signals", []) or []

        # small tier: 各 sys の top pick (rank==1) のみに集約
        if resolved_tier == "small":
            ranked = [s for s in raw_signals if (s.get("rank") or 99) <= 1]
            signals = ranked or (raw_signals[:1] if raw_signals else [])
        else:
            signals = raw_signals

        for sig in signals:
            sym = str(sig.get("symbol", "")).upper()
            if not sym:
                continue
            weight = sig.get("weight")
            price = sig.get("entry_price")
            side = _side_from_json(sig.get("side", ""))
            rank = sig.get("rank")
            reason = sig.get("reason")

            coid = _json_client_order_id(system, sym, date)
            if coid in seen:
                continue
            seen.add(coid)

            if weight in (None, "") or float(weight) <= 0:
                plan.skipped.append(SkippedSignal(sym, "weight 未設定/0", system, weight))
                continue

            eff_weight = float(weight)
            # large tier: SPY hedge (system7) を強化
            if resolved_tier == "large" and system == _HEDGE_SYSTEM:
                eff_weight *= _LARGE_HEDGE_BOOST

            target_notional = eff_weight * float(account_equity)
            if target_notional < min_notional_usd:
                plan.skipped.append(
                    SkippedSignal(
                        sym,
                        f"target_notional ${target_notional:.2f} < min ${min_notional_usd:.0f} ({resolved_tier})",
                        system,
                        weight,
                    )
                )
                continue

            if price in (None, "") or float(price) <= 0:
                plan.skipped.append(SkippedSignal(sym, "entry_price 未設定/0", system, weight))
                continue
            price_f = float(price)

            fractionable = _fractionable(
                sym,
                client=client,
                cache=frac_cache,
                fractionable_map=fractionable_map,
                default=prefer_fractional,
            )

            if prefer_fractional and fractionable:
                # dollar 建て (fractional) 発注: Alpaca fractional は market のみ
                po = PreparedOrder(
                    symbol=sym,
                    qty=round(target_notional / price_f, 6),
                    side=side,
                    order_type="market",
                    time_in_force=time_in_force,
                    client_order_id=coid,
                    system=system,
                    entry_date=date or None,
                    notional=round(target_notional, 2),
                    fractional=True,
                    price=price_f,
                    weight=float(weight),
                    rank=int(rank) if rank is not None else None,
                    reason=reason,
                    tier=resolved_tier,
                )
            else:
                whole = int(round(target_notional / price_f))
                if whole <= 0:
                    plan.skipped.append(
                        SkippedSignal(
                            sym,
                            f"whole-share 0 (notional ${target_notional:.2f} < 1株 ${price_f:.2f})",
                            system,
                            weight,
                        )
                    )
                    continue
                ot = _DEFAULT_SYSTEM_ORDER_TYPE.get(system, "market")
                po = PreparedOrder(
                    symbol=sym,
                    qty=whole,
                    side=side,
                    order_type=ot,
                    limit_price=price_f if ot == "limit" else None,
                    time_in_force=time_in_force,
                    client_order_id=coid,
                    system=system,
                    entry_date=date or None,
                    notional=None,
                    fractional=False,
                    price=price_f,
                    weight=float(weight),
                    rank=int(rank) if rank is not None else None,
                    reason=reason,
                    tier=resolved_tier,
                )
            plan.orders.append(po)

    logger.info(
        "signals_json_to_orders: tier=%s equity=$%.0f -> %d 注文 / %d skip",
        resolved_tier,
        account_equity,
        len(plan.orders),
        len(plan.skipped),
    )
    for po in plan.orders:
        _audit_log({"event": "dry_run_json", **po.to_row()})
    return plan


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
    "SkippedSignal",
    "OrderPlan",
    "LiveAccountGuardError",
    "OrderSubmitError",
    "assert_paper_env",
    "resolve_tier",
    "submit_paper_order",
    "signals_to_orders",
    "signals_json_to_orders",
]
