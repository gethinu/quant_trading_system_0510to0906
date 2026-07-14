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
# X-Title の実用上の最大長。iPhone 通知の 1 行目に収まる目安。
_TITLE_LIMIT = 120


def _to_safe_ascii_title(title: str, message: SignalMessage) -> str:
    """ntfy X-Title 用の安全な ASCII タイトルを組み立てる (最終防波堤)。

    narrator.py 側で headline は ASCII+emoji + <=50 字に validation され
    synth 済みだが、上流の変更・fallback 経路・古い narrative_YYYYMMDD.json
    再送などで日本語 headline が渡ってくる可能性は残る。ここでは:

      1. 元 title が ASCII 保持率 60% 以上ならそのまま採用 (narrator の
         synth 済 headline はここを通る)。
      2. 主に非 ASCII (日本語 headline 等) の場合は narrator を捨て、
         portfolio 統計から決定論的な ASCII を synth する:
             "YYYY-MM-DD | N signals | BUY x / SELL y | $Zk"
      3. 何も synth できない場合の最終 fallback は "Today's Signals"。

    2026-07-02 incident: 単純に encode("ascii", "ignore") だと
    「7系統49シグナル、BUY主流…」→「749BUYSELL10100%3」に潰れて読めない。
    """
    stripped = (title or "").encode("ascii", "ignore").decode("ascii")
    ascii_only = stripped.strip()
    original = (title or "").strip()

    # (1) 元 title が ASCII 主体 (非 ASCII 抜きで 60% 以上残る + 8 char 以上
    #     + 英字含む) ならそのまま採用。narrator の synth 済 headline
    #     (絵文字 + ASCII) はこの分岐に来る。
    if original and ascii_only:
        preserved = len(ascii_only) / max(len(original), 1)
        has_word_char = any(c.isalpha() for c in ascii_only)
        if preserved >= 0.6 and len(ascii_only) >= 8 and has_word_char:
            return ascii_only[:_TITLE_LIMIT]

    # (2) 構造化 ASCII を synth。portfolio から総数 / BUY / SELL / notional。
    parts: list[str] = []
    if message.date:
        parts.append(message.date)
    total = message.total_signals
    if total > 0:
        parts.append(f"{total} signals")

    buy, sell = 0, 0
    for cfg in (message.systems or {}).values():
        for s in cfg.get("signals", []) or []:
            side = str(s.get("side") or "").upper()
            if side == "BUY":
                buy += 1
            elif side == "SELL":
                sell += 1
    if buy or sell:
        parts.append(f"BUY {buy} / SELL {sell}")

    notional = float(
        (message.payload.get("portfolio", {}) or {}).get("total_notional_usd", 0) or 0
    )
    if notional > 0:
        if notional >= 1_000_000:
            parts.append(f"${notional / 1_000_000:.1f}M")
        elif notional >= 1_000:
            parts.append(f"${notional / 1_000:.0f}K")
        else:
            parts.append(f"${notional:.0f}")

    if parts:
        return " | ".join(parts)[:_TITLE_LIMIT]
    return "Today's Signals"


def _sanitize_ascii_title(title: str) -> str:
    """任意 title を ntfy X-Title 用に印字可能 ASCII + emoji のみへ絞る。

    ``_to_safe_ascii_title`` は SignalMessage を必要とするが、こちらは
    execution summary など payload を持たない汎用 title 用の軽量版。日本語などの
    非 ASCII は落とす (iPhone 通知が非 ASCII を strip して mangle するのを防ぐ)。
    """
    kept: list[str] = []
    for ch in title or "":
        cp = ord(ch)
        if 0x20 <= cp <= 0x7E:  # printable ASCII (space 含む)
            kept.append(ch)
        elif 0x2600 <= cp <= 0x27BF or 0x1F300 <= cp <= 0x1FAFF or cp == 0xFE0F:
            kept.append(ch)  # emoji ブロック
    out = "".join(kept).strip()
    # 連続 space を 1 つに圧縮 (非 ASCII を落とした跡の空白を整理)
    out = " ".join(out.split())
    if not out:
        return "Execution Summary"
    return out[:_TITLE_LIMIT]


