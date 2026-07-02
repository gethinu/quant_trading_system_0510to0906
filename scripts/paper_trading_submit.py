"""dry-run で確認した注文を Alpaca **Paper** 口座へ送信する (user 手動実行前提)。

安全設計:
    - --confirm が無ければ dry-run と等価 (誤爆防止)。
    - 送信前に ALPACA_PAPER=true と paper エンドポイントを検証 (live で fail-fast)。
    - 各注文の直前に summary を表示し [y/N] 確認 (CI 用に --yes で無人実行)。
    - 送信結果は logs/alpaca_orders_YYYYMMDD.log に追記。

使い方::

    # 1. dry-run で確認 (実発注なし)
    python scripts/paper_trading_submit.py --date 2026-06-30

    # 2. 確認できたら実発注 (対話確認あり)
    python scripts/paper_trading_submit.py --date 2026-06-30 --confirm

    # 3. 無人実行 (対話プロンプトなし・Paper のみ)
    python scripts/paper_trading_submit.py --date 2026-06-30 --confirm --yes

    # 4. daily_pipeline 経路 (JSON 入力):
    python scripts/paper_trading_submit.py \
        --signals-json results_csv/today_signals_20260701.json \
        --tier small --confirm --yes \
        --output-json results_csv/paper_orders_20260701.json
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import (  # noqa: E402
    LiveAccountGuardError,
    assert_paper_env,
    signals_json_to_orders,
    signals_to_orders,
    submit_paper_order,
)
from scripts.paper_trading_dryrun import (  # noqa: E402
    _write_orders_json,
    load_signals,
)


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _submit_from_json(args: argparse.Namespace) -> int:
    """--signals-json 経路: JSON → paper_orders JSON + Paper 口座送信 (--confirm 時)。"""
    src = Path(args.signals_json)
    if not src.exists():
        print(f"[error] signals JSON not found: {src}")
        return 2
    with src.open(encoding="utf-8") as fh:
        json_data = json.load(fh)

    if not args.confirm:
        print("[--confirm なし] dry-run モードで実行します (実発注なし)。")
        orders = signals_json_to_orders(
            json_data,
            tier=args.tier,
            dry_run=True,
            account_equity=args.equity,
            min_notional_usd=args.min_notional,
            prefer_fractional=(not args.no_fractional),
        )
    else:
        try:
            assert_paper_env()
        except LiveAccountGuardError as exc:
            print(f"[SAFETY ABORT] {exc}")
            return 2
        print(
            "=== PAPER 実発注モード (ALPACA_PAPER=true 確認済, tier="
            f"{args.tier}) ==="
        )
        orders = signals_json_to_orders(
            json_data,
            tier=args.tier,
            dry_run=False,
            account_equity=args.equity,
            min_notional_usd=args.min_notional,
            prefer_fractional=(not args.no_fractional),
        )

    ok = sum(1 for o in orders if o.order_id)
    fail = sum(1 for o in orders if o.error)
    print(f"\n完了: 生成={len(orders)} 送信={ok} 失敗={fail} (--confirm={args.confirm})")

    if args.output_json:
        out_path = Path(args.output_json)
        _write_orders_json(
            orders,
            out_path,
            {
                "date": str(json_data.get("date") or ""),
                "tier": args.tier,
                "account_equity_usd": args.equity,
                "min_notional_usd": args.min_notional,
                "prefer_fractional": (not args.no_fractional),
                "mode": "submitted" if args.confirm else "dry_run",
                "count": len(orders),
                "submitted": ok,
                "failed": fail,
            },
        )
        print(f"[write] paper_orders JSON: {out_path}")

    return 0 if fail == 0 else 1


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。")
    parser.add_argument("--signals-csv", help="final_df 形式の signals CSV パス。")
    parser.add_argument(
        "--signals-json",
        help="today_signals JSON パス。指定時は JSON 経路 (tier 発注)。",
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
    parser.add_argument("--equity", type=float, default=10000.0)
    parser.add_argument("--demo", action="store_true", help="内蔵デモ fixture。")
    parser.add_argument(
        "--confirm",
        action="store_true",
        help="実発注する。無指定では dry-run と等価 (誤爆防止)。",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="各注文の [y/N] 確認をスキップ (無人実行)。--confirm と併用。",
    )
    args = parser.parse_args(argv)

    if args.signals_json:
        return _submit_from_json(args)

    signals = load_signals(args)
    if signals is None or signals.empty:
        print("シグナルなし。発注対象はありません。")
        return 0

    if not args.confirm:
        print("[--confirm なし] dry-run モードで実行します (実発注なし)。")
        orders = signals_to_orders(signals, account_equity=args.equity, dry_run=True)
        for o in orders:
            print(" ", o.to_row())
        print(f"\n合計 {len(orders)} 注文 (dry-run)。実発注は --confirm を付けてください。")
        return 0

    try:
        assert_paper_env()
    except LiveAccountGuardError as exc:
        print(f"[SAFETY ABORT] {exc}")
        return 2

    print("=== PAPER 実発注モード (ALPACA_PAPER=true 確認済) ===")

    planned = signals_to_orders(signals, account_equity=args.equity, dry_run=True)
    if not planned:
        print("発注対象なし。")
        return 0

    submitted, skipped, failed = 0, 0, 0
    for po in planned:
        summary = (
            f"{po.side.upper()} {po.symbol} x{po.qty} "
            f"{po.order_type}"
            + (f"@{po.limit_price}" if po.limit_price else "")
            + f" tif={po.time_in_force} coid={po.client_order_id}"
        )
        if not args.yes and not _confirm(f"送信しますか? {summary}"):
            print(f"  skip: {summary}")
            skipped += 1
            continue
        try:
            result = submit_paper_order(
                po.symbol,
                po.qty,
                po.side,
                order_type=po.order_type,
                limit_price=po.limit_price,
                time_in_force=po.time_in_force,
                client_order_id=po.client_order_id,
                dry_run=False,
            )
            print(f"  OK: {summary} -> id={result.order_id} status={result.status}")
            submitted += 1
        except Exception as exc:  # noqa: BLE001
            print(f"  FAIL: {summary} -> {exc}")
            failed += 1

    print(f"\n完了: 送信={submitted} スキップ={skipped} 失敗={failed}")
    print("結果は Alpaca Paper dashboard と logs/alpaca_orders_*.log で確認できます。")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
