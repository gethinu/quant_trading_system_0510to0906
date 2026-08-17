"""実行 reconciliation JSON を生成する (signals → plan → entry → exit → fill の突合)。

daily_pipeline の 3 つの成果物を 1 本の ``recon_YYYYMMDD.json`` に join する:

    - ``today_signals_YYYYMMDD.json``  (Step2: signals + funnel)
    - ``paper_orders_YYYYMMDD.json``   (Step5b: entry 発注結果)
    - ``exit_orders_YYYYMMDD.json``    (Step5c: exit 発注結果)

出力は system × side (long/short) 粒度で

    signals → 生成 → entry 送信 → fill → exit 送信

を並べ、drop 内訳 (min_notional / wash / unsizable / fail) を集計する。
Vercel dashboard の execution funnel と、submit 後の execution summary 通知
(scripts/publish_execution_summary.py) の両方が本 JSON を single source にする。

**read-only**: Alpaca へは一切アクセスしない。既存 JSON を読むだけ。
入力が欠けても (dry-run 等) 部分 recon を出す (inputs フラグで明示)。
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# fill とみなす Alpaca order status (成行は submit 直後 accepted のことが多く、
# fill は非同期。ここに載るのは既に約定確認できた分のみ = best-effort)。
_FILLED_STATUSES = {"filled", "partially_filled"}
# exit protection の reason_code (それ以外の exit は close 扱い)
_PROTECT_REASONS = {"protect_stop", "protect_trailing", "protect_target"}

_SYSTEMS = tuple(f"system{i}" for i in range(1, 8))


def _norm_system(raw: Any) -> str | None:
    """'sys1' / 'system1' / '1' → 'system1' に正規化。'system1' はそのまま。

    注意: 単純な ``replace('sys','system')`` は 'system1'→'systemtem1' に化けるため
    startswith 判定で分岐する。
    """
    try:
        text = str(raw or "").strip().lower()
    except Exception:
        return None
    if not text:
        return None
    if text.startswith("system"):
        return text
    if text.startswith("sys"):
        rest = text[3:]
        return f"system{rest}" if rest else None
    if text.isdigit():
        return f"system{text}"
    return None


def _norm_side(raw: Any) -> str:
    """BUY/long → 'long'、SELL/short → 'short'。不明は 'long' 扱い (集計欠落を避ける)。"""
    s = str(raw or "").strip().lower()
    if s in ("sell", "short", "sell_short"):
        return "short"
    return "long"


def _empty_side_bucket() -> dict[str, int]:
    return {
        "signals": 0,
        "generated": 0,
        "entry_submitted": 0,
        "filled": 0,
        "skipped": 0,
        "failed": 0,
    }


def _empty_exit_bucket() -> dict[str, int]:
    """exit 集計。fired(submitted) と armed(未発火) を分けて持つ。

    - ``submitted`` = fired = broker へ送信できた手仕舞い (order_id あり & error なし)。
    - ``close`` / ``protect`` = **submitted 分だけ**の内訳 (N=close+protect が常に成立)。
    - ``armed`` = リスト計上されたが未送信 (order_id なし) = 保護注文が張られただけ。
      ``armed_close`` / ``armed_protect`` はその内訳。
    """
    return {
        "submitted": 0,
        "close": 0,
        "protect": 0,
        "armed": 0,
        "armed_close": 0,
        "armed_protect": 0,
    }


def _empty_system_bucket() -> dict[str, Any]:
    return {
        "long": _empty_side_bucket(),
        "short": _empty_side_bucket(),
        "funnel": None,
        "exit": _empty_exit_bucket(),
    }


# system 不明 (例: reason=flatten_all で system=null の exit) を集計から落とさず
# 振り分ける先。per-system 内訳には出るが sysN funnel には紐付かない。
_UNASSIGNED_SYSTEM = "__unassigned__"


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        logger.warning("recon: %s の読込に失敗 (無視して継続): %s", path, exc)
        return None


def execution_input_lineage(
    signals: dict[str, Any] | None,
    paper_orders: dict[str, Any] | None,
    exit_orders: dict[str, Any] | None,
) -> dict[str, str]:
    """各 execution input が *この* signals run 由来かを判定する。

    paper_orders / exit_orders は producer が ``source_signals_run_id`` を
    書く。同日再生成で Step5b/5c が skip / 失敗して古い JSON が残っている場合、
    その run_id は現行 signals と一致しないので ``stale`` になる。field 自体が
    無い旧 producer 出力は ``unverified`` (= 検証不能) とし、**推測で verified に
    昇格させない**。

    戻り値は input 名 -> "verified" | "stale" | "unverified" | "missing"。
    """
    run_id = str(((signals or {}).get("meta") or {}).get("run_id") or "")
    out: dict[str, str] = {}
    for name, payload in (("paper_orders", paper_orders), ("exit_orders", exit_orders)):
        if payload is None:
            out[name] = "missing"
            continue
        stamped = str(payload.get("source_signals_run_id") or "")
        if not stamped:
            out[name] = "unverified"
        elif not run_id or stamped != run_id:
            out[name] = "stale"
        else:
            out[name] = "verified"
    return out


def execution_lineage_ok(lineage: dict[str, str]) -> bool:
    """存在する execution input が全て current run に紐付いているか。

    ``missing`` は「その段が動かなかった」= 突合対象なしなので許容する。
    ``stale`` / ``unverified`` が 1 つでも有れば recon 全体を current run の
    測定値として扱ってはいけない。
    """
    return all(state in {"verified", "missing"} for state in lineage.values())


def build_recon(
    signals: dict[str, Any] | None,
    paper_orders: dict[str, Any] | None,
    exit_orders: dict[str, Any] | None,
    *,
    date_str: str | None = None,
    account_equity: float | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    """3 つの JSON payload を突合し recon dict を返す (pure、I/O なし)。"""
    signals_meta = signals.get("meta") if isinstance(signals, dict) else None
    source_signals_run_id = (
        signals_meta.get("run_id") if isinstance(signals_meta, dict) else None
    )
    # run_id を signals から取るだけでは不十分。paper_orders / exit_orders が前 run の
    # 残骸だと、古い execution 実績に新しい run_id を貼って「current」として publish
    # されてしまう。全 execution input が current run 由来と確認できた時だけ stamp し、
    # そうでなければ None にして下流 (bundle preflight) に fail-closed 判断を委ねる。
    _lineage = execution_input_lineage(signals, paper_orders, exit_orders)
    _lineage_ok = execution_lineage_ok(_lineage)
    if not _lineage_ok:
        source_signals_run_id = None
    systems: dict[str, dict[str, Any]] = {
        name: _empty_system_bucket() for name in _SYSTEMS
    }

    def _sys(name: str | None) -> dict[str, Any] | None:
        if name is None:
            return None
        return systems.setdefault(name, _empty_system_bucket())

    universe_target: int | None = None
    total_signals = 0

    # --- signals (Step2) -------------------------------------------------
    if signals:
        portfolio = signals.get("portfolio", {}) or {}
        universe_target = portfolio.get("universe_target")
        for raw_name, cfg in (signals.get("systems", {}) or {}).items():
            name = _norm_system(raw_name)
            bucket = _sys(name)
            if bucket is None or not isinstance(cfg, dict):
                continue
            if cfg.get("funnel") is not None:
                bucket["funnel"] = cfg.get("funnel")
            for sig in cfg.get("signals", []) or []:
                side = _norm_side(sig.get("side"))
                bucket[side]["signals"] += 1
                total_signals += 1

    # --- paper_orders (Step5b) ------------------------------------------
    drop_breakdown: dict[str, int] = {}
    if paper_orders:
        for o in paper_orders.get("orders", []) or []:
            name = _norm_system(o.get("system"))
            bucket = _sys(name)
            if bucket is None:
                continue
            side = _norm_side(o.get("side"))
            sb = bucket[side]
            sb["generated"] += 1
            skip_reason = o.get("skip_reason")
            error = o.get("error")
            order_id = o.get("order_id")
            status = str(o.get("status") or "").lower()
            if skip_reason:
                sb["skipped"] += 1
                kind = str(skip_reason).split(":", 1)[0]
                # "skip" prefix は冗長なので次の segment を使う
                if kind == "skip":
                    parts = str(skip_reason).split(":")
                    kind = parts[1] if len(parts) > 1 else "skip"
                drop_breakdown[kind] = drop_breakdown.get(kind, 0) + 1
            elif error:
                sb["failed"] += 1
                drop_breakdown["fail"] = drop_breakdown.get("fail", 0) + 1
            elif order_id:
                sb["entry_submitted"] += 1
                if status in _FILLED_STATUSES:
                    sb["filled"] += 1

    # --- exit_orders (Step5c) -------------------------------------------
    if exit_orders:
        for e in exit_orders.get("exits", []) or []:
            name = _norm_system(e.get("system"))
            # system 不明 (flatten_all で system=null 等) は落とさず __unassigned__ へ。
            # 落とすと close 内訳が過少計上される (旧: system=None → drop)。
            bucket = _sys(name) or _sys(_UNASSIGNED_SYSTEM)
            if bucket is None:  # 論理上到達しないが型のため
                continue
            ex = bucket["exit"]
            is_submitted = bool(e.get("order_id")) and not e.get("error")
            is_protect = str(e.get("reason") or "").lower() in _PROTECT_REASONS
            if is_submitted:
                # fired: 送信できた手仕舞い。close/protect 内訳は fired 分だけ。
                ex["submitted"] += 1
                if is_protect:
                    ex["protect"] += 1
                else:
                    ex["close"] += 1
            else:
                # armed: 計算されたが未送信 (保護注文が張られただけ)。
                ex["armed"] += 1
                if is_protect:
                    ex["armed_protect"] += 1
                else:
                    ex["armed_close"] += 1

    # --- portfolio aggregate --------------------------------------------
    def _agg(field: str) -> int:
        return sum(
            b[side][field] for b in systems.values() for side in ("long", "short")
        )

    long_signals = sum(b["long"]["signals"] for b in systems.values())
    short_signals = sum(b["short"]["signals"] for b in systems.values())
    exit_submitted = sum(b["exit"]["submitted"] for b in systems.values())
    exit_close = sum(b["exit"]["close"] for b in systems.values())
    exit_protect = sum(b["exit"]["protect"] for b in systems.values())
    exit_armed = sum(b["exit"]["armed"] for b in systems.values())
    exit_armed_close = sum(b["exit"]["armed_close"] for b in systems.values())
    exit_armed_protect = sum(b["exit"]["armed_protect"] for b in systems.values())

    portfolio_out = {
        "universe_target": universe_target,
        "signals": total_signals,
        "long_signals": long_signals,
        "short_signals": short_signals,
        "orders_generated": _agg("generated"),
        "entry_submitted": _agg("entry_submitted"),
        "entry_filled": _agg("filled"),
        "entry_skipped": _agg("skipped"),
        "entry_failed": _agg("failed"),
        "long_entry_submitted": sum(
            b["long"]["entry_submitted"] for b in systems.values()
        ),
        "short_entry_submitted": sum(
            b["short"]["entry_submitted"] for b in systems.values()
        ),
        "exit_submitted": exit_submitted,
        "exit_close": exit_close,
        "exit_protect": exit_protect,
        "exit_armed": exit_armed,
        "exit_armed_close": exit_armed_close,
        "exit_armed_protect": exit_armed_protect,
        "drop_breakdown": drop_breakdown,
        "account_equity": account_equity,
    }

    # 空 (全 0) の system は出力から落として dashboard を簡潔に保つ
    systems_out = {
        name: data
        for name, data in systems.items()
        if (
            data["long"]["signals"]
            or data["short"]["signals"]
            or data["long"]["generated"]
            or data["short"]["generated"]
            or data["exit"]["submitted"]
            or data["exit"]["armed"]
            or data["funnel"] is not None
        )
    }

    return {
        "version": "1.0",
        "date": date_str or (signals or {}).get("date") or "",
        "generated_at": generated_at
        or datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "source_signals_run_id": source_signals_run_id,
        "execution_lineage": _lineage,
        "execution_lineage_ok": _lineage_ok,
        "inputs": {
            "signals": signals is not None,
            "paper_orders": paper_orders is not None,
            "exit_orders": exit_orders is not None,
        },
        "portfolio": portfolio_out,
        "systems": systems_out,
    }


# ---------------------------------------------------------------------------
# pipeline funnel (signal_pipeline/v1) の Exit phase を recon から埋める配線
# ---------------------------------------------------------------------------
#
# なぜここに置くか:
#   ダッシュボードの絞込漏斗 (pipeline_YYYYMMDD.json) の最終段 Exit は
#   「本日手仕舞い発火数」だが、漏斗は daily_polygon_monitor が exit 執行 *前*
#   (Step3) に生成するため count=null → UI が「未計測」を出していた。
#   一方 exit 実績は Step5c/5d で recon 化され ntfy (publish_execution_summary)
#   が `exit N (close C / protect P)` として既に配信している。
#   両者を **同一 recon** から書けば ntfy と漏斗が必ず一致する。ここは recon を
#   単一ソースとして Exit phase を埋める pure 関数 (I/O 無し)。
#
# 正直さの担保:
#   - recon に exit_orders 入力が無い (部分 recon) 場合は **埋めない**。
#     count=null のまま = 「未計測」を維持する (0 で誤魔化さない)。
#   - recon はあるが該当 system に exit が無い → 0 (「発火しなかった」事実)。

EXIT_CONDITION_BASE = "本日手仕舞い発火"


def exit_counts_from_recon(recon: dict[str, Any] | None) -> dict[str, dict[str, int]]:
    """recon -> ``{"sys1": {"submitted", "close", "protect"}, ...}`` (pipeline schema の sysN key)。

    recon の system key は "systemN"。pipeline (signal_pipeline/v1) は "sysN" なので
    ここで正規化する。recon.systems に居ない system は呼び出し側で 0 扱い。
    """
    out: dict[str, dict[str, int]] = {}
    if not isinstance(recon, dict):
        return out
    for name, data in (recon.get("systems") or {}).items():
        norm = _norm_system(name)  # "system1"
        if not norm:
            continue
        sysk = norm.replace("system", "sys")  # "sys1"
        ex = (data or {}).get("exit") or {}

        def _i(v: Any) -> int:
            try:
                return int(v or 0)
            except (TypeError, ValueError):
                return 0

        out[sysk] = {
            "submitted": _i(ex.get("submitted")),
            "close": _i(ex.get("close")),
            "protect": _i(ex.get("protect")),
            "armed": _i(ex.get("armed")),
            "armed_close": _i(ex.get("armed_close")),
            "armed_protect": _i(ex.get("armed_protect")),
        }
    return out


def patch_pipeline_exit(
    pipeline: dict[str, Any], recon: dict[str, Any] | None
) -> tuple[dict[str, Any], int, str]:
    """pipeline_*.json の各 system の Exit phase を recon の実測で埋める (in-place)。

    ntfy (``exit {fired} fired (close Cs / protect Ps) · M armed``) と同じ recon を
    single source にするための配線。Exit の ``count`` は per-system ``exit.submitted``
    (= fired = ntfy 見出しの exit 数と同じ定義。funnel バーは発火数を表す)。
    close/protect 内訳は **fired 分だけ** (Cs+Ps=count が常に成立)。未発火の保護注文は
    ``armed`` として別枠に持ち、condition 末尾にも併記する。フロントが
    「count fired / armed armed」相当を出せるよう Exit オブジェクトに
    ``fired`` / ``armed`` / ``armed_close`` / ``armed_protect`` を追加する。

    戻り値 ``(pipeline, n_filled, status)``:
      - ``status="ok"``            : recon から Exit を埋めた
      - ``status="no_recon"``      : recon が無い/不正 → 未計測を維持 (何もしない)
      - ``status="exit_orders_input_missing"`` : 部分 recon (exit 未計測) → 未計測を維持

    idempotent: 既に埋めた pipeline を再度渡しても同じ結果 (condition 重複しない)。
    """
    if not isinstance(recon, dict):
        return pipeline, 0, "no_recon"
    # 部分 recon (exit_orders 入力欠損) は「発火 0」ではなく「未計測」。埋めない。
    if not (recon.get("inputs") or {}).get("exit_orders"):
        return pipeline, 0, "exit_orders_input_missing"

    exits = exit_counts_from_recon(recon)
    n_filled = 0
    for sysk, sysobj in (pipeline.get("systems") or {}).items():
        if not isinstance(sysobj, dict):
            continue
        phases = sysobj.get("phases") or []
        universe: int | None = next(
            (p.get("count") for p in phases if p.get("name") == "Tgt"), None
        )
        prev_count: int | None = None
        for p in phases:
            if p.get("name") == "Exit":
                ec = exits.get(sysk) or {
                    "submitted": 0,
                    "close": 0,
                    "protect": 0,
                    "armed": 0,
                    "armed_close": 0,
                    "armed_protect": 0,
                }
                cnt = int(ec["submitted"])  # fired
                armed = int(ec.get("armed") or 0)
                base = str(p.get("condition") or EXIT_CONDITION_BASE).split(" (close")[
                    0
                ]
                p["count"] = cnt
                p["measured"] = True
                p["fired"] = cnt
                p["exit_close"] = int(ec["close"])
                p["exit_protect"] = int(ec["protect"])
                p["armed"] = armed
                p["armed_close"] = int(ec.get("armed_close") or 0)
                p["armed_protect"] = int(ec.get("armed_protect") or 0)
                armed_suffix = f" · {armed} armed" if armed else ""
                p["condition"] = (
                    f"{base} (close {int(ec['close'])} / protect {int(ec['protect'])})"
                    f"{armed_suffix}"
                )
                p["ratio_of_prev"] = (
                    round(cnt / prev_count, 6)
                    if isinstance(prev_count, (int, float)) and prev_count
                    else None
                )
                p["ratio_of_universe"] = (
                    round(cnt / universe, 6)
                    if isinstance(universe, (int, float)) and universe
                    else None
                )
                n_filled += 1
                break
            if p.get("count") is not None:
                prev_count = p.get("count")

    if n_filled:
        notes = pipeline.get("notes")
        if isinstance(notes, list):
            marker = (
                "Exit = execution recon (recon_YYYYMMDD.json, ntfy と同一 source): "
                "count=fired(exit_submitted), close/protect は fired 分のみ, "
                "armed=未発火の保護注文 (別枠)。"
            )
            if marker not in notes:
                notes.append(marker)
    return pipeline, n_filled, "ok"


def _default_path(results_dir: Path, stem: str, date_str: str) -> Path:
    return results_dir / f"{stem}_{date_str.replace('-', '')}.json"


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date", help="対象日 (YYYY-MM-DD)。default paths の解決に使う。"
    )
    parser.add_argument("--signals-json", help="today_signals JSON path。")
    parser.add_argument("--paper-orders-json", help="paper_orders JSON path。")
    parser.add_argument("--exit-orders-json", help="exit_orders JSON path。")
    parser.add_argument(
        "--output-json",
        help="recon 出力先 (default: results_csv/recon_YYYYMMDD.json)。",
    )
    parser.add_argument(
        "--results-dir", default="results_csv", help="default path 解決の基準 dir。"
    )
    parser.add_argument(
        "--account-equity", type=float, default=None, help="口座残高 (通知表示用)。"
    )
    parser.add_argument("--log-level", default="INFO")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=str(args.log_level).upper(), format="%(levelname)s: %(message)s"
    )

    results_dir = Path(args.results_dir)
    date_str = args.date or datetime.now().strftime("%Y-%m-%d")

    signals_path = (
        Path(args.signals_json)
        if args.signals_json
        else _default_path(results_dir, "today_signals", date_str)
    )
    paper_path = (
        Path(args.paper_orders_json)
        if args.paper_orders_json
        else _default_path(results_dir, "paper_orders", date_str)
    )
    exit_path = (
        Path(args.exit_orders_json)
        if args.exit_orders_json
        else _default_path(results_dir, "exit_orders", date_str)
    )

    signals = _load_json(signals_path)
    paper_orders = _load_json(paper_path)
    exit_orders = _load_json(exit_path)

    if signals is None and paper_orders is None and exit_orders is None:
        logger.error(
            "recon 入力が 1 つも見つかりません (signals=%s paper=%s exit=%s)。",
            signals_path,
            paper_path,
            exit_path,
        )
        return 1

    recon = build_recon(
        signals,
        paper_orders,
        exit_orders,
        date_str=(args.date or (signals or {}).get("date") or None),
        account_equity=args.account_equity,
    )

    out_path = (
        Path(args.output_json)
        if args.output_json
        else _default_path(results_dir, "recon", date_str)
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    tmp = out_path.with_suffix(out_path.suffix + ".tmp")
    tmp.write_text(json.dumps(recon, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(out_path)

    p = recon["portfolio"]
    logger.info(
        "recon 書き出し: %s (Tgt=%s sig=%s gen=%s entry=%s exit=%s fill=%s)",
        out_path,
        p.get("universe_target"),
        p.get("signals"),
        p.get("orders_generated"),
        p.get("entry_submitted"),
        p.get("exit_submitted"),
        p.get("entry_filled"),
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