def _latin1_safe_headers(headers: dict[str, str]) -> dict[str, str]:
    """HTTP ヘッダー値を latin-1 エンコード可能に落とす最終防波堤。

    requests / urllib3 は HTTP ヘッダーを latin-1 でエンコードする。X-Title に
    emoji 等の非 latin-1 文字が入ると ``requests.post`` が
    ``'latin-1' codec can't encode`` を投げ、ntfy 送信が丸ごと失敗する。

    2026-07-13 incident: open_auto_run の exec summary が title
    「⚠️ 07-13 exec …」(先頭 emoji) で 4 retry 全滅し、通知が無音で消えた。
    ``_sanitize_ascii_title`` は仕様上 emoji を保持する (build_title の
    📊/⚠️ を残す) ため、送信直前のこの層で非 latin-1 文字を落とす。emoji の
    視覚情報は X-Tags (shortcode = bar_chart/warning) 側が担うので、title から
    emoji が落ちても通知アイコンは維持される。

    非 latin-1 を含む値は印字可能 ASCII (0x20-0x7E) のみへ絞り、空白を圧縮する
    (制御文字・改行はヘッダーに不正なため除外)。空になったら "notification"。
    """
    safe: dict[str, str] = {}
    for key, value in headers.items():
        s = str(value)
        try:
            s.encode("latin-1")
            safe[key] = s
            continue
        except UnicodeEncodeError:
            kept = "".join(ch for ch in s if 0x20 <= ord(ch) <= 0x7E)
            cleaned = " ".join(kept.split())
            safe[key] = cleaned or "notification"
    return safe


