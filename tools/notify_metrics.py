"""Notify daily metrics summary.

Reads ``results_csv/daily_metrics.csv`` and sends a compact summary for the
latest available date via ``common.notifier.Notifier``. Safe to run even when
no webhook is configured (logs only).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import logging
from pathlib import Path
from typing import Any

import pandas as pd

from config.settings import get_settings


def _optional_int(value: object | None) -> int | None:
    if value is None:
        return None
    try:
        if isinstance(value, float) and pd.isna(value):
            return None
    except Exception:
        pass
    try:
        return int(value)  # type: ignore[arg-type]
    except Exception:
        return None


def _delta(curr: int | None, prev: int | None) -> int | None:
    if curr is None or prev is None:
        return None
    try:
        return int(curr) - int(prev)
    except Exception:
        return None


def _fmt_delta(delta: int | None) -> str:
    if delta is None:
        return ""
    sign = "+" if delta > 0 else ""
    return f"{sign}{delta}"


def _fmt_delta_paren(delta: int | None) -> str:
    if delta is None:
        return ""
    return f" ({_fmt_delta(delta)})"


def _load_latest_metrics() -> (
    tuple[pd.DataFrame, str, pd.DataFrame | None, str | None] | tuple[None, None, None, None]
):
    try:
        settings = get_settings(create_dirs=True)
        fp = Path(settings.outputs.results_csv_dir) / "daily_metrics.csv"
    except Exception:
        fp = Path("results_csv") / "daily_metrics.csv"
    if not fp.exists():
        logging.info("metrics CSV not found: %s", fp)
        return None, None, None, None
    try:
        df = pd.read_csv(fp)
    except Exception:
        logging.exception("failed to read metrics: %s", fp)
        return None, None, None, None
    if df is None or df.empty or "date" not in df.columns:
        return None, None, None, None
    try:
        df["date"] = pd.to_datetime(df["date"]).dt.date
    except Exception:
        pass
    try:
        days = sorted(df["date"].dropna().unique())
        last_date = days[-1]
        prev_date = days[-2] if len(days) > 1 else None
    except Exception:
        return None, None, None, None
    day_df = df[df["date"] == last_date].copy()
    prev_df = df[df["date"] == prev_date].copy() if prev_date is not None else None
    return day_df, str(last_date), prev_df, (str(prev_date) if prev_date is not None else None)


def _collect_exit_counts(day_str: str, signals_dir: Path) -> dict[str, int]:
    exit_counts: dict[str, int] = {}
    path = signals_dir / f"signals_exit_{day_str}.csv"
    if not path.exists():
        return exit_counts
    try:
        df = pd.read_csv(path)
    except Exception:
        logging.exception("failed to read exit file: %s", path)
        return exit_counts
    if df is None or df.empty:
        return exit_counts
    if "system" not in df.columns:
        exit_counts["total"] = int(len(df))
        return exit_counts
    try:
        exit_counts = (
            df["system"]
            .astype(str)
            .str.strip()
            .str.lower()
            .value_counts()
            .to_dict()
        )
        exit_counts = {str(k): int(v) for k, v in exit_counts.items()}
    except Exception:
        exit_counts = {}
    return exit_counts


def _collect_entry_counts(day_str: str, signals_dir: Path) -> dict[str, int]:
    entry_counts: dict[str, int] = {}
    path = signals_dir / f"signals_final_{day_str}.csv"
    if not path.exists():
        return entry_counts
    try:
        df = pd.read_csv(path)
    except Exception:
        logging.exception("failed to read final file: %s", path)
        return entry_counts
    if df is None or df.empty or "system" not in df.columns:
        return entry_counts
    try:
        entry_counts = (
            df["system"]
            .astype(str)
            .str.strip()
            .str.lower()
            .value_counts()
            .to_dict()
        )
        entry_counts = {str(k): int(v) for k, v in entry_counts.items()}
    except Exception:
        entry_counts = {}
    return entry_counts


def _load_signal_df(path: Path) -> pd.DataFrame | None:
    if not path.exists():
        return None
    try:
        df = pd.read_csv(path)
    except Exception:
        logging.exception("failed to read signals: %s", path)
        return None
    if df is None or df.empty:
        return None
    return df


def _sort_signals_df(df: pd.DataFrame | None, *, score_desc: bool = True) -> pd.DataFrame | None:
    if df is None or df.empty:
        return df
    out = df.copy()
    if "system" in out.columns:
        try:
            out["_sys_no"] = (
                out["system"]
                .astype(str)
                .str.extract(r"(\\d+)", expand=False)
                .fillna("0")
                .astype(int)
            )
        except Exception:
            out["_sys_no"] = 0
    else:
        out["_sys_no"] = 0
    sort_cols = ["_sys_no"]
    ascending = [True]
    if score_desc and "score" in out.columns:
        sort_cols.append("score")
        ascending.append(False)
    elif "symbol" in out.columns:
        sort_cols.append("symbol")
        ascending.append(True)
    try:
        out = out.sort_values(sort_cols, ascending=ascending, kind="stable")
    except Exception:
        pass
    return out


def _system_order(df: pd.DataFrame | None) -> list[str]:
    if df is None or df.empty or "system" not in df.columns:
        return []
    systems = (
        df["system"].astype(str).str.strip().str.lower().dropna().unique().tolist()
    )
    def _key(name: str) -> int:
        try:
            return int("".join(ch for ch in name if ch.isdigit()) or 0)
        except Exception:
            return 0
    return sorted(systems, key=_key)


def _format_entry_lines(df: pd.DataFrame | None, limit: int = 50) -> list[str]:
    if df is None or df.empty:
        return ["(なし)"]
    df_sorted = _sort_signals_df(df, score_desc=True)
    lines: list[str] = []
    try:
        item_count = 0
        if df_sorted is not None and "system" in df_sorted.columns:
            for sys_name in _system_order(df_sorted):
                group_df = df_sorted[
                    df_sorted["system"].astype(str).str.lower() == sys_name
                ]
                if group_df.empty:
                    continue
                lines.append(f"[{sys_name}]")
                for _, row in group_df.iterrows():
                    sym = str(row.get("symbol", "")).upper()
                    system = str(row.get("system", "")).lower()
                    side = str(row.get("side", "")).lower()
                    price = row.get("entry_price")
                    price_txt = ""
                    try:
                        if price not in (None, ""):
                            price_txt = f" @ {float(price):.2f}"
                    except Exception:
                        price_txt = ""
                    suffix = f" ({system} {side})" if system or side else ""
                    lines.append(f"{sym}{suffix}{price_txt}".strip())
                    item_count += 1
                    if item_count >= limit:
                        break
                if item_count >= limit:
                    break
        else:
            for _, row in df_sorted.iterrows():
                sym = str(row.get("symbol", "")).upper()
                system = str(row.get("system", "")).lower()
                side = str(row.get("side", "")).lower()
                price = row.get("entry_price")
                price_txt = ""
                try:
                    if price not in (None, ""):
                        price_txt = f" @ {float(price):.2f}"
                except Exception:
                    price_txt = ""
                suffix = f" ({system} {side})" if system or side else ""
                lines.append(f"{sym}{suffix}{price_txt}".strip())
                item_count += 1
                if item_count >= limit:
                    break
    except Exception:
        return ["(なし)"]
    if df is not None and len(df) > limit:
        lines.append(f"... ほか{len(df) - limit}件")
    return lines or ["(なし)"]


def _format_exit_lines(df: pd.DataFrame | None, limit: int = 50) -> list[str]:
    if df is None or df.empty:
        return ["(なし)"]
    df_sorted = _sort_signals_df(df, score_desc=False)
    lines: list[str] = []
    try:
        item_count = 0
        if df_sorted is not None and "system" in df_sorted.columns:
            for sys_name in _system_order(df_sorted):
                group_df = df_sorted[
                    df_sorted["system"].astype(str).str.lower() == sys_name
                ]
                if group_df.empty:
                    continue
                lines.append(f"[{sys_name}]")
                for _, row in group_df.iterrows():
                    sym = str(row.get("symbol", "")).upper()
                    system = str(row.get("system", "")).lower()
                    side = str(row.get("side", "")).lower()
                    reason = str(row.get("exit_reason", "")).strip()
                    price = row.get("exit_price")
                    price_txt = ""
                    try:
                        if price not in (None, ""):
                            price_txt = f" @ {float(price):.2f}"
                    except Exception:
                        price_txt = ""
                    suffix = f" ({system} {side})" if system or side else ""
                    reason_txt = f" {reason}" if reason else ""
                    lines.append(f"{sym}{suffix}{reason_txt}{price_txt}".strip())
                    item_count += 1
                    if item_count >= limit:
                        break
                if item_count >= limit:
                    break
        else:
            for _, row in df_sorted.iterrows():
                sym = str(row.get("symbol", "")).upper()
                system = str(row.get("system", "")).lower()
                side = str(row.get("side", "")).lower()
                reason = str(row.get("exit_reason", "")).strip()
                price = row.get("exit_price")
                price_txt = ""
                try:
                    if price not in (None, ""):
                        price_txt = f" @ {float(price):.2f}"
                except Exception:
                    price_txt = ""
                suffix = f" ({system} {side})" if system or side else ""
                reason_txt = f" {reason}" if reason else ""
                lines.append(f"{sym}{suffix}{reason_txt}{price_txt}".strip())
                item_count += 1
                if item_count >= limit:
                    break
    except Exception:
        return ["(なし)"]
    if df is not None and len(df) > limit:
        lines.append(f"... ほか{len(df) - limit}件")
    return lines or ["(なし)"]


def send_metrics_notification(
    *,
    day_str: str | None,
    fields: Sequence[Mapping[str, Any]] | None = None,
    summary_pairs: Sequence[tuple[Any, Any]] | None = None,
    extra_lines: Sequence[str] | None = None,
    title: str = "\U0001f4c8 本日のメトリクス（system別）",
) -> None:
    """Send a metrics summary via the default notifier.

    Parameters
    ----------
    day_str:
        Target day label (e.g. ``"2024-05-01"``). ``None`` becomes an empty label.
    fields:
        Rich embed fields for Slack/Discord notifications.
    summary_pairs:
        Key/value pairs included in the message body (``key: value`` each line).
    extra_lines:
        Additional free-form lines appended to the body (e.g. code blocks).
    title:
        Notification title. Emoji default matches existing notifications.
    """

    body_lines: list[str] = []
    if day_str is not None:
        body_lines.append(f"対象日: {day_str}")
    elif summary_pairs or extra_lines:
        body_lines.append("対象日: ")

    if summary_pairs:
        for key, value in summary_pairs:
            body_lines.append(f"{key}: {value}")

    if extra_lines:
        body_lines.extend(str(line) for line in extra_lines if str(line).strip())

    if not body_lines:
        body_lines.append("対象日: -")

    msg = "\n".join(body_lines)

    try:
        from common.notifier import create_notifier
    except Exception:
        logging.info("metrics notified (log only)")
        return

    try:
        notifier = create_notifier(platform="auto", fallback=True)
        notifier.send(title, msg, fields=list(fields or []))
    except Exception:
        logging.exception("failed to send metrics notification")


def notify_metrics() -> None:
    day_df, day_str, prev_df, prev_str = _load_latest_metrics()
    if day_df is None or day_df.empty:
        logging.info("no metrics to notify")
        return
    try:
        settings = get_settings(create_dirs=True)
        signals_dir = Path(settings.outputs.signals_dir)
    except Exception:
        signals_dir = Path("data_cache") / "signals"

    prev_map = (
        {
            str(r.get("system", "")).strip().lower(): r
            for _, r in prev_df.iterrows()
        }
        if prev_df is not None
        else {}
    )
    entry_counts = _collect_entry_counts(day_str, signals_dir)
    prev_entry_counts = _collect_entry_counts(prev_str, signals_dir) if prev_str else {}
    exit_counts = _collect_exit_counts(day_str, signals_dir)
    prev_exit_counts = _collect_exit_counts(prev_str, signals_dir) if prev_str else {}

    fields: list[dict[str, str]] = []
    lines: list[str] = []
    totals = {"pre": 0, "cand": 0, "ent": 0, "exit": 0}
    totals_prev = {"pre": 0, "cand": 0, "ent": 0, "exit": 0}
    has_entries = False
    has_exits = bool(exit_counts)
    try:
        for _, r in day_df.iterrows():
            sys = str(r.get("system"))
            sys_key = sys.strip().lower()
            pre = _optional_int(r.get("prefilter_pass", 0)) or 0
            cand = _optional_int(r.get("candidates", 0)) or 0
            ent = _optional_int(r.get("entries", None))
            if ent is None:
                ent = _optional_int(entry_counts.get(sys_key))
            if ent is not None:
                has_entries = True
            exit_cnt = _optional_int(exit_counts.get(sys_key))
            if exit_cnt is not None:
                has_exits = True

            prev_row = prev_map.get(sys_key)
            pre_prev = (
                _optional_int(prev_row.get("prefilter_pass"))
                if prev_row is not None
                else None
            )
            cand_prev = (
                _optional_int(prev_row.get("candidates")) if prev_row is not None else None
            )
            ent_prev = (
                _optional_int(prev_row.get("entries")) if prev_row is not None else None
            )
            if ent_prev is None:
                ent_prev = _optional_int(prev_entry_counts.get(sys_key))
            exit_prev = _optional_int(prev_exit_counts.get(sys_key))

            dpre = _delta(pre, pre_prev)
            dcand = _delta(cand, cand_prev)
            dent = _delta(ent, ent_prev)
            dexit = _delta(exit_cnt, exit_prev)

            value = f"pre {pre}{_fmt_delta_paren(dpre)} / cand {cand}{_fmt_delta_paren(dcand)}"
            if ent is not None:
                value += f" / ent {ent}{_fmt_delta_paren(dent)}"
            if exit_cnt is not None:
                value += f" / exit {exit_cnt}{_fmt_delta_paren(dexit)}"

            fields.append({"name": sys, "value": value})

            totals["pre"] += pre
            totals["cand"] += cand
            if ent is not None:
                totals["ent"] += ent
            if exit_cnt is not None:
                totals["exit"] += exit_cnt

            if pre_prev is not None:
                totals_prev["pre"] += pre_prev
            if cand_prev is not None:
                totals_prev["cand"] += cand_prev
            if ent_prev is not None:
                totals_prev["ent"] += ent_prev
            if exit_prev is not None:
                totals_prev["exit"] += exit_prev

            lines.append(
                f"{sys:<7} {pre:>4} {(_fmt_delta(dpre)):>4} {cand:>4} {(_fmt_delta(dcand)):>4}"
                + (
                    f" {ent:>4} {(_fmt_delta(dent)):>4}"
                    if ent is not None
                    else ""
                )
                + (
                    f" {exit_cnt:>4}"
                    if exit_cnt is not None
                    else ""
                )
            )
    except Exception:
        pass
    header = f"{'System':<7} {'pre':>4} {'Δp':>4} {'cand':>4} {'Δc':>4}"
    if has_entries:
        header += f" {'ent':>4} {'Δe':>4}"
    if has_exits:
        header += f" {'exit':>4}"
    table = "\n".join([header] + lines)
    title = "\U0001f4c8 本日のメトリクス（前日差分つき）"

    summary_pairs: list[tuple[str, str]] = []
    if totals["pre"] or totals["cand"]:
        delta_pre = _delta(totals["pre"], totals_prev["pre"]) if prev_str else None
        delta_cand = _delta(totals["cand"], totals_prev["cand"]) if prev_str else None
        summary_pairs.append(
            (
                "合計 pre/cand",
                f"{totals['pre']} / {totals['cand']}"
                + (
                    f" (Δ { _fmt_delta(delta_pre) } / { _fmt_delta(delta_cand) })"
                    if prev_str
                    else ""
                ),
            )
        )
    if has_entries or has_exits:
        delta_ent = _delta(totals["ent"], totals_prev["ent"]) if prev_str else None
        delta_exit = _delta(totals["exit"], totals_prev["exit"]) if prev_str else None
        summary_pairs.append(
            (
                "合計 entry/exit",
                f"{totals['ent']} / {totals['exit']}"
                + (
                    f" (Δ { _fmt_delta(delta_ent) } / { _fmt_delta(delta_exit) })"
                    if prev_str
                    else ""
                ),
            )
        )
    entry_df = _load_signal_df(signals_dir / f"signals_final_{day_str}.csv")
    exit_df = _load_signal_df(signals_dir / f"signals_exit_{day_str}.csv")
    entry_lines = _format_entry_lines(entry_df, limit=50)
    exit_lines = _format_exit_lines(exit_df, limit=50)

    extra_lines: list[str] = [f"```{table}```"]
    extra_lines.append("Entry list:")
    extra_lines.append("```" + "\n".join(entry_lines) + "```")
    extra_lines.append("Exit list:")
    extra_lines.append("```" + "\n".join(exit_lines) + "```")

    send_metrics_notification(
        day_str=day_str,
        fields=fields,
        summary_pairs=summary_pairs,
        extra_lines=extra_lines,
        title=title,
    )


if __name__ == "__main__":
    notify_metrics()
