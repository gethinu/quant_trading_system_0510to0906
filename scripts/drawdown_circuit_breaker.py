"""drawdown サーキットブレーカ — equity ドローダウン閾値超えで全ポジション flatten (paper)。

``common/drawdown_breaker.py`` の純判定を実運用に配線する薄い CLI。**default は無効**
(config ``risk.portfolio.drawdown_flatten_pct`` が 0 の間は何も起きない)。有効化は
user が config に閾値を入れたときだけ。実 flatten は ``--confirm`` を明示したときだけ
(無指定は dry-run と等価 = 誤爆防止)。

判定入力:
    - equity      : Alpaca paper 口座の現 equity (fetch_account_equity, read-only)。
    - peak_equity : results_csv/alpaca_equity_history.json の履歴 + 現 equity の最大値。
    - threshold   : --threshold-pct 明示 > config drawdown_flatten_pct。

誤発火防止 (common.drawdown_breaker.assess):
    - config 無効 (threshold<=0) → armed=False で即 no-op。
    - equity / peak が欠損 → flatten しない。
    - 履歴点数 < --min-history-points → 薄い履歴で peak が不確か → flatten しない。
    - 絶対ドローダウン額 < --min-abs-drawdown-usd → flatten しない (0=ガード無効)。
    - 本日すでに flatten 済 (logs/drawdown_breaker_<date>.done) → 冪等 skip。

安全:
    - flatten 実行前に必ず assert_paper_env (live 口座禁止)。
    - Alpaca ネイティブ close_all_positions(cancel_orders=True) で決済。

Usage:
    # 疎通/状態確認 (発注しない。閾値未設定なら "disabled" と表示)
    python scripts/drawdown_circuit_breaker.py --dry-run
    # 閾値を明示して dry-run 検証 (config を汚さず発火条件を試す)
    python scripts/drawdown_circuit_breaker.py --threshold-pct 0.15 --dry-run
    # 本番 (config で有効化済み前提。閾値超え & 全ガード通過なら paper で全決済)
    python scripts/drawdown_circuit_breaker.py --confirm

Exit codes:
    0  = 何もしない (無効 / 閾値内 / ガードで抑止 / 既に実行済)
    10 = flatten を実行した
    11 = 閾値超えだが dry-run のため未実行 (WOULD flatten)
    2  = 実行時エラー (flatten 中の例外など)
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.drawdown_breaker import (  # noqa: E402
    DEFAULT_MIN_ABS_DRAWDOWN_USD,
    DEFAULT_MIN_HISTORY_POINTS,
    assess,
    flatten_all_paper,
    load_equity_history,
    resolve_peak_equity,
)
from common.portfolio_guard import load_guard_config  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _today_str() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%d")


def _ntfy(title: str, body: str, *, urgent: bool = False) -> None:
    """UTF-8-safe な NtfyPublisher で通知。失敗しても無視 (通知は best-effort)。"""
    try:
        from common.publishers.ntfy import NtfyPublisher

        pub = NtfyPublisher()
        if not pub.is_configured():
            print("[ntfy] NTFY_TOPIC 未設定のため通知スキップ")
            return
        tags = "rotating_light,warning" if urgent else "shield"
        res = pub.send_text(title, body, tags=tags, priority=(5 if urgent else None))
        print(f"[ntfy] 送信 ok={getattr(res, 'ok', '?')}")
    except Exception as exc:  # noqa: BLE001
        print(f"[ntfy] 送信失敗 (無視): {exc}")


def _resolve_threshold(args: argparse.Namespace) -> tuple[float, str]:
    if args.threshold_pct is not None:
        return float(args.threshold_pct), "cli"
    cfg = load_guard_config()
    return float(cfg.get("drawdown_flatten_pct", 0.0) or 0.0), "config"


def _resolve_equity(args: argparse.Namespace) -> float | None:
    if args.equity is not None:
        return float(args.equity)
    if args.no_alpaca:
        return None
    try:
        from common.alpaca_trading import fetch_account_equity

        return fetch_account_equity()
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] equity 取得失敗 (offline 扱い): {exc}")
        return None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (default: today UTC)"
    )
    parser.add_argument(
        "--threshold-pct",
        type=float,
        default=None,
        help="peak drawdown 閾値 (0.15=15%%)。未指定は config risk.portfolio.drawdown_flatten_pct。",
    )
    parser.add_argument(
        "--min-history-points",
        type=int,
        default=DEFAULT_MIN_HISTORY_POINTS,
        help=f"equity 履歴がこの点数未満なら flatten しない (default {DEFAULT_MIN_HISTORY_POINTS})",
    )
    parser.add_argument(
        "--min-abs-drawdown-usd",
        type=float,
        default=DEFAULT_MIN_ABS_DRAWDOWN_USD,
        help="絶対ドローダウン額がこの USD 未満なら flatten しない (0=無効)",
    )
    parser.add_argument(
        "--results-dir",
        default=str(ROOT / "results_csv"),
        help="alpaca_equity_history.json を探す dir",
    )
    parser.add_argument(
        "--equity", type=float, default=None, help="現 equity を明示 (test/offline)"
    )
    parser.add_argument(
        "--no-alpaca",
        action="store_true",
        help="Alpaca を叩かない (equity は --equity 必須)",
    )
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="実際に flatten する (無指定は dry-run と等価: 誤爆防止)",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="本日 DONE marker があっても再判定する (冪等ロックを無視)",
    )
    parser.add_argument("--output-json", default=None, help="判定結果 JSON の出力先")
    parser.add_argument(
        "--log-dir",
        default=str(ROOT / "logs"),
        help="durable ログ / DONE marker の dir",
    )
    args = parser.parse_args(argv)

    date_str = args.date or _today_str()
    compact = date_str.replace("-", "")
    log_dir = Path(args.log_dir)
    log_dir.mkdir(parents=True, exist_ok=True)
    done_marker = log_dir / f"drawdown_breaker_{compact}.done"
    out_path = (
        Path(args.output_json)
        if args.output_json
        else log_dir / f"drawdown_breaker_{compact}.json"
    )

    threshold, th_source = _resolve_threshold(args)
    equity = _resolve_equity(args)
    history = load_equity_history(Path(args.results_dir) / "alpaca_equity_history.json")
    peak, n_points = resolve_peak_equity(history, equity)

    a = assess(
        equity,
        peak,
        threshold,
        n_history_points=n_points,
        min_history_points=args.min_history_points,
        min_abs_drawdown_usd=args.min_abs_drawdown_usd,
    )

    print(
        f"[breaker] armed={a.armed} breached={a.breached} would_flatten={a.would_flatten} "
        f"equity={a.equity} peak={a.peak_equity} dd={a.drawdown_pct:.2%} "
        f"threshold={a.threshold_pct:.2%}({th_source}) hist_points={n_points} "
        f"reason={a.reason}"
    )

    record: dict[str, Any] = {
        "version": "1.0",
        "date": date_str,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "threshold_source": th_source,
        "confirm": bool(args.confirm),
        "assessment": a.to_dict(),
        "action": "none",
    }

    # 冪等: 本日すでに flatten 済なら再実行しない
    if a.would_flatten and args.confirm and done_marker.exists() and not args.force:
        print(
            f"[breaker] 本日 flatten 済 ({done_marker.name}) -> skip (--force で上書き)"
        )
        record["action"] = "skip_already_flattened"
        _write(out_path, record)
        return 0

    if not a.armed:
        # 無効 = 既定。静かに 0。dry-run 状態確認としては上の print で十分。
        record["action"] = "disabled"
        _write(out_path, record)
        return 0

    if not a.would_flatten:
        # 閾値内 or ガードで抑止。armed だが発火せず。診断のため JSON は残す。
        record["action"] = "no_breach" if not a.breached else "guarded"
        _write(out_path, record)
        return 0

    # ここに来たら armed & breached & 全ガード通過 = 発火条件成立。
    body = (
        f"peak drawdown {a.drawdown_pct:.2%} >= 閾値 {a.threshold_pct:.2%} "
        f"(equity ${a.equity:,.0f} / peak ${a.peak_equity:,.0f})"
    )

    if not args.confirm:
        print(f"[breaker] WOULD FLATTEN (dry-run, --confirm 未指定): {body}")
        record["action"] = "would_flatten_dry_run"
        _write(out_path, record)
        _ntfy(
            f"DrawdownBreaker WOULD FIRE {date_str}",
            "サーキットブレーカ発火条件成立 (dry-run のため未執行)。\n" + body,
            urgent=True,
        )
        return 11

    # --confirm: 実 flatten。paper 断言 → close_all_positions。
    try:
        from common import broker_alpaca as ba
        from common.alpaca_trading import assert_paper_env

        assert_paper_env()  # live なら例外
        client = ba.get_client(paper=True)
    except Exception as exc:  # noqa: BLE001
        print(f"[SAFETY ABORT] paper 断言/クライアント取得失敗、flatten 中止: {exc}")
        record["action"] = "abort_not_paper"
        record["error"] = str(exc)
        _write(out_path, record)
        _ntfy(
            f"DrawdownBreaker ABORT {date_str}",
            f"flatten を中止 (paper 断言失敗): {exc}",
            urgent=True,
        )
        return 2

    print(f"[breaker] FLATTEN 実行 (paper): {body}")
    result = flatten_all_paper(client)
    record["action"] = "flattened"
    record["flatten_result"] = {
        "ok": result.get("ok"),
        "failed": result.get("failed"),
        "order_ids": result.get("order_ids"),
        "error": result.get("error"),
    }
    _write(out_path, record)
    _write(
        log_dir / f"drawdown_flatten_{compact}.json",
        {"date": date_str, "assessment": a.to_dict(), "flatten": result},
    )
    done_marker.write_text(datetime.now(timezone.utc).isoformat(), encoding="utf-8")
    _ntfy(
        f"DrawdownBreaker FIRED {date_str}",
        (
            "サーキットブレーカ発火: 全ポジションを flatten (paper)。\n"
            f"{body}\n"
            f"close ok={result.get('ok')} failed={result.get('failed')}"
        ),
        urgent=True,
    )
    failed = int(result.get("failed") or 0)
    return 10 if failed == 0 else 2


def _write(path: Path, obj: dict[str, Any]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(obj, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
        )
        print(f"[write] {path}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] JSON 書き出し失敗 (無視): {exc}")


if __name__ == "__main__":
    raise SystemExit(main())
