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

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from decimal import ROUND_HALF_UP, Decimal
import json
import logging
import math
import os
from pathlib import Path
from typing import Any

import pandas as pd

from common import broker_alpaca as ba
from common.symbol_map import resolve_primary_system

logger = logging.getLogger(__name__)

# NOTE (docs-alignment 2026-07-03):
# System 別のデフォルト注文タイプは docs/systems/システム{N}.txt の「仕掛け」節を
# single source of truth とする:
#   S1 = 翌日寄付成行 (MARKET)
#   S2 = 翌日 前日終値+4% 以上の指値売 (LIMIT)
#   S3 = 前日終値-7% 指値買 (LIMIT)
#   S4 = 寄付成行 (MARKET)
#   S5 = 前日終値-3% 指値買 (LIMIT)
#   S6 = 前日終値+5% 指値売 (LIMIT)
#   S7 = 翌日寄付成行 (MARKET)  ← SPY 固定 catastrophe hedge
# 従来 (2026-07-03 手前) の map は S3/S5/S7 で docs と乖離していた:
#   S3=market (docs=limit), S5=market (docs=limit), S7=limit (docs=market)。
# limit_price が row に無い場合の runtime fallback (`ot = "market"`) は現状維持
# なので、この修正で S3/S5 の指値なし fallback は市場価格発注に落ちる (誤発注防止)。
_DEFAULT_SYSTEM_ORDER_TYPE = {
    "system1": "market",
    "system2": "limit",
    "system3": "limit",
    "system4": "market",
    "system5": "limit",
    "system6": "limit",
    "system7": "market",
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


# -----------------------------------------------------------------------
# price tick rounding (Alpaca sub-penny guard)
# -----------------------------------------------------------------------
#
# Alpaca は US equity の limit / stop 価格に「最小刻み (tick)」制約を課す:
#   - price >= $1.00  ->  $0.01 刻み (小数2桁)
#   - price <  $1.00  ->  $0.0001 刻み (小数4桁)
# これに反する sub-penny 価格 (>=$1 なのに小数3桁以上) を native limit/stop で
# 発注すると ``sub-penny increment does not fulfill minimum pricing criteria``
# (code 42210000) で全拒否される。
#
# 従来の native protection order 生成は stop/target を一律 ``round(price, 4)``
# して発注していたため、>=$1 銘柄 (例: BABA 109.4519 / GSHD 52.0288) の
# stop/target が 4桁のまま code 42210000 で reject され resting 保護が置けなか
# った。native limit/stop 価格を Alpaca へ渡す前に必ず本ヘルパーを通すこと。
#
# NOTE: 端株 (fractional) の synthetic 保護 exit は order_type="market" であり
# stop_price/limit_price は audit 用フィールドに過ぎない (Alpaca は成行の価格を
# 無視する)。よって synthetic 側の丸めは本 tick 制約の対象外で、端株の挙動は
# 変えない (端株の native stop 不可対応は別管轄)。
_TICK_ABOVE_1 = Decimal("0.01")
_TICK_BELOW_1 = Decimal("0.0001")


def round_to_alpaca_tick(price: float | None) -> float | None:
    """native stop / limit 価格を Alpaca の最小 tick に丸める (nearest tick)。

    Alpaca tick ルール:
        price >= $1.00 -> $0.01   (小数2桁)
        price <  $1.00 -> $0.0001 (小数4桁)

    - ``ROUND_HALF_UP`` (最寄り tick) で丸めるので stop/target が意図した水準
      からずれない (最大でも 0.5 tick)。方向丸めはしない。
    - ``None`` / 非正 (<=0) の入力はそのまま返す (caller 側で下限を別途 guard 済)。
    """
    if price is None:
        return None
    p = float(price)
    if p <= 0:
        # long stop は max(0.01, ...) 等で caller が下限保証済。負値・0 は触らない。
        return p
    tick = _TICK_ABOVE_1 if p >= 1.0 else _TICK_BELOW_1
    return float(Decimal(str(p)).quantize(tick, rounding=ROUND_HALF_UP))


# =========================================================================
# Position sizing (2026-07-09): equity 連動 or 固定 tier
# =========================================================================
# 旧: notional_i = weight_i × tier_notional (tier="small"=$1k 固定, equity 非連動)。
# 新: deploy_budget = equity × equity_deploy_pct (既定 0.5)。notional_i = weight_i ×
# deploy_budget。risk cap (per-name max_pct / gross / net) は *予算の内側* で従来通り
# 縛る (equity_deploy_pct を上げても cap は緩まない)。件数 cap / max_positions スロット
# は上流 (core.final_allocation) で today_signals JSON 生成時に既に効いている。
# 詳細: docs/EQUITY_LINKED_SIZING_20260709.md。
SIZING_EQUITY_LINKED = "equity_linked"
SIZING_FIXED_TIER = "fixed_tier"
# deploy_budget = equity × これ。既定 0.5 (gross 目標 ≈ 0.5×equity)。0 以下/NaN の
# フォールバック値も兼ねる。config sizing.equity_deploy_pct が single source of truth。
DEFAULT_EQUITY_DEPLOY_PCT = 0.5


@dataclass(slots=True)
class NotionalPlan:
    """compute_position_notionals の結果 (pure, side-effect free)。

    notionals は入力 entries と同順・同数。合計は deploy_budget 以下 (cap 適用後)。
    caps は発火した cap と scale factor の観測性ログ (呼び出し側サマリ用)。
    """

    notionals: list[float]
    mode: str
    deploy_budget: float
    total_weight: float
    gross_after: float
    net_after: float
    long_after: float
    short_after: float
    caps: dict[str, Any] = field(default_factory=dict)


def _is_short_side(side: str) -> bool:
    return (side or "").strip().lower() in ("sell", "short", "sell_short")


def compute_position_notionals(
    entries: list[tuple[float, str]],
    *,
    mode: str,
    tier: str,
    equity: float,
    equity_deploy_pct: float = DEFAULT_EQUITY_DEPLOY_PCT,
    max_pct: float = 0.10,
    max_gross_exposure_pct: float = 1.0,
    max_net_exposure_pct: float = 0.5,
) -> NotionalPlan:
    """weight × 予算で per-signal notional を計算する pure 関数。

    Args:
        entries: ``[(weight, side), ...]`` side は "buy"/"sell"(/"short" 等)。順序保持。
        mode: ``SIZING_FIXED_TIER`` = 従来の tier 固定予算 (dollar cap 掛けない)。
              ``SIZING_EQUITY_LINKED`` = deploy_budget=equity×pct, cap を内側で適用。
        tier: fixed_tier 時の予算 (small/medium/large)。
        equity: 口座 equity (equity_linked の予算と cap の分母)。
        equity_deploy_pct: deploy_budget = equity × これ (equity_linked のみ)。
        max_pct: per-name 上限 (equity 比)。0 以下で無効。
        max_gross_exposure_pct: gross(long$+short$)/equity 上限。0 以下で無効。
        max_net_exposure_pct: |net|(|long$-short$|)/equity 上限。0 以下で無効。

    Returns:
        NotionalPlan。``notionals[i]`` は entries[i] に対応する目標 notional(USD, 2dp)。
    """
    n = len(entries)
    weights = [max(0.0, float(w or 0.0)) for (w, _s) in entries]
    sides = [str(s) for (_w, s) in entries]
    total_weight = sum(weights)
    caps: dict[str, Any] = {"mode": mode}

    eq = max(0.0, float(equity or 0.0))
    if mode == SIZING_EQUITY_LINKED:
        try:
            pct = float(equity_deploy_pct)
        except (TypeError, ValueError):
            pct = DEFAULT_EQUITY_DEPLOY_PCT
        if not math.isfinite(pct) or pct <= 0:
            pct = DEFAULT_EQUITY_DEPLOY_PCT
        deploy_budget = eq * pct
        caps["equity"] = round(eq, 2)
        caps["equity_deploy_pct"] = pct
    else:
        deploy_budget = resolve_tier_notional(tier)
        caps["tier"] = (tier or "").strip().lower()
    caps["deploy_budget"] = round(deploy_budget, 2)

    if n == 0 or deploy_budget <= 0:
        return NotionalPlan(
            notionals=[0.0] * n,
            mode=mode,
            deploy_budget=round(deploy_budget, 2),
            total_weight=total_weight,
            gross_after=0.0,
            net_after=0.0,
            long_after=0.0,
            short_after=0.0,
            caps=caps,
        )

    # base: weight 比で予算配分 (weight が全 0 なら均等割り = 旧 per_signal_default 相当)
    if total_weight > 0:
        notionals = [w / total_weight * deploy_budget for w in weights]
    else:
        notionals = [deploy_budget / n] * n

    # --- fixed_tier: 従来挙動。dollar cap は掛けない。---
    if mode != SIZING_EQUITY_LINKED:
        notionals = [round(x, 2) for x in notionals]
        long_after = sum(x for x, s in zip(notionals, sides) if not _is_short_side(s))
        short_after = sum(x for x, s in zip(notionals, sides) if _is_short_side(s))
        return NotionalPlan(
            notionals=notionals,
            mode=mode,
            deploy_budget=round(deploy_budget, 2),
            total_weight=total_weight,
            gross_after=round(long_after + short_after, 2),
            net_after=round(abs(long_after - short_after), 2),
            long_after=round(long_after, 2),
            short_after=round(short_after, 2),
            caps=caps,
        )

    # --- equity_linked: per-name → gross → net cap を予算の内側で適用 ---
    # (1) per-name 上限 (equity×max_pct)。超過分は他銘柄へ再配分しない (hard cap)。
    if max_pct and max_pct > 0:
        name_cap = max_pct * eq
        clamped = 0
        new_notionals = []
        for x in notionals:
            if x > name_cap:
                clamped += 1
                new_notionals.append(name_cap)
            else:
                new_notionals.append(x)
        notionals = new_notionals
        caps["per_name"] = {
            "cap_usd": round(name_cap, 2),
            "clamped_count": clamped,
        }

    # (2) gross 上限 (equity×gross_pct)。超過なら全体を比例縮小。
    gross = sum(notionals)
    if max_gross_exposure_pct and max_gross_exposure_pct > 0:
        gross_cap = max_gross_exposure_pct * eq
        if gross > gross_cap and gross > 0:
            f = gross_cap / gross
            notionals = [x * f for x in notionals]
            caps["gross"] = {
                "cap_usd": round(gross_cap, 2),
                "before_usd": round(gross, 2),
                "scale": round(f, 6),
            }

    # (3) net 上限 (equity×net_pct)。超過なら優勢サイドのみ縮小 (gross は増えない)。
    long_usd = sum(x for x, s in zip(notionals, sides) if not _is_short_side(s))
    short_usd = sum(x for x, s in zip(notionals, sides) if _is_short_side(s))
    net = abs(long_usd - short_usd)
    if max_net_exposure_pct and max_net_exposure_pct > 0:
        net_cap = max_net_exposure_pct * eq
        if net > net_cap:
            if long_usd > short_usd and long_usd > 0:
                f_long = max(0.0, (short_usd + net_cap) / long_usd)
                notionals = [
                    (x * f_long if not _is_short_side(s) else x)
                    for x, s in zip(notionals, sides)
                ]
                caps["net"] = {
                    "cap_usd": round(net_cap, 2),
                    "before_usd": round(net, 2),
                    "scaled_side": "long",
                    "scale": round(f_long, 6),
                }
            elif short_usd > long_usd and short_usd > 0:
                f_short = max(0.0, (long_usd + net_cap) / short_usd)
                notionals = [
                    (x * f_short if _is_short_side(s) else x)
                    for x, s in zip(notionals, sides)
                ]
                caps["net"] = {
                    "cap_usd": round(net_cap, 2),
                    "before_usd": round(net, 2),
                    "scaled_side": "short",
                    "scale": round(f_short, 6),
                }

    notionals = [round(x, 2) for x in notionals]
    long_after = sum(x for x, s in zip(notionals, sides) if not _is_short_side(s))
    short_after = sum(x for x, s in zip(notionals, sides) if _is_short_side(s))
    return NotionalPlan(
        notionals=notionals,
        mode=mode,
        deploy_budget=round(deploy_budget, 2),
        total_weight=total_weight,
        gross_after=round(long_after + short_after, 2),
        net_after=round(abs(long_after - short_after), 2),
        long_after=round(long_after, 2),
        short_after=round(short_after, 2),
        caps=caps,
    )


def fetch_account_equity(client: Any | None = None) -> float | None:
    """Alpaca paper 口座の equity を取得 (read-only)。取得失敗/creds 無しは None。

    発注は一切しない。equity_linked サイジングの予算解決に使う。
    """
    try:
        if client is None:
            client = ba.get_client(paper=True)
        acct = client.get_account()
        val = float(getattr(acct, "equity", 0.0) or 0.0)
        return val if val > 0 else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("account equity 取得失敗、fallback します: %s", exc)
        return None


def resolve_sizing_equity(
    fallback_equity: float,
    *,
    mode: str,
    client: Any | None = None,
    allow_fetch: bool | None = None,
) -> tuple[float, str]:
    """equity_linked サイジング用の equity を安全に解決する。

    - mode != equity_linked: fallback をそのまま (equity は sizing に不使用)。
    - allow_fetch=None のとき、TEST_MODE 環境変数があれば fetch しない
      (テスト/従来挙動を壊さない。creds を叩かず決定論的)。
    - fetch 成功 → (Alpaca 実 equity, "alpaca")。
    - creds 無し/取得失敗/0 → (fallback, "fallback:...") で安全に退避。
    """
    fb = float(fallback_equity or 0.0)
    if mode != SIZING_EQUITY_LINKED:
        return fb, "fixed_tier:fallback_unused"
    if allow_fetch is None:
        allow_fetch = not bool(os.getenv("TEST_MODE", "").strip())
    if not allow_fetch:
        return fb, "fallback:fetch_disabled(test_mode)"
    val = fetch_account_equity(client)
    if val is not None and val > 0:
        return val, "alpaca"
    return fb, "fallback:fetch_failed_or_no_creds"


class LiveAccountGuardError(RuntimeError):
    pass


class OrderSubmitError(RuntimeError):
    pass


class PositionsFetchError(RuntimeError):
    """Alpaca open-positions 取得に失敗したことを明示するエラー。

    F2 P0#7 audit fix (2026-07-03):
        以前は ``_fetch_open_positions`` が例外を silent に呑んで ``{}`` を
        返していた。呼び出し側 (``signals_to_orders`` の非 dry_run 経路) は
        「今 flat」と「fetch 失敗」を区別できず、既に long で持っている銘柄に
        重ねて buy を出す duplicate exposure を発生させ得る。
        修正後は fetch fail を silent {} ではなく本例外で raise し、caller は
        (a) 例外を propagate して発注を中止するか、(b) 明示的に fallback を
        書くかの二択を強いる。silent duplicate は起きない。
    """


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
    exec_mode: str | None = None  # "notional" | "qty" — どちらで発注したか
    skip_reason: str | None = None  # pre-submit で skip した理由 (silent drop 禁止)
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
        prepared.side,
        prepared.symbol,
        prepared.qty,
        prepared.order_id,
        prepared.status,
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
                    # sub-penny guard: >=$1 の limit entry を小数4桁のまま
                    # 発注すると Alpaca が code 42210000 で拒否するため、native
                    # protection order と同じ tick ルールで丸める。
                    limit_price = round_to_alpaca_tick(float(raw_px))
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
        len(prepared),
        account_equity,
        dry_run,
    )

    if dry_run:
        for po in prepared:
            _audit_log({"event": "dry_run", **po.to_row()})
        return prepared

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
    """Alpaca open positions を ``{symbol: signed_qty}`` として返す。

    F2 P0#7 audit fix (2026-07-03):
        取得エラー時に silent ``{}`` を返さない。``PositionsFetchError`` を
        raise して caller (``signals_to_orders`` 非 dry_run 経路) が
        「fetch 失敗」を明示的に扱えるようにする。

        なぜ raise か: caller は fetch 結果を dedup に使う。
        ``open_positions.get(sym, 0.0)`` は「fetch=空」も「本当に flat」も
        ``0.0`` として区別できない。silent {} だと既に持ってる銘柄に buy を
        重ねて duplicate exposure が起き得る。
    """
    out: dict[str, float] = {}
    try:
        positions = client.get_all_positions()
    except Exception as exc:
        logger.error("open positions 取得失敗、safe abort: %s", exc)
        raise PositionsFetchError(f"Alpaca open positions fetch failed: {exc}") from exc
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
            out.append(
                {
                    "symbol": sym,
                    "side": side,
                    "entry_price": price,
                    "weight": weight,
                    "system": norm_sys,
                }
            )
    return out


