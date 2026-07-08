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
    build_sizing_kwargs,
    load_signals,
)


def _confirm(prompt: str) -> bool:
    try:
        ans = input(f"{prompt} [y/N]: ").strip().lower()
    except EOFError:
        return False
    return ans in ("y", "yes")


def _count_input_signals(json_data: dict) -> int:
    """input JSON の systems[*].signals 総数を数える。

    F2 P0#5 の判定用: input に signals があるのに orders=0 なら「schema
    drift / min_notional 過小 / tier key 誤り」等の anomaly。input が元々
    空 (真の flat book) と区別するために使う。
    """
    total = 0
    systems = (json_data or {}).get("systems") or {}
    if not isinstance(systems, dict):
        return 0
    for sys_block in systems.values():
        if not isinstance(sys_block, dict):
            continue
        sigs = sys_block.get("signals") or []
        if isinstance(sigs, list):
            total += len(sigs)
    return total


def _submit_from_json(args: argparse.Namespace) -> int:
    """--signals-json 経路: JSON → paper_orders JSON + Paper 口座送信 (--confirm 時).

    NOTE (F2 P0#5 audit fix, 2026-07-03):
        以前は ``signals_json_to_orders`` が [] を返しても exit 0 で silent
        success 扱いだった。input JSON に signals が並んでいても schema drift
        (weight 欠落、min_notional 過小、tier キー不整合等) で全弾かれるケース
        が実際に発生し、daily_pipeline は「注文が送信された」と誤認していた。
        修正後は input_signal_count を保存し、input>0 かつ orders=0 の場合は
        anomaly 扱いで:
            * loud warning を print
            * output JSON の meta.status に "no_orders_generated" マーカーを追記
            * exit code 3 (subscribers が silent success と区別可能)
    """
    src = Path(args.signals_json)
    if not src.exists():
        print(f"[error] signals JSON not found: {src}")
        return 2
    with src.open(encoding="utf-8") as fh:
        json_data = json.load(fh)

    input_signal_count = _count_input_signals(json_data)

    sizing_kwargs, sizing_meta = build_sizing_kwargs(args)
    if not args.confirm:
        print("[--confirm なし] dry-run モードで実行します (実発注なし)。")
        orders = signals_json_to_orders(
            json_data,
            tier=args.tier,
            dry_run=True,
            min_notional_usd=args.min_notional,
            prefer_fractional=(not args.no_fractional),
            **sizing_kwargs,
        )
    else:
        try:
            assert_paper_env()
        except LiveAccountGuardError as exc:
            print(f"[SAFETY ABORT] {exc}")
            return 2
        print(
            "=== PAPER 実発注モード (ALPACA_PAPER=true 確認済, mode="
            f"{sizing_meta['sizing_mode']} equity=${sizing_meta['account_equity_usd']:,.0f}"
            f" src={sizing_meta['equity_source']}) ==="
        )
        orders = signals_json_to_orders(
            json_data,
            tier=args.tier,
            dry_run=False,
            min_notional_usd=args.min_notional,
            prefer_fractional=(not args.no_fractional),
            **sizing_kwargs,
        )

    ok = sum(1 for o in orders if o.order_id)
    fail = sum(1 for o in orders if o.error)
    skipped = [o for o in orders if getattr(o, "skip_reason", None)]
    print(
        f"\n完了: 入力 signals={input_signal_count} "
        f"生成={len(orders)} 送信={ok} 失敗={fail} skip={len(skipped)} "
        f"(--confirm={args.confirm})"
    )
    # skip / fail は silent に落とさず、必ず理由付きで可視化する
    # (silent success / silent drop を潰す方針)。
    if skipped:
        from collections import Counter

        kinds = Counter(str(o.skip_reason).split(":", 1)[0] for o in skipped)
        print(f"[skip] pre-submit で {len(skipped)} 件スキップ (内訳: {dict(kinds)}):")
        for o in skipped:
            print(f"    - {o.system} {o.symbol} {o.side}: {o.skip_reason}")
    if fail:
        print(f"[fail] 発注失敗 {fail} 件:")
        for o in orders:
            if o.error:
                print(f"    - {o.system} {o.symbol} {o.side}: {str(o.error)[:80]}")

    # F2 P0#5: 「input signals > 0 なのに生成 orders = 0」は subscriber が
    # silent success と誤解できないよう可視化する。真の flat book
    # (input=0 → orders=0) との区別を必ず出力側に残す。
    if input_signal_count == 0:
        status_marker = "no_input_signals"  # 真の flat book: 静かに exit 0
    elif len(orders) == 0:
        status_marker = "no_orders_generated"  # anomaly: 関数が何も返さなかった
    elif fail > 0 and ok == 0:
        status_marker = "all_submit_failed"
    elif args.confirm and ok == 0:
        # 実発注 (--confirm) したのに 1 件も送信されなかった (全件 skip:
        # min_notional 未満 / wash / unsizable 等)。observability fix (2026-07-07)
        # で min_notional drop が skip として orders に載るようになったため、この
        # 「全 skip」を silent success させず no_orders_generated とは別の anomaly
        # として区別する。dry-run は order_id が無く ok==0 が正常なので対象外。
        status_marker = "no_orders_submitted"
    elif fail > 0:
        status_marker = "partial_failed"
    else:
        status_marker = "ok"

    if status_marker == "no_orders_generated":
        print(
            "[WARN] input signals があるのに 1 件も order が生成されませんでした "
            f"(input={input_signal_count})。schema drift / min_notional 過小 / "
            "tier キー不整合を確認してください。"
        )
    elif status_marker == "no_orders_submitted":
        print(
            "[WARN] order は生成されましたが 1 件も送信されませんでした "
            f"(input={input_signal_count} 生成={len(orders)} skip={len(skipped)})。"
            "skip 内訳 (min_notional 未満 / wash / unsizable 等) を確認してください。"
        )

    if args.output_json:
        out_path = Path(args.output_json)
        _write_orders_json(
            orders,
            out_path,
            {
                "date": str(json_data.get("date") or ""),
                "tier": args.tier,
                "min_notional_usd": args.min_notional,
                "prefer_fractional": (not args.no_fractional),
                "mode": "submitted" if args.confirm else "dry_run",
                "count": len(orders),
                "submitted": ok,
                "failed": fail,
                "skipped": len(skipped),
                "input_signals": input_signal_count,
                "status": status_marker,
                **sizing_meta,
            },
        )
        print(f"[write] paper_orders JSON: {out_path}")

    # exit code policy:
    #   0 = ok / no_input_signals (真の flat book)
    #   1 = partial_failed / all_submit_failed
    #   3 = no_orders_generated / no_orders_submitted (anomaly: subscribers が
    #       silent success と区別できるよう区別 code。1 だと submit_error と紛れる)
    if status_marker in ("no_orders_generated", "no_orders_submitted"):
        return 3
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
        # 真の flat book (input=0) は silent success で OK。
        return 0

    input_signal_count = int(len(signals))

    if not args.confirm:
        print("[--confirm なし] dry-run モードで実行します (実発注なし)。")
        orders = signals_to_orders(signals, account_equity=args.equity, dry_run=True)
        for o in orders:
            print(" ", o.to_row())
        print(
            f"\n合計 {len(orders)} 注文 (dry-run)。実発注は --confirm を付けてください。"
        )
        # dry-run 時も input>0 & orders=0 は anomaly を可視化する。
        if input_signal_count > 0 and len(orders) == 0:
            print(
                "[WARN] input signals があるのに order が 0 件。"
                "schema drift / side 不明 / shares<=0 を確認してください。"
            )
            return 3
        return 0

    try:
        assert_paper_env()
    except LiveAccountGuardError as exc:
        print(f"[SAFETY ABORT] {exc}")
        return 2

    print("=== PAPER 実発注モード (ALPACA_PAPER=true 確認済) ===")

    planned = signals_to_orders(signals, account_equity=args.equity, dry_run=True)
    if not planned:
        # F2 P0#5: input>0 なのに planned=0 は silent success させない。
        print(
            "[WARN] 発注対象なし。input signals があるのに 1 件も plan されませんでした "
            f"(input={input_signal_count})。schema drift / side 不明 / shares<=0 を"
            "確認してください。"
        )
        return 3

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
