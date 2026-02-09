"""Daily signal notification orchestration.

This module centralizes the signal notification workflow:
- load today's entry signals from CSVs
- send entry notifications (Slack/Discord)
- update the position tracker
- compute and notify exit signals
"""

from __future__ import annotations

from datetime import datetime
import math
import logging
import os
from pathlib import Path

from PIL import Image
import pandas as pd

from common.cache_manager import CacheManager
from common.exit_plan import (
    build_exit_plan_from_signal_row,
    compute_time_exit_date,
    format_exit_plan_from_signal_row,
)
from common.exit_planner import decide_exit_schedule
from common.exit_signals import build_exit_signals_from_tracker
from common.notifier import (
    Notifier,
    chunk_fields,
    create_notifier,
    format_table,
    now_jst_str,
)
from common.price_chart import save_price_chart
from common.position_tracker import load_tracker, remove_positions, update_positions_from_signals
from common.trade_cache import pop_entry, store_entry
from common.trade_management import SYSTEM_TRADE_RULES
from common.signal_io import get_signals_dir, read_signal_frames, select_signal_files
from common.today_filters import _pick_series
from config.environment import get_env_config
from config.settings import get_settings


_LONG_SYSTEMS = ("system1", "system3", "system4", "system5")
_SHORT_SYSTEMS = ("system2", "system6", "system7")


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


def _safe_float(val) -> float | None:
    try:
        if val is None:
            return None
        if isinstance(val, str) and not val.strip():
            return None
        num = float(val)
    except Exception:
        return None
    return num


def _safe_int(val) -> int | None:
    try:
        if val is None:
            return None
        if isinstance(val, str) and not val.strip():
            return None
        num = int(float(val))
    except Exception:
        return None
    return num


def _safe_bool(val) -> bool | None:
    if val is None:
        return None
    if isinstance(val, bool):
        return val
    try:
        s = str(val).strip().lower()
    except Exception:
        return None
    if s in {"1", "true", "yes", "on"}:
        return True
    if s in {"0", "false", "no", "off"}:
        return False
    return None


def _safe_date(val) -> pd.Timestamp | None:
    if val is None or val == "":
        return None
    try:
        ts = pd.to_datetime(val, errors="coerce")
    except Exception:
        return None
    if ts is None or pd.isna(ts):
        return None
    try:
        if ts.tzinfo is not None:
            ts = ts.tz_convert(None)
    except Exception:
        try:
            ts = ts.tz_localize(None)
        except Exception:
            pass
    return pd.Timestamp(ts).normalize()


def _resolve_side(info: dict, system_name: str | None = None) -> str:
    raw = str(info.get("side") or "").lower()
    if raw in {"long", "short"}:
        return raw
    sys = (system_name or str(info.get("system") or "")).lower()
    if sys in _LONG_SYSTEMS:
        return "long"
    if sys in _SHORT_SYSTEMS:
        return "short"
    return ""


def _latest_close(cache_manager: CacheManager, symbol: str) -> float | None:
    if not symbol:
        return None
    try:
        df = cache_manager.read(symbol, "rolling")
    except Exception:
        return None
    if df is None or getattr(df, "empty", True):
        return None
    series = None
    try:
        series = _pick_series(df, ["Close", "close", "CLOSE"])
    except Exception:
        series = None
    if series is None or getattr(series, "empty", False):
        return None
    try:
        val = series.iloc[-1]
        if pd.isna(val):
            val = series.dropna().iloc[-1]
        return float(val)
    except Exception:
        try:
            return float(series.dropna().iloc[-1])
        except Exception:
            return None


def _format_unrealized(
    entry_price: float | None,
    current_price: float | None,
    qty: float | None,
    side: str,
) -> str:
    if entry_price is None or current_price is None or entry_price == 0:
        return ""
    diff = current_price - entry_price
    if side == "short":
        diff = -diff
    pnl_pct = (diff / entry_price) * 100.0
    sign = "+" if pnl_pct >= 0 else ""
    text = f"{sign}{pnl_pct:.1f}%"
    if qty is not None and qty > 0:
        pnl_abs = diff * qty
        sign_abs = "+" if pnl_abs >= 0 else ""
        text = f"{text} ({sign_abs}${abs(pnl_abs):,.0f})"
    return text


