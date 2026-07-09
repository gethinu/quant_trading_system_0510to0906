"""当日シグナルを Alpaca Paper 口座へ流すシミュレーション (dry-run / 実発注なし)。

入力は CSV (final_df) または JSON (today_signals_YYYYMMDD.json) の両対応。
JSON 入力は daily_pipeline.ps1 の paper_orders step 用。**実発注は一切行わない。**
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import (  # noqa: E402
    resolve_sizing_equity,
    signals_json_to_orders,
    signals_to_orders,
)


def build_sizing_kwargs(args, *, client=None) -> tuple[dict, dict]:
    """CLI args + settings から signals_json_to_orders 用 sizing kwargs と meta を作る。

    - mode / equity_deploy_pct は CLI 明示 > settings(sizing.*) の順。
    - cap (per-name/gross/net) は settings(risk.*) を single source of truth に。
    - equity は equity_linked のとき Alpaca paper から解決 (creds 無し/失敗は --equity へ
      安全 fallback。--no-equity-fetch or TEST_MODE で fetch 抑止=従来挙動維持)。
    返り値: (signals_json_to_orders へ渡す kwargs, 出力 JSON 用 meta)。
    """
    from config.settings import get_settings

    s = get_settings()
    mode = getattr(args, "sizing_mode", None) or s.sizing.mode
    pct_arg = getattr(args, "equity_deploy_pct", None)
    try:
        pct = (
            float(pct_arg) if pct_arg is not None else float(s.sizing.equity_deploy_pct)
        )
    except (TypeError, ValueError):
        pct = float(s.sizing.equity_deploy_pct)
    max_pct = float(s.risk.max_pct)
    gross = float(s.risk.portfolio.max_gross_exposure_pct)
    net = float(s.risk.portfolio.max_net_exposure_pct)

    allow_fetch = False if getattr(args, "no_equity_fetch", False) else None
    equity, eq_src = resolve_sizing_equity(
        float(getattr(args, "equity", 10000.0) or 10000.0),
        mode=mode,
        client=client,
        allow_fetch=allow_fetch,
    )
    kwargs = dict(
        sizing_mode=mode,
        equity_deploy_pct=pct,
        account_equity=equity,
        max_pct=max_pct,
        max_gross_exposure_pct=gross,
        max_net_exposure_pct=net,
    )
    meta = dict(
        sizing_mode=mode,
        equity_deploy_pct=pct,
        account_equity_usd=equity,
        equity_source=eq_src,
        max_pct=max_pct,
        max_gross_exposure_pct=gross,
        max_net_exposure_pct=net,
    )
    return kwargs, meta


def _demo_signals() -> pd.DataFrame:
    return pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "system": "system1",
                "side": "long",
                "shares": 10,
                "entry_price": 195.5,
                "entry_date": "2026-06-30",
            },
            {
                "symbol": "MSFT",
                "system": "system3",
                "side": "long",
                "shares": 5,
                "entry_price": 420.0,
                "entry_date": "2026-06-30",
            },
            {
                "symbol": "TSLA",
                "system": "system2",
                "side": "short",
                "shares": 8,
                "entry_price": 250.0,
                "entry_date": "2026-06-30",
            },
            {
                "symbol": "SPY",
                "system": "system7",
                "side": "short",
                "shares": 3,
                "entry_price": 545.0,
                "entry_date": "2026-06-30",
            },
        ]
    )


def _default_csv_for_date(date):
    if not date:
        return None
    candidates = [
        Path("results_csv_test") / f"signals_final_{date}.csv",
        Path("results_csv_test") / "signals_final_test.csv",
        Path("data_cache") / "signals" / f"final_{date}.csv",
    ]
    for c in candidates:
        if c.exists():
            return c
    return None


def load_signals(args):
    if args.demo:
        print("[demo] 内蔵デモ fixture を使用します (API 不要)。")
        return _demo_signals()
    csv_path = (
        Path(args.signals_csv) if args.signals_csv else _default_csv_for_date(args.date)
    )
    if csv_path is None or not csv_path.exists():
        raise SystemExit(
            "signals CSV が見つかりません。--signals-csv <path> を指定するか、"
            "run_all_systems_today.py で当日シグナルを生成してください。"
            " (動作確認のみなら --demo)"
        )
    print(f"[load] {csv_path}")
    return pd.read_csv(csv_path)


def _write_orders_json(orders, output_path, meta):
    """PreparedOrder 列を paper_orders_YYYYMMDD.json として書き出す。"""
    output_path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        **meta,
        "orders": [o.to_row() for o in orders],
    }
    with output_path.open("w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2, default=str)


def _dryrun_from_json(args):
    src = Path(args.signals_json)
    if not src.exists():
        print(f"[error] signals JSON not found: {src}")
        return 2
    with src.open(encoding="utf-8") as fh:
        json_data = json.load(fh)

    sizing_kwargs, sizing_meta = build_sizing_kwargs(args)
    orders = signals_json_to_orders(
        json_data,
        tier=args.tier,
        dry_run=True,
        min_notional_usd=args.min_notional,
        prefer_fractional=(not args.no_fractional),
        **sizing_kwargs,
    )

    if not orders:
        print("[dry-run] 変換後の発注対象なし (weight=0 or min_notional 未達)。")
    else:
        rows = [o.to_row() for o in orders]
        df = pd.DataFrame(rows)
        cols = [
            "symbol",
            "side",
            "qty",
            "notional_usd",
            "order_type",
            "time_in_force",
            "system",
            "tier",
            "dry_run",
            "skip_reason",
            "client_order_id",
        ]
        df = df[[c for c in cols if c in df.columns]]
        print("\n===== DRY-RUN (from JSON): 送信予定注文 (実発注なし) =====")
        print(df.to_string(index=False))
        skipped = [o for o in orders if getattr(o, "skip_reason", None)]
        submittable = len(orders) - len(skipped)
        # total_notional は「送信可 (skip でない)」注文のみ集計 = 実際に deploy される額。
        total_notional = sum(
            (o.notional_usd or 0.0)
            for o in orders
            if not getattr(o, "skip_reason", None)
        )
        _mode = sizing_meta["sizing_mode"]
        _budget_hint = (
            f"tier={args.tier}"
            if _mode != "equity_linked"
            else f"deploy_pct={sizing_meta['equity_deploy_pct']:g}"
        )
        print(
            f"\n合計 {len(df)} 生成  送信可 {submittable}  skip {len(skipped)}  "
            f"mode={_mode} {_budget_hint}  total_notional(送信可)=${total_notional:,.2f}  "
            f"equity=${sizing_meta['account_equity_usd']:,.0f}"
            f" (src={sizing_meta['equity_source']})"
        )
        if skipped:
            from collections import Counter

            kinds = Counter(str(o.skip_reason).split(":", 1)[0] for o in skipped)
            print(f"[skip] {len(skipped)} 件 (内訳: {dict(kinds)})")

    if args.output_json:
        out_path = Path(args.output_json)
        _skipped = [o for o in orders if getattr(o, "skip_reason", None)]
        _write_orders_json(
            orders,
            out_path,
            {
                "date": str(json_data.get("date") or ""),
                "tier": args.tier,
                "min_notional_usd": args.min_notional,
                "prefer_fractional": (not args.no_fractional),
                "mode": "dry_run",
                "count": len(orders),
                "submittable": len(orders) - len(_skipped),
                "skipped": len(_skipped),
                **sizing_meta,
            },
        )
        print(f"[write] paper_orders JSON: {out_path}")

    print(
        "※ これは dry-run です。実発注は scripts/paper_trading_submit.py --confirm で行います。"
    )
    return 0


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。")
    parser.add_argument("--signals-csv", help="final_df 形式の signals CSV パス。")
    parser.add_argument(
        "--signals-json",
        help="today_signals JSON パス (systems.sysN.signals[])。指定時は JSON 経路。",
    )
    parser.add_argument(
        "--tier",
        default="small",
        choices=("small", "medium", "large"),
        help="発注 tier。small=$1k / medium=$10k / large=$100k (default: small)。",
    )
    parser.add_argument(
        "--output-json",
        help="paper_orders_YYYYMMDD.json 出力先 (JSON 経路のみ)。",
    )
    parser.add_argument(
        "--min-notional",
        type=float,
        default=5.0,
        help="1 注文の最小 notional (USD)。default 5。",
    )
    parser.add_argument(
        "--no-fractional",
        action="store_true",
        help="fractional (notional 発注) を無効化し整数株で発注する。",
    )
    parser.add_argument(
        "--equity",
        type=float,
        default=10000.0,
        help="口座資産 fallback (equity_linked で Alpaca 取得失敗時に使用)。",
    )
    parser.add_argument(
        "--sizing-mode",
        dest="sizing_mode",
        default=None,
        choices=("equity_linked", "fixed_tier"),
        help="サイジング方式。未指定なら settings(sizing.mode, 既定 equity_linked)。",
    )
    parser.add_argument(
        "--equity-deploy-pct",
        dest="equity_deploy_pct",
        type=float,
        default=None,
        help="deploy_budget=equity×pct。未指定なら settings(sizing.equity_deploy_pct)。",
    )
    parser.add_argument(
        "--no-equity-fetch",
        dest="no_equity_fetch",
        action="store_true",
        help="Alpaca からの equity 取得を抑止し --equity を使う (決定論/テスト用)。",
    )
    parser.add_argument(
        "--demo", action="store_true", help="内蔵デモ fixture で動作確認。"
    )
    args = parser.parse_args(argv)

    if args.signals_json:
        return _dryrun_from_json(args)

    signals = load_signals(args)
    if signals is None or signals.empty:
        print("シグナルなし。発注対象はありません。")
        return 0

    orders = signals_to_orders(signals, account_equity=args.equity, dry_run=True)

    if not orders:
        print("変換後の発注対象はありません (shares<=0 等でフィルタ)。")
        return 0

    rows = [o.to_row() for o in orders]
    df = pd.DataFrame(rows)
    cols = [
        "symbol",
        "side",
        "qty",
        "order_type",
        "limit_price",
        "time_in_force",
        "system",
        "client_order_id",
    ]
    df = df[[c for c in cols if c in df.columns]]

    print("\n===== DRY-RUN: 送信予定注文 (実発注なし) =====")
    print(df.to_string(index=False))
    print(f"\n合計 {len(df)} 注文 / equity=${args.equity:,.0f}")
    print(
        "※ これは dry-run です。実発注は scripts/paper_trading_submit.py --confirm で行います。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
