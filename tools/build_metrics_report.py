"""Build a validation report linking daily metrics and per-system signals.

This script joins ``results_csv/daily_metrics.csv`` with signal CSVs in
``outputs.signals_dir`` for the latest day, and saves a compact report to
``results_csv/daily_metrics_report.csv``. It includes per-system counts and
the first few symbols as a spot check.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pandas as pd

from common.cache_format import round_dataframe
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


def _read_metrics() -> pd.DataFrame:
    try:
        settings = get_settings(create_dirs=True)
        fp = Path(settings.outputs.results_csv_dir) / "daily_metrics.csv"
    except Exception:
        fp = Path("results_csv") / "daily_metrics.csv"
    if not fp.exists():
        return pd.DataFrame()
    try:
        df = pd.read_csv(fp)
    except Exception:
        logging.exception("failed to read metrics: %s", fp)
        return pd.DataFrame()
    if "date" in df.columns:
        try:
            df["date"] = pd.to_datetime(df["date"]).dt.date
        except Exception:
            pass
    return df


def _collect_signals_for_day(
    day_str: str, *, signals_dir: Path | None = None
) -> dict[str, pd.DataFrame]:
    systems: dict[str, pd.DataFrame] = {}
    if signals_dir is None:
        settings = get_settings(create_dirs=True)
        sig_dir = Path(settings.outputs.signals_dir)
    else:
        sig_dir = signals_dir
    if not sig_dir.exists():
        return systems
    for p in sig_dir.glob(f"signals_system*_{day_str}.csv"):
        try:
            df = pd.read_csv(p)
            # normalize system name from filename
            name = p.stem.replace("signals_", "").replace(f"_{day_str}", "")
            systems[name] = df
        except Exception:
            logging.exception("failed to read signal file: %s", p)
    return systems


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


def _collect_final_counts(day_str: str, signals_dir: Path) -> dict[str, int]:
    final_counts: dict[str, int] = {}
    path = signals_dir / f"signals_final_{day_str}.csv"
    if not path.exists():
        return final_counts
    try:
        df = pd.read_csv(path)
    except Exception:
        logging.exception("failed to read final file: %s", path)
        return final_counts
    if df is None or df.empty or "system" not in df.columns:
        return final_counts
    try:
        final_counts = (
            df["system"]
            .astype(str)
            .str.strip()
            .str.lower()
            .value_counts()
            .to_dict()
        )
        final_counts = {str(k): int(v) for k, v in final_counts.items()}
    except Exception:
        final_counts = {}
    return final_counts


def build_metrics_report() -> Path | None:
    metrics = _read_metrics()
    if metrics.empty:
        logging.info("no metrics; skip report")
        return None
    try:
        days = sorted(metrics["date"].dropna().unique())
        last_day = days[-1]
        prev_day = days[-2] if len(days) > 1 else None
    except Exception:
        return None
    day_str = str(last_day)
    prev_str = str(prev_day) if prev_day is not None else None
    settings = get_settings(create_dirs=True)
    sig_dir = Path(settings.outputs.signals_dir)
    per_sys = _collect_signals_for_day(day_str, signals_dir=sig_dir)
    prev_sys = (
        _collect_signals_for_day(prev_str, signals_dir=sig_dir)
        if prev_str
        else {}
    )
    exit_counts = _collect_exit_counts(day_str, sig_dir)
    prev_exit_counts = (
        _collect_exit_counts(prev_str, sig_dir) if prev_str else {}
    )
    final_counts = _collect_final_counts(day_str, sig_dir)
    prev_final_counts = _collect_final_counts(prev_str, sig_dir) if prev_str else {}

    curr_map = {
        str(r.get("system", "")).strip().lower(): r
        for _, r in metrics[metrics["date"] == last_day].iterrows()
    }
    prev_map = {}
    if prev_day is not None:
        prev_map = {
            str(r.get("system", "")).strip().lower(): r
            for _, r in metrics[metrics["date"] == prev_day].iterrows()
        }

    rows: list[dict] = []
    for sys_key, r in curr_map.items():
        sys_name = str(r.get("system"))
        pre = _optional_int(r.get("prefilter_pass", 0)) or 0
        setup = _optional_int(r.get("setup_pass", None))
        cand = _optional_int(r.get("candidates", 0)) or 0
        ent = _optional_int(r.get("entries", None))
        if ent is None:
            ent = _optional_int(final_counts.get(sys_key))
        sig_df = per_sys.get(sys_name)
        sig_count = int(len(sig_df)) if sig_df is not None else None
        syms = []
        try:
            if sig_df is not None and not sig_df.empty and "symbol" in sig_df.columns:
                syms = sig_df["symbol"].astype(str).head(10).tolist()
        except Exception:
            pass
        prev_row = prev_map.get(sys_key)
        pre_prev = _optional_int(prev_row.get("prefilter_pass")) if prev_row is not None else None
        setup_prev = _optional_int(prev_row.get("setup_pass")) if prev_row is not None else None
        cand_prev = _optional_int(prev_row.get("candidates")) if prev_row is not None else None
        ent_prev = _optional_int(prev_row.get("entries")) if prev_row is not None else None
        if ent_prev is None:
            ent_prev = _optional_int(prev_final_counts.get(sys_key))
        prev_sig_df = prev_sys.get(sys_name)
        sig_prev = int(len(prev_sig_df)) if prev_sig_df is not None else None
        exit_now = _optional_int(exit_counts.get(sys_key))
        exit_prev = _optional_int(prev_exit_counts.get(sys_key))
        rows.append(
            {
                "date": day_str,
                "system": sys_name,
                "prefilter_pass": pre,
                "setup_pass": setup,
                "candidates": cand,
                "entries": ent,
                "signals_count": sig_count,
                "exits": exit_now,
                "prefilter_delta": _delta(pre, pre_prev),
                "setup_delta": _delta(setup, setup_prev),
                "candidates_delta": _delta(cand, cand_prev),
                "entries_delta": _delta(ent, ent_prev),
                "signals_delta": _delta(sig_count, sig_prev),
                "exits_delta": _delta(exit_now, exit_prev),
                "signals_file": f"signals_{sys_name}_{day_str}.csv",
                "symbols_sample": ", ".join(syms),
            }
        )

    if rows:
        total_row: dict[str, object] = {"date": day_str, "system": "TOTAL"}
        for key in [
            "prefilter_pass",
            "setup_pass",
            "candidates",
            "entries",
            "signals_count",
            "exits",
        ]:
            vals = [r.get(key) for r in rows if r.get(key) is not None]
            try:
                total_row[key] = int(sum(int(v) for v in vals)) if vals else None
            except Exception:
                total_row[key] = None
        for key in [
            "prefilter_delta",
            "setup_delta",
            "candidates_delta",
            "entries_delta",
            "signals_delta",
            "exits_delta",
        ]:
            vals = [r.get(key) for r in rows if r.get(key) is not None]
            try:
                total_row[key] = int(sum(int(v) for v in vals)) if vals else None
            except Exception:
                total_row[key] = None
        total_row["signals_file"] = f"signals_final_{day_str}.csv"
        total_row["symbols_sample"] = ""
        rows.append(total_row)
    out_df = pd.DataFrame(rows)
    try:
        settings = get_settings(create_dirs=True)
        out_dir = Path(settings.outputs.results_csv_dir)
    except Exception:
        out_dir = Path("results_csv")
    out_dir.mkdir(parents=True, exist_ok=True)
    out_fp = out_dir / "daily_metrics_report.csv"
    try:
        settings = get_settings(create_dirs=True)
        round_dec = getattr(settings.cache, "round_decimals", None)
    except Exception:
        round_dec = None
    try:
        out_write = round_dataframe(out_df, round_dec)
    except Exception:
        out_write = out_df
    out_write.to_csv(out_fp, index=False)
    logging.info("metrics report saved: %s", out_fp)
    return out_fp


if __name__ == "__main__":
    build_metrics_report()