def _normalize_price_df(df: pd.DataFrame) -> pd.DataFrame | None:
    if df is None or getattr(df, "empty", True):
        return None
    try:
        work = df.copy()
    except Exception:
        return None
    try:
        work.columns = [str(c) for c in work.columns]
    except Exception:
        pass
    if "Date" in work.columns:
        idx = pd.to_datetime(work["Date"], errors="coerce")
    elif "date" in work.columns:
        idx = pd.to_datetime(work["date"], errors="coerce")
    else:
        idx = pd.to_datetime(work.index, errors="coerce")
    work = work.assign(_idx=idx).dropna(subset=["_idx"])
    work = work.sort_values("_idx")
    work = work.set_index("_idx")
    work.index = pd.to_datetime(work.index).normalize()
    return work


def _pick_atr(info: dict, period: int) -> float | None:
    if period <= 0:
        return None
    key = f"atr{int(period)}"
    val = _safe_float(info.get(key))
    if val is not None and val > 0:
        return val
    key2 = f"ATR{int(period)}"
    val2 = _safe_float(info.get(key2))
    if val2 is not None and val2 > 0:
        return val2
    return None


def _compute_stop_price(
    info: dict,
    entry_price: float | None,
    side: str,
    system: str,
) -> float | None:
    stop_price = _safe_float(info.get("stop_price"))
    if stop_price is not None and stop_price > 0:
        return stop_price
    if entry_price is None or entry_price <= 0:
        return None
    rules = SYSTEM_TRADE_RULES.get(system)
    if rules is None:
        return None
    atr_val = _pick_atr(info, int(rules.stop_atr_period or 0))
    if atr_val is None or atr_val <= 0:
        return None
    dist = float(rules.stop_atr_multiplier or 0.0) * atr_val
    if side == "short":
        return entry_price + dist
    return entry_price - dist


def _compute_target_price(
    info: dict,
    entry_price: float | None,
    side: str,
    system: str,
) -> float | None:
    target_price = _safe_float(info.get("profit_target_price"))
    if target_price is not None and target_price > 0:
        return target_price
    if entry_price is None or entry_price <= 0:
        return None
    rules = SYSTEM_TRADE_RULES.get(system)
    if rules is None:
        return None
    if rules.profit_target_type == "percentage":
        pct = float(rules.profit_target_value or 0.0) / 100.0
        if pct <= 0:
            return None
        if side == "short":
            return entry_price / (1.0 + pct)
        return entry_price * (1.0 + pct)
    if rules.profit_target_type == "atr":
        atr_period = int(rules.profit_target_atr_period or rules.stop_atr_period or 0)
        atr_val = _pick_atr(info, atr_period)
        if atr_val is None or atr_val <= 0:
            return None
        dist = atr_val * float(rules.profit_target_value or 0.0)
        if side == "short":
            return entry_price - dist
        return entry_price + dist
    return None


def _compute_trailing_stop_price(
    df: pd.DataFrame | None,
    entry_date: pd.Timestamp | None,
    side: str,
    trailing_pct: float | None,
) -> float | None:
    if df is None or trailing_pct is None:
        return None
    if trailing_pct <= 0:
        return None
    seg = df
    if entry_date is not None and entry_date in df.index:
        try:
            seg = df.loc[df.index >= entry_date]
        except Exception:
            seg = df
    if seg is None or getattr(seg, "empty", True):
        seg = df
    if side == "short":
        series = _pick_series(seg, ["Low", "low", "Close", "close"])
        if series is None or getattr(series, "empty", True):
            return None
        try:
            min_low = float(series.min())
        except Exception:
            return None
        return min_low * (1.0 + trailing_pct)
    series = _pick_series(seg, ["High", "high", "Close", "close"])
    if series is None or getattr(series, "empty", True):
        return None
    try:
        max_high = float(series.max())
    except Exception:
        return None
    return max_high * (1.0 - trailing_pct)


def _format_stop_price_floor_note() -> str | None:
    try:
        floor_raw = get_settings(create_dirs=False).risk.stop_price_floor
        floor = float(floor_raw)
    except Exception:
        return None
    if not math.isfinite(floor) or floor <= 0:
        return None
    floor_text = f"{floor:.4f}".rstrip("0").rstrip(".")
    return f"stop_price_floor=${floor_text}（ストップ下限）"


def _score_key_is_asc(key: str | None) -> bool:
    try:
        return str(key or "").upper() in {"RSI4"}
    except Exception:
        return False


def _short_system_label(name: str) -> str:
    s = str(name or "").lower()
    if s.startswith("system"):
        suffix = s.replace("system", "", 1)
        return f"S{suffix}"
    return s or "-"


