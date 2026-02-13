from __future__ import annotations

import json
import os
import platform
import subprocess
from pathlib import Path
from typing import Any

import pandas as pd


def _py_round(value: object, places: int) -> float | object:
    try:
        if value is None or pd.isna(value):
            return value
        return round(float(value), int(places))
    except Exception:
        return value


def _normalize_engine_name(engine: str | None) -> str:
    value = (engine or os.getenv("INTEGRATED_BACKTEST_ENGINE", "auto")).strip().lower()
    if value not in {"python", "rust", "auto"}:
        return "auto"
    return value


def resolve_rust_binary(rust_bin: str | None = None) -> Path | None:
    explicit = rust_bin or os.getenv("INTEGRATED_BACKTEST_RUST_BIN", "").strip()
    if explicit:
        path = Path(explicit)
        return path if path.exists() else None

    root = Path(__file__).resolve().parents[1]
    exe_name = "integrated_backtest_core.exe" if platform.system() == "Windows" else "integrated_backtest_core"
    default_path = root / "rust" / "integrated_backtest_core" / "target" / "release" / exe_name
    if default_path.exists():
        return default_path
    return None


def should_use_rust_engine(
    *,
    engine: str | None = None,
    rust_bin: str | None = None,
) -> bool:
    mode = _normalize_engine_name(engine)
    if mode == "python":
        return False
    resolved = resolve_rust_binary(rust_bin=rust_bin)
    if resolved is not None:
        return True
    if mode == "rust":
        raise RuntimeError(
            "Rust engine requested but rust core binary is not available. "
            "Build it with `python tools/build_rust_backtest_core.py` or set "
            "INTEGRATED_BACKTEST_RUST_BIN."
        )
    return False


def run_rust_backtest_core(
    payload: dict[str, Any],
    *,
    engine: str | None = None,
    rust_bin: str | None = None,
    timeout_sec: int = 1800,
    log_callback=None,
) -> pd.DataFrame | None:
    mode = _normalize_engine_name(engine)
    binary = resolve_rust_binary(rust_bin=rust_bin)
    if binary is None:
        if mode == "rust":
            raise RuntimeError(
                "Rust engine requested but rust core binary is not available."
            )
        return None

    try:
        proc = subprocess.run(
            [str(binary)],
            input=json.dumps(payload).encode("utf-8"),
            capture_output=True,
            check=False,
            timeout=max(1, int(timeout_sec)),
        )
    except Exception as exc:
        if mode == "rust":
            raise RuntimeError(f"Rust backtest core execution failed: {exc}") from exc
        if log_callback is not None:
            try:
                log_callback(f"[integrated][rust] execution failed; fallback to python: {exc}")
            except Exception:
                pass
        return None

    if proc.returncode != 0:
        stderr = proc.stderr.decode("utf-8", errors="ignore").strip()
        if mode == "rust":
            raise RuntimeError(
                f"Rust backtest core failed with code {proc.returncode}: {stderr}"
            )
        if log_callback is not None:
            try:
                log_callback(
                    "[integrated][rust] non-zero exit; fallback to python: "
                    f"code={proc.returncode} stderr={stderr}"
                )
            except Exception:
                pass
        return None

    try:
        result = json.loads(proc.stdout.decode("utf-8", errors="ignore"))
    except Exception as exc:
        if mode == "rust":
            raise RuntimeError(f"Rust core output parse failed: {exc}") from exc
        if log_callback is not None:
            try:
                log_callback(
                    f"[integrated][rust] output parse failed; fallback to python: {exc}"
                )
            except Exception:
                pass
        return None

    trades = result.get("trades", [])
    if not isinstance(trades, list):
        if mode == "rust":
            raise RuntimeError("Rust core output is invalid: `trades` is not a list.")
        return None

    trades_df = pd.DataFrame(trades)
    if trades_df.empty:
        return trades_df

    for col in ("entry_date", "exit_date"):
        if col in trades_df.columns:
            trades_df[col] = pd.to_datetime(trades_df[col], errors="coerce")
    for col, places in (
        ("entry_price", 2),
        ("exit_price", 2),
        ("pnl", 2),
        ("return_%", 4),
    ):
        if col in trades_df.columns:
            trades_df[col] = trades_df[col].map(lambda v, p=places: _py_round(v, p))
    return trades_df
