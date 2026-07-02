"""当日シグナル JSON から AI narrative を生成する CLI wrapper。

``common.narrator.SignalNarrator`` を呼び、結果を narrative_YYYYMMDD.json に
書き出す。daily_pipeline.ps1 の narrator step から呼ばれる。

fail-safe: narrator が空 dict を返しても (API key 未設定等) exit 0 で終わり、
pipeline は narrative 無しで publish へ進める。--dry-run では書き込まず表示のみ。

Usage:
    python scripts/generate_narrative.py --signals results_csv/today_signals_20260701.json \\
        --output results_csv/narrative_20260701.json
    python scripts/generate_narrative.py --signals <json> --model claude-haiku-4-5-20251001 --dry-run

Exit codes:
    0 : 成功 (narrative 生成、または fail-safe で空スキップ)
    1 : 入力 JSON が読めない等の実行時エラー
"""

from __future__ import annotations

import argparse
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.narrator import SignalNarrator  # noqa: E402

logger = logging.getLogger(__name__)


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--signals", required=True, help="入力 today_signals_YYYYMMDD.json path。")
    p.add_argument("--output", default=None, help="出力 narrative JSON path (未指定なら stdout)。")
    p.add_argument("--model", default=None, help="narrator model 上書き (default NARRATOR_MODEL / Haiku)。")
    p.add_argument("--dry-run", action="store_true", help="書き込まず結果を表示。")
    p.add_argument("--log-level", default="INFO", help="ログレベル。")
    return p


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    signals_path = Path(args.signals)
    try:
        signals_json = json.loads(signals_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.error("signals JSON 読み込み失敗: %s", exc)
        return 1

    narrator = SignalNarrator(model=args.model)
    if not narrator.is_configured():
        logger.warning(
            "ANTHROPIC_API_KEY 未設定。narrative 無しで継続します "
            "(pipeline は publish へ進めます)。"
        )

    narrative = narrator.narrate(signals_json)

    if not narrative:
        logger.info("narrative は空です (fail-safe)。出力をスキップします。")
        return 0

    logger.info(
        "narrative 生成: headline=%r model=%s cost=$%.4f%s",
        narrative.get("headline", ""),
        narrative.get("model", ""),
        float(narrative.get("cost_usd", 0.0) or 0.0),
        " [FALLBACK]" if narrative.get("fallback") else "",
    )

    if args.dry_run or not args.output:
        print(json.dumps(narrative, ensure_ascii=False, indent=2))
        return 0

    out_path = Path(args.output)
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = out_path.with_suffix(out_path.suffix + ".tmp")
        tmp.write_text(json.dumps(narrative, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(out_path)
        logger.info("narrative 書き出し: %s", out_path)
    except Exception as exc:  # noqa: BLE001
        logger.error("narrative 書き出し失敗: %s", exc)
        return 1

    return 0


if __name__ == "__main__":
    sys.exit(main())
