"""当日シグナルを Alpaca Paper 口座へ流すシミュレーション (dry-run / 実発注なし)。

2 モードを提供する:

**JSON preview モード (推奨、account_equity scale 対応)**
    ``results_csv/today_signals_YYYYMMDD.json`` (Phase 1 signals pack) を読み、
    ``signals_json_to_orders`` で account_equity に応じた orders preview を生成する。
    結果を pretty-print し ``results_csv/orders_preview_YYYYMMDD_${equity}.json`` に書き出す。

        python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 1000
        python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 10000
        python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 100000
        python scripts/paper_trading_dryrun.py --demo-json --account-equity 10000

**legacy DataFrame モード (final_df CSV)**
    ``run_all_systems_today.py`` の ``final_df`` (列: symbol, system, side, shares,
    entry_price, entry_date) を Alpaca 注文へ変換して表示する。

        python scripts/paper_trading_dryrun.py --signals-csv path/to/final.csv
        python scripts/paper_trading_dryrun.py --demo

**実発注は一切行わない。** 実発注は scripts/paper_trading_submit.py --confirm。
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

# リポジトリルートを import path に追加 (scripts/ から実行される想定)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import (  # noqa: E402
    OrderPlan,
    signals_json_to_orders,
    signals_to_orders,
)

_MOCK_JSON = (
    Path(__file__).resolve().parents[1]
    / "apps"
    / "dashboards"
    / "alpaca-next"
    / "mock"
    / "today_signals_20260701.json"
)


# ---------------------------------------------------------------------------
# JSON preview モード
# ---------------------------------------------------------------------------
def _default_signals_json_for_date(date: str | None) -> Path | None:
    if not date:
        return None
    compact = date.replace("-", "")
    for c in [
        Path("results_csv") / f"today_signals_{compact}.json",
        Path("results_csv_test") / f"today_signals_{compact}.json",
    ]:
        if c.exists():
            return c
    return None


def resolve_signals_json(args: argparse.Namespace) -> Path | None:
    if args.demo_json:
        return _MOCK_JSON if _MOCK_JSON.exists() else None
    if args.signals_json:
        p = Path(args.signals_json)
        return p if p.exists() else None
    return _default_signals_json_for_date(args.date)


def _print_plan(plan: OrderPlan) -> None:
    print(f"\n===== DRY-RUN orders preview: tier={plan.tier} equity=${plan.account_equity:,.0f} =====")
    if plan.orders:
        rows = []
        for o in plan.orders:
            notional = o.notional if o.notional is not None else (o.qty * (o.price or 0))
            rows.append(
                {
                    "symbol": o.symbol,
                    "side": o.side,
                    "notional_usd": round(notional, 2),
                    "qty": round(float(o.qty), 4),
                    "fractional": o.fractional,
                    "type": o.order_type,
                    "sys": o.system,
                    "rank": o.rank,
                    "coid": o.client_order_id,
                }
            )
        print(pd.DataFrame(rows).to_string(index=False))
    else:
        print("(発注対象なし)")
    if plan.skipped:
        print("\n-- skipped --")
        for s in plan.skipped:
            print(f"   {s.symbol}: {s.reason}")
    s = plan.summary()
    print(
        f"\n合計 {s['n_orders']} 注文 / skip {s['n_skipped']} / "
        f"total ${s['total_notional']:,.2f} / hedge ${s['hedge_notional']:,.2f}"
    )
    print("※ これは dry-run です。実発注は scripts/paper_trading_submit.py --confirm。")


def run_json_preview(json_path: Path, args: argparse.Namespace) -> int:
    print(f"[load] {json_path}")
    signals_json = json.loads(json_path.read_text(encoding="utf-8"))

    plan = signals_json_to_orders(
        signals_json,
        account_equity=args.account_equity,
        tier=args.tier,
        min_notional_usd=args.min_notional,
        prefer_fractional=not args.no_fractional,
        dry_run=True,  # 変換のみ。実発注は絶対しない
    )
    _print_plan(plan)

    # preview JSON を書き出す (Vercel dashboard / submit 突合用)
    out_dir = Path(args.output_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    compact = (plan.date or args.date or "").replace("-", "") or "unknown"
    eq_tag = int(args.account_equity)
    out_path = out_dir / f"orders_preview_{compact}_{eq_tag}.json"
    out_path.write_text(
        json.dumps(plan.to_preview_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    print(f"[write] {out_path}")
    return 0


# ---------------------------------------------------------------------------
# legacy DataFrame モード
# ---------------------------------------------------------------------------
def _demo_signals() -> pd.DataFrame:
    """API 不要で動作確認するための内蔵デモ fixture。"""
    return pd.DataFrame(
        [
            {"symbol": "AAPL", "system": "system1", "side": "long", "shares": 10, "entry_price": 195.5, "entry_date": "2026-06-30"},
            {"symbol": "MSFT", "system": "system3", "side": "long", "shares": 5, "entry_price": 420.0, "entry_date": "2026-06-30"},
            {"symbol": "TSLA", "system": "system2", "side": "short", "shares": 8, "entry_price": 250.0, "entry_date": "2026-06-30"},
            {"symbol": "SPY", "system": "system7", "side": "short", "shares": 3, "entry_price": 545.0, "entry_date": "2026-06-30"},
        ]
    )


def _default_csv_for_date(date: str | None) -> Path | None:
    if not date:
        return None
    for c in [
        Path("results_csv_test") / f"signals_final_{date}.csv",
        Path("results_csv_test") / "signals_final_test.csv",
        Path("data_cache") / "signals" / f"final_{date}.csv",
    ]:
        if c.exists():
            return c
    return None


def load_signals(args: argparse.Namespace) -> pd.DataFrame:
    if args.demo:
        print("[demo] 内蔵デモ fixture を使用します (API 不要)。")
        return _demo_signals()
    csv_path = Path(args.signals_csv) if args.signals_csv else _default_csv_for_date(args.date)
    if csv_path is None or not csv_path.exists():
        raise SystemExit(
            "signals CSV が見つかりません。--signals-csv <path> を指定するか、"
            "run_all_systems_today.py で当日シグナルを生成してください。 (動作確認のみなら --demo)"
        )
    print(f"[load] {csv_path}")
    return pd.read_csv(csv_path)


def run_dataframe_preview(args: argparse.Namespace) -> int:
    signals = load_signals(args)
    if signals is None or signals.empty:
        print("シグナルなし。発注対象はありません。")
        return 0
    orders = signals_to_orders(signals, account_equity=args.account_equity, dry_run=True)
    if not orders:
        print("変換後の発注対象はありません (shares<=0 等でフィルタ)。")
        return 0
    df = pd.DataFrame([o.to_row() for o in orders])
    cols = ["symbol", "side", "qty", "order_type", "limit_price", "time_in_force", "system", "client_order_id"]
    df = df[[c for c in cols if c in df.columns]]
    print("\n===== DRY-RUN: 送信予定注文 (実発注なし) =====")
    print(df.to_string(index=False))
    print(f"\n合計 {len(df)} 注文 / equity=${args.account_equity:,.0f}")
    print("※ これは dry-run です。実発注は scripts/paper_trading_submit.py --confirm。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。既定パス推定に使用。")
    parser.add_argument("--signals-json", help="signals JSON (Phase 1 pack) のパス。")
    parser.add_argument("--signals-csv", help="legacy final_df 形式 CSV のパス。")
    parser.add_argument(
        "--account-equity", type=float, default=10000.0, help="口座資産 (USD)。tier/sizing に使用。"
    )
    parser.add_argument("--equity", type=float, dest="account_equity", help=argparse.SUPPRESS)  # legacy alias
    parser.add_argument("--tier", default="auto", choices=["auto", "small", "medium", "large"])
    parser.add_argument("--min-notional", type=float, default=5.0, help="$X 未満の position は skip。")
    parser.add_argument("--no-fractional", action="store_true", help="分数株を使わず whole share のみ。")
    parser.add_argument("--output-dir", default="results_csv", help="preview JSON 出力先。")
    parser.add_argument("--demo-json", action="store_true", help="内蔵 mock signals JSON で JSON preview。")
    parser.add_argument("--demo", action="store_true", help="内蔵デモ fixture で legacy DataFrame preview。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # legacy DataFrame モードを明示指定した場合はそちらを優先
    if args.demo or args.signals_csv:
        return run_dataframe_preview(args)

    json_path = resolve_signals_json(args)
    if json_path is not None:
        return run_json_preview(json_path, args)

    # JSON が無ければ legacy DataFrame モードにフォールバック
    return run_dataframe_preview(args)


if __name__ == "__main__":
    raise SystemExit(main())
