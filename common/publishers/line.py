"""LINE publisher (Phase 2 skeleton)。

LINE Messaging API / Notify で subscriber へ push する想定。Phase 2 で
per-subscriber の LINE user_id または group_id を subscribers DB から引き、
channel access token で push する。Phase 1 では interface のみ用意。
"""

from __future__ import annotations

import logging
import os

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)


class LinePublisher(Publisher):
    name = "line"

    def __init__(
        self,
        access_token: str | None = None,
        *,
        to: str | None = None,
        env_var: str = "LINE_CHANNEL_ACCESS_TOKEN",
    ) -> None:
        self.access_token = access_token or os.getenv(env_var) or ""
        self.to = to  # LINE user_id / group_id (Phase 2 で subscribers から)

    def is_configured(self) -> bool:
        return bool(self.access_token and self.to)

    def publish(
        self, message: SignalMessage, *, dry_run: bool = False
    ) -> PublishResult:
        # Phase 2 実装予定: LINE Messaging API push
        detail = (
            "LinePublisher は Phase 2 で実装予定 (LINE Messaging API push)。"
            f" would send run_id={message.run_id} to={self.to}"
        )
        logger.info(detail)
        return PublishResult(
            publisher=self.name,
            ok=dry_run,  # dry_run では ok 扱い、本番は未実装として False
            detail=detail,
            target=str(self.to or "unset"),
        )
