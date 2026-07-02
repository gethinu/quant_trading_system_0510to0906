"""当日シグナル JSON を配信する (Phase 1: ntfy.sh primary + SendGrid Email backup)。

``results_csv/today_signals_YYYYMMDD.json`` を読み、PublisherRegistry で
primary→backup の chain 配信を行う。すべて失敗したら signals JSON の
``meta.publish_status`` に "failed" を書き戻し、Vercel dashboard 側で検知可能にする。

Usage:
    python scripts/publish_signals.py --date 2026-07-01                 # ntfy (default)
    python scripts/publish_signals.py --input <json> --publisher all    # ntfy + email 並列
    python scripts/publish_signals.py --date 2026-07-01 --fallback      # ntfy 失敗時 email
    python scripts/publish_signals.py --dry-run --publisher email       # 送信せず payload 検証

Exit codes:
    0 : 配信成功 (status=ok/partial) または dry-run
    1 : 実行時エラー (入力欠損 / 全 publisher 未設定)
    2 : 全宛先で配信失敗 (status=failed)
"""

from __future__ import annotations

import argparse
from datetime import datetime
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.publishers import (  # noqa: E402
    EmailPublisher,
    NtfyPublisher,
    PublisherRegistry,
    SignalMessage,
)

logger = logging.getLogger(__name__)


def _default_input_path(date_str: str) -> Path:
    return Path("results_csv") / f"today_signals_{date_str.replace('-', '')}.json"


def load_payload(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"signals JSON が見つかりません: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _default_narrative_path(signals_path: Path) -> Path:
    """today_signals_YYYYMMDD.json と同じ dir の narrative_YYYYMMDD.json。"""
    name = signals_path.name.replace("today_signals_", "narrative_")
    return signals_path.with_name(name)


def merge_narrative(payload: dict, narrative_path: Path | None, signals_path: Path) -> None:
    """narrative JSON を payload['narrative'] に merge (optional, best-effort)。

    明示 path が無ければ signals と同じ dir の narrative_YYYYMMDD.json を探す。
    見つからない/壊れていても publish は既存 body で継続する。"""
    path = narrative_path or _default_narrative_path(signals_path)
    if not path.exists():
        return
    try:
        narrative = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(narrative, dict) and (narrative.get("headline") or narrative.get("summary")):
            payload["narrative"] = narrative
            logger.info("narrative merged: %s (headline=%r)", path, narrative.get("headline", ""))
    except Exception as exc:  # noqa: BLE001
        logger.warning("narrative 読み込み失敗 (無視して継続): %s", exc)


def _configure_logging(level: str) -> None:
    log_dir = Path("logs")
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    log_file = log_dir / f"publish_{datetime.now().strftime('%Y%m%d')}.log"
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        handlers.append(logging.FileHandler(log_file, encoding="utf-8"))
    except Exception:
        pass
    logging.basicConfig(
        level=str(level).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        handlers=handlers,
    )


def build_registry(kind: str, *, fallback: bool) -> PublisherRegistry:
    """--publisher と --fallback / EMAIL_ALWAYS から registry を組む。"""
    email_always = os.getenv("EMAIL_ALWAYS", "0").strip() in {"1", "true", "yes"}
    kind = kind.lower()

    if kind == "email":
        return PublisherRegistry(primary=EmailPublisher())

    if kind == "all":
        # ntfy + email を常に並列 (both) 送信
        return PublisherRegistry(
            primary=NtfyPublisher(), secondary=EmailPublisher(), always_secondary=True
        )

    # default: ntfy primary。--fallback または EMAIL_ALWAYS で email を副に。
    secondary = EmailPublisher() if (fallback or email_always) else None
    return PublisherRegistry(
        primary=NtfyPublisher(),
        secondary=secondary,
        always_secondary=email_always,
    )


def _write_publish_status(input_path: Path, payload: dict, status: str) -> None:
    """signals JSON の meta.publish_status を書き戻す (dashboard monitoring 用)。"""
    try:
        payload.setdefault("meta", {})["publish_status"] = status
        # merge した transient な narrative は signals JSON に残さない
        # (narrative は narrative_YYYYMMDD.json 側が single source of truth)
        to_write = {k: v for k, v in payload.items() if k != "narrative"}
        tmp = input_path.with_suffix(input_path.suffix + ".tmp")
        tmp.write_text(json.dumps(to_write, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(input_path)
    except Exception as exc:  # noqa: BLE001
        logger.warning("publish_status 書き戻し失敗: %s", exc)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", type=str, default=None, help="対象日 (YYYY-MM-DD)。--input 未指定時に使用。")
    p.add_argument("--input", type=str, default=None, help="signals JSON path (直接指定)。")
    p.add_argument(
        "--publisher",
        choices=["ntfy", "email", "all"],
        default="ntfy",
        help="配信先 (default: ntfy)。all=ntfy+email 並列。",
    )
    p.add_argument(
        "--fallback",
        action="store_true",
        help="primary (ntfy) 失敗時に email へ自動 fallback。",
    )
    p.add_argument(
        "--narrative",
        type=str,
        default=None,
        help="narrative JSON path (未指定なら signals と同じ dir の narrative_YYYYMMDD.json を自動探索)。",
    )
    p.add_argument("--dry-run", action="store_true", help="送信せず payload を検証・表示。")
    p.add_argument("--log-level", default="INFO", help="ログレベル。")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    _configure_logging(args.log_level)

    if args.input:
        input_path = Path(args.input)
    else:
        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        input_path = _default_input_path(date_str)

    try:
        payload = load_payload(input_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("入力読み込み失敗: %s", exc)
        return 1

    # narrative (optional): AI narrator 出力を payload に merge (無ければ既存 body)
    narrative_path = Path(args.narrative) if args.narrative else None
    merge_narrative(payload, narrative_path, input_path)

    message = SignalMessage(payload=payload)
    logger.info(
        "publish: date=%s run_id=%s total_signals=%d warn=%s publisher=%s",
        message.date,
        message.run_id,
        message.total_signals,
        message.has_warnings(),
        args.publisher,
    )

    registry = build_registry(args.publisher, fallback=args.fallback)

    # fail-fast: 本番で configured な publisher が 1 つも無ければエラー
    if not args.dry_run:
        configured = registry.primary.is_configured() or (
            registry.secondary is not None and registry.secondary.is_configured()
        )
        if not configured:
            logger.error(
                "配信先が未設定です。.env の NTFY_TOPIC (primary) か "
                "SENDGRID_* (backup) を設定してください。"
            )
            return 1

    result = registry.publish(payload, dry_run=args.dry_run)

    for r in result.results:
        tag = "OK" if r.ok else "FAIL"
        logger.info("[%s] %s -> %s %s", tag, r.publisher, r.target, "" if r.ok else r.detail)
        if args.dry_run:
            logger.info("  payload: %s", r.detail[:600])

    logger.info("配信 status=%s (%d results)", result.status, len(result.results))

    if not args.dry_run:
        _write_publish_status(input_path, payload, result.status)

    if args.dry_run:
        return 0
    return 0 if result.status in {"ok", "partial"} else 2


if __name__ == "__main__":
    sys.exit(main())
