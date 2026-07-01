"""当日シグナル JSON を配信する (Phase 1: Discord, Phase 2/3 拡張の礎)。

``results_csv/today_signals_YYYYMMDD.json`` を読み、``common.publishers`` の
Publisher 経由で配信する。Phase 1 では env ``DISCORD_WEBHOOK_URL`` 単一宛先。
``config/subscribers.json`` があれば per-subscriber routing (skeleton) に切替える。

Usage:
    python scripts/publish_signals.py --date 2026-07-01
    python scripts/publish_signals.py --input results_csv/today_signals_20260701.json
    python scripts/publish_signals.py --date 2026-07-01 --dry-run   # 送信せず payload 検証

Exit codes:
    0 : 全宛先へ配信成功 (または dry-run)
    1 : 実行時エラー (入力欠損 / webhook 未設定 等)
    2 : 一部/全宛先で配信失敗
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.publishers import (  # noqa: E402
    Publisher,
    PublishResult,
    SignalMessage,
    build_publisher,
)
from common.publishers.discord import DiscordPublisher  # noqa: E402

logger = logging.getLogger(__name__)

DEFAULT_SUBSCRIBERS_PATH = Path("config") / "subscribers.json"


def _default_input_path(date_str: str) -> Path:
    return Path("results_csv") / f"today_signals_{date_str.replace('-', '')}.json"


def load_payload(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"signals JSON が見つかりません: {path}")
    return json.loads(path.read_text(encoding="utf-8"))


def _publishers_from_subscribers(
    subscribers_path: Path,
) -> list[tuple[str, Publisher]]:
    """subscribers.json (Phase 2 の下地) から (subscriber_id, publisher) を構築。

    schema:
        {"subscribers": [
            {"id": "...", "tier": "...", "systems": ["*"],
             "channels": [{"type": "discord", "webhook_env": "DISCORD_WEBHOOK_URL"}]}
        ]}

    Phase 1 では tier / systems フィルタは未使用 (全 system を全 subscriber へ)。
    Phase 2 で per-subscriber の system 絞り込み・課金 tier ゲートを実装する。
    """
    data = json.loads(subscribers_path.read_text(encoding="utf-8"))
    out: list[tuple[str, Publisher]] = []
    for sub in data.get("subscribers", []):
        sub_id = str(sub.get("id", "?"))
        for ch in sub.get("channels", []):
            kind = str(ch.get("type", "")).lower()
            cfg: dict = {}
            # webhook_env / token_env 経由で secret を env から解決 (JSON に生値を置かない)
            if ch.get("webhook_env"):
                env_val = os.getenv(str(ch["webhook_env"]))
                if kind == "discord":
                    cfg["webhook_url"] = env_val
                    cfg["target_label"] = f"{sub_id}:{ch['webhook_env']}"
                elif kind == "webhook":
                    cfg["url"] = env_val
                    cfg["target_label"] = f"{sub_id}:{ch['webhook_env']}"
            if ch.get("to"):
                cfg["to"] = ch["to"]
            try:
                pub = build_publisher(kind, **cfg)
            except Exception as exc:  # noqa: BLE001
                logger.warning("subscriber %s channel %s 構築失敗: %s", sub_id, kind, exc)
                continue
            out.append((sub_id, pub))
    return out


def resolve_publishers(
    subscribers_path: Path,
) -> list[tuple[str, Publisher]]:
    """配信先 publisher 群を決める。subscribers.json 優先、無ければ env Discord 単発。"""
    if subscribers_path.exists():
        logger.info("subscribers routing: %s", subscribers_path)
        pubs = _publishers_from_subscribers(subscribers_path)
        if pubs:
            return pubs
        logger.warning("subscribers.json に有効な channel が無いため default に fallback")

    # Phase 1 default: env の DISCORD_WEBHOOK_URL 単発
    return [("owner", DiscordPublisher())]


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", type=str, default=None, help="対象日 (YYYY-MM-DD)。--input 未指定時に使用。")
    p.add_argument("--input", type=str, default=None, help="signals JSON path (直接指定)。")
    p.add_argument(
        "--subscribers",
        type=str,
        default=str(DEFAULT_SUBSCRIBERS_PATH),
        help="subscribers.json path (存在すれば per-subscriber routing)。",
    )
    p.add_argument("--dry-run", action="store_true", help="送信せず payload を検証・表示。")
    p.add_argument("--log-level", default="INFO", help="ログレベル。")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    # 入力 path 解決
    if args.input:
        input_path = Path(args.input)
    else:
        from datetime import datetime

        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        input_path = _default_input_path(date_str)

    try:
        payload = load_payload(input_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("入力読み込み失敗: %s", exc)
        return 1

    message = SignalMessage(payload=payload)
    logger.info(
        "publish: date=%s run_id=%s total_signals=%d warn=%s",
        message.date,
        message.run_id,
        message.total_signals,
        message.has_warnings(),
    )

    publishers = resolve_publishers(Path(args.subscribers))

    # fail-fast: 本番 (非 dry-run) で configured な publisher が 1 つも無ければエラー
    if not args.dry_run and not any(p.is_configured() for _, p in publishers):
        logger.error(
            "配信先が 1 つも設定されていません。.env の DISCORD_WEBHOOK_URL を設定するか "
            "config/subscribers.json を用意してください。"
        )
        return 1

    results: list[PublishResult] = []
    for sub_id, pub in publishers:
        res = pub.publish(message, dry_run=args.dry_run)
        res.target = res.target or sub_id
        results.append(res)
        status = "OK" if res.ok else "FAIL"
        logger.info(
            "[%s] %s -> %s (%s) %s",
            status,
            pub.name,
            res.target,
            res.status_code,
            "" if res.ok else res.detail,
        )
        if args.dry_run:
            logger.info("  payload: %s", res.detail[:500])

    n_ok = sum(1 for r in results if r.ok)
    n_total = len(results)
    logger.info("配信完了: %d/%d 成功", n_ok, n_total)

    if args.dry_run:
        return 0
    if n_ok == 0:
        return 2
    if n_ok < n_total:
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
