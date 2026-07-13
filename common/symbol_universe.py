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


# ---------------------------------------------------------------------------
# Authoritative common-stock classification via Polygon reference API
# ---------------------------------------------------------------------------
# 2026-07-13: the pattern filter above (is_common_stock_symbol) only removes
# *dotted*-suffix tickers (``FOO.W``). Real Polygon / cache tickers are
# *concatenated* (``FOOW``), so the pattern filter is a near-no-op on live
# data (drops ~68 of ~12,400). The trading universe was therefore ~26-58%
# non-common-stock (ETF ~42%, ADR, preferred, warrant, unit, ...).
#
# This block adds a network-backed, disk-cached filter that uses Polygon's
# ``/v3/reference/tickers?type=CS`` as the source of truth. It degrades
# safely: if Polygon is unreachable it returns an empty set and callers fall
# back to the pattern filter (i.e. current behaviour), never nuking the whole
# universe.

_POLYGON_CS_CACHE_REL = "reference/polygon_common_stocks.json"
# SPY is an ETF but is the System7 hedge underlying; always retained.
COMMON_STOCK_ALWAYS_KEEP: tuple[str, ...] = ("SPY",)


def _polygon_cs_cache_path(settings: Any | None = None):
    from pathlib import Path

    try:
        if settings is None:
            from config.settings import get_settings

            settings = get_settings(create_dirs=True)
        base = Path(getattr(settings, "DATA_CACHE_DIR"))
        return base / _POLYGON_CS_CACHE_REL
    except Exception:
        return None


def fetch_polygon_common_stock_set(
    *, timeout: int = 30, logger: logging.Logger | None = None
) -> set[str]:
    """Fetch the set of active US common-stock tickers from Polygon.

    Queries ``/v3/reference/tickers?market=stocks&type=CS&active=true`` with
    pagination. Returns upper-cased tickers. Returns an EMPTY set on any
    failure (including partial pagination) so callers can fall back rather
    than over-filter.
    """
    log = _normalize_logger(logger)
    try:
        import common.polygon_data as _pd

        _pd._load_env()
        api_key = _pd._get_api_key()
        api_base = _pd._API_BASE
    except Exception as exc:  # pragma: no cover - env/key missing
        log.warning("Polygon CS filter: API key/base unavailable (%s)", exc)
        return set()

    import time as _time

    out: set[str] = set()
    url = f"{api_base}/v3/reference/tickers"
    params: dict[str, Any] = {
        "market": "stocks",
        "type": "CS",
        "active": "true",
        "limit": 1000,
        "apiKey": api_key,
    }
    pages = 0
    max_429_retries = 5
    backoff_seconds = 15.0
    while True:
        payload = None
        for attempt in range(max_429_retries + 1):
            try:
                resp = requests.get(url, params=params, timeout=timeout)
                if resp.status_code == 429:
                    if attempt >= max_429_retries:
                        log.warning(
                            "Polygon CS filter: still 429 after %d retries; aborting",
                            max_429_retries,
                        )
                        return set()
                    log.info(
                        "Polygon CS filter: 429 on page %d (retry %d/%d in %.0fs)",
                        pages,
                        attempt + 1,
                        max_429_retries,
                        backoff_seconds,
                    )
                    _time.sleep(backoff_seconds)
                    continue
                resp.raise_for_status()
                payload = resp.json()
                break
            except Exception as exc:  # pragma: no cover - network only
                log.warning(
                    "Polygon CS filter: fetch failed on page %d (%s)", pages, exc
                )
                return set()
        if payload is None:
            return set()
        for row in payload.get("results", []) or []:
            tk = str(row.get("ticker", "")).strip().upper()
            if tk:
                out.add(tk)
        pages += 1
        next_url = payload.get("next_url")
        if not next_url:
            break
        url, params = next_url, {"apiKey": api_key}
        _time.sleep(0.4)  # gentle spacing between pages
    log.info("Polygon CS universe: %d common stocks (%d pages)", len(out), pages)
    return out


