"""ユニバース（銘柄集合）を取得・フィルタするユーティリティ."""

from __future__ import annotations

from collections.abc import Iterable, Mapping
import logging
import os
from typing import Any

import requests

DEFAULT_NASDAQ_URLS = (
    "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt",
    "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt",
)

DEFAULT_EODHD_EXCHANGES = ("NYSE", "NASDAQ", "AMEX")


def _normalize_logger(logger: logging.Logger | None) -> logging.Logger:
    if logger is not None:
        return logger
    return logging.getLogger(__name__)


def _coerce_str(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def _coerce_bool(value: Any) -> bool | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return value
    if isinstance(value, (int | float)):
        return bool(value)
    text = str(value).strip().lower()
    if not text:
        return None
    truthy = {"1", "true", "t", "yes", "y", "on", "active", "listed"}
    falsy = {"0", "false", "f", "no", "n", "off", "inactive", "delisted"}
    if text in truthy:
        return True
    if text in falsy:
        return False
    return None


def _first(mapping: Mapping[str, Any], *keys: str) -> Any:
    for key in keys:
        if key in mapping:
            return mapping[key]
    return None


def fetch_nasdaq_trader_symbols(
    urls: Iterable[str],
    *,
    timeout: int = 10,
    logger: logging.Logger | None = None,
) -> set[str]:
    """NASDAQ Trader の銘柄一覧（複数 URL）からシンボル集合を構築する."""

    log = _normalize_logger(logger)
    symbols: set[str] = set()
    for url in urls:
        if not url:
            continue
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover - 通信エラーはログのみ
            log.warning("NASDAQListed取得失敗: %s (%s)", url, exc)
            continue

        header: list[str] | None = None
        for line in resp.text.splitlines():
            if "|" not in line:
                continue
            parts = [segment.strip() for segment in line.split("|")]
            if header is None:
                header = [item.upper() for item in parts]
                continue
            symbol = parts[0].upper()
            if not symbol or symbol in {"SYMBOL", "ACT SYMBOL"}:
                continue
            # ETF/Test Issue を NASDAQ Trader 側のフラグで除外
            limit = min(len(header), len(parts))
            row = {header[idx]: parts[idx] for idx in range(limit)}
            etf_flag = _coerce_bool(row.get("ETF"))
            test_flag = _coerce_bool(row.get("TEST ISSUE"))
            if etf_flag:
                continue
            if test_flag:
                continue
            symbols.add(symbol)
    return symbols


def fetch_eodhd_exchange_metadata(
    api_base: str,
    api_key: str,
    exchanges: Iterable[str],
    *,
    timeout: int = 10,
    logger: logging.Logger | None = None,
) -> dict[str, Mapping[str, Any]]:
    """EODHD exchange-symbol-list エンドポイントからメタデータを収集する."""

    log = _normalize_logger(logger)
    base_url = str(api_base).rstrip("/")
    metadata: dict[str, Mapping[str, Any]] = {}

    for exchange in exchanges:
        if not exchange:
            continue
        exch = str(exchange).strip().upper()
        if not exch:
            continue
        url = f"{base_url}/api/exchange-symbol-list/{exch}?api_token={api_key}&fmt=json"
        try:
            resp = requests.get(url, timeout=timeout)
            resp.raise_for_status()
        except Exception as exc:  # pragma: no cover - ログのみ
            log.warning("EODHD %s 銘柄一覧取得失敗: %s", exch, exc)
            continue

        try:
            payload = resp.json()
        except ValueError:  # pragma: no cover - JSON 以外
            log.warning("EODHD %s 銘柄一覧が JSON ではありません", exch)
            continue

        if isinstance(payload, Mapping):
            for key in ("data", "symbols", "items", "results"):
                candidate = payload.get(key)
                if isinstance(candidate, list):
                    payload = candidate
                    break

        if not isinstance(payload, list):
            log.warning("EODHD %s 銘柄一覧の形式が不正です", exch)
            continue

        for row in payload:
            if not isinstance(row, Mapping):
                continue
            code = _first(row, "Code", "code", "Symbol", "symbol")
            ticker = _coerce_str(code)
            if not ticker:
                continue
            ticker = ticker.upper()
            metadata.setdefault(ticker, row)

    return metadata


def build_symbol_universe(
    api_base: str,
    api_key: str,
    *,
    timeout: int = 10,
    urls: Iterable[str] = DEFAULT_NASDAQ_URLS,
    exchanges: Iterable[str] = DEFAULT_EODHD_EXCHANGES,
    logger: logging.Logger | None = None,
) -> list[str]:
    """NASDAQ Trader/EODHD の情報を組み合わせて銘柄ユニバースを構築する."""

    log = _normalize_logger(logger)
    raw_symbols = fetch_nasdaq_trader_symbols(urls, timeout=timeout, logger=log)
    if not raw_symbols:
        log.warning("NASDAQ Trader から有効な銘柄を取得できませんでした")
        return []

    if not api_key:
        log.warning("EODHD API キーが未設定のためフィルタリングをスキップします")
        return sorted(raw_symbols)

    metadata = fetch_eodhd_exchange_metadata(
        api_base,
        api_key,
        exchanges,
        timeout=timeout,
        logger=log,
    )
    if not metadata:
        log.warning(
            "EODHD からのメタデータ取得に失敗したためフィルタリングをスキップします"
        )
        return sorted(raw_symbols)

    filtered: list[str] = []
    skipped_missing_meta = 0
    for symbol in sorted(raw_symbols):
        info = metadata.get(symbol)
        if info is None:
            skipped_missing_meta += 1
            continue

        type_label = _coerce_str(
            _first(
                info,
                "Type",
                "type",
                "TypeDescription",
                "typeDescription",
                "TypeDesc",
            )
        )
        if not type_label:
            continue
        if "common stock" not in type_label.lower():
            continue

        is_delisted = _coerce_bool(
            _first(
                info, "IsDelisted", "is_delisted", "Delisted", "delisted", "isDelisted"
            )
        )
        if is_delisted:
            continue

        etf_flag = _coerce_bool(_first(info, "ETF", "Etf", "is_etf", "IsETF"))
        if etf_flag:
            continue

        filtered.append(symbol)

    if skipped_missing_meta:
        log.info("EODHD メタデータ欠損により %s 銘柄を除外", skipped_missing_meta)

    if not filtered:
        log.warning("フィルタ後の銘柄が 0 件だったため未フィルタのリストを返します")
        return sorted(raw_symbols)

    return filtered


def resolve_eodhd_config(
    settings: Any | None,
    *,
    default_timeout: int = 10,
) -> tuple[str | None, str, int]:
    """設定オブジェクトや環境変数から EODHD 接続情報を解決する."""

    base_url: str | None = None
    api_key = ""
    timeout = default_timeout

    if settings is not None:
        base_url = _coerce_str(getattr(settings, "API_EODHD_BASE", None))
        if not base_url:
            data_cfg = getattr(settings, "data", None)
            base_url = _coerce_str(getattr(data_cfg, "eodhd_base", None))

        api_key = _coerce_str(getattr(settings, "EODHD_API_KEY", None)) or ""
        if not api_key:
            data_cfg = getattr(settings, "data", None)
            env_name = (
                _coerce_str(getattr(data_cfg, "api_key_env", None)) or "EODHD_API_KEY"
            )
            api_key = os.getenv(env_name, "")

        try:
            timeout = int(getattr(settings, "REQUEST_TIMEOUT", timeout))
        except Exception:
            pass

    if not base_url:
        base_url = _coerce_str(os.getenv("API_EODHD_BASE"))

    if not api_key:
        api_key = os.getenv("EODHD_API_KEY", "")

    return base_url, api_key, timeout


def build_symbol_universe_from_settings(
    settings: Any | None,
    *,
    urls: Iterable[str] = DEFAULT_NASDAQ_URLS,
    exchanges: Iterable[str] = DEFAULT_EODHD_EXCHANGES,
    logger: logging.Logger | None = None,
) -> list[str]:
    """設定情報をもとに銘柄ユニバースを構築するヘルパー."""

    base_url, api_key, timeout = resolve_eodhd_config(settings)
    if not base_url:
        base_url = "https://eodhistoricaldata.com"
    return build_symbol_universe(
        base_url,
        api_key,
        timeout=timeout,
        urls=urls,
        exchanges=exchanges,
        logger=logger,
    )


# ---------------------------------------------------------------------------
# Lightweight symbol pattern filter (NO network) - 2026-07-02 hygiene
# ---------------------------------------------------------------------------
# daily_polygon_monitor.py / cache_daily_polygon.py が Polygon Grouped Daily
# の raw universe (~12,445 銘柄) を素通しで処理していた事象への対処。
# build_symbol_universe (EODHD 経由) と `_symbols.json` manifest が
# unavailable な local 実行環境でも動くよう、ticker 文字列の pattern だけで
# 「非普通株」を弾く軽量フィルターを提供する。
#
# 除外パターン (NASDAQ Trader / Polygon 表記に基づく):
#   - Preferred stocks: ticker 内に `$` 記号を含む (例: `AAB$P`)
#   - Warrants:  `.W` / `.WS` / `.WI` 末尾
#   - Units:     `.U` / `.UN` 末尾 (SPAC 上場直後)
#   - Rights:    `.R` / `.RT` 末尾
#   - Notes:     `.N` / `.NT` 末尾
#
# 保持: 通常銘柄、および `BRK.A`/`BRK.B` などの class share。
#
# 期待効果 (2026-07-02 log 実測): 12,445 → ~6,981 銘柄 (~44% 短縮)、
# trading universe と一致 (build_rolling が使う `_symbols.json` と揃う)。

_NON_COMMON_SUFFIXES = (
    ".W",
    ".WS",
    ".WI",
    ".U",
    ".UN",
    ".R",
    ".RT",
    ".N",
    ".NT",
)


def is_common_stock_symbol(symbol: object) -> bool:
    """Return True if ``symbol`` looks like a US common stock ticker.

    Pure string/pattern check — no network. Used to filter Polygon Grouped
    Daily universe down to the trading universe when the EODHD-based
    ``build_symbol_universe`` isn't available (offline / local runs).

    Returns False for empty / non-string / suspicious tickers. This is
    intentionally conservative: an ambiguous ticker is dropped rather than
    letting warrant / preferred noise leak into the trading universe.
    """
    if symbol is None:
        return False
    s = str(symbol).strip().upper()
    if not s:
        return False
    # Preferred / rights hybrids often carry `$` (NASDAQ Trader convention).
    if "$" in s:
        return False
    for suffix in _NON_COMMON_SUFFIXES:
        if s.endswith(suffix):
            return False
    # Reject symbols with only special chars (no ASCII letter).
    if not any("A" <= ch <= "Z" for ch in s):
        return False
    return True


def filter_common_stocks(symbols: Iterable[object]) -> list[str]:
    """Apply :func:`is_common_stock_symbol` across an iterable.

    Preserves input order and de-duplicates (upper-cased). Non-string /
    empty entries are silently dropped.
    """
    seen: set[str] = set()
    out: list[str] = []
    for sym in symbols:
        if not is_common_stock_symbol(sym):
            continue
        s = str(sym).strip().upper()
        if s in seen:
            continue
        seen.add(s)
        out.append(s)
    return out


__all__ = [
    "DEFAULT_EODHD_EXCHANGES",
    "DEFAULT_NASDAQ_URLS",
    "build_symbol_universe",
    "build_symbol_universe_from_settings",
    "fetch_eodhd_exchange_metadata",
    "fetch_nasdaq_trader_symbols",
    "filter_common_stocks",
    "is_common_stock_symbol",
    "resolve_eodhd_config",
]
