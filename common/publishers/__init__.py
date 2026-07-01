"""配信 publisher パッケージ。

Phase 1: ntfy.sh (primary) + SendGrid Email (backup)。
Phase 2/3: 同じ Publisher ABC の下で Discord / LINE Messaging API / SMS / Slack を
いつでも追加可能 (registry に登録するだけ)。

    from common.publishers import build_publisher, PublisherRegistry, SignalMessage

`build_publisher(kind, **cfg)` で kind ("ntfy"|"email"|"line") から publisher を生成。
"""

from __future__ import annotations

from typing import Any

from common.publishers.base import (
    Publisher,
    PublishResult,
    SignalMessage,
    WARN_SURVIVAL_THRESHOLD,
)
from common.publishers.email import EmailPublisher
from common.publishers.line import LinePublisher
from common.publishers.ntfy import NtfyPublisher
from common.publishers.registry import PublisherRegistry, RegistryResult
from common.publishers.substack import SubstackPublisher
from common.publishers.twitter import TwitterPublisher

_REGISTRY: dict[str, type[Publisher]] = {
    "ntfy": NtfyPublisher,
    "email": EmailPublisher,
    "line": LinePublisher,
    # Phase 2 skeleton: 同じ narrative を public/subscriber へ fan-out する下地。
    "twitter": TwitterPublisher,
    "substack": SubstackPublisher,
}


def build_publisher(kind: str, **cfg: Any) -> Publisher:
    """kind から publisher を生成する factory。未知 kind は ValueError。"""
    cls = _REGISTRY.get(str(kind).lower())
    if cls is None:
        raise ValueError(f"未知の publisher kind: {kind!r} (対応: {sorted(_REGISTRY)})")
    return cls(**cfg)


__all__ = [
    "Publisher",
    "PublishResult",
    "SignalMessage",
    "NtfyPublisher",
    "EmailPublisher",
    "LinePublisher",
    "TwitterPublisher",
    "SubstackPublisher",
    "PublisherRegistry",
    "RegistryResult",
    "build_publisher",
    "WARN_SURVIVAL_THRESHOLD",
]
