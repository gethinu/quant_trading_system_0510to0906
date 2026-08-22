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
from datetime import datetime, timezone
import json
import logging
import os
from pathlib import Path
import sys

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from common.publishers.execution_summary import format_execution_summary  # noqa: E402
from common.publishers.ntfy import NtfyPublisher  # noqa: E402
from scripts.build_execution_recon import (  # noqa: E402
    _default_path,
    _load_json,
    build_recon,
    patch_pipeline_exit,
)

logger = logging.getLogger(__name__)

# execution summary の ntfy 結果を残す sidecar。
#
# signals JSON の ``meta.publish_delivery`` は **publish_signals.py (朝の予告便)**
# 専用で、夜の open run が送る execution summary は別チャネル。open_auto_run は
# publish_signals を呼ばないため、実績通知が届いたかどうかがどこにも残らず、
# 「ntfy が不調でも観測できない」状態だった。signals JSON を書き換えると run を
# またいで状態が混ざるので、run_id を持つ独立ファイルとして書く。
_NOTIFY_DELIVERY_SCHEMA = "notify_delivery/v1"


def notify_delivery_path(results_dir: Path, date_str: str) -> Path:
    return results_dir / f"notify_delivery_{date_str.replace('-', '')}.json"


def build_notify_delivery(
    *,
    date_str: str,
    run_id: str | None,
    channel: str,
    state: str,
    status_code: int | None = None,
    attempted_at: str | None = None,
) -> dict:
    """ntfy 実績通知の配信状態を、secret を含まない形で組み立てる。

    ``state`` の語彙は signals 側 (``publish_delivery``) と揃える:
      accepted / failed / not_configured / not_attempted
    ntfy の 2xx は **server が受理** した意味であり、端末到達ではない。
    """
    return {
        "schema": _NOTIFY_DELIVERY_SCHEMA,
        "kind": "execution_summary",
        "date": date_str,
        "source_signals_run_id": run_id or None,
        "state": state,
        "attempted_at": attempted_at,
        "channels": {channel: {"state": state, "status_code": status_code}},
    }


def write_notify_delivery(results_dir: Path, payload: dict) -> Path | None:
    """sidecar を atomic に書く。失敗しても通知処理自体は落とさない。"""
    path = notify_delivery_path(results_dir, str(payload.get("date") or ""))
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(path)
        return path
    except Exception as exc:  # noqa: BLE001 - 観測用の副産物
        logger.warning("notify_delivery 書き込み失敗: %s", exc)
        return None


