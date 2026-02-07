"""Daily signal notification orchestration.

This module centralizes the signal notification workflow:
- load today's entry signals from CSVs
- send entry notifications (Slack/Discord)
- update the position tracker
- compute and notify exit signals
"""

from __future__ import annotations

from datetime import datetime
import logging
from pathlib import Path

from PIL import Image
import pandas as pd

from common.exit_signals import build_exit_signals_from_tracker
from common.notifier import chunk_fields, create_notifier, now_jst_str
from common.price_chart import save_price_chart
from common.position_tracker import update_positions_from_signals
from common.trade_cache import pop_entry, store_entry
from common.signal_io import get_signals_dir, read_signal_frames, select_signal_files


def _combine_images(paths: list[str]) -> str:
    """Combine images vertically and return the output path."""
    images = [Image.open(p) for p in paths if p]
    if not images:
        return ""
    width = max(img.width for img in images)
    height = sum(img.height for img in images)
    canvas = Image.new("RGB", (width, height), "white")
    y = 0
    for img in images:
        canvas.paste(img, (0, y))
        y += img.height
        img.close()
    out_dir = Path(paths[0]).resolve().parent
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"combined_{datetime.now().strftime('%Y%m%d_%H%M%S')}.png"
    canvas.save(out_path)
    return str(out_path)


def _infer_action(row: pd.Series) -> str:
    for key in ("signal_type", "action", "side"):
        try:
            raw = row.get(key)
        except Exception:
            raw = None
        if raw is None:
            continue
        val = str(raw).strip().lower()
        if not val:
            continue
        if val in {"buy", "long", "entry"}:
            return "BUY"
        if val in {"sell", "short", "exit"}:
            return "SELL"
    return ""


def notify_signals() -> None:
    sig_dir = get_signals_dir(create_dirs=True)
    if not sig_dir.exists():
        logging.info("signals ディレクトリが存在しません: %s", sig_dir)
        return

    today_str = datetime.today().strftime("%Y-%m-%d")
    files = select_signal_files(sig_dir, today_str)
    if not files:
        logging.info("本日の新規シグナルCSVは見つかりませんでした。")
        _send_exit_notifications(sig_dir, today_str)
        return

    frames = read_signal_frames(files)
    for f, df in zip(files, frames, strict=False):
        try:
            logging.info("シグナル: %s (%d 件)", f.name, len(df))
        except Exception:
            pass
    total = sum(len(df) for df in frames)
    logging.info("本日の合計シグナル件数: %d", total)
    if frames:
        all_df = pd.concat(frames, ignore_index=True)
        send_signal_notification(all_df, signals_dir=sig_dir, date_str=today_str)
    else:
        _send_exit_notifications(sig_dir, today_str)


def _send_entry_notifications(df: pd.DataFrame) -> None:
    """Send entry notifications using Slack (fallback to Discord)."""
    n = create_notifier(platform="slack", fallback=True)
    try:
        if "system" in df.columns:
            groups = df.groupby("system")
        else:
            groups = [("integrated", df)]
        for sys_name, g in groups:
            raw_symbols = g["symbol"].astype(str).tolist()
            if "close" in g.columns:
                closes = g["close"].tolist()
                symbols = []
                for sym, close in zip(raw_symbols, closes, strict=False):
                    try:
                        price = float(close)
                        symbols.append(f"{sym} ${price:.2f}")
                    except Exception:
                        symbols.append(sym)
            else:
                symbols = raw_symbols
            n.send_signals(str(sys_name), symbols)
            chart_paths: list[str] = []
            for sym in raw_symbols:
                try:
                    row = g[g["symbol"] == sym].iloc[0]
                except Exception:
                    row = None
                trades_df: pd.DataFrame | None = None
                if row is not None:
                    action = _infer_action(row)
                    if action == "BUY":
                        entry_date = row.get("entry_date")
                        entry_price = row.get("entry_price")
                        if entry_date and entry_price:
                            store_entry(sym, str(entry_date), float(entry_price))
                        trades_df = pd.DataFrame([row])
                    elif action == "SELL":
                        entry_info = pop_entry(sym) or {}
                        if entry_info:
                            row = {**row.to_dict(), **entry_info}
                        trades_df = pd.DataFrame([row])
                try:
                    img_path, _ = save_price_chart(sym, trades=trades_df)
                    if img_path:
                        chart_paths.append(img_path)
                except Exception:
                    logging.exception("failed to generate chart for %s", sym)
            if chart_paths:
                try:
                    combined = _combine_images(chart_paths)
                    if combined:
                        send_with_mention = getattr(n, "send_with_mention", None)
                        if callable(send_with_mention):
                            msg = "\n".join(symbols)
                            send_with_mention(
                                "📈 日足チャート",
                                msg,
                                mention=False,
                                image_path=combined,
                            )
                        else:
                            n.send_signals("charts", ["\n".join(symbols)])
                except Exception:
                    logging.exception("failed to send combined chart")
    except Exception:
        logging.exception("signal notification failed (slack+discord)")