# =========================================================================
# Pre-submit execution-mode validator (2026-07-04)
# =========================================================================
# 実 paper 発注 (2026-07-04) で判明した Alpaca のフラクショナル制約:
#   - fractional (notional) 注文は空売りできない        -> code 42210000
#   - 非 fractionable 銘柄は notional 注文を拒否する       -> code 40310000
# よって notional が使えるのは「long かつ fractionable」の時だけ。
# それ以外 (short / 非fractionable / prefer_fractional=False) は整数株 qty へ
# 自動フォールバックする。サイズできない場合は **silent drop せず** skip 理由を
# 付けて返し、呼び出し側のサマリで必ず可視化する。
EXEC_NOTIONAL = "notional"
EXEC_QTY = "qty"
EXEC_SKIP = "skip"

_FRACTIONABLE_CACHE: dict[str, bool | None] = {}


def plan_order_execution(
    *,
    side: str,
    notional_usd: float,
    price: float,
    fractionable: bool | None,
    prefer_fractional: bool = True,
) -> tuple[str, int, float, str]:
    """1 注文の実行方式を Alpaca のフラクショナル制約に触れないよう決める (pure)。

    Returns:
        (mode, qty, notional, reason)
        - mode == EXEC_NOTIONAL: MarketOrderRequest(notional=...) で発注 (long+fractionable)
        - mode == EXEC_QTY:      整数株 qty で発注 (short / 非fractionable / prefer_fractional=False)
        - mode == EXEC_SKIP:     サイズ不能。reason を付けて返す (呼び出し側が必ず可視化)
    """
    side_l = (side or "").strip().lower()
    is_short = side_l in ("sell", "short", "sell_short")

    # notional が合法なのは「long かつ fractionable かつ caller が fractional 希望」時のみ
    if prefer_fractional and not is_short and fractionable is True:
        return (EXEC_NOTIONAL, 0, round(notional_usd, 2), "long+fractionable→notional")

    # それ以外は整数株。サイズには正の price が要る。
    if is_short:
        why = "short"
    elif fractionable is False:
        why = "non_fractionable"
    elif fractionable is None:
        why = "fractionable_unknown"
    else:
        why = "prefer_qty"

    if price is None or price <= 0:
        return (
            EXEC_SKIP,
            0,
            round(notional_usd, 2),
            f"skip:{why}:no_positive_price_to_size_whole_shares",
        )
    qty = int(notional_usd // price)
    if qty < 1:
        return (
            EXEC_SKIP,
            0,
            round(notional_usd, 2),
            f"skip:{why}:notional_${notional_usd:.2f}_below_1_share_@${price:.2f}",
        )
    return (EXEC_QTY, qty, round(notional_usd, 2), f"{why}→whole_share_qty={qty}")


def get_asset_fractionable(client: Any, symbol: str) -> bool | None:
    """Alpaca asset の ``fractionable`` フラグを照会 (結果を module cache)。

    取得失敗時は ``None`` (unknown) を返し、classifier 側で保守的に整数株へ倒す。
    """
    key = (symbol or "").upper()
    if not key:
        return None
    if key in _FRACTIONABLE_CACHE:
        return _FRACTIONABLE_CACHE[key]
    frac: bool | None
    try:
        asset = client.get_asset(key)
        frac = bool(getattr(asset, "fractionable", False))
    except Exception as exc:  # noqa: BLE001
        logger.warning("get_asset(%s) 失敗、非fractionable扱い(unknown): %s", key, exc)
        frac = None
    _FRACTIONABLE_CACHE[key] = frac
    return frac


def fetch_open_order_state(client: Any) -> tuple[dict[str, set[str]], set[str]]:
    """現在の open orders から wash-guard / 冪等性判定の材料を取る (read-only)。

    Returns:
        (open_sides, open_coids)
        - open_sides: ``{symbol: {"buy","sell"}}`` — 反対側 order 検知 (wash trade 回避)
        - open_coids: 既に open な client_order_id 集合 — 二重 submit 回避

    ユーザーの既存注文 (別経路/手動の exit 注文含む) は **一切変更しない**。
    衝突する自注文を skip するだけで、他人の注文は read のみ。
    """
    open_sides: dict[str, set[str]] = {}
    open_coids: set[str] = set()
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        req = GetOrdersRequest(status=QueryOrderStatus.OPEN, limit=500)
        orders = client.get_orders(filter=req)
    except Exception as exc:  # noqa: BLE001
        logger.warning("open orders 取得失敗 (wash-guard/冪等性 無効化): %s", exc)
        return open_sides, open_coids
    for o in orders:
        sym = str(getattr(o, "symbol", "")).upper()
        raw_side = str(getattr(o, "side", "")).lower()
        side = (
            "buy" if "buy" in raw_side else ("sell" if "sell" in raw_side else raw_side)
        )
        coid = getattr(o, "client_order_id", None)
        if sym:
            open_sides.setdefault(sym, set()).add(side)
        if coid:
            open_coids.add(str(coid))
    return open_sides, open_coids


# =========================================================================
# Per-system standing cap (P1 fix, 2026-07-21)
# =========================================================================
# 根因 (logs/audit_20260719/AUDIT_REPORT.md 🔴P1): 発注境界には per-symbol の
# ``already_held`` しか無く、**別銘柄で同一 system の保有数が上限 (max_positions=10)
# を超えて積み上がる**のを止められなかった。07-13 の 10 銘柄 + 07-14 の別 10 銘柄が
# 両方通り system 別 20 に。finalize_allocation の slot cap は open_auto_run 経路
# (JSON→paper_trading_submit) を通らないので効かない。ここが両経路共通の発注直前点=
# **最終防波堤**。詳細 docs/POSITION_MANAGEMENT_P1_STANDING_CAP_20260721.md。
#
# cap 値は新規でなく既存 spec の実効化:
#   per-system=risk.max_positions(=10, docs/systems S1/S2 明記/他 global 既定)
#   total     =risk.portfolio.max_total_positions(=70, PHASE5 §2.1)
# per-run TOP_N=10 (1 run の候補生成数) とは別概念 (こちらは日跨ぎの同時保有残高)。


@dataclass(slots=True)
class HeldPositionCounts:
    """standing cap 判定用の現保有集計。

    per_system: system 帰属できた保有数 (coid → symbol_system_map)。
    unmapped: system 帰属できない nonzero 保有 (delisted/orphan, 例 FOLD/CDTX)。
    total: nonzero 保有総数 (unmapped 含む)。long_total/short_total は qty 符号で。
    """

    per_system: dict[str, int]
    unmapped: int
    total: int
    long_total: int
    short_total: int


def _fetch_entry_coid_by_symbol(client: Any) -> dict[str, str]:
    """直近 Alpaca orders から entry 由来 client_order_id を symbol 別に引く (read-only)。

    exit 経路 ``_hydrate_from_alpaca_coids`` と同じ信頼できる帰属源。取得失敗は
    握り潰して空 dict (best-effort。position fetch 自体は別で fail-closed 済)。
    """
    out: dict[str, str] = {}
    try:
        from alpaca.trading.enums import QueryOrderStatus
        from alpaca.trading.requests import GetOrdersRequest

        raw = client.get_orders(
            filter=GetOrdersRequest(status=QueryOrderStatus.ALL, limit=500)
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("standing cap: 直近 orders 取得失敗、coid 帰属を skip: %s", exc)
        return out
    for o in raw or []:
        try:
            sym = str(getattr(o, "symbol", "") or "").upper()
            coid = str(getattr(o, "client_order_id", "") or "")
            if not sym or not coid:
                continue
            if parse_system_from_client_order_id(coid) is None:
                continue  # entry 由来 (system... prefix) のみ
            out.setdefault(sym, coid)  # 最初にヒット (新しい fill から返る想定)
        except Exception:
            continue
    return out


def count_held_positions_by_system(
    client: Any,
    *,
    open_positions: dict[str, float],
    symbol_system_map: Mapping[str, Any] | None = None,
    coid_by_symbol: Mapping[str, str] | None = None,
) -> HeldPositionCounts:
    """現保有を system 別に集計する (standing cap の分母)。

    ``open_positions`` は ``{symbol: signed_qty}`` (``_fetch_open_positions`` 由来)。
    帰属優先順位 = entry order の client_order_id (``system{N}-…``) → symbol_system_map。
    どちらでも帰属できない nonzero 保有は delisted/orphan として ``unmapped`` に積む
    (total/side には算入するが per-system には入れない = docs の設計判断)。
    """
    ssm: dict[str, str] = {}
    if symbol_system_map:
        for k, v in symbol_system_map.items():
            try:
                sym = str(k).strip().upper()
            except Exception:
                continue
            prim = resolve_primary_system(v)
            if not prim:
                try:
                    prim = str(v).strip().lower() or None
                except Exception:
                    prim = None
            if sym and prim:
                ssm[sym] = str(prim).strip().lower()

    coids: Mapping[str, str] = coid_by_symbol if coid_by_symbol is not None else {}
    if coid_by_symbol is None and client is not None:
        coids = _fetch_entry_coid_by_symbol(client)

    per_system: dict[str, int] = {}
    unmapped = total = long_total = short_total = 0
    for sym_raw, qty_raw in (open_positions or {}).items():
        try:
            qty = float(qty_raw or 0.0)
        except (TypeError, ValueError):
            qty = 0.0
        if qty == 0.0:
            continue
        sym = str(sym_raw).strip().upper()
        total += 1
        if qty >= 0:
            long_total += 1
        else:
            short_total += 1

        system = parse_system_from_client_order_id(coids.get(sym))
        if not system:
            system = ssm.get(sym)
        if not system and sym == "SPY" and qty < 0:
            system = "system7"  # SPY short = catastrophe hedge
        if system:
            key = str(system).strip().lower()
            per_system[key] = per_system.get(key, 0) + 1
        else:
            unmapped += 1  # delisted/orphan (帰属不能)

    return HeldPositionCounts(
        per_system=per_system,
        unmapped=unmapped,
        total=total,
        long_total=long_total,
        short_total=short_total,
    )


def evaluate_standing_cap(
    *,
    system: str,
    held_by_system: Mapping[str, int],
    total_held: int,
    batch_by_system: Mapping[str, int],
    batch_total: int,
    per_system_cap: int,
    total_cap: int,
) -> str | None:
    """新規 1 件が standing cap に触れるなら skip 理由を返す (pure, なければ None)。

    total_held / batch_total は delisted 含む口座全体。held_by_system は帰属済保有。
    """
    if total_cap > 0 and (total_held + batch_total) >= total_cap:
        return (
            f"standing_cap:portfolio_total_held={total_held}"
            f"+batch={batch_total}>=cap={total_cap}"
        )
    key = (system or "").strip().lower()
    if per_system_cap > 0 and key:
        held = int(held_by_system.get(key, 0))
        batch = int(batch_by_system.get(key, 0))
        if (held + batch) >= per_system_cap:
            return f"standing_cap:{key}_held={held}+batch={batch}>=cap={per_system_cap}"
    return None


def _resolve_standing_caps() -> tuple[bool, int, int]:
    """(enforce, per_system_cap, total_cap) を config + env から解決する。

    既定は docs 通り (per-system=risk.max_positions=10 / total=
    risk.portfolio.max_total_positions=70)。新しいリスク値は入れない。
    env で ops 上書き / 無効化可 (docs §4)。
    """

    def _flag(name: str, default: str = "1") -> bool:
        return os.environ.get(name, default).strip().lower() not in (
            "0",
            "false",
            "no",
            "off",
        )

    def _int_env(name: str, fallback: int) -> int:
        try:
            raw = os.environ.get(name)
            return int(raw) if raw not in (None, "") else fallback
        except (TypeError, ValueError):
            return fallback

    per_system = 10
    total = 70
    try:
        from config.settings import get_settings

        risk = get_settings().risk
        per_system = int(getattr(risk, "max_positions", 10))
        total = int(
            getattr(getattr(risk, "portfolio", None), "max_total_positions", 70)
        )
    except Exception:
        pass

    enforce = _flag("SUBMIT_ENFORCE_STANDING_CAP", "1")
    per_system = _int_env("SUBMIT_MAX_POSITIONS_PER_SYSTEM", per_system)
    total = _int_env("SUBMIT_MAX_TOTAL_POSITIONS", total)
    return enforce, max(0, per_system), max(0, total)


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
    sizing_mode: str = SIZING_EQUITY_LINKED,
    equity_deploy_pct: float = DEFAULT_EQUITY_DEPLOY_PCT,
    max_pct: float = 0.10,
    max_gross_exposure_pct: float = 1.0,
    max_net_exposure_pct: float = 0.5,
) -> list[PreparedOrder]:
    """today_signals JSON を配分し Alpaca 注文へ変換する。

    sizing_mode:
      - ``SIZING_EQUITY_LINKED`` (既定): deploy_budget = account_equity ×
        equity_deploy_pct。per-name/gross/net cap を予算の内側で適用。
      - ``SIZING_FIXED_TIER``: 従来の tier 固定予算 (後方互換, dollar cap 掛けない)。
    account_equity は equity_linked の予算と cap の分母に使う (fixed_tier では未使用)。
    """
    signals = _flatten_json_signals(json_data)
    if not signals:
        return []

    if not dry_run:
        assert_paper_env()
        if client is None:
            client = ba.get_client(paper=True)

    if entry_date is None:
        entry_date = str(json_data.get("date") or "")
    date_compact = entry_date.replace("-", "").replace(" ", "")[:8]

    # dedup (sym, system) を先に確定してから予算配分する。dollar cap
    # (per-name/gross/net) は予算の内側でここで適用する。
    # NOTE (P1 fix 2026-07-21): **件数 cap / max_positions は上流 finalize_allocation
    # で必ず効く、という旧前提は誤り** (audit 🔴P1)。open_auto_run 経路は finalize を
    # 通らず JSON→ここへ直行するため、per-system の保有件数上限は下の実発注ループで
    # standing cap として enforce する (最終防波堤)。
    deduped: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()
    for s in signals:
        dedup = (s["symbol"], s["system"])
        if dedup in seen:
            continue
        seen.add(dedup)
        deduped.append(s)

    plan = compute_position_notionals(
        [(float(s["weight"]), s["side"]) for s in deduped],
        mode=sizing_mode,
        tier=tier,
        equity=account_equity,
        equity_deploy_pct=equity_deploy_pct,
        max_pct=max_pct,
        max_gross_exposure_pct=max_gross_exposure_pct,
        max_net_exposure_pct=max_net_exposure_pct,
    )

    prepared: list[PreparedOrder] = []

    for s, notional in zip(deduped, plan.notionals):
        sym = s["symbol"]
        side = s["side"]
        price = s["entry_price"]
        system = s["system"]

        client_order_id = (
            f"{system}-{sym}-{date_compact}" if date_compact else f"{system}-{sym}"
        )

        # observability fix (2026-07-07): min_notional 未満 / 整数株サイズ不能は
        # silent ``continue`` で落とさず、skip_reason を付けた PreparedOrder として
        # 残す。こうすると caller サマリ (paper_trading_submit) の skip 内訳に必ず
        # 現れ、「入力 signals=N → 生成/送信」の乖離が理由付きで説明可能になる。
        # (旧挙動は sub-$5 signal を無言で drop し、48→X の差が unobservable だった)
        pre_skip: str | None = None
        if notional < min_notional_usd:
            pre_skip = (
                f"skip:below_min_notional:${notional:.2f}<${min_notional_usd:.2f}"
            )

        qty: int = 0
        if pre_skip is None and not prefer_fractional:
            if price <= 0:
                pre_skip = "skip:no_positive_price:whole_share_size_impossible"
            else:
                qty = int(notional / price)
                if qty <= 0:
                    pre_skip = f"skip:below_1_share:${notional:.2f}_@${price:.2f}"

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
        po.extra["price"] = price  # whole-share フォールバックのサイズ計算用
        if pre_skip is not None:
            po.skip_reason = pre_skip
        prepared.append(po)

    logger.info(
        "signals_json_to_orders: %d 注文 mode=%s deploy_budget=$%.0f "
        "gross=$%.0f net=$%.0f dry_run=%s equity=$%.0f caps=%s",
        len(prepared),
        plan.mode,
        plan.deploy_budget,
        plan.gross_after,
        plan.net_after,
        dry_run,
        account_equity,
        plan.caps,
    )

    if dry_run:
        for po in prepared:
            _audit_log({"event": "dry_run_json", **po.to_row()})
        return prepared

    # --- 発注前バリデーション用の口座状態を 1 回だけ read (read-only) ---
    open_sides, open_coids = fetch_open_order_state(client)
    # 既保有ポジション {symbol: signed_qty}。同方向の重ね買いを防ぐための突合材料。
    # docs today_signal_scan/6 (現保有と突合) + fable5 audit item7/8 の duplicate
    # exposure 対策。fetch 失敗は _fetch_open_positions が PositionsFetchError を
    # raise して fail-closed (silent {} で既保有に重ねて buy しない = P0#7 の設計)。
    open_positions = _fetch_open_positions(client)

    # --- per-system standing cap の分母を一度だけ集計 (P1 fix 2026-07-21) ---
    # already_held は同一銘柄しか止めない。別銘柄で system の保有数が上限を超えて
    # 積み上がる (07-13 の 10 + 07-14 の別 10 = 20) のを、ここで held+batch を数えて
    # 弾く。これが finalize を経ない open_auto_run 経路の最終防波堤。
    enforce_cap, per_system_cap, total_cap = _resolve_standing_caps()
    if enforce_cap:
        try:
            from common.symbol_map import load_symbol_system_map

            ssm = load_symbol_system_map()
        except Exception:
            ssm = {}
        held_counts = count_held_positions_by_system(
            client, open_positions=open_positions, symbol_system_map=ssm
        )
        logger.info(
            "standing cap 有効: per_system<=%d total<=%d (現保有 total=%d "
            "unmapped/delisted=%d per_system=%s)",
            per_system_cap,
            total_cap,
            held_counts.total,
            held_counts.unmapped,
            held_counts.per_system,
        )
    else:
        held_counts = HeldPositionCounts({}, 0, 0, 0, 0)
    batch_by_system: dict[str, int] = {}
    batch_total = 0

    submitted: list[PreparedOrder] = []
    for po in prepared:
        # pre-generation で既に skip 判定済 (min_notional 未満 / 整数株サイズ不能)
        # は発注せず結果にそのまま残す (skip_reason は付与済)。
        if po.skip_reason:
            _audit_log({"event": "skip_pre_generation", **po.to_row()})
            submitted.append(po)
            continue
        price = float(po.extra.get("price") or 0.0)
        opp_side = "sell" if po.side == "buy" else "buy"

        # (0) 既保有ポジションとの突合: 同方向で既に保有している銘柄は重ね買いしない。
        #     反対方向 (netting / 反対売買) は wash-guard (open orders) と Alpaca 側
        #     netting に委ね、ここでは touch しない (ユーザーの exit 注文を壊さない)。
        held = float(open_positions.get(po.symbol, 0.0) or 0.0)
        if (po.side == "buy" and held > 0) or (po.side == "sell" and held < 0):
            po.skip_reason = f"already_held:{po.side}_qty={held:g}"
            _audit_log({"event": "skip_already_held", **po.to_row()})
            logger.info("skip (既保有 %s): %s qty=%s", po.side, po.symbol, held)
            submitted.append(po)
            continue

        # (0.5) per-system standing cap + portfolio total cap (P1 fix 2026-07-21):
        #     already_held を通っても、別銘柄で system の保有が max_positions を超える
        #     新規はここで弾く。実 submit 成功時のみ batch を積むので、skip/失敗は
        #     cap 予算を消費しない。既存ポジションには一切触れない (新規を止めるだけ)。
        if enforce_cap:
            cap_reason = evaluate_standing_cap(
                system=po.system or "",
                held_by_system=held_counts.per_system,
                total_held=held_counts.total,
                batch_by_system=batch_by_system,
                batch_total=batch_total,
                per_system_cap=per_system_cap,
                total_cap=total_cap,
            )
            if cap_reason:
                po.skip_reason = cap_reason
                _audit_log({"event": "skip_standing_cap", **po.to_row()})
                logger.info(
                    "skip (standing cap): %s %s %s",
                    po.symbol,
                    po.system,
                    cap_reason,
                )
                submitted.append(po)
                continue

        # (1) 冪等性: 同一 client_order_id が既に open なら二重 submit しない
        if po.client_order_id and po.client_order_id in open_coids:
            po.skip_reason = "already_open:duplicate_client_order_id"
            _audit_log({"event": "skip_duplicate_coid", **po.to_row()})
            logger.info("skip (既に open): %s", po.client_order_id)
            submitted.append(po)
            continue

        # (2) wash-trade 回避: 反対側の open order がある銘柄は skip。
        #     ユーザーの既存注文 (exit 注文等) は絶対にキャンセルしない。
        if opp_side in open_sides.get(po.symbol, set()):
            po.skip_reason = (
                f"wash_trade_conflict:existing_{opp_side}_order (既存注文は保持)"
            )
            _audit_log({"event": "skip_wash_conflict", **po.to_row()})
            logger.info(
                "skip (wash 回避): %s %s vs 既存 %s", po.symbol, po.side, opp_side
            )
            submitted.append(po)
            continue

        # (3) 実行方式を分類: long+fractionable→notional / それ以外→整数株 / 不能→skip
        fractionable = get_asset_fractionable(client, po.symbol)
        mode, qty, notional, reason = plan_order_execution(
            side=po.side,
            notional_usd=float(po.notional_usd or 0.0),
            price=price,
            fractionable=fractionable,
            prefer_fractional=prefer_fractional,
        )
        po.exec_mode = mode
        po.extra["fractionable"] = fractionable
        po.extra["plan_reason"] = reason

        if mode == EXEC_SKIP:
            po.skip_reason = reason
            _audit_log({"event": "skip_unsizable", **po.to_row()})
            logger.info("skip (サイズ不能): %s %s", po.symbol, reason)
            submitted.append(po)
            continue

        try:
            if mode == EXEC_NOTIONAL:
                from alpaca.trading.requests import MarketOrderRequest

                req = MarketOrderRequest(
                    symbol=po.symbol,
                    notional=float(notional),
                    side="buy" if po.side == "buy" else "sell",
                    time_in_force="day",
                    client_order_id=po.client_order_id,
                )
                order = client.submit_order(order_data=req)
                po.order_id = str(getattr(order, "id", "") or "")
                po.status = str(getattr(order, "status", "") or "")
                _audit_log({"event": "submitted_notional", **po.to_row()})
            else:  # EXEC_QTY — 整数株 (short / 非fractionable)
                po.qty = qty
                result = submit_paper_order(
                    po.symbol,
                    qty,
                    po.side,
                    order_type=po.order_type,
                    time_in_force=po.time_in_force,
                    client_order_id=po.client_order_id,
                    dry_run=False,
                    client=client,
                )
                po.order_id = result.order_id
                po.status = result.status
                _audit_log({"event": "submitted_qty", **po.to_row()})
            # 自注文を口座状態に反映し、同一バッチ内の後続 self-wash / 二重を防ぐ
            open_sides.setdefault(po.symbol, set()).add(po.side)
            if po.client_order_id:
                open_coids.add(po.client_order_id)
            # standing cap: 実 submit 成功分のみ batch に計上 (skip/失敗は消費しない)
            batch_total += 1
            _sys_key = (po.system or "").strip().lower()
            if _sys_key:
                batch_by_system[_sys_key] = batch_by_system.get(_sys_key, 0) + 1
            submitted.append(po)
        except Exception as exc:  # noqa: BLE001
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
# 端株 (fractional) 用 synthetic protection の coid suffix。Alpaca は端株に
# native stop/limit/trailing を出せないため、日次 exit_check で現値が stop/target
# を突破していれば成行DAYの全数クローズをこの suffix で 1 件だけ発注する。
_EXIT_SYN_STOP_SUFFIX = "exit-synstop"
_EXIT_SYN_TARGET_SUFFIX = "exit-syntarget"

# 整数株判定の許容誤差。float 表現誤差 (例 5.0000000001) は整数株として扱う。
_QTY_WHOLE_TOL = 1e-9


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
    def abs_qty(self) -> float:
        """符号なしの実 position qty (端株を保持する)。

        以前は ``int(abs(self.qty))`` で端株 (qty<1 や小数株) を 0 に切り捨て、
        ``build_exit_orders_from_positions`` の ``if snap.abs_qty <= 0`` により
        time / breakout / protection の全 exit 種別から **silent 除外** していた
        (2026-07-12 端株 exit 未計画バグ)。equity 連動サイジングは端株を日常的に
        作るため、多数の建玉が閉じられなくなっていた。切り捨てを廃止する。
        """
        return abs(float(self.qty))

    @property
    def is_fractional(self) -> bool:
        """整数株でない (端株) かどうか。

        Alpaca は端株に native な stop/limit/trailing を出せず成行 DAY のみ
        受け付ける。整数株は従来どおり native protection を発注し、端株は
        synthetic (日次現値突破→成行 DAY 全数クローズ) へ振り分ける判定に使う。
        """
        aq = self.abs_qty
        return abs(aq - round(aq)) > _QTY_WHOLE_TOL

    @property
    def current_price(self) -> float | None:
        """market_value / qty から現値を逆算する (Alpaca live quote 由来)。

        market_value が無い / qty=0 なら None。synthetic stop/target の突破
        判定に使う (取得不能なら synthetic は発注しない safe fallback)。
        """
        if self.market_value is None:
            return None
        aq = self.abs_qty
        if aq <= 0:
            return None
        return abs(float(self.market_value)) / aq

    def exit_qty(self) -> float:
        """exit order に載せる qty。整数株は int、端株は float(9桁丸め) を返す。

        整数株を int で返すのは native stop/limit・既存 JSON/ログの後方互換を
        壊さないため。端株は Alpaca が最大 9 桁小数まで受け付ける。
        """
        aq = self.abs_qty
        if not self.is_fractional:
            return int(round(aq))
        return round(aq, 9)


@dataclass(slots=True)
class PreparedExit:
    """exit_check step が生成する 1 exit order 案。

    dry_run/submit を切り替えても schema が変わらないよう、to_row() で JSON に落ちる。
    """

    symbol: str
    system: str
    qty: float  # 整数株は int、端株は小数 (Alpaca は端株を成行DAYのみ受付)
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


def fetch_position_snapshots(
    client: Any, *, raise_on_error: bool = False
) -> list[PositionSnapshot]:
    """Alpaca client から現 positions を取得し、PositionSnapshot list を返す。

    ``raise_on_error=True`` の場合、``get_all_positions`` の失敗を silent ``[]`` に
    畳まず ``PositionsFetchError`` を raise する。exit_check が「取得失敗 (broker
    unreachable)」と「本当に空 (flat book)」を区別できるようにするため
    (entry 側 ``_fetch_open_positions`` と同じ fail-closed 方針)。default False は
    既存 caller (``paper_trading_status``) の挙動を保つ。
    """
    out: list[PositionSnapshot] = []
    try:
        raw = client.get_all_positions()
    except Exception as exc:
        logger.warning("get_all_positions 失敗: %s", exc)
        if raise_on_error:
            raise PositionsFetchError(f"Alpaca positions fetch failed: {exc}") from exc
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


def compute_holding_days(
    entry_date: str | None, today: str | None = None
) -> int | None:
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
        qty=snap.exit_qty(),
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
        qty=snap.exit_qty(),
        side=close_side,
        order_type="market",
        reason=ExitReasonCode.BREAKOUT,
        entry_date=snap.entry_date,
        client_order_id=coid,
        dry_run=True,
    )


