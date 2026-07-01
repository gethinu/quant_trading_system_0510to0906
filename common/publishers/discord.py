"""Discord Webhook publisher (Phase 1 実装)。

Discord incoming webhook へ当日シグナル summary を送る。
- rate limit: 1 webhook あたり 5 req / 2 sec。429 は ``retry_after`` を尊重。
- 5xx は指数バックオフで retry。
- ``run_id`` を embed footer に載せ、重複配信を検出可能にする。

dry_run=True の場合は HTTP を投げず、生成した payload を PublishResult.detail
(JSON 文字列) に載せて返す。unit test はこの経路で payload を検証する。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)

# Discord embed color (0x = decimal)
_COLOR_OK = 0x4ADE80
_COLOR_WARN = 0xFACC15
_MAX_RETRIES = 4
_DISCORD_FIELD_LIMIT = 1024  # 1 field value の最大長


def _mask(url: str) -> str:
    if not url:
        return ""
    if len(url) <= 24:
        return url[:8] + "…"
    return url[:30] + "…" + url[-6:]


class DiscordPublisher(Publisher):
    name = "discord"

    def __init__(
        self,
        webhook_url: str | None = None,
        *,
        env_var: str = "DISCORD_WEBHOOK_URL",
        username: str = "Quant Signals",
        timeout: float = 10.0,
        target_label: str = "",
    ) -> None:
        self.webhook_url = webhook_url or os.getenv(env_var) or ""
        self.username = username
        self.timeout = timeout
        self.target_label = target_label or _mask(self.webhook_url)

    def is_configured(self) -> bool:
        return bool(self.webhook_url)

    # -- payload rendering ----------------------------------------------
    def _build_payload(self, message: SignalMessage) -> dict[str, Any]:
        warn = message.has_warnings()
        lines = message.system_summary_lines()
        # Discord field value は 1024 文字上限。超えたら丸める。
        body = "\n".join(lines) if lines else "(no systems)"
        if len(body) > _DISCORD_FIELD_LIMIT:
            body = body[: _DISCORD_FIELD_LIMIT - 3] + "…"

        hedge = message.hedge
        hedge_str = (
            f"{hedge.get('side')} {hedge.get('symbol')}"
            if hedge and hedge.get("symbol")
            else "none"
        )

        title = message.title()
        if warn:
            title = "⚠️ " + title

        embed = {
            "title": title,
            "color": _COLOR_WARN if warn else _COLOR_OK,
            "fields": [
                {"name": "Systems", "value": body, "inline": False},
                {
                    "name": "Portfolio",
                    "value": f"{message.total_signals} signals · hedge: {hedge_str}",
                    "inline": False,
                },
            ],
            "footer": {"text": message.footer()},
        }
        return {"username": self.username, "embeds": [embed]}

    # -- transport ------------------------------------------------------
    def publish(
        self, message: SignalMessage, *, dry_run: bool = False
    ) -> PublishResult:
        payload = self._build_payload(message)

        if dry_run:
            return PublishResult(
                publisher=self.name,
                ok=True,
                status_code=None,
                detail=json.dumps(payload, ensure_ascii=False),
                target=self.target_label or "dry-run",
            )

        if not self.is_configured():
            return PublishResult(
                publisher=self.name,
                ok=False,
                detail="DISCORD_WEBHOOK_URL 未設定",
                target="unset",
            )

        import requests  # 遅延 import (dry_run / test で requests 不要)

        last_detail = ""
        last_status: int | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    self.webhook_url, json=payload, timeout=self.timeout
                )
                last_status = resp.status_code
                if resp.status_code in (200, 204):
                    return PublishResult(
                        publisher=self.name,
                        ok=True,
                        status_code=resp.status_code,
                        detail="sent",
                        target=self.target_label,
                    )
                if resp.status_code == 429:
                    # rate limited: retry_after (秒) を尊重
                    try:
                        retry_after = float(
                            resp.json().get("retry_after", 1.0)
                        )
                    except Exception:
                        retry_after = 1.0
                    logger.warning(
                        "Discord 429 rate limited, retry_after=%.2fs (attempt %d)",
                        retry_after,
                        attempt,
                    )
                    time.sleep(min(retry_after, 10.0))
                    last_detail = "rate_limited_429"
                    continue
                if 500 <= resp.status_code < 600:
                    backoff = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        "Discord %d server error, backoff=%ss (attempt %d)",
                        resp.status_code,
                        backoff,
                        attempt,
                    )
                    time.sleep(backoff)
                    last_detail = f"server_error_{resp.status_code}"
                    continue
                # その他 4xx は retry しない (fail-fast)
                last_detail = f"http_{resp.status_code}: {resp.text[:200]}"
                break
            except Exception as exc:  # noqa: BLE001 - network 例外は retry
                backoff = min(2 ** (attempt - 1), 8)
                last_detail = f"exception: {exc}"
                logger.warning(
                    "Discord post 例外 (attempt %d): %s — backoff %ss",
                    attempt,
                    exc,
                    backoff,
                )
                time.sleep(backoff)

        return PublishResult(
            publisher=self.name,
            ok=False,
            status_code=last_status,
            detail=last_detail or "failed",
            target=self.target_label,
        )