def _pick_entry_price(row: pd.Series) -> float | None:
    for key in ("entry_price_final", "entry_order_price", "entry_price"):
        val = _safe_float(row.get(key))
        if val is not None and val > 0:
            return float(val)
    return None


def _rank_or_score_text(row: pd.Series) -> str:
    rank = _safe_int(row.get("score_rank"))
    total = _safe_int(row.get("score_rank_total"))
    if rank is not None and total is not None and total > 0:
        return f"rank {rank}/{total}"
    score = _safe_float(row.get("score"))
    key = row.get("score_key")
    if score is not None and key:
        try:
            return f"{str(key).upper()}={score:.2f}"
        except Exception:
            return f"{str(key).upper()}={score}"
    return ""


def _format_time_exit_text(date_str: str | None, when: str | None) -> str:
    if not date_str:
        return "-"
    when_key = str(when or "").strip().lower()
    when_map = {
        "today_close": "CLS",
        "tomorrow_close": "CLS",
        "tomorrow_open": "OPG",
    }
    label = when_map.get(when_key, "")
    return f"{date_str} {label}".strip()


def _pick_time_exit(row: pd.Series) -> str:
    plan = build_exit_plan_from_signal_row(row)
    if plan:
        date_str = str(plan.get("time_exit_date") or "")
        when = str(plan.get("time_exit_when") or "")
        if date_str:
            return _format_time_exit_text(date_str, when)
    date_str, when = compute_time_exit_date(
        str(row.get("system", "")) or "",
        row.get("entry_date"),
        row.get("max_holding_days"),
    )
    return _format_time_exit_text(date_str, when)


def _sort_signals_frame(df: pd.DataFrame) -> pd.DataFrame:
    if df is None or getattr(df, "empty", True):
        return df
    x = df.copy()

    def _calc_sort(row: pd.Series) -> tuple[int, float]:
        rank = _safe_float(row.get("score_rank"))
        total = _safe_float(row.get("score_rank_total"))
        if rank is not None and total is not None and total > 0:
            return 0, float(rank) / float(total)
        score = _safe_float(row.get("score"))
        if score is not None:
            asc = _score_key_is_asc(row.get("score_key"))
            return 1, float(score) if asc else -float(score)
        return 2, 0.0

    try:
        sort_keys = x.apply(_calc_sort, axis=1, result_type="expand")
        x["_sort_group"] = sort_keys[0]
        x["_sort_value"] = sort_keys[1]
        x = x.sort_values(["_sort_group", "_sort_value"]).drop(
            columns=["_sort_group", "_sort_value"]
        )
    except Exception:
        pass
    return x.reset_index(drop=True)


def _format_pick_line(row: pd.Series) -> str:
    sym = str(row.get("symbol", "")).upper()
    system = _short_system_label(row.get("system", ""))
    side = str(row.get("side", "")).lower()
    icon = "🟢" if side == "long" else ("🔴" if side == "short" else "")
    entry = _pick_entry_price(row)
    price_text = f"${entry:.2f}" if entry is not None else ""
    rank_text = _rank_or_score_text(row)
    base = f"{icon} {sym} {price_text}".strip()
    extra = f"{system}" if system else ""
    if rank_text:
        extra = f"{extra} {rank_text}".strip()
    return f"{base} ({extra})" if extra else base


def _format_detail_line(row: pd.Series, *, highlight: bool = False) -> str:
    sym = str(row.get("symbol", "")).upper()
    entry = _pick_entry_price(row)
    prefix = "★" if highlight else "・"
    line = f"{prefix} {sym}"
    if entry is not None:
        line = f"{line} ${entry:.2f}"
    rank_text = _rank_or_score_text(row)
    if rank_text:
        line = f"{line} [{rank_text}]"
    plan = format_exit_plan_from_signal_row(row)
    if plan:
        line = f"{line} | {plan}"
    return line


def _allocate_pick_slots(
    long_total: int,
    short_total: int,
    max_positions: int,
    long_ratio: float,
) -> tuple[int, int]:
    if max_positions <= 0:
        return 0, 0
    long_slots = int(round(max_positions * max(0.0, min(1.0, long_ratio))))
    short_slots = max_positions - long_slots
    long_slots = min(long_slots, long_total)
    short_slots = min(short_slots, short_total)
    remaining = max_positions - (long_slots + short_slots)
    if remaining > 0:
        add_long = min(long_total - long_slots, remaining)
        long_slots += add_long
        remaining -= add_long
    if remaining > 0:
        add_short = min(short_total - short_slots, remaining)
        short_slots += add_short
    return long_slots, short_slots