def _stop_price_for(
    snap: PositionSnapshot, rules: Any, atr_value: float | None
) -> float | None:
    """ATR ベースの protective stop 価格。ATR 無し / rules 無しなら None。

    long: entry - stop_dist (最低 0.01), short: entry + stop_dist。
    native protection (整数株) と synthetic (端株) の双方から使う共通式。
    """
    if rules is None or atr_value is None or atr_value <= 0:
        return None
    stop_dist = float(atr_value) * float(rules.stop_atr_multiplier)
    if snap.side == "long":
        return max(0.01, snap.avg_entry_price - stop_dist)
    return snap.avg_entry_price + stop_dist


def _target_price_for(
    snap: PositionSnapshot, rules: Any, atr_value: float | None
) -> float | None:
    """profit target 価格 (S2/S3/S6=%, S5=ATR)。target 未定義なら None。

    native protection (整数株) と synthetic (端株) の双方から使う共通式。
    """
    if rules is None:
        return None
    ttype = getattr(rules, "profit_target_type", "none")
    if ttype == "percentage" and rules.profit_target_value > 0:
        mult = 1.0 + (float(rules.profit_target_value) / 100.0)
        if snap.side == "long":
            return snap.avg_entry_price * mult
        return snap.avg_entry_price / mult
    if ttype == "atr" and atr_value is not None and atr_value > 0:
        dist = float(atr_value) * float(rules.profit_target_value)
        if snap.side == "long":
            return snap.avg_entry_price + dist
        return snap.avg_entry_price - dist
    return None


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
                    qty=snap.exit_qty(),
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
    stop_price = _stop_price_for(snap, rules, atr_value)
    if stop_price is not None:
        coid = (
            f"protect-{snap.system}-{snap.symbol}-{entry_date_compact}-"
            f"{_PROTECT_STOP_SUFFIX}"
        )
        if coid not in existing_protect_coids:
            proposals.append(
                PreparedExit(
                    symbol=snap.symbol,
                    system=snap.system,
                    qty=snap.exit_qty(),
                    side=close_side,
                    order_type="stop",
                    reason=ExitReasonCode.PROTECT_STOP,
                    entry_date=snap.entry_date,
                    stop_price=round_to_alpaca_tick(stop_price),
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="gtc",
                )
            )

    # profit target (S2/S3/S6 = %, S5 = ATR)
    target_price = _target_price_for(snap, rules, atr_value)
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
                    qty=snap.exit_qty(),
                    side=close_side,
                    order_type="limit",
                    reason=ExitReasonCode.PROTECT_TARGET,
                    entry_date=snap.entry_date,
                    limit_price=round_to_alpaca_tick(target_price),
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="gtc",
                )
            )

    return proposals


