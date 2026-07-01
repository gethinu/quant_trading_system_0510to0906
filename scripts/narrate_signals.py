"""当日シグナル JSON に AI narrative を付与する (pipeline step: coverage -> narrator -> publish)。

``results_csv/today_signals_YYYYMMDD.json`` を読み、:class:`SignalNarrator` で
headline / summary / per_symbol_reasons を生成し ``meta.narrative`` に merge して
書き戻す。後段の publish (ntfy/email) と Vercel dashboard が ``meta.narrative`` を
参照する。

**fail-safe**: ANTHROPIC_API_KEY 未設定でも exit 0 (空 narrative を書いて継続)。
narrator は例外を投げない設計なので、この script は原則 exit 0。入力欠損のみ exit 1。

Usage:
    python scripts/narrate_signals.py --date 2026-07-01
    python scripts/narrate_signals.py --input results_csv/today_signals_20260701.json
    python scripts/narrate_signals.py --input <json> --dry-run   # 書き戻さず表示のみ

Exit codes:
    0 : narrative を付与 (または fail-safe skip)
    1 : 入力 JSON 欠損 / 読込失敗
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

from common.narrator import SignalNarrator  # noqa: E402

logger = logging.getLogger(__name__)


def _default_input_path(date_str: str) -> Path:
    return Path("results_csv") / f"today_signals_{date_str.replace('-', '')}.json"


def _write_back(path: Path, payload: dict) -> None:
    """meta.narrative を書き戻す (atomic tmp -> replace)。"""
    tmp = path.with_suffix(path.suffix + ".narr.tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--date", type=str, default=None, help="対象日 (YYYY-MM-DD)。--input 未指定時に使用。")
    p.add_argument("--input", type=str, default=None, help="signals JSON path (直接指定)。")
    p.add_argument("--model", type=str, default=None, help="narrator model (default: NARRATOR_MODEL env)。")
    p.add_argument("--dry-run", action="store_true", help="書き戻さず narrative を表示のみ。")
    p.add_argument("--log-level", default="INFO", help="ログレベル。")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    if args.input:
        input_path = Path(args.input)
    else:
        date_str = args.date or datetime.now().strftime("%Y-%m-%d")
        input_path = _default_input_path(date_str)

    if not input_path.exists():
        logger.error("signals JSON が見つかりません: %s", input_path)
        return 1
    try:
        payload = json.loads(input_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("入力読み込み失敗: %s", exc)
        return 1

    narrator = SignalNarrator(model=args.model)
    if not narrator.is_configured():
        logger.warning(
            "ANTHROPIC_API_KEY 未設定のため narrative を skip (pipeline は継続)。"
            " setup 手順は .env.example の NARRATOR / ANTHROPIC_API_KEY 節を参照。"
        )

    result = narrator.narrate(payload)
    logger.info(
        "narrative: configured=%s fallback=%s cost=$%.6f elapsed=%.2fs warnings=%d headline=%r",
        result.configured,
        result.fallback,
        result.cost_usd,
        result.elapsed_seconds,
        len(result.warnings),
        result.headline,
    )
    for w in result.warnings:
        logger.warning("narrator warning: %s", w)

    payload.setdefault("meta", {})["narrative"] = result.as_dict()

    if args.dry_run:
        print(json.dumps(result.as_dict(), ensure_ascii=False, indent=2))
        return 0

    try:
        _write_back(input_path, payload)
    except Exception as exc:  # noqa: BLE001
        logger.error("narrative 書き戻し失敗: %s", exc)
        return 1
    logger.info("meta.narrative を %s に書き戻しました。", input_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
