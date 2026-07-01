"""汎用 Webhook publisher (Phase 1 実装、Slack/自前エンドポイント兼用)。

任意の JSON エンドポイントへ ``SignalMessage`` を POST する。Discord 固有の
embed を使わず、schema v1.0 の payload に summary テキストを添えて送る素朴な形。
Phase 2 で自前バックエンド (subscribers DB / 配信 API) に差し替えやすいよう
最小限の実装に留める。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)

_MAX_RETRIES = 3


class WebhookPublisher(Publisher):
    name = "webhook"

    def __init__(
        self,
        url: str | None = None,
        *,
        env_var: str = "SIGNALS_WEBHOOK_URL",
        timeout: float = 10.0,
        target_label: str = "",
    ) -> None:
        self.url = url or os.getenv(env_var) or ""
        self.timeout = timeout
        self.target_label = target_label or (self.url[:40] + "…" if self.url else "unset")

    def is_configured(self) -> bool:
        return bool(self.url)

    def _build_payload(self, message: SignalMessage) -> dict[str, Any]:
        return {
            "type": "today_signals",
            "run_id": message.run_id,
            "date": message.date,
            "summary": message.system_summary_lines(),
            "warn": message.has_warnings(),
            "signals": message.payload,
        }

    def publish(
        self, message: SignalMessage, *, dry_run: bool = False
    ) -> PublishResult:
        payload = self._build_payload(message)
        if dry_run:
            return PublishResult(
                publisher=self.name,
                ok=True,
                detail=json.dumps(payload, ensure_ascii=False),
                target=self.target_label or "dry-run",
            )
        if not self.is_configured():
            return PublishResult(
                publisher=self.name, ok=False, detail="webhook URL 未設定", target="unset"
            )

        import requests

        last_detail = ""
        last_status: int | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(self.url, json=payload, timeout=self.timeout)
                last_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    return PublishResult(
                        publisher=self.name,
                        ok=True,
                        status_code=resp.status_code,
                        detail="sent",
                        target=self.target_label,
                    )
                if resp.status_code >= 500:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    last_detail = f"server_error_{resp.status_code}"
                    continue
                last_detail = f"http_{resp.status_code}"
                break
            except Exception as exc:  # noqa: BLE001
                last_detail = f"exception: {exc}"
                time.sleep(min(2 ** (attempt - 1), 8))

        return PublishResult(
            publisher=self.name,
            ok=False,
            status_code=last_status,
            detail=last_detail or "failed",
            target=self.target_label,
        )