def _round_robin_picks(
    system_rows: dict[str, list[dict]],
    order: tuple[str, ...],
    slots: int,
) -> list[dict]:
    picks: list[dict] = []
    if slots <= 0:
        return picks
    indices = {s: 0 for s in order}
    while len(picks) < slots:
        progressed = False
        for system in order:
            rows = system_rows.get(system) or []
            idx = indices.get(system, 0)
            if idx < len(rows):
                picks.append(rows[idx])
                indices[system] = idx + 1
                progressed = True
                if len(picks) >= slots:
                    break
        if not progressed:
            break
    return picks


def _build_summary_lines(df: pd.DataFrame) -> tuple[str, int]:
    if df is None or df.empty:
        return "", 0

    total = int(len(df))
    df_norm = df.copy()
    if "system" in df_norm.columns:
        df_norm["system"] = df_norm["system"].astype(str).str.lower()
    if "side" in df_norm.columns:
        df_norm["side"] = df_norm["side"].astype(str).str.lower()

    by_system = (
        df_norm["system"].value_counts().to_dict()
        if "system" in df_norm.columns
        else {}
    )
    long_total = int(
        df_norm[df_norm.get("side", "").eq("long")].shape[0]
        if "side" in df_norm.columns
        else sum(by_system.get(s, 0) for s in _LONG_SYSTEMS)
    )
    short_total = int(
        df_norm[df_norm.get("side", "").eq("short")].shape[0]
        if "side" in df_norm.columns
        else sum(by_system.get(s, 0) for s in _SHORT_SYSTEMS)
    )

    try:
        settings = get_settings(create_dirs=False)
        max_positions = int(getattr(settings.risk, "max_positions", 10) or 10)
        long_ratio = float(getattr(settings.ui, "default_long_ratio", 0.5) or 0.5)
        default_capital = int(getattr(settings.ui, "default_capital", 0) or 0)
    except Exception:
        max_positions = 10
        long_ratio = 0.5
        default_capital = 0

    long_slots, short_slots = _allocate_pick_slots(
        long_total, short_total, max_positions, long_ratio
    )

    summary_rows: list[list[str]] = [
        ["Total", str(total)],
        ["Long", str(long_total)],
        ["Short", str(short_total)],
        ["max_positions", str(max_positions)],
        ["long_ratio", f"{long_ratio:.0%}"],
        ["focus_long", str(long_slots)],
        ["focus_short", str(short_slots)],
    ]
    if default_capital and max_positions > 0:
        per_pos = default_capital / max_positions
        summary_rows.append(["capital", f"${default_capital:,.0f}"])
        summary_rows.append(["per_position", f"${per_pos:,.0f}"])
    stop_note = _format_stop_price_floor_note()
    if stop_note:
        summary_rows.append(["stop_floor", stop_note])

    summary_table = format_table(
        summary_rows,
        headers=["Item", "Value"],
        max_width=100,
    )

    system_rows: list[list[str]] = []
    for sys in _LONG_SYSTEMS:
        system_rows.append(
            [_short_system_label(sys), "Long", str(int(by_system.get(sys, 0)))]
        )
    for sys in _SHORT_SYSTEMS:
        system_rows.append(
            [_short_system_label(sys), "Short", str(int(by_system.get(sys, 0)))]
        )
    system_table = format_table(
        system_rows,
        headers=["System", "Side", "Count"],
        max_width=80,
    )

    # Build round-robin picks per side
    long_rows: dict[str, list[dict]] = {}
    short_rows: dict[str, list[dict]] = {}
    if "system" in df_norm.columns:
        for sys in _LONG_SYSTEMS:
            sys_df = df_norm[df_norm["system"] == sys]
            if not sys_df.empty:
                sorted_df = _sort_signals_frame(sys_df)
                long_rows[sys] = sorted_df.to_dict(orient="records")
        for sys in _SHORT_SYSTEMS:
            sys_df = df_norm[df_norm["system"] == sys]
            if not sys_df.empty:
                sorted_df = _sort_signals_frame(sys_df)
                short_rows[sys] = sorted_df.to_dict(orient="records")

    long_picks = _round_robin_picks(long_rows, _LONG_SYSTEMS, long_slots)
    short_picks = _round_robin_picks(short_rows, _SHORT_SYSTEMS, short_slots)
    focus_rows: list[list[str]] = []
    for p in long_picks:
        row = pd.Series(p)
        focus_rows.append(
            [
                "Long",
                str(row.get("symbol", "")).upper(),
                f"{_pick_entry_price(row):.2f}"
                if _pick_entry_price(row) is not None
                else "-",
                _rank_or_score_text(row) or "-",
                _short_system_label(row.get("system", "")),
                _pick_time_exit(row),
            ]
        )
    for p in short_picks:
        row = pd.Series(p)
        focus_rows.append(
            [
                "Short",
                str(row.get("symbol", "")).upper(),
                f"{_pick_entry_price(row):.2f}"
                if _pick_entry_price(row) is not None
                else "-",
                _rank_or_score_text(row) or "-",
                _short_system_label(row.get("system", "")),
                _pick_time_exit(row),
            ]
        )
    if not focus_rows:
        focus_rows.append(["-", "-", "-", "-", "-", "-"])
    focus_table = format_table(
        focus_rows,
        headers=["Side", "Symbol", "Entry", "Rank/Score", "System", "Exit"],
        max_width=140,
    )

    entry_symbols: list[str] = []
    for p in long_picks:
        row = pd.Series(p)
        sym = str(row.get("symbol", "")).upper()
        if sym:
            entry_symbols.append(f"L:{sym}")
    for p in short_picks:
        row = pd.Series(p)
        sym = str(row.get("symbol", "")).upper()
        if sym:
            entry_symbols.append(f"S:{sym}")

    exit_symbols: list[str] = []
    try:
        exit_df = build_exit_signals_from_tracker()
        if exit_df is not None and not getattr(exit_df, "empty", True):
            exit_symbols = [
                str(sym).upper()
                for sym in exit_df.get("symbol", pd.Series(dtype=str)).tolist()
                if str(sym).strip()
            ]
    except Exception:
        exit_symbols = []

    hold_items: list[str] = []
    try:
        tracker = load_tracker()
    except Exception:
        tracker = {}
    hold_count = len(tracker)
    if tracker:
        try:
            cache_manager = CacheManager(get_settings(create_dirs=False))
        except Exception:
            cache_manager = None
        max_hold_display = 20
        for i, (sym, info) in enumerate(sorted(tracker.items())):
            if i >= max_hold_display:
                remaining = max(0, len(tracker) - max_hold_display)
                if remaining:
                    hold_items.append(f"…他{remaining}件")
                break
            symbol = str(sym).upper()
            if not symbol:
                continue
            entry_price = _safe_float(info.get("entry_price"))
            qty = _safe_float(info.get("qty"))
            side = _resolve_side(info, info.get("system"))
            current_price = (
                _latest_close(cache_manager, symbol)
                if cache_manager is not None
                else None
            )
            pnl_text = _format_unrealized(entry_price, current_price, qty, side)
            if not pnl_text:
                pnl_text = "?"
            hold_items.append(f"{symbol} {pnl_text}")

    entry_count = len(entry_symbols)
    exit_count = len(exit_symbols)
    entry_text = ", ".join(entry_symbols) if entry_symbols else "なし"
    exit_text = ", ".join(exit_symbols) if exit_symbols else "なし"
    hold_text = ", ".join(hold_items) if hold_items else "なし"

    action_lines = [
        f"本日のやること: エントリー{entry_count}件、エグジット{exit_count}件、ホールド中{hold_count}銘柄。今日はこれを実行。",
        f"- エントリー: {entry_text}",
        f"- エグジット: {exit_text}",
        f"- ホールド: {hold_text}",
    ]

    parts = [
        "\n".join(action_lines),
        "Summary",
        summary_table,
        "By System",
        system_table,
        "Focus Picks",
        focus_table,
    ]
    return "\n".join(parts), total




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
        _send_exit_radar_notifications()
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
        _send_exit_radar_notifications()


