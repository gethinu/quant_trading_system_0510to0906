"""ntfy.sh publisher (Phase 1 primary)。

無料の ntfy.sh へ POST するだけで iPhone に push 通知が届く (アプリ 3 分 setup)。
topic 名 (= URL path) が事実上の secret なので、推測不能な文字列を env で渡す。

    POST https://ntfy.sh/{topic}
    headers: X-Title / X-Priority / X-Tags / Actions (dashboard へ jump)

無料 tier の rate limit (~5 msg/sec) に配慮し 429/5xx は指数バックオフで retry。
NTFY_TOPIC 未設定なら fail-fast (is_configured=False)。
"""

from __future__ import annotations

import logging
import os
import time
from typing import Any

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)

DASHBOARD_URL = "https://quant-trading-monitor.vercel.app"
_MAX_RETRIES = 4
# ntfy body は 4KB 程度が無難。長い summary は丸める。
_BODY_LIMIT = 3800


def _default_url() -> str:
    return os.getenv("NTFY_URL") or "https://ntfy.sh"


class NtfyPublisher(Publisher):
    name = "ntfy"

    def __init__(
        self,
        topic: str | None = None,
        *,
        base_url: str | None = None,
        priority: int | None = None,
        timeout: float = 10.0,
        dashboard_url: str = DASHBOARD_URL,
    ) -> None:
        self.topic = topic or os.getenv("NTFY_TOPIC") or ""
        self.base_url = (base_url or _default_url()).rstrip("/")
        try:
            self.priority = int(priority if priority is not None else os.getenv("NTFY_PRIORITY", 4))
        except (TypeError, ValueError):
            self.priority = 4
        self.timeout = timeout
        self.dashboard_url = dashboard_url

    def is_configured(self) -> bool:
        return bool(self.topic)

    @property
    def endpoint(self) -> str:
        return f"{self.base_url}/{self.topic}"

    # -- payload rendering ----------------------------------------------
    def _build(self, signals_json: dict[str, Any]) -> tuple[str, dict[str, str]]:
        message = SignalMessage(payload=signals_json)
        warn = message.has_warnings()

        lines = message.system_summary_lines()
        hedge = message.hedge
        hedge_str = (
            f"{hedge.get('side')} {hedge.get('symbol')}"
            if hedge and hedge.get("symbol")
            else "none"
        )
        body = "\n".join(lines) if lines else "(no signals today)"
        body += f"\n\nportfolio: {message.total_signals} signals · hedge: {hedge_str}"
        body += f"\n{message.footer()}"
        if len(body) > _BODY_LIMIT:
            body = body[: _BODY_LIMIT - 1] + "…"

        tags = "chart_with_upwards_trend"
        if warn:
            tags += ",warning"
        # urgent(5) if WARN else configured priority
        priority = 5 if warn else self.priority

        title = message.title()
        # ヘッダは ASCII 制約が安全。絵文字はタグ(X-Tags)側で表現し、title は素の text に。
        safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Today's Signals"

        headers = {
            "X-Title": safe_title,
            "X-Priority": str(priority),
            "X-Tags": tags,
            # iPhone 通知から dashboard を開くアクションボタン
            "X-Actions": f"view, Open dashboard, {self.dashboard_url}, clear=true",
        }
        return body, headers

    # -- transport ------------------------------------------------------
    def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
        body, headers = self._build(signals_json)

        if dry_run:
            return PublishResult(
                publisher=self.name,
                ok=True,
                detail=_dump_dry_run(self.endpoint, headers, body),
                target=self.topic or "dry-run",
            )

        if not self.is_configured():
            return PublishResult(
                publisher=self.name, ok=False, detail="NTFY_TOPIC 未設定", target="unset"
            )

        import requests

        last_detail = ""
        last_status: int | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    self.endpoint,
                    data=body.encode("utf-8"),
                    headers=headers,
                    timeout=self.timeout,
                )
                last_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    return PublishResult(
                        publisher=self.name,
                        ok=True,
                        status_code=resp.status_code,
                        detail="sent",
                        target=self.topic,
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    backoff = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        "ntfy %d (attempt %d) backoff=%ss", resp.status_code, attempt, backoff
                    )
                    time.sleep(backoff)
                    last_detail = f"retryable_{resp.status_code}"
                    continue
                last_detail = f"http_{resp.status_code}: {resp.text[:200]}"
                break
            except Exception as exc:  # noqa: BLE001
                backoff = min(2 ** (attempt - 1), 8)
                last_detail = f"exception: {exc}"
                logger.warning("ntfy post 例外 (attempt %d): %s backoff=%ss", attempt, exc, backoff)
                time.sleep(backoff)

        return PublishResult(
            publisher=self.name,
            ok=False,
            status_code=last_status,
            detail=last_detail or "failed",
            target=self.topic,
        )


def _dump_dry_run(endpoint: str, headers: dict[str, str], body: str) -> str:
    import json

    return json.dumps(
        {"endpoint": endpoint, "headers": headers, "body": body}, ensure_ascii=False
    )
