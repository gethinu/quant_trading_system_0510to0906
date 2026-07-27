"""Symbol-to-system mapping utilities with multi-system support.

This module provides helper functions to normalize, load, and store mappings
between ticker symbols and strategy systems. Each symbol may belong to multiple
systems; the first entry in the list is treated as the *primary* system.  The
functions accept legacy formats (single string per symbol) and newer list/dict
formats so that older caches remain compatible.

Highlights
=========
- Automatic normalization and deduplication of system names.
- Backward compatibility with historic string/dict based JSON payloads.
- Support for special tokens such as ``"all"`` to represent every system.
- Safe load/save helpers that never raise and degrade gracefully on errors.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
import json
import logging
from pathlib import Path
from typing import Any, TypeAlias

from common.system_constants import SYSTEM_CONFIGS

SymbolSystemMap: TypeAlias = dict[str, list[str]]
DEFAULT_SYMBOL_SYSTEM_MAP_PATH = Path("data/symbol_system_map.json")

logger = logging.getLogger(__name__)

# Pre-compute canonical system name lookups using configuration metadata.
ALL_SYSTEM_NAMES = tuple(SYSTEM_CONFIGS.keys())
_SYSTEM_CANONICAL: dict[str, str] = {
    str(name).strip().lower(): str(name).strip() for name in ALL_SYSTEM_NAMES
}


def _deduplicate(items: Iterable[str]) -> list[str]:
    """Return items in original order while removing duplicates."""

    seen: set[str] = set()
    output: list[str] = []
    for item in items:
        if item not in seen:
            seen.add(item)
            output.append(item)
    return output


def _normalize_system(value: Any | None) -> str | None:
    """Convert *value* to the canonical system name, if possible."""

    if value in (None, ""):
        return None
    try:
        text = str(value).strip()
    except Exception:
        return None
    if not text:
        return None

    lower = text.lower()
    if lower == "all":
        return "all"
    return _SYSTEM_CANONICAL.get(lower, text)


def _tokenize_string(value: str) -> list[str]:
    """Split a string into potential system names."""

    tokens: list[str] = []
    for chunk in value.replace(";", ",").split(","):
        name = chunk.strip()
        if name:
            tokens.append(name)
    return tokens


def _flatten_sequence(value: Sequence[Any] | Iterable[Any]) -> list[str]:
    """Flatten nested iterables of system names."""

    result: list[str] = []
    for item in value:
        if isinstance(item, (list, tuple, set)):
            result.extend(_flatten_sequence(item))
        elif isinstance(item, Mapping):
            result.extend(_flatten_mapping(item))
        else:
            normalized = _normalize_system(item)
            if normalized:
                result.append(normalized)
    return result


def _flatten_mapping(value: Mapping[Any, Any]) -> list[str]:
    """Extract systems from mapping structures."""

    collected: list[str] = []
    # Prefer well-known key names first.
    for key in ("primary", "preferred", "name", "system"):
        if key in value:
            collected.extend(_flatten_system_entries(value.get(key)))
    if not collected:
        for item in value.values():
            collected.extend(_flatten_system_entries(item))
    return collected


def _flatten_system_entries(value: Any) -> list[str]:
    """Recursively turn *value* into a list of system names."""

    if value in (None, ""):
        return []
    if isinstance(value, str):
        items: list[str] = []
        for token in _tokenize_string(value):
            normalized = _normalize_system(token)
            if normalized:
                items.append(normalized)
        return items
    if isinstance(value, Mapping):
        return _flatten_mapping(value)
    if isinstance(value, (list, tuple, set)):
        return _flatten_sequence(value)
    normalized = _normalize_system(value)
    return [normalized] if normalized else []


def _expand_all(values: list[str]) -> list[str]:
    """If ``"all"`` is present, expand into every known system."""

    if not values:
        return []
    lowered = {value.lower() for value in values}
    if "all" in lowered:
        return list(ALL_SYSTEM_NAMES)
    return values


def coerce_system_list(value: Any, *, ensure_all: bool = True) -> list[str]:
    """Normalize an arbitrary *value* into a list of system names.

    Args:
        value: Input value (string/list/dict/None).
        ensure_all: When ``True`` the special token ``"all"`` expands to
            the complete system list. When ``False`` the literal value is kept.
    """

    entries = [item for item in _flatten_system_entries(value) if item]
    deduped = _deduplicate(entries)
    if ensure_all:
        return _expand_all(deduped)
    return deduped


def resolve_primary_system(value: Any) -> str | None:
    """Return the first system from *value* or ``None`` when absent."""

    systems = coerce_system_list(value, ensure_all=False)
    return systems[0] if systems else None


def update_primary_system(existing: Any, primary: str | None) -> list[str]:
    """Insert *primary* at the front while preserving other systems."""

    systems = coerce_system_list(existing, ensure_all=True)
    primary_name = _normalize_system(primary)
    if primary_name and primary_name != "all":
        systems = [item for item in systems if item.lower() != primary_name.lower()]
        systems.insert(0, _SYSTEM_CANONICAL.get(primary_name.lower(), primary_name))
    elif not systems:
        systems = list(ALL_SYSTEM_NAMES)
    return _deduplicate(systems)


def normalize_symbol_system_map(
    raw_map: Mapping[Any, Any] | None,
    *,
    ensure_all: bool = True,
) -> SymbolSystemMap:
    """Normalize raw mapping into ``SymbolSystemMap`` format."""

    if not isinstance(raw_map, Mapping):
        return {}

    normalized: SymbolSystemMap = {}
    for key, value in raw_map.items():
        try:
            symbol = str(key).strip().upper()
        except Exception:
            continue
        if not symbol:
            continue
        systems = coerce_system_list(value, ensure_all=ensure_all)
        if systems:
            normalized[symbol] = systems
    return normalized


def load_symbol_system_map(path: Path | str | None = None) -> dict[str, str]:
    """Load a legacy-compatible symbol->system mapping.

    Returns mapping with lowercase symbol keys and lowercase primary system
    values (single string per symbol), to preserve historical expectations
    in allocation tests and scripts.
    """

    target = Path(path) if path else DEFAULT_SYMBOL_SYSTEM_MAP_PATH
    if not target.exists():
        return {}
    try:
        raw = json.loads(target.read_text(encoding="utf-8"))
    except Exception:
        return {}

    if not isinstance(raw, dict):
        return {}

    result: dict[str, str] = {}
    for k, v in raw.items():
        try:
            sym = str(k).strip().lower()
        except Exception:
            continue
        if not sym:
            continue
        systems = coerce_system_list(v, ensure_all=False)
        primary = systems[0] if systems else None
        if not primary:
            # 旧形式 (値が素の文字列) だけ str() 経由で拾う。空の list/dict/None を
            # str() すると "[]" のような **存在しない system 名** が生まれてしまい、
            # 集計に架空のバケットが立つので、コンテナ型は諦めて落とす
            # (resolve_primary_system([]) is None と同じ扱い)。
            if isinstance(v, (list, tuple, set, Mapping)) or v is None:
                continue
            try:
                primary = str(v).strip()
            except Exception:
                primary = ""
        primary_lc = str(primary).strip().lower()
        if primary_lc:
            result[sym] = primary_lc
    return result


def dump_symbol_system_map(
    mapping: Mapping[str, Sequence[str]] | None,
    path: Path | str | None = None,
) -> None:
    """Persist *mapping* as JSON list representation.

    The function ignores write errors and never raises. ``mapping`` is first
    normalized to guarantee every value is stored as ``list[str]``.
    """

    if mapping is None:
        mapping = {}

    normalized = normalize_symbol_system_map(mapping, ensure_all=True)
    target = Path(path) if path else DEFAULT_SYMBOL_SYSTEM_MAP_PATH

    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        serializable = {key: list(values) for key, values in normalized.items()}

        # 原子的書き込み: 一時ファイルに書き込んでから置換
        tmp_path = target.with_suffix(target.suffix + ".tmp")
        tmp_path.write_text(
            json.dumps(serializable, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        # os.replace 相当の Path.replace は同一 FS 上で原子的
        tmp_path.replace(target)
    except Exception as e:
        # ベストエフォートだが、診断のために DEBUG ログを残す
        try:
            logger.debug("Failed to write symbol_system_map to %s: %s", target, e)
        except Exception:
            pass
        return