def _send_entry_notifications(df: pd.DataFrame) -> None:
    """Send entry notifications using Slack (fallback to Discord)."""
    def _emit_with_notifier(notifier) -> None:
        summary_text, total = _build_summary_lines(df)
        if summary_text:
            notifier.send_signals(
                "Daily Summary",
                [],
                display_count=total,
                table=summary_text,
                discord_kind="summary",
            )
        if "system" in df.columns:
            groups = df.groupby("system")
        else:
            groups = [("integrated", df)]
        for sys_name, g in groups:
            sorted_df = _sort_signals_frame(g)
            raw_symbols = sorted_df["symbol"].astype(str).tolist()
            symbols: list[str] = []
            sent_table = False
            max_positions = 10
            try:
                settings = get_settings(create_dirs=False)
                max_positions = int(getattr(settings.risk, "max_positions", 10) or 10)
            except Exception:
                max_positions = 10
            focus_n = min(len(sorted_df), max(1, min(3, max_positions)))
            table_rows: list[list[str]] = []
            for idx, row in sorted_df.iterrows():
                sym = str(row.get("symbol", ""))
                if not sym:
                    continue
                rank_label = f"★{idx + 1}" if idx < focus_n else str(idx + 1)
                entry_price = _pick_entry_price(row)
                stop_price = _safe_float(row.get("stop_price"))
                plan = format_exit_plan_from_signal_row(row)
                if plan.startswith("stop "):
                    plan = " / ".join(plan.split(" / ")[1:]).strip()
                plan = plan or "-"
                score_text = _rank_or_score_text(row) or "-"
                table_rows.append(
                    [
                        rank_label,
                        sym.upper(),
                        f"{entry_price:.2f}" if entry_price is not None else "-",
                        f"{stop_price:.2f}" if stop_price is not None else "-",
                        plan,
                        score_text,
                    ]
                )
            if table_rows:
                table_text = format_table(
                    table_rows,
                    headers=["#", "Symbol", "Entry", "Stop", "Exit", "Score"],
                    max_width=120,
                )
                sys_kind = str(sys_name).lower()
                notifier.send_signals(
                    str(sys_name),
                    [],
                    display_count=len(sorted_df),
                    table=table_text,
                    discord_kind=sys_kind if sys_kind.startswith("system") else "signals",
                )
                sent_table = True
            else:
                for idx, row in sorted_df.iterrows():
                    sym = str(row.get("symbol", ""))
                    if not sym:
                        continue
                    line = _format_detail_line(row, highlight=idx < focus_n)
                    symbols.append(line)
            if not symbols:
                symbols = raw_symbols
            if symbols and not sent_table:
                sys_kind = str(sys_name).lower()
                notifier.send_signals(
                    str(sys_name),
                    symbols,
                    display_count=len(sorted_df),
                    discord_kind=sys_kind if sys_kind.startswith("system") else "signals",
                )
            chart_paths: list[str] = []
            for sym in raw_symbols:
                try:
                    row = sorted_df[sorted_df["symbol"] == sym].iloc[0]
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
                        send_with_mention = getattr(notifier, "send_with_mention", None)
                        if callable(send_with_mention):
                            msg = "\n".join(symbols)
                            sys_kind = str(sys_name).lower()
                            send_with_mention(
                                "📈 日足チャート",
                                msg,
                                mention=False,
                                image_path=combined,
                                discord_kind=sys_kind
                                if sys_kind.startswith("system")
                                else "signals",
                            )
                        else:
                            notifier.send_signals("charts", ["\n".join(symbols)])
                except Exception:
                    logging.exception("failed to send combined chart")

    env = get_env_config()
    platform = str(getattr(env, "signals_platform", "slack") or "slack").lower()
    if platform in {"discord"}:
        primary = create_notifier(platform="discord", fallback=False)
        try:
            _emit_with_notifier(primary)
            return
        except Exception:
            logging.exception("discord通知に失敗。Slackへフォールバックします。")
            fallback = create_notifier(platform="auto", fallback=True)
            _emit_with_notifier(fallback)
            return
    if platform in {"both", "broadcast", "all"}:
        n = create_notifier(platform="auto", broadcast=True, fallback=True)
    else:
        n = create_notifier(platform="auto", fallback=True)
    try:
        _emit_with_notifier(n)
    except Exception:
        logging.exception("signal notification failed (slack+discord)")


