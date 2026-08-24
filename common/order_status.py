"""Alpaca order status の正規化 (stdlib のみ / broker へは触れない)。

なぜ独立モジュールなのか
------------------------
``OrderStatus`` は素の ``Enum`` なので ``str(OrderStatus.FILLED)`` は
``"filled"`` ではなく ``"OrderStatus.FILLED"`` になる。artifact を
``json.dumps(..., default=str)`` で書くとこの前置き付きの文字列が JSON に
焼き付き、後段が ``status == "filled"`` で突合すると **絶対に一致しない**。

2026-08-20 はこれで entry 47/47 が fill しているのに recon の
``entry_filled`` が 0 に固定された。書き手 (``common.alpaca_order``)・
待ち手 (``scripts.open_auto_run``)・読み手 (``scripts.build_execution_recon``)
が同じ正規化を共有するために切り出す。alpaca-py にも pandas にも依存しない
ので、SDK が無い環境の recon からも安全に import できる。
"""

from __future__ import annotations

from typing import Any

# 既知の終端 status。未知 status を終端扱いすると broker 観測を早期終了して
# settled と誤認するため、is_working はこの denylist だけを停止条件にする。
TERMINAL_ORDER_STATUSES = frozenset(
    {
        "filled",
        "done_for_day",
        "canceled",
        "cancelled",  # legacy artifact / broker spelling variation
        "expired",
        "replaced",
        "rejected",
    }
)

# 現在知られている non-terminal status。公開定数として残すが、判定自体は
# terminal denylist を使う。stopped / suspended / 将来の未知値も観測を継続する。
WORKING_ORDER_STATUSES = frozenset(
    {
        "new",
        "accepted",
        "pending_new",
        "partially_filled",
        "held",
        "accepted_for_bidding",
        "pending_replace",
        "pending_review",
        "calculated",
        "pending_cancel",
    }
)

# fill とみなす status。
FILLED_ORDER_STATUSES = frozenset({"filled", "partially_filled"})


def normalize_order_status(raw: Any) -> str:
    """order status を素の小文字トークンへ正規化する。

    ``OrderStatus.FILLED`` (enum) / ``"OrderStatus.FILLED"`` (default=str で
    serialize された残骸) / ``"FILLED"`` / ``"filled"`` のどれでも ``"filled"``
    を返す。None / 空は ``""``。

    >>> normalize_order_status("OrderStatus.PENDING_NEW")
    'pending_new'
    >>> normalize_order_status(None)
    ''
    """
    if raw is None:
        return ""
    value = getattr(raw, "value", None)
    text = str(value if value is not None else raw).strip()
    if not text or text.lower() == "none":
        return ""
    # "OrderStatus.PENDING_NEW" -> "PENDING_NEW"
    return text.rsplit(".", 1)[-1].lower()


def is_working(raw: Any) -> bool:
    """known-terminal でなければ、まだ再 poll する価値があるとみなす。"""
    status = normalize_order_status(raw)
    return status not in TERMINAL_ORDER_STATUSES


def is_filled(raw: Any) -> bool:
    """fill (全部/一部) とみなせるか。"""
    return normalize_order_status(raw) in FILLED_ORDER_STATUSES
