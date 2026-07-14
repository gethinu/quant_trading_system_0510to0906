from __future__ import annotations

from collections.abc import Iterable
from pathlib import Path

import pandas as pd

from config.settings import get_settings


def build_universe_from_cache(
    *,
    min_price: float = 5.0,
    min_dollar_vol: float = 25_000_000.0,
    limit: int | None = 2000,
    prefer_base: bool = True,
    common_only: bool = True,
) -> list[str]:
    """data_cache から最新行の Close と DollarVolume50 を見てユニバースを構築。
    prefer_base=True の場合は data_cache/base/*.csv を優先。
    common_only=True の場合は US 普通株 (Polygon type=CS) のみに絞る (SPY は温存)。
    """
    settings = get_settings(create_dirs=True)
    cache_dir = Path(settings.DATA_CACHE_DIR)
    base_dir = cache_dir / "base"

    files: Iterable[Path]
    if prefer_base and base_dir.exists():
        files = base_dir.glob("*.csv")
    else:
        files = cache_dir.glob("*.csv")

    symbols: list[str] = []
    for _i, f in enumerate(files, 1):
        try:
            df = pd.read_csv(f)
            if "Date" not in df.columns or "Close" not in df.columns:
                continue
            df["Date"] = pd.to_datetime(df["Date"])  # ensure sorted
            df = df.sort_values("Date")
            last = df.iloc[-1]
            close = float(last.get("Close", 0.0))
            dv50 = last.get("DollarVolume50")
            if pd.isna(dv50):
                # フォールバック: 直近50本で再計算（軽量化のため列のみ）
                if {"Close", "Volume"}.issubset(df.columns):
                    tail = df[["Close", "Volume"]].tail(50)
                    dv50 = float((tail["Close"] * tail["Volume"]).mean())
                else:
                    dv50 = 0.0
            dv50 = float(dv50 or 0.0)
            if close >= min_price and dv50 >= min_dollar_vol:
                symbols.append(f.stem)
        except Exception:
            continue
        if limit and len(symbols) >= int(limit):
            break
    # 2026-07-13: US 普通株のみに絞る (Polygon type=CS を正とする)。SPY は
    # System7 ヘッジ用に温存。CS セット取得不能時は pattern フォールバック。
    if common_only:
        try:
            from common.symbol_universe import filter_to_common_stock

            symbols = filter_to_common_stock(symbols)
        except Exception:
            pass
    # 代表ETFを先頭に残す
    out = list(dict.fromkeys(["SPY"] + [s for s in symbols if s != "SPY"]))
    return out


def save_universe_file(symbols: list[str], path: str | None = None) -> str:
    settings = get_settings(create_dirs=True)
    if path is None:
        path = str(Path(settings.PROJECT_ROOT) / "data" / "universe_auto.txt")
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text("\n".join(symbols), encoding="utf-8")
    return str(p)


def _read_text_with_fallback(p: Path) -> str:
    """Read text with encoding fallbacks to avoid hard failures on Windows-created files.

    Tries utf-8, then utf-8-sig, cp932 (Shift_JIS), cp1252, and latin-1.
    As a last resort, decodes with utf-8 and errors='ignore'.
    """
    encodings = ("utf-8", "utf-8-sig", "cp932", "cp1252", "latin-1")
    for enc in encodings:
        try:
            return p.read_text(encoding=enc)
        except UnicodeDecodeError:
            continue
        except Exception:
            # Other IO errors should bubble up in the caller; continue for robustness here
            continue
    # last resort: ignore undecodable bytes
    try:
        return p.read_bytes().decode("utf-8", errors="ignore")
    except Exception:
        return ""


def load_universe_file(path: str | None = None) -> list[str]:
    settings = get_settings(create_dirs=True)
    if path is None:
        path = str(Path(settings.PROJECT_ROOT) / "data" / "universe_auto.txt")
    p = Path(path)
    if not p.exists():
        return []
    txt = _read_text_with_fallback(p).strip()
    if not txt:
        return []
    return [s.strip().upper() for s in txt.splitlines() if s.strip()]


def get_all_symbols_from_cache(cache_dir: str | Path | None = None) -> list[str]:
    """
    data_cache フォルダ内の全CSVファイル名（拡張子除く）を銘柄リストとして返す。
    """
    if cache_dir is None:
        settings = get_settings(create_dirs=True)
        cache_dir = Path(settings.DATA_CACHE_DIR)
    else:
        cache_dir = Path(cache_dir)
    files = cache_dir.glob("*.csv")
    symbols = [f.stem.upper() for f in files if f.is_file()]
    # 代表ETFを先頭に
    out = list(dict.fromkeys(["SPY"] + [s for s in symbols if s != "SPY"]))
    return out


def save_universe_from_cache(
    cache_dir: str | Path | None = None,
    out_path: str | Path | None = None,
    *,
    common_only: bool = True,
) -> str:
    """
    data_cache フォルダ内の全CSVファイル名（拡張子除く）を universe_auto.txt に保存する。
    common_only=True の場合は US 普通株 (Polygon type=CS) のみに絞る (SPY は温存)。
    """
    if cache_dir is None:
        settings = get_settings(create_dirs=True)
        cache_dir = Path(settings.DATA_CACHE_DIR)
    else:
        cache_dir = Path(cache_dir)
    symbols = [f.stem.upper() for f in cache_dir.glob("*.csv") if f.is_file()]
    if common_only:
        try:
            from common.symbol_universe import filter_to_common_stock

            symbols = filter_to_common_stock(symbols)
        except Exception:
            pass
    # 代表ETFを先頭に
    out_syms = list(dict.fromkeys(["SPY"] + [s for s in symbols if s != "SPY"]))
    if out_path is None:
        settings = get_settings(create_dirs=True)
        out_path = Path(settings.PROJECT_ROOT) / "data" / "universe_auto.txt"
    else:
        out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text("\n".join(out_syms), encoding="utf-8")
    return str(out_path)


__all__ = [
    "build_universe_from_cache",
    "save_universe_file",
    "load_universe_file",
    "get_all_symbols_from_cache",
    "save_universe_from_cache",
]