def get_common_stock_set(
    *,
    settings: Any | None = None,
    max_age_days: int = 7,
    force_refresh: bool = False,
    logger: logging.Logger | None = None,
) -> set[str]:
    """Return the authoritative CS ticker set, disk-cached under
    ``data_cache/reference/polygon_common_stocks.json``.

    Reads the cache when present and younger than ``max_age_days``; otherwise
    refetches from Polygon and rewrites it. Honors ``POLYGON_CS_FILTER_DISABLE``
    (returns empty set → callers fall back to the pattern filter). Empty set on
    failure.
    """
    import datetime as _dt
    import json as _json

    log = _normalize_logger(logger)
    if os.getenv("POLYGON_CS_FILTER_DISABLE", "").strip().lower() in {
        "1",
        "true",
        "yes",
        "on",
    }:
        log.info("Polygon CS filter disabled via POLYGON_CS_FILTER_DISABLE")
        return set()

    path = _polygon_cs_cache_path(settings)
    if path is not None and path.exists() and not force_refresh:
        try:
            payload = _json.loads(path.read_text(encoding="utf-8"))
            tickers = payload.get("tickers") or []
            asof = str(payload.get("as_of") or "")[:10]
            fresh = True
            if asof:
                try:
                    age = (_dt.date.today() - _dt.date.fromisoformat(asof)).days
                    fresh = 0 <= age <= max_age_days
                except Exception:
                    fresh = True
            if tickers and fresh:
                return {str(t).strip().upper() for t in tickers if t}
        except Exception as exc:
            log.warning("Polygon CS cache read failed (%s): %s", path, exc)

    cs = fetch_polygon_common_stock_set(logger=log)
    if cs and path is not None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(
                _json.dumps(
                    {
                        "as_of": _dt.date.today().isoformat(),
                        "count": len(cs),
                        "tickers": sorted(cs),
                    },
                    ensure_ascii=False,
                ),
                encoding="utf-8",
            )
        except Exception as exc:
            log.warning("Polygon CS cache write failed (%s): %s", path, exc)
    return cs


def filter_to_common_stock(
    symbols: Iterable[object],
    *,
    cs_set: set[str] | None = None,
    always_keep: Iterable[str] = COMMON_STOCK_ALWAYS_KEEP,
    settings: Any | None = None,
    logger: logging.Logger | None = None,
) -> list[str]:
    """Filter ``symbols`` to US common stocks (Polygon ``type=CS``).

    - Preserves input order and de-duplicates (upper-cased).
    - ``always_keep`` tickers (default ``SPY`` for System7) are retained
      regardless of type.
    - When the authoritative CS set is unavailable (offline / disabled), falls
      back to the pattern filter :func:`is_common_stock_symbol` so the whole
      universe is never dropped.
    """
    log = _normalize_logger(logger)
    keep = {str(s).strip().upper() for s in (always_keep or ()) if str(s).strip()}
    materialized = [str(s).strip().upper() for s in symbols if str(s).strip()]

    if cs_set is None:
        cs_set = get_common_stock_set(settings=settings, logger=log)

    seen: set[str] = set()
    out: list[str] = []

    if not cs_set:
        log.warning(
            "filter_to_common_stock: authoritative CS set unavailable; "
            "falling back to pattern filter (near-no-op on live data)"
        )
        for u in materialized:
            if u in seen:
                continue
            if u in keep or is_common_stock_symbol(u):
                seen.add(u)
                out.append(u)
        return out

    dropped = 0
    for u in materialized:
        if u in seen:
            continue
        if u in keep or u in cs_set:
            seen.add(u)
            out.append(u)
        else:
            dropped += 1
    log.info(
        "filter_to_common_stock: %d in -> %d common stock (%d dropped as non-CS)",
        len(materialized),
        len(out),
        dropped,
    )
    return out


__all__ = [
    "COMMON_STOCK_ALWAYS_KEEP",
    "DEFAULT_EODHD_EXCHANGES",
    "DEFAULT_NASDAQ_URLS",
    "build_symbol_universe",
    "build_symbol_universe_from_settings",
    "fetch_eodhd_exchange_metadata",
    "fetch_nasdaq_trader_symbols",
    "fetch_polygon_common_stock_set",
    "filter_common_stocks",
    "filter_to_common_stock",
    "get_common_stock_set",
    "is_common_stock_symbol",
    "resolve_eodhd_config",
]