def _wire_pipeline_exit(results_dir: Path, date_str: str, recon: dict) -> None:
    """漏斗 (pipeline_YYYYMMDD.json) の Exit phase を *この recon* から埋めて書き戻す。

    ntfy 本文と漏斗を同一 recon から書くことで両者を必ず一致させる。漏斗は
    daily_polygon_monitor が exit 執行 *前* (Step3) に生成し Exit=null (未計測) の
    ままなので、exit 実績が確定した本 step (Step5d) で上書きする。Step6 の
    publish_data_to_vercel が results_csv/ の pipeline を data/ へ copy するため、
    ここで書き戻せばダッシュボードに反映される。

    pipeline が無い / 部分 recon (exit 未計測) の時は **何もしない** = 未計測を維持。
    """
    pipeline_path = _default_path(results_dir, "pipeline", date_str)
    pipeline = _load_json(pipeline_path)
    if pipeline is None:
        logger.info("pipeline funnel が無いため Exit 配線をスキップ: %s", pipeline_path)
        return
    _, n_filled, status = patch_pipeline_exit(pipeline, recon)
    if status != "ok":
        logger.info("pipeline Exit 配線せず (%s): %s", status, pipeline_path)
        return
    try:
        tmp = pipeline_path.with_suffix(pipeline_path.suffix + ".tmp")
        tmp.write_text(
            json.dumps(pipeline, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        tmp.replace(pipeline_path)
        logger.info(
            "pipeline Exit 配線: %s (%d system, ntfy と同一 recon)",
            pipeline_path,
            n_filled,
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("pipeline Exit 書き戻し失敗 (無視): %s", exc)


def _resolve_recon(args: argparse.Namespace) -> dict | None:
    results_dir = Path(args.results_dir)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    if args.recon_json:
        recon = _load_json(Path(args.recon_json))
        if recon is None:
            logger.error("recon JSON を読めません: %s", args.recon_json)
        return recon

    signals = _load_json(
        Path(args.signals_json)
        if args.signals_json
        else _default_path(results_dir, "today_signals", date_str)
    )
    signals_meta = signals.get("meta") if isinstance(signals, dict) else None
    current_run_id = (
        signals_meta.get("run_id") if isinstance(signals_meta, dict) else None
    )

    # default recon は、current signals runとのlineageが一致する場合だけ再利用する。
    # 旧schema（source_signals_run_idなし）や朝run由来のmismatchを使うと、夜runの
    # 発注結果を古いsignalsへ結び付けて誤通知するため、current inputsから再構築する。
    default_recon = _default_path(results_dir, "recon", date_str)
    if default_recon.exists():
        recon = _load_json(default_recon)
        if (
            isinstance(current_run_id, str)
            and bool(current_run_id.strip())
            and isinstance(recon, dict)
            and recon.get("source_signals_run_id") == current_run_id
        ):
            return recon
        logger.info(
            "default recon lineage不一致のため再構築: recon=%r signals=%r",
            (
                (recon or {}).get("source_signals_run_id")
                if isinstance(recon, dict)
                else None
            ),
            current_run_id,
        )

    # lineage一致のreconが無ければcurrent inputsからbuild
    paper = _load_json(
        Path(args.paper_orders_json)
        if args.paper_orders_json
        else _default_path(results_dir, "paper_orders", date_str)
    )
    exits = _load_json(
        Path(args.exit_orders_json)
        if args.exit_orders_json
        else _default_path(results_dir, "exit_orders", date_str)
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
    parser.add_argument(
        "--results-dir", default="results_csv", help="default path 基準 dir。"
    )
    parser.add_argument(
        "--account-equity", type=float, default=None, help="口座残高 (通知表示用)。"
    )
    parser.add_argument(
        "--dry-run", action="store_true", help="送信せず title/body を表示。"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=str(args.log_level).upper(), format="%(levelname)s: %(message)s"
    )

    recon = _resolve_recon(args)
    if recon is None:
        logger.error(
            "recon を解決できません (入力 JSON が見つからない)。通知をスキップ。"
        )
        return 1

    title, body = format_execution_summary(recon)

    # 副産物として recon を書き戻す (build した場合、dashboard が execution funnel を
    # 参照できるよう)。dry-run でも書く = dry-run 実行でもダッシュにサマリが出る。
    date_str = args.date or (recon.get("date") or datetime.now().strftime("%Y-%m-%d"))
    if not args.recon_json:
        out = _default_path(Path(args.results_dir), "recon", str(date_str))
        try:
            out.parent.mkdir(parents=True, exist_ok=True)
            out.write_text(
                json.dumps(recon, ensure_ascii=False, indent=2), encoding="utf-8"
            )
            logger.info("recon 書き出し: %s", out)
        except Exception as exc:  # noqa: BLE001
            logger.warning("recon 書き戻し失敗 (無視): %s", exc)

    # 漏斗 (pipeline funnel) の Exit を *この recon* から埋めて未計測を消す。
    # ntfy 本文と同一 recon を single source にするので両者が必ず一致する。
    # dry-run でも書く (ダッシュへ反映させるため。実送信の有無とは独立)。
    _wire_pipeline_exit(Path(args.results_dir), str(date_str), recon)

    # sidecar は **全経路で** 書く。書かない経路があると「送っていない」と
    # 「記録し忘れた」を dashboard が区別できなくなる。
    run_id = str(recon.get("source_signals_run_id") or "") or None
    results_dir = Path(args.results_dir)
    now_iso = datetime.now(timezone.utc).isoformat(timespec="seconds")

    if args.dry_run:
        print(f"X-Title: {title}\n---\n{body}")
        write_notify_delivery(
            results_dir,
            build_notify_delivery(
                date_str=str(date_str),
                run_id=run_id,
                channel="ntfy",
                state="not_attempted",
            ),
        )
        return 0

    pub = NtfyPublisher()
    if not pub.is_configured():
        logger.error("NTFY_TOPIC 未設定のため配信できません (--dry-run で本文確認可)。")
        write_notify_delivery(
            results_dir,
            build_notify_delivery(
                date_str=str(date_str),
                run_id=run_id,
                channel="ntfy",
                state="not_configured",
                attempted_at=now_iso,
            ),
        )
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

    write_notify_delivery(
        results_dir,
        build_notify_delivery(
            date_str=str(date_str),
            run_id=run_id,
            channel="ntfy",
            # 2xx = server が受理。端末到達の保証ではないので "accepted"。
            state="accepted" if result.ok else "failed",
            status_code=getattr(result, "status_code", None),
            attempted_at=now_iso,
        ),
    )

    return 0 if result.ok else 2


if __name__ == "__main__":
    raise SystemExit(main())
