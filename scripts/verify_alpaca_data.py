#!/usr/bin/env python3
"""#9 verify: alpaca_snapshot / exit_ledger が「本当に」生成できたかを schema で検証する。

生成 (host で Alpaca paper に read-only 接続) の **後段** に置く確定チェック。
発注・課金・書き込みは一切しない。json を読むだけ。1 つでも落ちたら exit!=0 +
理由を stdout に出す (silent success = exit0+空 output を禁止する #9 の教訓)。

使い方 (host, repo root から):
    python verify_alpaca_data.py --date 2026-08-04 --results-dir results_csv

検証内容:
  snapshot (alpaca_snapshot_YYYYMMDD.json, schema alpaca_snapshot/v1):
    - schema 一致 / 必須トップキー存在
    - generated_at が ISO 8601 で parse でき、対象日から 36h 以内 (鮮度)
    - account.equity が数値 > 0
    - equity_curve.points が非空 (行数 > 0) で t が **単調増加** (連続性)
    - 最終 point の日付 == 対象日 (当日反映)
    - positions が list、summary.n_positions == len(positions) (整合)
  exit_ledger (exit_ledger_YYYYMMDD.json, schema exit_ledger/v1):
    - schema 一致 / 必須トップキー存在
    - generated_at parse 可
    - closed_trades が list で行数 > 0、各行に必須列、realized_pl が有限値
    - realized.all_time 存在
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import math
from pathlib import Path

SNAP_SCHEMA = "alpaca_snapshot/v1"
LEDGER_SCHEMA = "exit_ledger/v1"

SNAP_TOP = {
    "schema",
    "date",
    "generated_at",
    "provider",
    "account",
    "equity_curve",
    "summary",
    "positions",
}
LEDGER_TOP = {"schema", "date", "generated_at", "provider", "closed_trades", "realized"}
TRADE_COLS = {"symbol", "side", "qty", "exit_time", "realized_pl"}


def _parse_ts(s):
    if not isinstance(s, str):
        return None
    t = s.strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(t)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def verify_snapshot(path: Path, date_str: str, errs: list[str]) -> None:
    tag = f"snapshot({path.name})"
    if not path.is_file():
        errs.append(f"{tag}: ファイルが存在しない (生成されていない)")
        return
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        errs.append(f"{tag}: JSON 破損 ({e})")
        return
    if d.get("schema") != SNAP_SCHEMA:
        errs.append(f"{tag}: schema={d.get('schema')!r} != {SNAP_SCHEMA!r}")
    missing = SNAP_TOP - set(d)
    if missing:
        errs.append(f"{tag}: 必須トップキー欠落 {sorted(missing)}")
    gen = _parse_ts(d.get("generated_at"))
    if gen is None:
        errs.append(f"{tag}: generated_at 不正 ({d.get('generated_at')!r})")
    else:
        base = _parse_ts(date_str + "T00:00:00+00:00")
        if base and abs((gen - base).total_seconds()) > 36 * 3600:
            errs.append(
                f"{tag}: generated_at {gen.isoformat()} が対象日 {date_str} から 36h 超 (stale)"
            )
    acct = d.get("account") or {}
    eq = acct.get("equity")
    if not isinstance(eq, (int, float)) or not eq > 0:
        errs.append(f"{tag}: account.equity が正の数値でない ({eq!r})")
    ec = d.get("equity_curve") or {}
    pts = ec.get("points") or []
    if len(pts) < 1:
        errs.append(f"{tag}: equity_curve.points が空 (行数 0)")
    else:
        last_t = None
        prev = None
        broke = False
        for p in pts:
            t = p.get("t")
            if t is None:
                errs.append(f"{tag}: equity_curve point に t 欠落")
                broke = True
                break
            if prev is not None and str(t) < str(prev):
                errs.append(f"{tag}: equity_curve.t が単調増加でない ({prev} -> {t})")
                broke = True
                break
            prev = t
            last_t = t
        if not broke and last_t is not None and str(last_t)[:10] != date_str:
            errs.append(
                f"{tag}: equity_curve 最終 t={last_t} が対象日 {date_str} でない (当日未反映)"
            )
    pos = d.get("positions")
    if not isinstance(pos, list):
        errs.append(f"{tag}: positions が list でない")
    else:
        n = (d.get("summary") or {}).get("n_positions")
        if isinstance(n, int) and n != len(pos):
            errs.append(f"{tag}: summary.n_positions={n} != len(positions)={len(pos)}")


def verify_ledger(path: Path, date_str: str, errs: list[str]) -> None:
    tag = f"exit_ledger({path.name})"
    if not path.is_file():
        errs.append(f"{tag}: ファイルが存在しない (生成されていない)")
        return
    try:
        d = json.loads(path.read_text(encoding="utf-8"))
    except Exception as e:  # noqa: BLE001
        errs.append(f"{tag}: JSON 破損 ({e})")
        return
    if d.get("schema") != LEDGER_SCHEMA:
        errs.append(f"{tag}: schema={d.get('schema')!r} != {LEDGER_SCHEMA!r}")
    missing = LEDGER_TOP - set(d)
    if missing:
        errs.append(f"{tag}: 必須トップキー欠落 {sorted(missing)}")
    if _parse_ts(d.get("generated_at")) is None:
        errs.append(f"{tag}: generated_at 不正 ({d.get('generated_at')!r})")
    ct = d.get("closed_trades")
    if not isinstance(ct, list):
        errs.append(f"{tag}: closed_trades が list でない")
    elif len(ct) < 1:
        errs.append(f"{tag}: closed_trades 行数 0 (決済台帳が空)")
    else:
        for i, row in enumerate(ct[:2000]):
            miss = TRADE_COLS - set(row)
            if miss:
                errs.append(f"{tag}: closed_trades[{i}] 必須列欠落 {sorted(miss)}")
                break
            rp = row.get("realized_pl")
            if isinstance(rp, float) and (math.isnan(rp) or math.isinf(rp)):
                errs.append(f"{tag}: closed_trades[{i}].realized_pl が非有限値")
                break
    if not isinstance((d.get("realized") or {}).get("all_time"), dict):
        errs.append(f"{tag}: realized.all_time 欠落")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--date", required=True, help="対象日 YYYY-MM-DD")
    ap.add_argument("--results-dir", default="results_csv")
    ap.add_argument(
        "--skip-ledger",
        action="store_true",
        help="exit_ledger を検証しない (snapshot のみ確認したい時)",
    )
    args = ap.parse_args(argv)

    dc = args.date.replace("-", "")
    rd = Path(args.results_dir)
    errs: list[str] = []
    verify_snapshot(rd / f"alpaca_snapshot_{dc}.json", args.date, errs)
    if not args.skip_ledger:
        verify_ledger(rd / f"exit_ledger_{dc}.json", args.date, errs)

    if errs:
        print(f"[verify] FAIL ({len(errs)} 件) — {args.date}")
        for e in errs:
            print("  - " + e)
        return 1
    print(
        f"[verify] OK — {args.date}: snapshot / exit_ledger の schema・行数・連続性すべて検証通過"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
