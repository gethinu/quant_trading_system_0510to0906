"""dry-run で確認した注文を Alpaca **Paper** 口座へ送信する (user 手動実行前提)。

多層 safeguard (発注前):
    1. ``--confirm`` が無ければ dry-run と等価 (誤爆防止)。
    2. ``ALPACA_PAPER=true`` を検証 (false なら fail-fast)。
    3. ``ALPACA_API_BASE_URL`` が paper エンドポイントを指すか検証 (live 検出で abort)。
    4. dry-run preview JSON との突合 (乖離があれば abort)。
    5. 各注文の直前に summary を表示し ``[y/N]`` 確認 (``--yes`` で無人 bypass)。
    6. Ctrl+C で即時 halt (残注文は skip)。
    7. 送信結果は ``logs/alpaca_orders_YYYYMMDD.log`` に追記。

使い方 (JSON / account_equity scale)::

    # 1. まず dry-run で preview を作る (実発注なし)
    python scripts/paper_trading_dryrun.py --date 2026-07-01 --account-equity 10000

    # 2. 確認できたら実発注 (対話確認あり、Paper のみ)
    python scripts/paper_trading_submit.py --date 2026-07-01 --account-equity 10000 --confirm

    # 3. 無人実行 (対話プロンプトなし・Paper のみ)
    python scripts/paper_trading_submit.py --date 2026-07-01 --account-equity 10000 --confirm --yes
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import (  # noqa: E402
    LiveAccountGuardError,
    OrderPlan,
    assert_paper_env,
    signals_json_to_orders,
    signals_to_orders,
    submit_paper_order,
)
from scripts.paper_trading_dryrun import (  # noqa: E402
    load_signals,
    resolve_signals_json,
)


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _order_notional(o) -> float:
    if o.notional is not None:
        return float(o.notional)
    if o.price is not None:
        return float(o.qty) * float(o.price)
    return 0.0


# ---------------------------------------------------------------------------
# safeguard 4: dry-run preview JSON との突合
# ---------------------------------------------------------------------------
def _reconcile_with_preview(plan: OrderPlan, preview_path: Path) -> None:
    """preview JSON (dryrun 出力) と実発注 plan が一致するか検証する。

    client_order_id の集合と、各注文の side / notional が乖離していれば
    :class:`SystemExit` で abort する (誤ったサイズ/銘柄の発注を防ぐ)。
    """
    if not preview_path.exists():
        raise SystemExit(
            f"[SAFETY ABORT] preview JSON が見つかりません: {preview_path}\n"
            "先に paper_trading_dryrun.py で同じ --date/--account-equity の preview を生成してください。"
        )
    preview = json.loads(preview_path.read_text(encoding="utf-8"))
    prev_by_coid = {o["client_order_id"]: o for o in preview.get("orders", [])}
    plan_by_coid = {o.client_order_id: o for o in plan.orders}

    if set(prev_by_coid) != set(plan_by_coid):
        only_prev = set(prev_by_coid) - set(plan_by_coid)
        only_plan = set(plan_by_coid) - set(prev_by_coid)
        raise SystemExit(
            "[SAFETY ABORT] preview と実発注 plan の注文集合が乖離しています。\n"
            f"  preview のみ: {sorted(only_prev)}\n  plan のみ:    {sorted(only_plan)}\n"
            "preview を再生成してから実行してください。"
        )
    for coid, po in plan_by_coid.items():
        pv = prev_by_coid[coid]
        if pv["side"] != po.side or abs(float(pv["notional_usd"]) - _order_notional(po)) > 0.01:
            raise SystemExit(
                f"[SAFETY ABORT] {coid} が preview と乖離: "
                f"preview(side={pv['side']}, notional={pv['notional_usd']}) != "
                f"plan(side={po.side}, notional={_order_notional(po):.2f})"
            )
    print(f"[reconcile OK] preview と一致: {len(plan.orders)} 注文 ({preview_path.name})")


def _submit_plan(plan: OrderPlan, args: argparse.Namespace) -> int:
    """OrderPlan の各注文を 1 件ずつ確認しながら Paper 口座へ送信する。"""
    if not plan.orders:
        print("発注対象なし。")
        return 0

    submitted, skipped, failed = 0, 0, 0
    total = len(plan.orders)
    try:
        for idx, po in enumerate(plan.orders, start=1):
            size = (
                f"notional ${po.notional:,.2f}"
                if po.fractional
                else f"x{int(po.qty)} @ {po.order_type}"
            )
            summary = (
                f"[{idx}/{total}] {po.side.upper()} {po.symbol} {size} "
                f"(sys={po.system} rank={po.rank} wt={po.weight}) coid={po.client_order_id}"
            )
            if not args.yes and not _confirm(f"送信しますか? {summary}"):
                print(f"  skip: {summary}")
                skipped += 1
                continue
            try:
                result = submit_paper_order(
                    po.symbol,
                    int(po.qty) if not po.fractional else 0,
                    po.side,
                    order_type=po.order_type,
                    limit_price=po.limit_price,
                    time_in_force=po.time_in_force,
                    client_order_id=po.client_order_id,
                    notional=po.notional if po.fractional else None,
                    dry_run=False,
                )
                print(f"  OK: {summary} -> id={result.order_id} status={result.status}")
                submitted += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL: {summary} -> {exc}")
                failed += 1
    except KeyboardInterrupt:
        remaining = total - submitted - skipped - failed
        print(f"\n[INTERRUPT] Ctrl+C 検出。残 {remaining} 注文を中止しました。")

    print(f"\n完了: 送信={submitted} スキップ={skipped} 失敗={failed}")
    print("結果は Alpaca Paper dashboard と logs/alpaca_orders_*.log で確認できます。")
    return 0 if failed == 0 else 1


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。")
    parser.add_argument("--signals-json", help="signals JSON (Phase 1 pack) のパス。")
    parser.add_argument("--signals-csv", help="legacy final_df 形式 CSV のパス。")
    parser.add_argument("--account-equity", type=float, default=10000.0, help="口座資産 (USD)。")
    parser.add_argument("--equity", type=float, dest="account_equity", help=argparse.SUPPRESS)
    parser.add_argument("--tier", default="auto", choices=["auto", "small", "medium", "large"])
    parser.add_argument("--min-notional", type=float, default=5.0)
    parser.add_argument("--no-fractional", action="store_true")
    parser.add_argument("--preview-dir", default="results_csv", help="突合する preview JSON の探索先。")
    parser.add_argument("--demo-json", action="store_true", help="内蔵 mock signals JSON。")
    parser.add_argument("--demo", action="store_true", help="legacy デモ fixture。")
    parser.add_argument("--confirm", action="store_true", help="実発注する。無指定は dry-run 等価。")
    parser.add_argument("--yes", action="store_true", help="各注文の [y/N] 確認を bypass (無人)。")
    parser.add_argument("--skip-reconcile", action="store_true", help="preview 突合を skip (非推奨)。")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    # legacy DataFrame 経路 (--demo / --signals-csv 明示時のみ)
    if args.demo or args.signals_csv:
        return _main_dataframe(args)

    json_path = resolve_signals_json(args)
    if json_path is None:
        # JSON が無ければ legacy にフォールバック
        return _main_dataframe(args)

    print(f"[load] {json_path}")
    signals_json = json.loads(json_path.read_text(encoding="utf-8"))
    plan = signals_json_to_orders(
        signals_json,
        account_equity=args.account_equity,
        tier=args.tier,
        min_notional_usd=args.min_notional,
        prefer_fractional=not args.no_fractional,
        dry_run=True,  # 変換のみ
    )

    # safeguard 1: --confirm 無しは dry-run
    if not args.confirm:
        print("[--confirm なし] dry-run モード (実発注なし)。")
        for o in plan.orders:
            print("  ", o.to_row())
        print(f"\n合計 {len(plan.orders)} 注文 (dry-run)。実発注は --confirm を付けてください。")
        return 0

    # safeguard 2 & 3: Paper env + URL 検証 (live で fail-fast)
    try:
        assert_paper_env()
    except LiveAccountGuardError as exc:
        print(f"[SAFETY ABORT] {exc}")
        return 2
    print("=== PAPER 実発注モード (ALPACA_PAPER=true / paper URL 確認済) ===")

    # safeguard 4: preview JSON 突合
    if not args.skip_reconcile:
        compact = (plan.date or args.date or "").replace("-", "") or "unknown"
        preview_path = Path(args.preview_dir) / f"orders_preview_{compact}_{int(args.account_equity)}.json"
        try:
            _reconcile_with_preview(plan, preview_path)
        except SystemExit as exc:
            print(exc)
            return 2

    return _submit_plan(plan, args)


def _main_dataframe(args: argparse.Namespace) -> int:
    """legacy final_df CSV 経路 (後方互換)。"""
    signals = load_signals(args)
    if signals is None or signals.empty:
        print("シグナルなし。発注対象はありません。")
        return 0
    if not args.confirm:
        print("[--confirm なし] dry-run モードで実行します (実発注なし)。")
        orders = signals_to_orders(signals, account_equity=args.account_equity, dry_run=True)
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
    planned = signals_to_orders(signals, account_equity=args.account_equity, dry_run=True)
    if not planned:
        print("発注対象なし。")
        return 0
    submitted, skipped, failed = 0, 0, 0
    try:
        for po in planned:
            summary = (
                f"{po.side.upper()} {po.symbol} x{int(po.qty)} {po.order_type}"
                + (f"@{po.limit_price}" if po.limit_price else "")
                + f" coid={po.client_order_id}"
            )
            if not args.yes and not _confirm(f"送信しますか? {summary}"):
                print(f"  skip: {summary}")
                skipped += 1
                continue
            try:
                result = submit_paper_order(
                    po.symbol, int(po.qty), po.side,
                    order_type=po.order_type, limit_price=po.limit_price,
                    time_in_force=po.time_in_force, client_order_id=po.client_order_id,
                    dry_run=False,
                )
                print(f"  OK: {summary} -> id={result.order_id} status={result.status}")
                submitted += 1
            except Exception as exc:  # noqa: BLE001
                print(f"  FAIL: {summary} -> {exc}")
                failed += 1
    except KeyboardInterrupt:
        print("\n[INTERRUPT] Ctrl+C 検出。残注文を中止しました。")
    print(f"\n完了: 送信={submitted} スキップ={skipped} 失敗={failed}")
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
