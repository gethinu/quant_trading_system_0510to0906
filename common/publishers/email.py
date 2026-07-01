"""SendGrid Email publisher (Phase 1 backup, Phase 2/3 で subscriber fan-out)。

SendGrid v3 REST API (無料 tier 100 通/日) で日次シグナルメールを送る。
Phase 1 は自分 1 宛先 (SENDGRID_TO_EMAIL)。Phase 2 で subscribers.json /
DB の宛先リストへ fan-out する下地として HTML + plaintext 両方を生成する。

env:
    SENDGRID_API_KEY   (required)
    SENDGRID_FROM_EMAIL(required, 要 verify)
    SENDGRID_TO_EMAIL  (required, カンマ区切りで複数可)
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

from common.publishers.base import Publisher, PublishResult, SignalMessage

logger = logging.getLogger(__name__)

_SENDGRID_ENDPOINT = "https://api.sendgrid.com/v3/mail/send"
_MAX_RETRIES = 3


class EmailPublisher(Publisher):
    """SendGrid REST API 経由の email publisher。"""

    name = "email"

    def __init__(
        self,
        *,
        api_key: str | None = None,
        from_email: str | None = None,
        to_emails: str | list[str] | None = None,
        timeout: float = 15.0,
    ) -> None:
        self.api_key = api_key or os.getenv("SENDGRID_API_KEY") or ""
        self.from_email = from_email or os.getenv("SENDGRID_FROM_EMAIL") or ""
        raw_to = to_emails if to_emails is not None else os.getenv("SENDGRID_TO_EMAIL") or ""
        if isinstance(raw_to, str):
            self.to_emails = [e.strip() for e in raw_to.split(",") if e.strip()]
        else:
            self.to_emails = list(raw_to)
        self.timeout = timeout

    def is_configured(self) -> bool:
        return bool(self.api_key and self.from_email and self.to_emails)

    # -- rendering ------------------------------------------------------
    @staticmethod
    def _narrative(message: SignalMessage) -> dict[str, Any]:
        """meta.narrative を取得 (base.py 不変のため payload から直接読む)。"""
        return (message.payload.get("meta") or {}).get("narrative") or {}

    def _render_text(self, message: SignalMessage) -> str:
        lines = message.system_summary_lines()
        body = "\n".join(lines) if lines else "(no signals today)"
        hedge = message.hedge
        hedge_str = (
            f"{hedge.get('side')} {hedge.get('symbol')}"
            if hedge and hedge.get("symbol")
            else "none"
        )
        narrative = self._narrative(message)
        narr_block = ""
        if narrative.get("headline") or narrative.get("summary"):
            narr_block = f"{narrative.get('headline', '')}\n{narrative.get('summary', '')}\n\n"
        return (
            f"{message.title()}\n\n{narr_block}{body}\n\n"
            f"portfolio: {message.total_signals} signals · hedge: {hedge_str}\n"
            f"{message.footer()}"
        )

    def _render_html(self, message: SignalMessage) -> str:
        warn = message.has_warnings()
        rows = "".join(
            f"<li style='margin:4px 0'>{line}</li>"
            for line in message.system_summary_lines()
        )
        hedge = message.hedge
        hedge_str = (
            f"{hedge.get('side')} {hedge.get('symbol')}"
            if hedge and hedge.get("symbol")
            else "none"
        )
        warn_banner = (
            "<div style='background:#facc15;color:#000;padding:6px 10px;"
            "border-radius:6px;margin-bottom:10px'>⚠️ gate 生存率が低い system があります</div>"
            if warn
            else ""
        )
        # AI narrative card (headline 大見出し + summary + per-symbol reasons)。
        narrative = self._narrative(message)
        narr_card = ""
        if narrative.get("headline") or narrative.get("summary"):
            reasons = narrative.get("per_symbol_reasons") or {}
            reason_rows = "".join(
                f"<li style='margin:2px 0'><b>{sym}</b>: {why}</li>"
                for sym, why in reasons.items()
            )
            reason_html = (
                f"<ul style='padding-left:18px;font-size:12px;color:#333'>{reason_rows}</ul>"
                if reason_rows
                else ""
            )
            narr_card = (
                "<div style='background:#eef2ff;border-left:4px solid #6366f1;"
                "padding:10px 12px;border-radius:6px;margin-bottom:12px'>"
                f"<div style='font-weight:700;font-size:15px;margin-bottom:4px'>"
                f"🧠 {narrative.get('headline', '')}</div>"
                f"<div style='font-size:13px;color:#333'>{narrative.get('summary', '')}</div>"
                f"{reason_html}"
                "</div>"
            )
        return (
            "<div style='font-family:-apple-system,Segoe UI,sans-serif;max-width:520px'>"
            f"<h2 style='margin:0 0 8px'>{message.title()}</h2>"
            f"{warn_banner}"
            f"{narr_card}"
            f"<ul style='padding-left:18px;font-size:14px'>{rows}</ul>"
            f"<p style='font-size:13px'>portfolio: <b>{message.total_signals}</b> "
            f"signals · hedge: {hedge_str}</p>"
            "<p style='margin-top:12px'><a href='https://quant-trading-monitor.vercel.app' "
            "style='color:#2563eb'>Open dashboard →</a></p>"
            f"<p style='color:#888;font-size:11px'>{message.footer()}</p>"
            "</div>"
        )

    def _build_payload(self, message: SignalMessage) -> dict[str, Any]:
        return {
            "personalizations": [
                {"to": [{"email": e} for e in self.to_emails]}
            ],
            "from": {"email": self.from_email, "name": "Quant Signals"},
            "subject": message.title(),
            "content": [
                {"type": "text/plain", "value": self._render_text(message)},
                {"type": "text/html", "value": self._render_html(message)},
            ],
        }

    # -- transport ------------------------------------------------------
    def send(self, signals_json: dict[str, Any], *, dry_run: bool = False) -> PublishResult:
        message = SignalMessage(payload=signals_json)
        payload = self._build_payload(message)
        target = ",".join(self.to_emails) or "unset"

        if dry_run:
            return PublishResult(
                publisher=self.name,
                ok=True,
                detail=json.dumps(payload, ensure_ascii=False),
                target=target or "dry-run",
            )

        if not self.is_configured():
            return PublishResult(
                publisher=self.name,
                ok=False,
                detail="SENDGRID_API_KEY / FROM / TO のいずれか未設定",
                target=target,
            )

        import requests

        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        last_detail = ""
        last_status: int | None = None
        for attempt in range(1, _MAX_RETRIES + 1):
            try:
                resp = requests.post(
                    _SENDGRID_ENDPOINT, json=payload, headers=headers, timeout=self.timeout
                )
                last_status = resp.status_code
                if 200 <= resp.status_code < 300:
                    return PublishResult(
                        publisher=self.name,
                        ok=True,
                        status_code=resp.status_code,
                        detail="sent",
                        target=target,
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    time.sleep(min(2 ** (attempt - 1), 8))
                    last_detail = f"retryable_{resp.status_code}"
                    continue
                last_detail = f"http_{resp.status_code}: {resp.text[:200]}"
                break
            except Exception as exc:  # noqa: BLE001
                last_detail = f"exception: {exc}"
                time.sleep(min(2 ** (attempt - 1), 8))

        return PublishResult(
            publisher=self.name,
            ok=False,
            status_code=last_status,
            detail=last_detail or "failed",
            target=target,
        )
