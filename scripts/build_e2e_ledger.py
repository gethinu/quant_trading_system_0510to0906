"""E2E paper-trading measurement ledger (READ-ONLY consolidation).

サービスイン基準 = 「Alpaca paper で entry + exit が E2E で完結して1週間実測」。
その「durable な記録の仕組み」がこれ。日次ランナー (open_auto_run) が既に書く
per-day 成果物を 1 行/取引日に集約し、E2E が実際に回った日を客観化する。

読むだけ (発注・cancel・変更は一切しない)。入力 (results_csv/、全て日次生成):
    - recon_{YYYYMMDD}.json         : entry/exit submitted・filled 等の実行サマリ
    - exit_orders_{YYYYMMDD}.json   : exit の mode / submitted / failed
    - paper_orders_{YYYYMMDD}.json  : entry の mode (submitted/dry_run)
    - alpaca_snapshot_{YYYYMMDD}.json: equity・保有数・ledger desync・期限超過 exit 数

出力:
    - logs/e2e_measurement/ledger.jsonl  (1 行/日, upsert)
    - logs/e2e_measurement/ledger.md     (人間可読サマリ)

「E2E clean な取引日」の判定 (measurement 起点/継続の基準):
    ran(submitted mode) かつ time_exit_failed==0 かつ n_desync==0 かつ overdue_exits==0。
    (protect_* の重複拒否は保護が既に有効な印なので clean を妨げない。)
    連続 5 取引日 clean で「1週間 E2E 実測」達成。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]


def _load(p: Path) -> dict[str, Any] | None:
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _dates(results_dir: Path) -> list[str]:
    """recon か snapshot が存在する全日付 (YYYYMMDD) を昇順で。"""
    out: set[str] = set()
    for prefix in ("recon_", "alpaca_snapshot_"):
        for f in results_dir.glob(f"{prefix}*.json"):
            digits = "".join(c for c in f.stem if c.isdigit())[:8]
            if len(digits) == 8:
                out.add(digits)
    return sorted(out)


def _overdue_exits(snap: dict[str, Any] | None) -> int | None:
    if not snap:
        return None
    n = 0
    for p in snap.get("positions", []) or []:
        if p.get("exit_expected") == "time_based" or (
            p.get("days_remaining") is not None
            and p["days_remaining"] <= 0
            and p.get("max_holding_days", 0) > 0
        ):
            n += 1
    return n


def build_row(results_dir: Path, d8: str) -> dict[str, Any]:
    iso = f"{d8[:4]}-{d8[4:6]}-{d8[6:]}"
    recon = _load(results_dir / f"recon_{d8}.json")
    eo = _load(results_dir / f"exit_orders_{d8}.json")
    po = _load(results_dir / f"paper_orders_{d8}.json")
    snap = _load(results_dir / f"alpaca_snapshot_{d8}.json")

    port = (recon or {}).get("portfolio", {}) if recon else {}
    exit_mode = (eo or {}).get("mode")
    entry_mode = (po or {}).get("mode")
    # "ran" = 実発注モードで exit/entry のどちらかが submitted 由来
    ran = exit_mode == "submitted" or entry_mode == "submitted"

    exit_failed = (eo or {}).get("failed")
    # 失敗を「有害 (time/breakout の full close が失敗=ポジション未 exit)」と
    # 「無害 (protect_* の再発注が held_for_orders で重複拒否=保護は既に有効)」に分離。
    time_exit_failed = protect_dup_failed = None
    if eo and isinstance(eo.get("exits"), list):
        time_exit_failed = protect_dup_failed = 0
        for e in eo["exits"]:
            if not e.get("error"):
                continue
            reason = str(e.get("reason") or "")
            if reason in ("time_based", "spy_breakout"):
                time_exit_failed += 1
            elif reason.startswith("protect"):
                protect_dup_failed += 1
    lr = (snap or {}).get("ledger_reconciliation", {}) if snap else {}
    n_desync = lr.get("n_desync")
    overdue = _overdue_exits(snap)
    acct = (snap or {}).get("account", {}) if snap else {}

    # clean は「有害な失敗」だけを見る: time/breakout close 失敗が 0、desync 0、
    # 期限超過 exit 0。protect_* の重複拒否 (protect_dup_failed) は保護が既に有効な
    # ことを意味するので clean を妨げない。
    clean = bool(ran and (time_exit_failed == 0) and (n_desync == 0) and (overdue == 0))

    return {
        "date": iso,
        "ran": ran,
        "exit_mode": exit_mode,
        "entry_mode": entry_mode,
        "signals": port.get("signals"),
        "orders_generated": port.get("orders_generated"),
        "entry_submitted": port.get("entry_submitted"),
        "entry_filled": port.get("entry_filled"),
        "entry_failed": port.get("entry_failed"),
        "entry_skipped": port.get("entry_skipped"),
        "exit_submitted": port.get("exit_submitted"),
        "exit_close_time": port.get("exit_close"),
        "exit_protect": port.get("exit_protect"),
        "exit_failed": exit_failed,
        "time_exit_failed": time_exit_failed,
        "protect_dup_failed": protect_dup_failed,
        "n_desync": n_desync,
        "overdue_exits": overdue,
        "equity": acct.get("equity"),
        "n_positions": (
            (snap or {}).get("summary", {}).get("n_positions") if snap else None
        ),
        "e2e_clean": clean,
        "artifacts": {
            "recon": recon is not None,
            "exit_orders": eo is not None,
            "paper_orders": po is not None,
            "snapshot": snap is not None,
        },
    }


def _write_md(rows: list[dict[str, Any]], path: Path) -> None:
    lines = [
        "# E2E paper measurement ledger",
        "",
        "サービスイン基準 = Alpaca paper で entry+exit が E2E 完結して **連続5取引日** clean。",
        "clean = ran(submitted) & time_exit_failed==0 & n_desync==0 & overdue_exits==0。",
        "",
        "harmful = time/breakout close 失敗 (ポジション未 exit)。dup = protect_* 重複拒否 (無害)。",
        "",
        "| date | ran | mode(x/e) | entry sub/fill/fail | exit sub/close | time-fail(harmful) | dup(benign) | desync | overdue | equity | pos | CLEAN |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
    ]
    for r in rows:
        mode = f"{(r['exit_mode'] or '-')[:3]}/{(r['entry_mode'] or '-')[:3]}"
        lines.append(
            f"| {r['date']} | {'Y' if r['ran'] else '.'} | {mode} "
            f"| {r['entry_submitted']}/{r['entry_filled']}/{r['entry_failed']} "
            f"| {r['exit_submitted']}/{r['exit_close_time']} "
            f"| {r['time_exit_failed']} | {r['protect_dup_failed']} "
            f"| {r['n_desync']} | {r['overdue_exits']} "
            f"| {r['equity']} | {r['n_positions']} | {'✅' if r['e2e_clean'] else '—'} |"
        )
    # streak summary
    streak = 0
    best = 0
    for r in rows:
        streak = streak + 1 if r["e2e_clean"] else 0
        best = max(best, streak)
    lines += [
        "",
        f"**最長 E2E-clean 連続取引日: {best} / 5**  "
        f"(直近: {'clean' if rows and rows[-1]['e2e_clean'] else 'not clean'})",
    ]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--results-dir", default=str(ROOT / "results_csv"))
    ap.add_argument("--out-dir", default=str(ROOT / "logs" / "e2e_measurement"))
    args = ap.parse_args(argv)
    results_dir = Path(args.results_dir)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    rows = [build_row(results_dir, d8) for d8 in _dates(results_dir)]
    (out_dir / "ledger.jsonl").write_text(
        "\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
        encoding="utf-8",
    )
    _write_md(rows, out_dir / "ledger.md")
    clean_days = sum(1 for r in rows if r["e2e_clean"])
    print(f"[e2e_ledger] {len(rows)} trading-days scanned, {clean_days} E2E-clean")
    print(f"[write] {out_dir / 'ledger.jsonl'}")
    print(f"[write] {out_dir / 'ledger.md'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