def _build_synthetic_protection_orders(
    snap: PositionSnapshot,
    rules: Any,
    *,
    atr_value: float | None,
    current_price: float | None,
    today: str,
    existing_exit_coids: set[str],
) -> list[PreparedExit]:
    """端株 (fractional) position 用の synthetic 保護 exit を返す。

    Alpaca は端株に native な stop/limit/trailing を出せない (成行 DAY のみ)。
    そこで日次 exit_check 時に現値が stop / target を突破していれば、成行 DAY の
    **全数クローズを 1 件だけ** 発注する (synthetic stop / synthetic target)。
    突破していなければ何も出さず、翌 run で再評価する。

    設計上の割り切り:
      - trailing stop は HWM 状態が必要で stateless では合成できないため、
        ATR stop-loss で下方を代替する (S1/S4 の端株も ATR stop で保護)。
      - stop と target が同時に突破することは無い (long: stop<entry<target、
        short: target<entry<stop) ので、二重クローズは起きない。優先は stop。
      - close は full qty・成行 DAY・決定的 coid。同日再 run は同一 coid で
        Alpaca 側 422 duplicate となり二重発注しない (existing_exit_coids に
        含まれていれば発注自体を skip する二重防止も併設)。
      - 現値が不明 (market_value も rolling Close も無い) なら発注しない
        (safe fallback = 何もしない。満期は time-exit が別途カバーする)。
    """
    if rules is None or snap.system is None:
        return []
    if current_price is None or current_price <= 0:
        logger.debug(
            "synthetic exit skip: %s 現値不明 (market_value/rolling Close 無し)",
            snap.symbol,
        )
        return []

    close_side = "sell" if snap.side == "long" else "buy"
    qty = snap.exit_qty()
    date_compact = today.replace("-", "")

    # (1) synthetic stop (ATR ベース)。突破していれば即クローズ。
    stop_price = _stop_price_for(snap, rules, atr_value)
    if stop_price is not None:
        breached = (
            current_price <= stop_price
            if snap.side == "long"
            else current_price >= stop_price
        )
        if breached:
            coid = (
                f"exit-{snap.system}-{snap.symbol}-{date_compact}-"
                f"{_EXIT_SYN_STOP_SUFFIX}"
            )
            if coid in existing_exit_coids:
                return []
            return [
                PreparedExit(
                    symbol=snap.symbol,
                    system=snap.system,
                    qty=qty,
                    side=close_side,
                    order_type="market",
                    reason=ExitReasonCode.PROTECT_STOP,
                    entry_date=snap.entry_date,
                    stop_price=round(stop_price, 4),
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="day",
                )
            ]

    # (2) synthetic target。突破していれば即クローズ。
    target_price = _target_price_for(snap, rules, atr_value)
    if target_price is not None and target_price > 0:
        breached = (
            current_price >= target_price
            if snap.side == "long"
            else current_price <= target_price
        )
        if breached:
            coid = (
                f"exit-{snap.system}-{snap.symbol}-{date_compact}-"
                f"{_EXIT_SYN_TARGET_SUFFIX}"
            )
            if coid in existing_exit_coids:
                return []
            return [
                PreparedExit(
                    symbol=snap.symbol,
                    system=snap.system,
                    qty=qty,
                    side=close_side,
                    order_type="market",
                    reason=ExitReasonCode.PROTECT_TARGET,
                    entry_date=snap.entry_date,
                    limit_price=round(target_price, 4),
                    client_order_id=coid,
                    dry_run=True,
                    time_in_force="day",
                )
            ]

    return []