def send_signal_notification(
    df: pd.DataFrame, *, signals_dir: Path | None = None, date_str: str | None = None
) -> None:
    """Send a brief notification for the given signals DataFrame."""
    if df is None or df.empty:
        return
    logging.info("Today signals: %d picks", len(df))
    _send_entry_notifications(df)
    try:
        update_positions_from_signals(df)
    except Exception:
        logging.exception("position tracker update failed")
    try:
        _send_exit_notifications(signals_dir, date_str)
    except Exception:
        logging.exception("exit signal notification failed")


def _format_exit_line(row: pd.Series) -> str:
    symbol = str(row.get("symbol", "")).upper()
    system = str(row.get("system", "")).lower()
    side = str(row.get("side", "")).lower()
    reason = str(row.get("exit_reason", ""))
    when = str(row.get("when", ""))
    exit_price = row.get("exit_price")
    action = "SELL" if side == "long" else "BUY"
    try:
        price_text = f"${float(exit_price):.2f}"
    except Exception:
        price_text = str(exit_price or "")
    suffix = f"{price_text} {when}".strip()
    if system:
        return f"{symbol} {action} ({system}) {reason} {suffix}".strip()
    return f"{symbol} {action} {reason} {suffix}".strip()


def _save_exit_signals(
    exit_df: pd.DataFrame, signals_dir: Path | None, date_str: str | None
) -> None:
    if exit_df is None or getattr(exit_df, "empty", True):
        return
    if signals_dir is None:
        signals_dir = get_signals_dir(create_dirs=True)
    if date_str is None:
        date_str = datetime.today().strftime("%Y-%m-%d")
    try:
        signals_dir.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    out_path = signals_dir / f"signals_exit_{date_str}.csv"
    try:
        from common.io_utils import df_to_csv

        df_to_csv(exit_df, out_path, index=False)
    except Exception:
        try:
            exit_df.to_csv(out_path, index=False)
        except Exception:
            logging.exception("exit signal csv write failed: %s", out_path)


def _send_exit_notifications(
    signals_dir: Path | None = None, date_str: str | None = None
) -> None:
    exit_df = build_exit_signals_from_tracker()
    if exit_df is None or getattr(exit_df, "empty", True):
        logging.info("本日のエグジットシグナルはありません。")
        return

    _save_exit_signals(exit_df, signals_dir, date_str)

    notifier = create_notifier(platform="slack", fallback=True)
    lines = [_format_exit_line(row) for _, row in exit_df.iterrows()]
    title = f"Exit Signals ・ {now_jst_str()}"
    summary = f"エグジット件数: {len(lines)}"
    fields = chunk_fields("銘柄", lines, inline=False) if lines else None
    notifier.send(title, summary, fields=fields)


__all__ = ["notify_signals", "send_signal_notification"]
