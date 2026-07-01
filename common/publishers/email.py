"""Email publisher (Phase 2 skeleton)。

SMTP または SendGrid/SES 等で subscriber へ日次シグナルメールを送る想定。
Phase 2 で HTML template + per-subscriber 宛先 (subscribers DB) を実装する。
Phase 1 では interface のみ用意。
"""

from __future__ import annotations

import logging
import os

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)


class EmailPublisher(Publisher):
    name = "email"

    def __init__(
        self,
        *,
        to: str | None = None,
        smtp_host: str | None = None,
        smtp_user_env: str = "SMTP_USER",
        smtp_pass_env: str = "SMTP_PASSWORD",
    ) -> None:
        self.to = to
        self.smtp_host = smtp_host or os.getenv("SMTP_HOST") or ""
        self.smtp_user = os.getenv(smtp_user_env) or ""
        self.smtp_pass = os.getenv(smtp_pass_env) or ""

    def is_configured(self) -> bool:
        return bool(self.smtp_host and self.smtp_user and self.to)

    def _render_html(self, message: SignalMessage) -> str:
        rows = "".join(f"<li>{line}</li>" for line in message.system_summary_lines())
        return (
            f"<h2>{message.title()}</h2><ul>{rows}</ul>"
            f"<p style='color:#888'>{message.footer()}</p>"
        )

    def publish(
        self, message: SignalMessage, *, dry_run: bool = False
    ) -> PublishResult:
        # Phase 2 実装予定: SMTP/SendGrid で self._render_html() を送信
        detail = (
            "EmailPublisher は Phase 2 で実装予定 (SMTP/SendGrid)。"
            f" would email run_id={message.run_id} to={self.to}"
        )
        logger.info(detail)
        return PublishResult(
            publisher=self.name,
            ok=dry_run,
            detail=detail,
            target=str(self.to or "unset"),
        )