# -----------------------------------------------------------------------
# top-level: build all exit proposals from positions
# -----------------------------------------------------------------------


def _coid_already_open(po: PreparedExit, existing_exit_coids: set[str]) -> bool:
    """既に同一 client_order_id の exit- 注文が open なら True (二重発注防止)。"""
    return bool(po.client_order_id) and po.client_order_id in existing_exit_coids


def build_exit_orders_from_positions(
    snapshots: list[PositionSnapshot],
    *,
    today: str,
    tracker: dict[str, Any] | None = None,
    entry_orders_index: dict[str, dict[str, Any]] | None = None,
    existing_protect_coids: set[str] | None = None,
    existing_exit_coids: set[str] | None = None,
    spy_high: float | None = None,
    spy_max70: float | None = None,
    atr_by_symbol: dict[str, dict[int, float]] | None = None,
    price_by_symbol: dict[str, float] | None = None,
) -> list[PreparedExit]:
    """position snapshots から exit 発注案を build する pure function。

    - time-based (S2/S3/S5/S6): holding_days >= max_holding_days なら 成行 close
    - SPY breakout (S7): spy_high >= spy_max70 なら 翌寄成行 close
    - protection (整数株): 未発注 (existing_protect_coids に無い) なら
      trailing/stop/target を native で発注
    - protection (端株): native 不可のため synthetic (現値が stop/target を
      突破していれば成行 DAY 全数クローズ) を発注
    - S1/S4 は time-based 無いので protection のみ

    現値は snapshot.current_price (market_value/qty) を優先し、無い場合は
    price_by_symbol (rolling Close 等) を fallback に使う。existing_exit_coids
    に既に open な exit- coid があれば time/breakout/synthetic を skip (二重防止)。

    副作用なし。dry_run=True で返す。実発注 / dry_run flag は呼び出し側が差し替える。
    """
    hydrate_system_tags(
        snapshots,
        tracker=tracker,
        entry_orders_index=entry_orders_index,
    )
    existing_coids = existing_protect_coids or set()
    existing_exit = existing_exit_coids or set()
    atr_lookup = atr_by_symbol or {}
    price_lookup = price_by_symbol or {}

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
            if snap.is_fractional:
                # 端株: native stop/limit/trailing 不可 → synthetic
                # (現値が stop/target を突破していれば成行 DAY 全数クローズ)。
                cur_px = snap.current_price
                if cur_px is None:
                    cur_px = price_lookup.get(snap.symbol)
                protection = _build_synthetic_protection_orders(
                    snap,
                    rules,
                    atr_value=atr_value,
                    current_price=cur_px,
                    today=today,
                    existing_exit_coids=existing_exit,
                )
            else:
                protection = _build_protection_orders(
                    snap,
                    rules,
                    atr_value=atr_value,
                    existing_protect_coids=existing_coids,
                )

        # (3) 優先順位: time/breakout の close order > protection 発注。
        # 既に同一 exit- coid が open (existing_exit) なら二重発注しない。
        if time_exit is not None and not _coid_already_open(time_exit, existing_exit):
            out.append(time_exit)
        if breakout_exit is not None and not _coid_already_open(
            breakout_exit, existing_exit
        ):
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
        raise ValueError(f"exit qty は正の数: {po.qty}")
    if po.side not in ("buy", "sell"):
        raise ValueError(f"exit side は 'buy'/'sell': {po.side!r}")
    # 端株 (整数株でない qty) は Alpaca 上、成行 DAY でしか約定できない。native
    # stop/limit/trailing を端株で送ると reject されるため、silent に落ちる前に
    # fail-fast する (回帰ガード)。builder は端株を market/day のみで生成する。
    _frac_qty = abs(float(po.qty) - round(float(po.qty))) > _QTY_WHOLE_TOL
    if _frac_qty and (po.order_type != "market" or po.time_in_force.lower() != "day"):
        raise ValueError(
            "端株 exit は成行DAYのみ: "
            f"{po.symbol} qty={po.qty} order_type={po.order_type} "
            f"tif={po.time_in_force}"
        )

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
        "Paper exit submitted: %s %s x%s %s id=%s status=%s reason=%s",
        po.side,
        po.symbol,
        po.qty,
        po.order_type,
        po.order_id,
        po.status,
        po.reason,
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


