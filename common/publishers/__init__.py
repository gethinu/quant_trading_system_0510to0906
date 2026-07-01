"""配信 publisher パッケージ (Phase 1: Discord/Webhook, Phase 2: LINE/Email)。

    from common.publishers import Publisher, SignalMessage, build_publisher

`build_publisher(kind, **cfg)` で kind ("discord"|"webhook"|"line"|"email")
から具体 publisher を生成する factory。subscribers.json の channel type を
そのまま kind として渡せる。
"""

from __future__ import annotations

from typing import Any

from common.publishers.base import (
    Publisher,
    PublishResult,
    SignalMessage,
    WARN_SURVIVAL_THRESHOLD,
)
from common.publishers.discord import DiscordPublisher
from common.publishers.email import EmailPublisher
from common.publishers.line import LinePublisher
from common.publishers.webhook import WebhookPublisher

_REGISTRY: dict[str, type[Publisher]] = {
    "discord": DiscordPublisher,
    "webhook": WebhookPublisher,
    "line": LinePublisher,
    "email": EmailPublisher,
}


def build_publisher(kind: str, **cfg: Any) -> Publisher:
    """kind から publisher を生成する factory。未知 kind は ValueError。"""
    cls = _REGISTRY.get(str(kind).lower())
    if cls is None:
        raise ValueError(
            f"未知の publisher kind: {kind!r} (対応: {sorted(_REGISTRY)})"
        )
    return cls(**cfg)


__all__ = [
    "Publisher",
    "PublishResult",
    "SignalMessage",
    "DiscordPublisher",
    "WebhookPublisher",
    "LinePublisher",
    "EmailPublisher",
    "build_publisher",
    "WARN_SURVIVAL_THRESHOLD",
]
