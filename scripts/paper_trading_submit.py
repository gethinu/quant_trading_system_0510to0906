"""dry-run で確認した注文を Alpaca **Paper** 口座へ送信する (user 手動実行前提)。

安全設計:
    - ``--confirm`` が無ければ dry-run と等価 (誤爆防止)。
    - 送信前に ``ALPACA_PAPER=true`` と paper エンドポイントを検証 (live で fail-fast)。
    - 各注文の直前に summary を表示し ``[y/N]`` 確認 (CI 用に ``--yes`` で無人実行)。
    - 送信結果は ``logs/alpaca_orders_YYYYMMDD.log`` に追記。

使い方::

    # 1. まず dry-run で確認 (実発注なし)
    python scripts/paper_trading_submit.py --date 2026-06-30

    # 2. 確認できたら実発注 (対話確認あり)
    python scripts/paper_trading_submit.py --date 2026-06-30 --confirm

    # 3. 無人実行 (対話プロンプトなし・Paper のみ)
    python scripts/paper_trading_submit.py --date 2026-06-30 --confirm --yes
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import (  # noqa: E402
    LiveAccountGuardError,
    assert_paper_env,
    signals_to_orders,
    submit_paper_order,
)
from scripts.paper_trading_dryrun import load_signals  # noqa: E402


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。")
    parser.add_argument("--signals-csv", help="final_df 形式の signals CSV パス。")
    parser.add_argument("--equity", type=float, default=100000.0)
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

    signals = load_signals(args)
    if signals is None or signals.empty:
        print("シグナルなし。発注対象はありません。")
        return 0

    # --confirm 無しは常に dry-run 扱い
    if not args.confirm:
        print("[--confirm なし] dry-run モードで実行します (実発注なし)。")
        orders = signals_to_orders(signals, account_equity=args.equity, dry_run=True)
        for o in orders:
            print(" ", o.to_row())
        print(f"\n合計 {len(orders)} 注文 (dry-run)。実発注は --confirm を付けてください。")
        return 0

    # ---- ここから実発注経路 ----
    try:
        assert_paper_env()  # live fail-fast
    except LiveAccountGuardError as exc:
        print(f"[SAFETY ABORT] {exc}")
        return 2

    print("=== PAPER 実発注モード (ALPACA_PAPER=true 確認済) ===")

    # 変換のみ dry_run=True で行い、確認しながら 1 件ずつ送信する
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