def fetch_existing_exit_coids(client: Any) -> set[str]:
    """Alpaca の open orders から exit- (time/breakout/synthetic) coid を集める。

    端株の synthetic クローズや time-exit の成行注文が同日再 run で二重発注
    されないよう、既に open な ``exit-`` 系 coid を dedup 材料として返す。
    エラー時は空集合 (safe fallback = Alpaca 側の 422 duplicate に委ねる)。
    """
    out: set[str] = set()
    try:
        orders = ba.get_open_orders(client)
    except Exception as exc:  # pragma: no cover
        logger.warning("open orders 取得失敗 (exit coids): %s", exc)
        return out
    for o in orders or []:
        try:
            coid = str(getattr(o, "client_order_id", "") or "")
            if coid.startswith("exit-"):
                out.add(coid)
        except Exception:
            continue
    return out


__all__ = [
    "PreparedOrder",
    "PreparedExit",
    "PositionSnapshot",
    "ExitReasonCode",
    "InvalidSideError",
    "LiveAccountGuardError",
    "OrderSubmitError",
    "PositionsFetchError",
    "TIER_NOTIONAL_USD",
    "SIZING_EQUITY_LINKED",
    "SIZING_FIXED_TIER",
    "DEFAULT_EQUITY_DEPLOY_PCT",
    "NotionalPlan",
    "compute_position_notionals",
    "fetch_account_equity",
    "resolve_sizing_equity",
    "assert_paper_env",
    "resolve_tier_notional",
    "submit_paper_order",
    "signals_to_orders",
    "signals_json_to_orders",
    "plan_order_execution",
    "get_asset_fractionable",
    "fetch_open_order_state",
    "HeldPositionCounts",
    "count_held_positions_by_system",
    "evaluate_standing_cap",
    "EXEC_NOTIONAL",
    "EXEC_QTY",
    "EXEC_SKIP",
    "parse_system_from_client_order_id",
    "parse_entry_date_from_client_order_id",
    "fetch_position_snapshots",
    "hydrate_system_tags",
    "compute_holding_days",
    "build_exit_orders_from_positions",
    "submit_paper_exit_order",
    "submit_paper_exit_orders",
    "fetch_existing_protect_coids",
    "fetch_existing_exit_coids",
]
