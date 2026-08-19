"""replay_portfolio_caps — portfolio cap を「固定 100k」vs「実 equity」で再生する。

**完全オフライン / 発注ゼロ**: Alpaca API を一切叩かない。既存の
``results_csv/today_signals_<date>.json`` (post-cap signals + cap report) と
``results_csv/alpaca_equity_history.json`` (実 equity 履歴) だけを読み、
本物の ``core.final_allocation._apply_portfolio_caps`` を再実行する。

なぜ履歴リプレイなのか
----------------------
現在 book は delisted/orphan の建玉が占有しており (held_unmapped が held の大半)、
「今日のライブ」で測ると monopoly の影響と cap 単体の影響が混ざる。過去日の
cap report を入力に固定して equity 基準だけを差し替えれば、**cap 単体の効果**を
isolate できる。

arm (計測腕)
------------
    A_fixed100k       現行本番。equity_base = default_capital = 100,000
    B_real_equity     本 fix。equity_base = その日の実口座 equity
    C_no_orphan       診断専用の counterfactual。equity は実値のまま、held から
                      unmapped (delisted/orphan) を除いた場合。**提案する変更では
                      ない** — 「cap か、それとも held 独占か」を切り分けるため。

pre-cap frame の再構成 (前提の明示)
-----------------------------------
today_signals JSON は **cap 適用後**の signals しか持たない。そこで:

  - 採用された行 (kept): JSON の ``weight * portfolio.total_notional_usd`` を
    position_value として **実測値**を使う。
  - trim された行: position_value が残っていないため **仮定値**を置く
    (``--assumed-pv-mode``)。既定 ``mean`` = その日の kept 行の平均 notional。
    ``budget`` は per-system 予算 (100,000 * 0.5 * 0.25 / 10 本) を使う保守側。
  - 行の順序は ``_sort_final_frame`` と同じ side (long→short) → system 番号。
  - trim 行の system 配分: cap は frame の**先頭から keep** するので、
    「最後に採用のあった system」(boundary) より前の system は pre-cap 本数 =
    entry_count で確定する (それらの candidate_count との差は cap ではなく
    上流の dedup / sizing で落ちた分)。boundary 以降の system にだけ、記録された
    trim 件数を ``candidate_count - entry_count`` を上限として順に埋め戻す。

再構成が正しいかは **自己検証**する: arm A (現行本番と同じ入力) を流し、その日
実際に記録された per-system 本数 / allow / kept / trimmed を再現できたかを
``reconstruction_matches_recorded`` に出す。False の日は B/C の比較も信用しない。

なお仮定値 (assumed_pv) は結論に影響しない: 件数 cap は position_value に依存せず、
かつ exposure cap はループ上 **件数 cap の後**に評価されるため、件数 cap で落ちた行は
equity をいくら増やしても復活しない。``--assumed-pv-mode`` を振っても allow_long は
動かない (exposure cap 側の感度を見るためのつまみ)。

使い方::

    python scripts/replay_portfolio_caps.py                      # 全日 / 表 + JSON
    python scripts/replay_portfolio_caps.py --assumed-pv-mode budget
    python scripts/replay_portfolio_caps.py --out logs/cap_replay.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from typing import Any

import pandas as pd

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from core.final_allocation import _apply_portfolio_caps  # noqa: E402

LONG_SYSTEMS = ["system1", "system3", "system4", "system5"]
SHORT_SYSTEMS = ["system2", "system6", "system7"]
DEFAULT_CAPITAL = 100000.0
DEFAULT_LONG_RATIO = 0.5


def _sys_key(name: str) -> str:
    """``sys5`` -> ``system5``."""
    return name.replace("sys", "system")


def _load_equity_history(results_dir: Path) -> dict[str, float]:
    fp = results_dir / "alpaca_equity_history.json"
    if not fp.exists():
        return {}
    try:
        rows = json.loads(fp.read_text(encoding="utf-8"))
    except Exception:
        return {}
    out: dict[str, float] = {}
    for row in rows or []:
        try:
            out[str(row["t"])] = float(row["equity"])
        except Exception:
            continue
    return out


def _synth_positions(
    held: dict[str, int], unmapped: dict[str, int]
) -> tuple[list[dict], dict]:
    """記録された held / held_unmapped を再現する合成ポジション + symbol map。"""
    positions: list[dict] = []
    smap: dict[str, list[str]] = {}
    mapped_long = max(0, int(held.get("long", 0)) - int(unmapped.get("long", 0)))
    mapped_short = max(0, int(held.get("short", 0)) - int(unmapped.get("short", 0)))
    for i in range(mapped_long):
        sym = f"HL{i}"
        positions.append({"symbol": sym, "qty": 10, "side": "long"})
        smap[sym] = ["system1"]
    for i in range(mapped_short):
        sym = f"HS{i}"
        positions.append({"symbol": sym, "qty": -10, "side": "short"})
        smap[sym] = ["system2"]
    for i in range(int(unmapped.get("long", 0))):
        positions.append({"symbol": f"UL{i}", "qty": 10, "side": "long"})
    for i in range(int(unmapped.get("short", 0))):
        positions.append({"symbol": f"US{i}", "qty": -10, "side": "short"})
    return positions, smap


def _build_precap_frame(
    payload: dict, assumed_pv_mode: str
) -> tuple[pd.DataFrame, dict[str, Any]]:
    """post-cap JSON から pre-cap final_df を再構成する。"""
    portfolio = payload.get("portfolio") or {}
    caps_report = portfolio.get("caps") or {}
    total_notional = float(portfolio.get("total_notional_usd") or 0.0)
    systems = payload.get("systems") or {}

    kept_rows: dict[str, list[dict[str, Any]]] = {}
    kept_pvs: list[float] = []
    headroom: dict[str, int] = {}  # candidate_count - entry_count (= cap で落ちた上限)
    for name, block in systems.items():
        key = _sys_key(str(name))
        side = "long" if key in LONG_SYSTEMS else "short"
        rows_for_system: list[dict[str, Any]] = []
        for sig in block.get("signals") or []:
            pv = float(sig.get("weight") or 0.0) * total_notional
            kept_pvs.append(pv)
            rows_for_system.append(
                {
                    "symbol": str(sig.get("symbol")),
                    "system": key,
                    "side": side,
                    "position_value": round(pv, 4),
                    "_origin": "kept",
                }
            )
        kept_rows[key] = rows_for_system
        funnel = block.get("funnel") or {}
        try:
            headroom[key] = max(
                0,
                int(funnel.get("candidate_count") or 0)
                - int(funnel.get("entry_count") or 0),
            )
        except (TypeError, ValueError):
            headroom[key] = 0

    mean_pv = (sum(kept_pvs) / len(kept_pvs)) if kept_pvs else 0.0
    max_pv = max(kept_pvs) if kept_pvs else 0.0
    # per-system 予算 / 1 銘柄 (long: 100k*0.5*0.25/10 = $1,250)
    budget_pv = DEFAULT_CAPITAL * DEFAULT_LONG_RATIO * 0.25 / 10.0
    assumed_pv = {"mean": mean_pv, "max": max_pv, "budget": budget_pv}[assumed_pv_mode]

    recorded_trims = caps_report.get("trimmed") or {}
    # "total" 理由の trim は frame の末尾 = short 側で起きる (loop は long を先に舐める)。
    # 観測 7 日ではいずれも 0 件なので、この振り分けは結論に影響しない。
    want = {
        "long": int(recorded_trims.get("long_count", 0)),
        "short": int(recorded_trims.get("short_count", 0))
        + int(recorded_trims.get("total", 0)),
    }

    rows: list[dict[str, Any]] = []
    truncated: dict[str, int] = {}
    for side, systems_for_side in (("long", LONG_SYSTEMS), ("short", SHORT_SYSTEMS)):
        remaining = want[side]
        # boundary = その side で最後に採用のあった system。これより前の system の
        # 「candidate_count - entry_count」は cap ではなく上流 (dedup / sizing) で
        # 落ちた分なので、trim 行として埋め戻してはいけない。
        served = [k for k in systems_for_side if kept_rows.get(k)]
        boundary = systems_for_side.index(served[-1]) if served else 0
        for pos, key in enumerate(systems_for_side):
            rows.extend(kept_rows.get(key, []))
            if pos < boundary:
                continue
            take = min(remaining, headroom.get(key, 0))
            for i in range(take):
                rows.append(
                    {
                        "symbol": f"T_{key}_{i}",
                        "system": key,
                        "side": side,
                        "position_value": round(assumed_pv, 4),
                        "_origin": "assumed",
                    }
                )
            remaining -= take
        if remaining > 0:
            truncated[side] = remaining

    meta = {
        "assumed_pv_mode": assumed_pv_mode,
        "assumed_pv_usd": round(assumed_pv, 2),
        "n_kept_rows": sum(len(v) for v in kept_rows.values()),
        "n_assumed_rows": sum(1 for r in rows if r["_origin"] == "assumed"),
        "recorded_trimmed": dict(recorded_trims),
        "unplaced_trims": truncated,  # headroom で吸収しきれなかった分 (通常 0)
        "recorded_entry_counts": {
            _sys_key(str(k)): int((v.get("funnel") or {}).get("entry_count") or 0)
            for k, v in systems.items()
        },
    }
    return pd.DataFrame(rows), meta


def _run_arm(
    df: pd.DataFrame, caps: dict, positions, smap, equity: float, source: str | None
):
    out, report = _apply_portfolio_caps(
        df.drop(columns=["_origin"]),
        caps=caps,
        active_positions=positions,
        symbol_system_map=smap,
        long_systems=LONG_SYSTEMS,
        short_systems=SHORT_SYSTEMS,
        equity=equity,
        equity_source=source,
    )
    per_system = {}
    if not out.empty:
        per_system = {
            str(k): int(v) for k, v in out["system"].astype(str).value_counts().items()
        }
    return report, per_system


def replay_day(
    fp: Path, equity_hist: dict[str, float], assumed_pv_mode: str
) -> dict[str, Any] | None:
    payload = json.loads(fp.read_text(encoding="utf-8"))
    portfolio = payload.get("portfolio") or {}
    recorded = portfolio.get("caps") or {}
    if not recorded.get("applied"):
        return None

    date = str(payload.get("date") or fp.stem.replace("today_signals_", ""))
    caps_cfg = {
        "max_total_positions": int(recorded["caps"]["max_total"]),
        "max_long_positions": int(recorded["caps"]["max_long"]),
        "max_short_positions": int(recorded["caps"]["max_short"]),
        # 記録されているのは $ 値なので、当時の 100k 基準から pct を復元する
        "max_gross_exposure_pct": float(recorded["caps"]["gross_cap_usd"])
        / DEFAULT_CAPITAL,
        "max_net_exposure_pct": float(recorded["caps"]["net_cap_usd"])
        / DEFAULT_CAPITAL,
    }
    held = recorded.get("held") or {}
    unmapped = recorded.get("held_unmapped") or {}
    positions, smap = _synth_positions(held, unmapped)
    df, meta = _build_precap_frame(payload, assumed_pv_mode)

    real_equity = equity_hist.get(date)
    arms: dict[str, Any] = {}

    r, ps = _run_arm(df, caps_cfg, positions, smap, DEFAULT_CAPITAL, None)
    arms["A_fixed100k"] = {"report": r, "per_system": ps}

    # 自己検証: arm A (= 現行本番と同じ入力) が、その日 **実際に記録された**
    # per-system 本数と cap report を再現できたか。ここが False の日は
    # 再構成が現実とズレているので、B/C の比較も信用してはいけない。
    recorded_entry = {
        k: v for k, v in (meta["recorded_entry_counts"] or {}).items() if v
    }
    meta["reconstruction_matches_recorded"] = bool(
        ps == recorded_entry
        and r["kept"] == recorded.get("kept")
        and r["allow"] == recorded.get("allow")
        and r["trimmed"] == recorded.get("trimmed")
    )
    meta["replayed_per_system"] = ps

    if real_equity:
        r, ps = _run_arm(df, caps_cfg, positions, smap, real_equity, f"replay:{date}")
        arms["B_real_equity"] = {"report": r, "per_system": ps}

        # C: equity 実値 + held から unmapped を除外 (counterfactual 診断)
        pos_c, smap_c = _synth_positions(
            {
                "long": max(0, int(held.get("long", 0)) - int(unmapped.get("long", 0))),
                "short": max(
                    0, int(held.get("short", 0)) - int(unmapped.get("short", 0))
                ),
            },
            {"long": 0, "short": 0, "total": 0},
        )
        r, ps = _run_arm(
            df, caps_cfg, pos_c, smap_c, real_equity, f"replay-noorphan:{date}"
        )
        arms["C_no_orphan"] = {"report": r, "per_system": ps}

    return {
        "date": date,
        "real_equity_usd": real_equity,
        "recorded": recorded,
        "reconstruction": meta,
        "arms": arms,
    }


def _fmt_row(date: str, arm: str, data: dict[str, Any]) -> str:
    rep = data["report"]
    ps = data["per_system"]
    return (
        f"{date}  {arm:<14}"
        f" allowL={rep['allow']['long']:>3} allowS={rep['allow']['short']:>3}"
        f" netcap=${rep['caps']['net_cap_usd']:>10,.0f}"
        f" keptL={rep['kept']['long']:>3} keptS={rep['kept']['short']:>3}"
        f" sys1={ps.get('system1', 0):>2} sys3={ps.get('system3', 0):>2}"
        f" sys4={ps.get('system4', 0):>2} sys5={ps.get('system5', 0):>2}"
        f" netUSD=${abs(rep['new_long_usd'] - rep['new_short_usd']):>9,.0f}"
        f" trims={rep['trimmed']}"
    )


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--results-dir", default="results_csv")
    ap.add_argument(
        "--assumed-pv-mode", choices=["mean", "max", "budget"], default="mean"
    )
    ap.add_argument("--out", default=None, help="結果 JSON の書き出し先")
    args = ap.parse_args(argv)

    results_dir = Path(args.results_dir)
    equity_hist = _load_equity_history(results_dir)
    files = sorted(results_dir.glob("today_signals_*.json"))
    if not files:
        print(f"no today_signals_*.json under {results_dir}")
        return 1

    days: list[dict[str, Any]] = []
    for fp in files:
        try:
            day = replay_day(fp, equity_hist, args.assumed_pv_mode)
        except Exception as exc:  # noqa: BLE001
            print(f"[skip] {fp.name}: {type(exc).__name__}: {exc}")
            continue
        if day:
            days.append(day)

    print(f"# portfolio cap replay (offline, no broker calls) — {len(days)} days")
    print(f"# assumed_pv_mode={args.assumed_pv_mode}")
    print()
    for day in days:
        for arm in ("A_fixed100k", "B_real_equity", "C_no_orphan"):
            if arm in day["arms"]:
                print(_fmt_row(day["date"], arm, day["arms"][arm]))
        print()

    # 判定サマリ
    changed = [
        d["date"]
        for d in days
        if "B_real_equity" in d["arms"]
        and d["arms"]["B_real_equity"]["report"]["kept"]
        != d["arms"]["A_fixed100k"]["report"]["kept"]
    ]
    unlocked = [
        d["date"]
        for d in days
        if "C_no_orphan" in d["arms"]
        and d["arms"]["C_no_orphan"]["report"]["kept"]["long"]
        > d["arms"]["A_fixed100k"]["report"]["kept"]["long"]
    ]
    bad = [
        d["date"]
        for d in days
        if not d["reconstruction"].get("reconstruction_matches_recorded")
    ]
    print("## 判定")
    print(
        f"- 再構成の自己検証 (arm A が当日の記録を再現): {len(days) - len(bad)}/{len(days)} 日 OK"
        + (f" / 不一致={bad}" if bad else "")
    )
    print(
        f"- equity 基準の差し替え (A->B) で kept が変わった日: {changed or 'なし (0/%d)' % len(days)}"
    )
    print(
        f"- orphan を held から外した場合 (A->C) に long が増える日: {unlocked or 'なし'}"
    )

    if args.out:
        out = Path(args.out)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(
                {
                    "generated_for": "portfolio cap equity-base replay",
                    "assumed_pv_mode": args.assumed_pv_mode,
                    "days": days,
                    "verdict": {
                        "reconstruction_mismatch_days": bad,
                        "kept_changed_days": changed,
                        "no_orphan_unlocked_days": unlocked,
                    },
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        print(f"\nwrote {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
