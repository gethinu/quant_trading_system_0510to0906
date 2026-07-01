"""Twitter/X publisher (Phase 2 skeleton)。

Phase 2 で「同じ narrative を 1 LLM call で self + subscriber + public の 3 用途に
fan-out」する public チャネル。当日シグナルの ``meta.narrative.headline`` +
上位 signals を 1 tweet (280 字) に要約して投稿する下地。

いまは skeleton (is_configured() が env 未設定なら False)。send / is_configured の
2 メソッドを実装し registry に登録するだけで chain に組み込める (Publisher ABC 不変)。
"""

from __future__ import annotations

import logging
import os
from typing import Any

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)


class TwitterPublisher(Publisher):
    name = "twitter"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        api_secret: str | None = None,
        access_token: str | None = None,
        access_secret: str | None = None,
    ) -> None:
        self.api_key = api_key or os.getenv("TWITTER_API_KEY") or ""
        self.api_secret = api_secret or os.getenv("TWITTER_API_SECRET") or ""
        self.access_token = access_token or os.getenv("TWITTER_ACCESS_TOKEN") or ""
        self.access_secret = access_secret or os.getenv("TWITTER_ACCESS_SECRET") or ""

    def is_configured(self) -> bool:
        # Phase 2 で実装。今は skeleton につき常に False (chain に載っても no-op)。
        return False

    def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
        message = SignalMessage(payload=signals_json)
        narrative = (signals_json.get("meta") or {}).get("narrative") or {}
        headline = str(narrative.get("headline") or message.title())
        preview = f"{headline} — {message.total_signals} signals (auto)"
        detail = (
            "TwitterPublisher は Phase 2 で実装予定 (X API v2 tweet)。"
            f" would post: {preview[:280]!r}"
        )
        logger.info(detail)
        return PublishResult(
            publisher=self.name,
            ok=dry_run,
            detail=detail,
            target="@public" if dry_run else "unconfigured",
        )
