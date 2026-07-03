"""過去の narrative_YYYYMMDD.json の headline を新 format に書き換える backfill script。

2026-07-02 の mangled-title incident 前に生成された narrative は headline が
日本語 (「7系統49シグナル、BUY主流…」) で保存されており、ntfy 通知経路では
mangled ASCII に潰れる。この script は該当 narrative の headline field を
決定論的な synth format (「📈 07-02 49 signals / BUY:39 SELL:10 / $37K」)
で置き換える。summary / per_symbol_reasons / model / cost 等は温存する。

deterministic モード (default) は Claude API を呼ばず 0 円で完了。
`--regenerate` を渡した場合は narrator を再実行する (Haiku 4.5, 概算 $0.005/日)。

Usage:
    # dry-run で差分だけ表示 (書き込み無し)
    python scripts/rewrite_narrative_headlines.py \\
        --narratives results_csv/narrative_20260701.json results_csv/narrative_20260702.json \\
        --signals-dir results_csv --dry-run

    # 実書き込み (deterministic synth)
    python scripts/rewrite_narrative_headlines.py \\
        --narratives results_csv/narrative_20260701.json results_csv/narrative_20260702.json \\
        --signals-dir results_csv

    # narrator (Claude Haiku) を再実行して新 headline+summary を再生成
    python scripts/rewrite_narrative_headlines.py \\
        --narratives results_csv/narrative_20260702.json \\
        --signals-dir results_csv --regenerate

Exit codes:
    0 : 成功 (全 narrative 更新完了、または dry-run 表示のみ)
    1 : 入力 error (path 不在等)
    2 : 一部 narrative の再生成に失敗 (残りは処理継続)
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import logging
import os
import re
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.narrator import (  # noqa: E402
    SignalNarrator,
    _is_valid_headline,
    _synth_headline,
)

logger = logging.getLogger(__name__)

# narrative_YYYYMMDD.json → today_signals_YYYYMMDD.json の対応
_DATE_RE = re.compile(r"(\d{8})")


def build_arg_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--narratives",
        nargs="+",
        required=True,
        help="対象 narrative_YYYYMMDD.json path (複数指定可)。",
    )
    p.add_argument(
        "--signals-dir",
        default="results_csv",
        help="today_signals_YYYYMMDD.json が置かれた directory (default: results_csv)。",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="書き込まず、before/after の headline を表示。",
    )
    p.add_argument(
        "--regenerate",
        action="store_true",
        help="決定論的 synth ではなく narrator (Claude Haiku) を再呼び出し。",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="既存 headline が新 format 準拠でも上書きする。",
    )
    p.add_argument("--model", default=None, help="narrator model 上書き (--regenerate 時のみ有効)。")
    p.add_argument("--log-level", default="INFO", help="ログレベル。")
    return p


def _find_signals_path(narrative_path: Path, signals_dir: Path) -> Path | None:
    """narrative filename の YYYYMMDD 部分から today_signals ファイルを推定。"""
    m = _DATE_RE.search(narrative_path.name)
    if not m:
        return None
    yyyymmdd = m.group(1)
    cand = signals_dir / f"today_signals_{yyyymmdd}.json"
    if cand.exists():
        return cand
    # backfill 用 fallback: 同 dir に signals_YYYYMMDD.json があるかもチェック
    alt = signals_dir / f"signals_{yyyymmdd}.json"
    return alt if alt.exists() else None


def _write_json_atomic(path: Path, payload: dict) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _rewrite_one(
    narrative_path: Path,
    signals_dir: Path,
    *,
    dry_run: bool,
    regenerate: bool,
    force: bool,
    model: str | None,
) -> tuple[bool, str]:
    """1 file を rewrite。(success, note) を返す。"""
    if not narrative_path.exists():
        return False, f"narrative 不在: {narrative_path}"

    try:
        narrative = json.loads(narrative_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"narrative 読込失敗: {exc}"

    old_headline = str(narrative.get("headline", ""))
    already_valid = _is_valid_headline(old_headline)
    if already_valid and not force:
        return True, f"skip (既に valid): {old_headline!r}"

    signals_path = _find_signals_path(narrative_path, signals_dir)
    if not signals_path:
        return False, f"対応 signals JSON が {signals_dir} に見つからない"

    try:
        signals_json = json.loads(signals_path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return False, f"signals 読込失敗 {signals_path}: {exc}"

    if regenerate:
        narrator = SignalNarrator(model=model)
        if not narrator.is_configured():
            return False, "ANTHROPIC_API_KEY 未設定のため --regenerate 不可"
        new_narrative = narrator.narrate(signals_json)
        if not new_narrative:
            return False, "narrator が空 dict を返した (API failure?)"
        # summary/per_symbol_reasons ごと入れ替え。cost もメモ。
        narrative["headline"] = new_narrative.get("headline", "")
        narrative["summary"] = new_narrative.get("summary", narrative.get("summary", ""))
        narrative["per_symbol_reasons"] = new_narrative.get(
            "per_symbol_reasons", narrative.get("per_symbol_reasons", {})
        )
        narrative["model"] = new_narrative.get("model", narrative.get("model", ""))
        prev_cost = float(narrative.get("cost_usd", 0.0) or 0.0)
        narrative["cost_usd"] = round(
            prev_cost + float(new_narrative.get("cost_usd", 0.0) or 0.0), 6
        )
        if new_narrative.get("headline_synth"):
            narrative["headline_synth"] = True
    else:
        narrative["headline"] = _synth_headline(signals_json)
        narrative["headline_synth"] = True

    # audit 用の marker
    narrative["headline_rewritten_at"] = dt.datetime.utcnow().isoformat(timespec="seconds") + "Z"
    narrative["headline_prev"] = old_headline

    note = f"{old_headline!r} -> {narrative['headline']!r}"
    if dry_run:
        return True, f"[dry-run] {note}"

    try:
        _write_json_atomic(narrative_path, narrative)
    except Exception as exc:  # noqa: BLE001
        return False, f"書き込み失敗: {exc}"
    return True, f"rewrote {narrative_path.name}: {note}"


def main(argv: list[str] | None = None) -> int:
    args = build_arg_parser().parse_args(argv)
    logging.basicConfig(
        level=str(args.log_level).upper(),
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )

    signals_dir = Path(args.signals_dir)
    if not signals_dir.exists():
        logger.error("--signals-dir 不在: %s", signals_dir)
        return 1

    n_ok, n_fail = 0, 0
    for path_str in args.narratives:
        path = Path(path_str)
        ok, note = _rewrite_one(
            path,
            signals_dir,
            dry_run=args.dry_run,
            regenerate=args.regenerate,
            force=args.force,
            model=args.model,
        )
        if ok:
            logger.info("[OK] %s | %s", path.name, note)
            n_ok += 1
        else:
            logger.error("[FAIL] %s | %s", path.name, note)
            n_fail += 1

    logger.info("完了: %d ok / %d fail", n_ok, n_fail)
    return 0 if n_fail == 0 else 2


if __name__ == "__main__":
    sys.exit(main())
