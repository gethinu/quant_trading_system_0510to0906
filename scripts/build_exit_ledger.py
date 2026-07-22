"""Alpaca Paper の約定履歴から exit 実績台帳を作り、durable に保存する。

なぜ必要か
----------
``scripts/paper_exit_check.py`` は exit の **意図** (``results_csv/exit_orders_*.json``)
しか残さない。その後 *実際に決済されたか / いくらだったか* を記録する場所が
どこにも無く、実現損益 (realized P&L) が系のどこにも存在しなかった
= 「exit が未計測」。この script がその穴を埋める。

やること (すべて read-only。発注も決済も一切しない)
-------------------------------------------------
1. ``/v2/account/activities/FILL`` を全 page 取得 (約定の ground truth)
2. FIFO で round-trip を再構成 → 決済済みトレードと実現損益を確定
3. broker の実 position と突合 → 食い違いを **未計測** として明示
4. 同日の ``exit_orders_YYYYMMDD.json`` の意図と突合
   → 「exit するつもりだったのに約定していない」を検出
5. ``results_csv/exit_ledger_YYYYMMDD.json`` に run_id つきで書き出し

exit code
---------
    0 : 計測できた (measured=true, 取りこぼし無し)
    1 : 取得エラー (broker 不通など)
    2 : safety abort (paper でない環境)
    3 : **未計測を検知** (measured=false もしくは意図した exit が未約定)
        -- silent success を作らないため 0 と区別する。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import sys
from typing import Any

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common import broker_alpaca as ba  # noqa: E402
from common.alpaca_trading import (  # noqa: E402
    LiveAccountGuardError,
    assert_paper_env,
    parse_system_from_client_order_id,
)
from common.exit_ledger import (  # noqa: E402
    MARKET_TZ,
    SESSION_BEFORE_OPEN,
    SESSION_CLOSED,
    SESSION_OPEN,
    SESSION_UNKNOWN,
    ExitLedgerError,
    parse_fills,
    realized_by_day,
    realized_cumulative,
    reconcile_intents_with_fills,
    reconcile_with_broker,
    reconstruct_round_trips,
    summarize_by_system,
    summarize_realized,
)
from common.signal_export import generate_run_id  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]
SCHEMA = "exit_ledger/v1"

EXIT_OK = 0
EXIT_FETCH_ERROR = 1
EXIT_SAFETY_ABORT = 2
EXIT_UNMEASURED = 3


def _today_str() -> str:
    """対象日の既定値 = *ローカル* 日付。

    ``exit_orders_YYYYMMDD.json`` は ``daily_pipeline.ps1`` が
    ``Get-Date -Format yyyy-MM-dd`` (= ローカル/JST) で命名するのでそれに合わせる。
    JST 早朝 (06:00) は UTC だと前日なので、UTC 日付にすると前日の意図ファイルを
    読んでしまい「exit 予定が全部未約定」の偽陽性になる。
    """
    return datetime.now().strftime("%Y-%m-%d")


def resolve_session_state(target_session: str, clock: Any) -> str:
    """対象立会日が broker clock 基準で「まだ来ていない / 進行中 / 終わった」か。

    判定できない (clock 不通など) 時は ``unknown``。unknown は
    :func:`reconcile_intents_with_fills` 側で *取りこぼしを表に出す* 方に倒す。
    """
    if clock is None:
        return SESSION_UNKNOWN
    try:
        now_et = str(pd.Timestamp(clock.timestamp).tz_convert(MARKET_TZ).date())
    except Exception:
        return SESSION_UNKNOWN
    if target_session > now_et:
        return SESSION_BEFORE_OPEN
    if target_session < now_et:
        return SESSION_CLOSED
    if bool(getattr(clock, "is_open", False)):
        return SESSION_OPEN
    try:
        next_open = str(pd.Timestamp(clock.next_open).tz_convert(MARKET_TZ).date())
    except Exception:
        return SESSION_CLOSED
    # 同日で market closed: next_open が同じ日なら「寄り前」、翌日以降なら「引け後」。
    return SESSION_BEFORE_OPEN if next_open == target_session else SESSION_CLOSED


# ---------------------------------------------------------------------------
# broker 取得 (read-only)
# ---------------------------------------------------------------------------


def fetch_all_fills(
    client: Any, *, page_size: int = 100, max_pages: int = 400
) -> list[dict[str, Any]]:
    """FILL activity を全 page 取得する。

    Alpaca の activities API は ``page_token`` に *直前 page の最終 id* を渡す
    cursor 方式。取り切れなかった場合に黙って短い list を返さないよう、
    上限に当たったら例外にする (silent truncation 防止)。
    """
    out: list[dict[str, Any]] = []
    token: str | None = None
    for _ in range(max_pages):
        params: dict[str, Any] = {"page_size": page_size}
        if token:
            params["page_token"] = token
        batch = client.get("/account/activities/FILL", params)
        if not batch:
            return out
        out.extend(batch)
        if len(batch) < page_size:
            return out
        token = batch[-1].get("id")
        if not token:
            return out
    raise ExitLedgerError(
        f"FILL activity の page 上限 ({max_pages}) に到達。取りこぼしの可能性があるため中断する。"
    )


def fetch_broker_positions(client: Any) -> dict[str, float]:
    """symbol -> 符号つき qty。"""
    out: dict[str, float] = {}
    for p in client.get_all_positions() or []:
        try:
            out[str(p.symbol).upper()] = float(p.qty)
        except (TypeError, ValueError):
            continue
    return out


def build_system_map(client: Any, results_dir: Path) -> dict[str, str]:
    """symbol -> system tag。client_order_id と保存済み map の両方から拾う。"""
    mapping: dict[str, str] = {}

    map_file = ROOT / "data" / "symbol_system_map.json"
    if map_file.exists():
        try:
            raw = json.loads(map_file.read_text(encoding="utf-8")) or {}
            for k, v in raw.items():
                if isinstance(v, str):
                    mapping[str(k).upper()] = v
        except (OSError, ValueError):
            pass

    # 発注 intent 側 (paper_orders_*.json) の system tag を上書き適用
    for f in sorted(results_dir.glob("paper_orders_*.json"), reverse=True)[:60]:
        try:
            data = json.loads(f.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            continue
        for row in data.get("orders", []) or []:
            sym = str(row.get("symbol", "")).upper()
            if not sym:
                continue
            tag = row.get("system") or parse_system_from_client_order_id(
                row.get("client_order_id")
            )
            if tag:
                mapping.setdefault(sym, tag)
    return mapping


def load_exit_intents(results_dir: Path, date_compact: str) -> list[dict[str, Any]]:
    """その日の exit_orders_YYYYMMDD.json の ``exits`` 行を返す (無ければ空)。"""
    path = results_dir / f"exit_orders_{date_compact}.json"
    if not path.exists():
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8")) or {}
    except (OSError, ValueError):
        return []
    return list(data.get("exits") or [])


def _attach_exit_reasons(
    trades: list[Any], intents: list[dict[str, Any]], session_date: str
) -> None:
    """当日決済分に exit_orders 側の reason を付ける (付かない分は None のまま)。"""
    reason_by_symbol: dict[str, str] = {}
    for row in intents:
        sym = str(row.get("symbol", "")).upper()
        reason = row.get("reason")
        if sym and reason:
            reason_by_symbol.setdefault(sym, str(reason))
    for t in trades:
        if t.exit_session == session_date:
            t.exit_reason = reason_by_symbol.get(t.symbol)


def attach_historical_exit_reasons(
    trades: list[Any], results_dir: Path, *, max_files: int = 120
) -> int:
    """過去の ``exit_orders_*.json`` からも exit 理由を復元する (当日分以外)。

    exit 理由が付かないと dashboard の履歴が全部「不明」になる。意図ファイルは
    立会日ごとに残っているので、``(立会日, symbol)`` で引ければ復元できる。
    引けないものは **推測しない** (``None`` のまま = 「記録なし」と表示する)。

    戻り値 = 理由を付けられた trade 件数。
    """
    by_session: dict[str, dict[str, str]] = {}
    for path in sorted(results_dir.glob("exit_orders_*.json"), reverse=True)[
        :max_files
    ]:
        stem = path.stem.replace("exit_orders_", "")
        if len(stem) != 8 or not stem.isdigit():
            continue
        session = f"{stem[:4]}-{stem[4:6]}-{stem[6:]}"
        try:
            data = json.loads(path.read_text(encoding="utf-8")) or {}
        except (OSError, ValueError):
            continue
        bucket = by_session.setdefault(session, {})
        for row in data.get("exits") or []:
            sym = str(row.get("symbol", "")).upper()
            reason = row.get("reason")
            if sym and reason:
                bucket.setdefault(sym, str(reason))

    tagged = 0
    for t in trades:
        if t.exit_reason:
            continue
        reason = (by_session.get(t.exit_session) or {}).get(t.symbol)
        if reason:
            t.exit_reason = reason
            tagged += 1
    return tagged


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (default: today UTC)"
    )
    parser.add_argument("--output-json", default=None)
    parser.add_argument("--results-dir", default=str(ROOT / "results_csv"))
    parser.add_argument("--run-id", default=None, help="上流 run と紐付ける run_id")
    parser.add_argument(
        "--fail-on-unmeasured",
        action="store_true",
        help="未計測を検知したら exit 3 を返す (default: 検知しても 0。CI/監視では有効化推奨)",
    )
    parser.add_argument(
        "--no-alpaca", action="store_true", help="broker に接続しない (offline test)"
    )
    args = parser.parse_args(argv)

    date_str = args.date or _today_str()
    date_compact = date_str.replace("-", "")
    results_dir = Path(args.results_dir)
    output_path = (
        Path(args.output_json)
        if args.output_json
        else results_dir / f"exit_ledger_{date_compact}.json"
    )
    run_id = args.run_id or os.getenv("QTS_RUN_ID") or generate_run_id()

    if args.no_alpaca:
        print("[info] --no-alpaca 指定: broker に接続せず終了 (ledger 未生成)")
        return EXIT_OK

    try:
        assert_paper_env()
    except LiveAccountGuardError as exc:
        print(f"[SAFETY ABORT] {exc}")
        return EXIT_SAFETY_ABORT

    try:
        client = ba.get_client(paper=True)
    except Exception as exc:
        print(f"[ERROR] Alpaca client 取得失敗: {exc}")
        return EXIT_FETCH_ERROR

    try:
        raw_fills = fetch_all_fills(client)
        fills = parse_fills(raw_fills)
        broker_positions = fetch_broker_positions(client)
    except Exception as exc:
        print(f"[ERROR] 約定履歴 / position の取得に失敗: {exc}")
        return EXIT_FETCH_ERROR

    try:
        clock = client.get_clock()
    except Exception as exc:  # clock が引けなくても台帳自体は作る (状態は unknown)
        print(f"[WARN] broker clock 取得失敗 ({exc}) -> session_state=unknown")
        clock = None
    session_state = resolve_session_state(date_str, clock)

    result = reconstruct_round_trips(fills)
    reconcile_with_broker(result, broker_positions)

    system_map = build_system_map(client, results_dir)
    for t in result.closed_trades:
        t.system = system_map.get(t.symbol)

    intents = load_exit_intents(results_dir, date_compact)
    _attach_exit_reasons(result.closed_trades, intents, date_str)
    attach_historical_exit_reasons(result.closed_trades, results_dir)
    intent_recon = reconcile_intents_with_fills(
        intents,
        result.closed_trades,
        session_date=date_str,
        session_state=session_state,
    )

    by_day = realized_by_day(result.closed_trades)
    today_trades = [t for t in result.closed_trades if t.exit_session == date_str]
    today_realized = by_day.get(date_str)

    # 「約定 ground truth を掴めているか」(measured) と
    # 「取りこぼしがゼロか」(complete) を分ける。
    # ticker rename のような symbol 単位の綻びで台帳全体を無効化しない。
    measured = result.measured
    completeness_reasons = result.measurement_reasons()
    if not intent_recon["fully_reconciled"]:
        n = len(intent_recon["intended_not_filled"])
        completeness_reasons.append(
            f"exit_intent_not_filled: exit 予定 {n} 件が当日約定していない (未執行 or 約定記録の取りこぼし)"
        )
    complete = measured and not completeness_reasons
    n_pending = len(intent_recon["intended_pending"])

    # 当日分だけのスコープ。当日決済した symbol に綻びが無く、当日の exit 意図が
    # すべて約定していれば「今日の実現損益」は信用してよい。
    today_unmeasured = sorted(
        {t.symbol for t in today_trades} & set(result.unmeasured_symbols)
    )
    today_measured = (
        measured and not today_unmeasured and intent_recon["fully_reconciled"]
    )
    today_reasons: list[str] = []
    if not measured:
        today_reasons.append("no_fill_activities: 約定履歴が取得できていない")
    if today_unmeasured:
        today_reasons.append(
            f"lot_mismatch: 当日決済 symbol に建玉不一致 [{', '.join(today_unmeasured)}]"
        )
    if not intent_recon["fully_reconciled"]:
        today_reasons.append(
            f"exit_intent_not_filled: exit 予定 {len(intent_recon['intended_not_filled'])} 件が未約定"
        )

    payload: dict[str, Any] = {
        "schema": SCHEMA,
        "date": date_str,
        "run_id": run_id,
        "generated_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "provider": "alpaca_paper",
        "measurement": {
            "measured": measured,
            "complete": complete,
            "reasons": completeness_reasons,
            "fills_seen": result.fills_seen,
            "coverage_start": result.coverage_start,
            "coverage_end": result.coverage_end,
            "unmeasured_symbols": result.unmeasured_symbols,
            "discrepancies": [d.to_row() for d in result.discrepancies],
        },
        "today": {
            "date": date_str,
            # 「当日 exit が 1 件も無かった」(= 0, 事実) と
            # 「計測できていない」(= None, 不明) を絶対に混同しない。
            "realized_pl": (
                round(float(today_realized), 2)
                if today_realized is not None
                else (0.0 if measured else None)
            ),
            "n_closed": len(today_trades),
            "measured": today_measured,
            "reasons": today_reasons,
            # 立会の進行状態。closed 以外は「まだ確定していない途中経過」。
            "session_state": session_state,
            "final": session_state == SESSION_CLOSED,
            "pending_exit_intents": n_pending,
        },
        "realized": {
            "all_time": summarize_realized(result.closed_trades),
            "by_day": realized_cumulative(by_day),
            "by_system": summarize_by_system(result.closed_trades),
        },
        "closed_trades": [t.to_row() for t in result.closed_trades],
        "exit_intent_reconciliation": intent_recon,
    }

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)

    summ = payload["realized"]["all_time"]
    print(
        f"[exit_ledger] fills={result.fills_seen} closed_trades={summ['n_trades']} "
        f"realized_all_time={summ['total_realized_pl']} win_rate={summ['win_rate_pct']}% "
        f"today_realized={payload['today']['realized_pl']} (n={payload['today']['n_closed']}) "
        f"session={date_str}/{session_state} pending_intents={n_pending} "
        f"measured={measured} complete={complete} run_id={run_id}"
    )
    for r in completeness_reasons:
        print(f"[exit_ledger][UNMEASURED] {r}")
    print(f"[write] {output_path}")

    if not complete and args.fail_on_unmeasured:
        return EXIT_UNMEASURED
    return EXIT_OK


if __name__ == "__main__":
    raise SystemExit(main())