def _mask_topic(topic: str | None) -> str:
    """Return an unguessable-shape but useful-in-logs marker for the ntfy topic.

    NOTE(F2 P0#3 audit fix, 2026-07-03): the topic acts as the secret access
    token for the ntfy channel — anyone who has it can subscribe to (or
    spoof-push into) the operator's push feed. Previously the full topic
    flowed into ``PublishResult.target`` and the dry-run detail, which are
    persisted in logs and (via ``meta.publish_status``) in the exported
    signals JSON that subscribers can download. We now expose only a short
    prefix + length so operators can still distinguish channels.
    """
    if not topic:
        return "unset"
    return f"{topic[:3]}…({len(topic)})"


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
        # None (default) → fall back to env; explicit "" → respect empty (used
        # by callers to test the "unconfigured / dry-run only" code path without
        # having an ambient NTFY_TOPIC env var leak in).
        if topic is None:
            topic = os.getenv("NTFY_TOPIC") or ""
        self.topic = topic
        self.base_url = (base_url or _default_url()).rstrip("/")
        try:
            self.priority = int(
                priority if priority is not None else os.getenv("NTFY_PRIORITY", 4)
            )
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
        # narrative (optional): headline + summary を本文冒頭に (UTF-8 で iPhone に表示)
        headline = message.narrative_headline()
        summary = message.narrative_summary()
        if headline or summary:
            narrative_block = "\n".join(p for p in (headline, summary) if p)
            body = f"{narrative_block}\n\n{body}"
        body += f"\n\nportfolio: {message.total_signals} signals · hedge: {hedge_str}"
        body += f"\n{message.footer()}"
        if len(body) > _BODY_LIMIT:
            body = body[: _BODY_LIMIT - 1] + "…"

        tags = "chart_with_upwards_trend"
        if warn:
            tags += ",warning"
        # urgent(5) if WARN else configured priority
        priority = 5 if warn else self.priority

        # narrator.headline を優先し X-Title に (無ければ既存 title)。
        # narrator.py 側で ASCII+emoji + <=50 字に validation 済 headline が
        # 渡ってくる想定だが、古い narrative_YYYYMMDD.json や API 経路の
        # 経緯で日本語が来る可能性を残すため _to_safe_ascii_title で最終
        # 防波堤を通す (2026-07-02 mangled-title incident regression 防止)。
        title = message.narrative_headline() or message.title()
        safe_title = _to_safe_ascii_title(title, message)

        headers = {
            "X-Title": safe_title,
            "X-Priority": str(priority),
            "X-Tags": tags,
            # iPhone 通知から dashboard を開くアクションボタン
            "X-Actions": f"view, Open dashboard, {self.dashboard_url}, clear=true",
        }
        return body, headers

    # -- text sending (execution summary 等、signals JSON 以外の通知用) ----
    def send_text(
        self,
        title: str,
        body: str,
        *,
        tags: str = "chart_with_upwards_trend",
        priority: int | None = None,
        dry_run: bool = False,
    ) -> PublishResult:
        """任意の title/body を ntfy へ送信する (signal publish 以外の汎用経路)。

        execution summary (submit 後の実発注サマリ) など、SignalMessage schema に
        乗らない通知を、既存の retry / topic masking / dashboard action ボタンを
        再利用して送るための入口。title は ASCII+emoji に sanitize する
        (iPhone X-Title は非 ASCII を strip するため)。
        """
        safe_title = _sanitize_ascii_title(title)
        if len(body) > _BODY_LIMIT:
            body = body[: _BODY_LIMIT - 1] + "…"
        headers = {
            "X-Title": safe_title,
            "X-Priority": str(priority if priority is not None else self.priority),
            "X-Tags": tags,
            "X-Actions": f"view, Open dashboard, {self.dashboard_url}, clear=true",
        }
        return self._transport(body, headers, dry_run=dry_run)

    # -- transport ------------------------------------------------------
    def send(
        self, signals_json: dict[str, Any], *, dry_run: bool = False
    ) -> PublishResult:
        body, headers = self._build(signals_json)
        return self._transport(body, headers, dry_run=dry_run)

    def _transport(
        self, body: str, headers: dict[str, str], *, dry_run: bool = False
    ) -> PublishResult:
        """組み立て済み body/headers を ntfy へ POST する (retry + masking 共通処理)。"""
        if dry_run:
            return PublishResult(
                publisher=self.name,
                ok=True,
                detail=_dump_dry_run(self.endpoint, headers, body),
                target=_mask_topic(self.topic) if self.topic else "dry-run",
            )

        if not self.is_configured():
            return PublishResult(
                publisher=self.name,
                ok=False,
                detail="NTFY_TOPIC 未設定",
                target="unset",
            )

        import requests

        # 最終防波堤: 非 latin-1 (emoji 等) が X-Title に残っていると requests が
        # ヘッダー encode で例外を投げ、送信が丸ごと落ちる (2026-07-13 incident)。
        headers = _latin1_safe_headers(headers)

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
                        target=_mask_topic(self.topic),
                    )
                if resp.status_code == 429 or resp.status_code >= 500:
                    backoff = min(2 ** (attempt - 1), 8)
                    logger.warning(
                        "ntfy %d (attempt %d) backoff=%ss",
                        resp.status_code,
                        attempt,
                        backoff,
                    )
                    time.sleep(backoff)
                    last_detail = f"retryable_{resp.status_code}"
                    continue
                last_detail = f"http_{resp.status_code}: {resp.text[:200]}"
                break
            except Exception as exc:  # noqa: BLE001
                backoff = min(2 ** (attempt - 1), 8)
                last_detail = f"exception: {exc}"
                logger.warning(
                    "ntfy post 例外 (attempt %d): %s backoff=%ss",
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
            target=_mask_topic(self.topic),
        )


def _dump_dry_run(endpoint: str, headers: dict[str, str], body: str) -> str:
    import json

    # Mask the topic segment inside the endpoint URL too — otherwise dry-run
    # detail (persisted in PublishResult.detail) leaks the same secret we
    # just masked out of `target`. See F2 P0#3 audit fix.
    from urllib.parse import urlsplit, urlunsplit

    try:
        parts = urlsplit(endpoint)
        if parts.path:
            segments = parts.path.strip("/").split("/")
            if segments:
                segments[-1] = _mask_topic(segments[-1])
            masked_path = "/" + "/".join(segments)
            endpoint = urlunsplit(parts._replace(path=masked_path))
    except Exception:
        # Never fail dry-run rendering on an unparseable URL; better to
        # ship the raw endpoint than crash the publisher.
        pass
    return json.dumps(
        {"endpoint": endpoint, "headers": headers, "body": body}, ensure_ascii=False
    )
