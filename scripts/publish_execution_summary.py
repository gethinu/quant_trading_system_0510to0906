"""submit 後の execution summary を配信する (Step5c の後に 1 通)。

daily_pipeline の entry(5b)/exit(5c) が終わった *後* に実行し、「実際に何件
発注・約定したか」を recon JSON から組み立てて ntfy へ push する。Step5 の
publish (signal 予告: narrator + system別 signal) はそのまま残し、本通知は
実発注確定後の *実行結果* を別便で伝える。

recon JSON が無ければ today_signals / paper_orders / exit_orders の 3 つから
その場で build する (scripts/build_execution_recon.build_recon 再利用)。

**read-only / paper 前提**: Alpaca へは発注しない。既存 JSON を読んで通知するだけ。

Usage:
    # recon を明示
    python scripts/publish_execution_summary.py --recon-json results_csv/recon_20260708.json
    # 3 JSON から build して送信 (date から default path 解決)
    python scripts/publish_execution_summary.py --date 2026-07-08
    # 送信せず本文だけ確認
    python scripts/publish_execution_summary.py --date 2026-07-08 --dry-run
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

from common.publishers.execution_summary import format_execution_summary  # noqa: E402
from common.publishers.ntfy import NtfyPublisher  # noqa: E402
from scripts.build_execution_recon import _default_path, _load_json, build_recon  # noqa: E402

logger = logging.getLogger(__name__)


def _resolve_recon(args: argparse.Namespace) -> dict | None:
    results_dir = Path(args.results_dir)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.recon_json:
        recon = _load_json(Path(args.recon_json))
        if recon is None:
            logger.error("recon JSON を読めません: %s", args.recon_json)
        return recon

    # recon default path があればそれを使う
    default_recon = _default_path(results_dir, "recon", date_str)
    if default_recon.exists():
        return _load_json(default_recon)

    # 無ければ 3 JSON から build
    signals = _load_json(
        Path(args.signals_json) if args.signals_json else _default_path(results_dir, "today_signals", date_str)
    )
    paper = _load_json(
        Path(args.paper_orders_json) if args.paper_orders_json else _default_path(results_dir, "paper_orders", date_str)
    )
    exits = _load_json(
        Path(args.exit_orders_json) if args.exit_orders_json else _default_path(results_dir, "exit_orders", date_str)
    )
    if signals is None and paper is None and exits is None:
        return None
    return build_recon(
        signals,
        paper,
        exits,
        date_str=(args.date or (signals or {}).get("date") or None),
        account_equity=args.account_equity,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。default path 解決に使う。")
    parser.add_argument("--recon-json", help="recon JSON path (明示)。")
    parser.add_argument("--signals-json", help="today_signals JSON path。")
    parser.add_argument("--paper-orders-json", help="paper_orders JSON path。")
    parser.add_argument("--exit-orders-json", help="exit_orders JSON path。")
    parser.add_argument("--results-dir", default="results_csv", help="default path 基準 dir。")
    parser.add_argument("--account-equity", type=float, default=None, help="口座残高 (通知表示用)。")
    parser.add_argument("--dry-run", action="store_true", help="送信せず title/body を表示。")
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(level=str(args.log_level).upper(), format="%(levelname)s: %(message)s")

    recon = _resolve_recon(args)
    if recon is None:
        logger.error("recon を解決できません (入力 JSON が見つからない)。通知をスキップ。")
        return 1

    title, body = format_execution_summary(recon)

    # 副産物として recon を書き戻す (build した場合、dashboard が execution funnel を
    # 参照できるよう)。dry-run でも書く = dry-run 実行でもダッシュにサマリが出る。
    if not args.recon_json:
        date_str = args.date or (recon.get("date") or datetime.now().strftime("%Y-%m-%d"))
        out = _default_path(Path(args.results_dir), "recon", str(date_str))
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(json.dumps(recon, ensure_ascii=False, indent=2), encoding="utf-8")
            logger.info("recon 書き出し: %s", out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon 書き戻し失敗 (無視): %s", exc)

    if args.dry_run:
        print(f"X-Title: {title}\n---\n{body}")
        return 0

    pub = NtfyPublisher()
    if not pub.is_configured():
        logger.error("NTFY_TOPIC 未設定のため配信できません (--dry-run で本文確認可)。")
        return 1

    # entry_failed があれば urgent(5)、それ以外は既定 priority
    p = recon.get("portfolio", {}) or {}
    try:
        urgent = int(p.get("entry_failed") or 0) > 0
    except (TypeError, ValueError):
        urgent = False
    tags = "bar_chart" + (",warning" if urgent else "")
    result = pub.send_text(title, body, tags=tags, priority=(5 if urgent else None))
    logger.info("execution summary 配信: ok=%s detail=%s", result.ok, result.detail)

    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
