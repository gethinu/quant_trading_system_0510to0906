"""当日シグナルを Alpaca Paper 口座へ流すシミュレーション (dry-run / 実発注なし)。

当日 ``final_df`` (アロケーション済シグナル) を読み込み、
``common.alpaca_trading.signals_to_orders(dry_run=True)`` で Alpaca 注文へ変換し、
送信予定内容を表形式で表示するだけ。**実発注は一切行わない。**

使い方::

    python scripts/paper_trading_dryrun.py --date 2026-06-30
    python scripts/paper_trading_dryrun.py --signals-csv path/to/final_signals.csv
    python scripts/paper_trading_dryrun.py --demo    # 内蔵デモ fixture で動作確認

シグナル CSV は ``run_all_systems_today.py`` が出力する ``final_df`` を想定
(列: symbol, system, side, shares, entry_price, entry_date)。
"""

from __future__ import annotations

import argparse
from pathlib import Path
import sys

import pandas as pd

# リポジトリルートを import path に追加 (scripts/ から実行される想定)
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from common.alpaca_trading import signals_to_orders  # noqa: E402


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
    """--date 指定時に既定の signals CSV パスを推定する。"""
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


def load_signals(args: argparse.Namespace) -> pd.DataFrame:
    if args.demo:
        print("[demo] 内蔵デモ fixture を使用します (API 不要)。")
        return _demo_signals()
    csv_path = Path(args.signals_csv) if args.signals_csv else _default_csv_for_date(args.date)
    if csv_path is None or not csv_path.exists():
        raise SystemExit(
            "signals CSV が見つかりません。--signals-csv <path> を指定するか、"
            "run_all_systems_today.py で当日シグナルを生成してください。"
            " (動作確認のみなら --demo)"
        )
    print(f"[load] {csv_path}")
    return pd.read_csv(csv_path)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--date", help="対象日 (YYYY-MM-DD)。既定 CSV パス推定に使用。")
    parser.add_argument("--signals-csv", help="final_df 形式の signals CSV パス。")
    parser.add_argument("--equity", type=float, default=100000.0, help="口座資産 (ログ用)。")
    parser.add_argument("--demo", action="store_true", help="内蔵デモ fixture で動作確認。")
    args = parser.parse_args(argv)

    signals = load_signals(args)
    if signals is None or signals.empty:
        print("シグナルなし。発注対象はありません。")
        return 0

    # dry_run=True なので実発注は絶対に行われない
    orders = signals_to_orders(signals, account_equity=args.equity, dry_run=True)

    if not orders:
        print("変換後の発注対象はありません (shares<=0 等でフィルタ)。")
        return 0

    rows = [o.to_row() for o in orders]
    df = pd.DataFrame(rows)
    cols = [
        "symbol", "side", "qty", "order_type", "limit_price",
        "time_in_force", "system", "client_order_id",
    ]
    df = df[[c for c in cols if c in df.columns]]

    print("\n===== DRY-RUN: 送信予定注文 (実発注なし) =====")
    print(df.to_string(index=False))
    print(f"\n合計 {len(df)} 注文 / equity=${args.equity:,.0f}")
    print("※ これは dry-run です。実発注は scripts/paper_trading_submit.py --confirm で行います。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
