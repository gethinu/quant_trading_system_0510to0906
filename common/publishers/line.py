"""LINE publisher (Phase 2 skeleton)。

Publisher ABC の拡張性デモも兼ねる。Phase 2 で LINE Messaging API push を
実装する (per-subscriber の user_id / group_id を subscribers DB から解決)。
新 publisher は send / is_configured の 2 メソッドを実装し registry に登録する
だけで chain に組み込める。
"""

from __future__ import annotations

import logging
import os
from typing import Any

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
        self.to = to  # Phase 2 で subscribers から解決

    def is_configured(self) -> bool:
        return bool(self.access_token and self.to)

    def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
        message = SignalMessage(payload=signals_json)
        detail = (
            "LinePublisher は Phase 2 で実装予定 (LINE Messaging API push)。"
            f" would send run_id={message.run_id} to={self.to}"
        )
        logger.info(detail)
        return PublishResult(
            publisher=self.name,
            ok=dry_run,
            detail=detail,
            target=str(self.to or "unset"),
        )