def send_signal_notification(
    df: pd.DataFrame, *, signals_dir: Path | None = None, date_str: str | None = None
) -> None:
    """Send a brief notification for the given signals DataFrame."""
    if df is None or df.empty:
        return
    _apply_confirmation_files(date_str)
    logging.info("Today signals: %d picks", len(df))
    _send_entry_notifications(df)
    try:
        env = get_env_config()
        auto_update = bool(getattr(env, "position_tracker_auto_update", True))
    except Exception:
        auto_update = True
    if auto_update:
        try:
            update_positions_from_signals(df)
        except Exception:
            logging.exception("position tracker update failed")
    else:
        logging.info("position tracker auto-update disabled (POSITION_TRACKER_AUTO_UPDATE=0)")
    try:
        _send_exit_notifications(signals_dir, date_str)
    except Exception:
        logging.exception("exit signal notification failed")
    try:
        _send_exit_radar_notifications()
    except Exception:
        logging.exception("risk notification failed")


def _apply_confirmation_files(date_str: str | None) -> None:
    try:
        env = get_env_config()
        auto_apply = bool(getattr(env, "position_tracker_confirm_auto_apply", True))
        base_dir = Path(
            getattr(env, "position_tracker_confirm_dir", "data") or "data"
        )
    except Exception:
        auto_apply = True
        base_dir = Path("data")
    if not auto_apply:
        return
    if date_str is None:
        date_str = datetime.today().strftime("%Y-%m-%d")
    entry_path = base_dir / f"entry_confirmations_{date_str}.csv"
    exit_path = base_dir / f"exit_confirmations_{date_str}.csv"

    if entry_path.exists():
        try:
            entry_df = pd.read_csv(entry_path)
            cols = {c.lower(): c for c in entry_df.columns}
            required = {"symbol", "system", "entry_date", "entry_price"}
            if not required.issubset(cols.keys()):
                missing = ", ".join(sorted(required - set(cols.keys())))
                logging.warning(
                    "entry_confirmations missing columns: %s (%s)", missing, entry_path
                )
            else:
                update_positions_from_signals(entry_df)
                logging.info("applied entry confirmations: %s", entry_path)
        except Exception:
            logging.exception("failed to apply entry confirmations: %s", entry_path)

    if exit_path.exists():
        try:
            exit_df = pd.read_csv(exit_path)
            cols = {c.lower(): c for c in exit_df.columns}
            sym_col = cols.get("symbol") or cols.get("ticker")
            if not sym_col:
                logging.warning(
                    "exit_confirmations missing column symbol/ticker (%s)", exit_path
                )
            else:
                symbols = [
                    str(sym).upper()
                    for sym in exit_df[sym_col].tolist()
                    if str(sym).strip()
                ]
                if symbols:
                    remove_positions(symbols)
                logging.info("applied exit confirmations: %s", exit_path)
        except Exception:
            logging.exception("failed to apply exit confirmations: %s", exit_path)


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

    def _emit_with_notifier(notifier) -> None:
        rows: list[list[str]] = []
        for _, row in exit_df.iterrows():
            symbol = str(row.get("symbol", "")).upper()
            if not symbol:
                continue
            system = str(row.get("system", "")).lower() or "-"
            side = str(row.get("side", "")).lower()
            action = "SELL" if side == "long" else ("BUY" if side == "short" else "-")
            reason = str(row.get("exit_reason", "")) or "-"
            when = str(row.get("when", "")) or "-"
            exit_price = _safe_float(row.get("exit_price"))
            price_text = f"{exit_price:.2f}" if exit_price is not None else "-"
            rows.append([symbol, action, system, reason, price_text, when])

        title = f"Exit Signals ・ {now_jst_str()}"
        summary = f"エグジット件数: {len(rows)}"
        table = (
            format_table(
                rows,
                headers=["Symbol", "Action", "System", "Reason", "Price", "When"],
                max_width=120,
            )
            if rows
            else ""
        )
        message = summary + (f"\n{table}" if table else "")
        ch = os.getenv("SLACK_CHANNEL_SIGNALS") if notifier.platform == "slack" else None
        notifier.send(title, message, channel=ch, discord_kind="summary")

    env = get_env_config()
    platform = str(getattr(env, "signals_platform", "slack") or "slack").lower()
    if platform in {"discord"}:
        primary = create_notifier(platform="discord", fallback=False)
        try:
            _emit_with_notifier(primary)
            return
        except Exception:
            logging.exception("discord通知に失敗。Slackへフォールバックします。")
            fallback = create_notifier(platform="auto", fallback=True)
            _emit_with_notifier(fallback)
            return
    if platform in {"both", "broadcast", "all"}:
        n = create_notifier(platform="auto", broadcast=True, fallback=True)
    else:
        n = create_notifier(platform="auto", fallback=True)
    try:
        _emit_with_notifier(n)
    except Exception:
        logging.exception("exit signal notification failed (slack+discord)")


def _send_exit_radar_notifications() -> None:
    slack_ch = os.getenv("SLACK_CHANNEL_EXIT_RADAR", "").strip()
    discord_url = os.getenv("DISCORD_WEBHOOK_URL_EXIT_RADAR", "").strip()
    if not slack_ch and not discord_url:
        logging.info("exit radar notification skipped (channel/webhook not set)")
        return

    try:
        tracker = load_tracker()
    except Exception:
        tracker = {}
    if not tracker:
        logging.info("exit radar notification skipped (no positions)")
        return

    try:
        cache_manager = CacheManager(get_settings(create_dirs=False))
    except Exception:
        cache_manager = None

    rows: list[list[str]] = []
    for sym, info in sorted(tracker.items()):
        symbol = str(sym).upper()
        if not symbol:
            continue
        system = str(info.get("system", "")).lower()
        side = _resolve_side(info, system)
        entry_price = _safe_float(info.get("entry_price"))
        qty = _safe_float(info.get("qty"))
        entry_date = _safe_date(info.get("entry_date"))
        max_exit_date = _safe_date(info.get("max_exit_date"))
        max_holding_days = _safe_int(info.get("max_holding_days"))

        rules = SYSTEM_TRADE_RULES.get(system) if system else None
        stop_price = _compute_stop_price(info, entry_price, side, system)
        target_price = _compute_target_price(info, entry_price, side, system)

        use_trailing_stop = _safe_bool(info.get("use_trailing_stop"))
        trailing_pct = _safe_float(info.get("trailing_stop_pct"))
        if use_trailing_stop is False:
            trailing_pct = None
        if trailing_pct is None and rules and rules.use_trailing_stop:
            try:
                trailing_pct = float(rules.trailing_stop_pct or 0.0)
            except Exception:
                trailing_pct = None

        if max_exit_date is not None:
            due, when = decide_exit_schedule(system, max_exit_date, max_exit_date)
            exit_date_str = max_exit_date.strftime("%Y-%m-%d")
            exit_when = when if when else ("today_close" if due else "")
        else:
            exit_date_str, exit_when = compute_time_exit_date(
                system, entry_date, max_holding_days
            )

        exit_text = _format_time_exit_text(exit_date_str, exit_when)
        price_df_raw = None
        if cache_manager is not None:
            try:
                price_df_raw = cache_manager.read(symbol, "rolling")
            except Exception:
                price_df_raw = None
            if price_df_raw is None or getattr(price_df_raw, "empty", True):
                try:
                    price_df_raw = cache_manager.read(symbol, "base")
                except Exception:
                    price_df_raw = price_df_raw
        price_df = _normalize_price_df(price_df_raw) if price_df_raw is not None else None
        current_price = None
        if price_df is not None:
            series = _pick_series(price_df, ["Close", "close", "CLOSE"])
            try:
                if series is not None and not getattr(series, "empty", True):
                    val = series.iloc[-1]
                    if pd.isna(val):
                        val = series.dropna().iloc[-1]
                    current_price = float(val)
            except Exception:
                current_price = None
        if current_price is None and cache_manager is not None:
            current_price = _latest_close(cache_manager, symbol)

        trailing_stop_price = _compute_trailing_stop_price(
            price_df, entry_date, side, trailing_pct
        )

        stop_text = f"{stop_price:.2f}" if stop_price is not None else "-"
        target_text = f"{target_price:.2f}" if target_price is not None else "-"
        trail_text = (
            f"{trailing_stop_price:.2f}" if trailing_stop_price is not None else "-"
        )

        exit_price_text = "-"
        if target_price is not None:
            exit_price_text = f"target {target_price:.2f}"
        elif trailing_stop_price is not None:
            exit_price_text = f"trail {trailing_stop_price:.2f}"
        elif stop_price is not None:
            exit_price_text = f"stop {stop_price:.2f}"
        elif exit_text and exit_text != "-":
            exit_price_text = "close"

        pnl_text = _format_unrealized(entry_price, current_price, qty, side)
        rows.append(
            [
                symbol,
                side or "-",
                f"{entry_price:.2f}" if entry_price is not None else "-",
                stop_text,
                target_text,
                trail_text,
                exit_text,
                exit_price_text,
                pnl_text or "-",
            ]
        )

    if not rows:
        logging.info("exit radar notification skipped (no valid rows)")
        return

    title = f"Exit Radar ・ {now_jst_str()}"
    summary = f"保有ポジション: {len(rows)}"
    table = format_table(
        rows,
        headers=["Symbol", "Side", "Entry", "Stop", "Target", "Trail", "TimeExit", "ExitPx", "PnL"],
        max_width=180,
    )
    message = summary + (f"\n{table}" if table else "")

    if slack_ch:
        try:
            Notifier(platform="slack").send(
                title, message, channel=slack_ch, discord_kind="exit_radar"
            )
        except Exception:
            logging.exception("exit radar notification failed (slack)")
    if discord_url:
        try:
            Notifier(platform="discord", webhook_url=discord_url).send(
                title, message, discord_kind="exit_radar"
            )
        except Exception:
            logging.exception("exit radar notification failed (discord)")


__all__ = ["notify_signals", "send_signal_notification"]
