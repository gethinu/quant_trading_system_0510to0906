"""Strategies package unified factory.

戦略クラスの遅延インスタンス化と生成回数メトリクスを提供する。

使用例:
        from strategies import get_strategy
        s1 = get_strategy("system1")

環境変数 ENABLE_SUBSTEP_LOGS=1 で生成ログを出力。
"""

from __future__ import annotations

import os
from typing import Dict

_instances: Dict[str, object] = {}
_create_counts: Dict[str, int] = {}


def _log(msg: str) -> None:
    if (os.environ.get("ENABLE_SUBSTEP_LOGS") or "").lower() in {"1", "true", "yes"}:
        try:
            print(f"[strategies] {msg}")
        except Exception:  # pragma: no cover
            pass


_CLASS_MAP = {
    "system1": ("strategies.system1_strategy", "System1Strategy"),
    "system2": ("strategies.system2_strategy", "System2Strategy"),
    "system3": ("strategies.system3_strategy", "System3Strategy"),
    "system4": ("strategies.system4_strategy", "System4Strategy"),
    "system5": ("strategies.system5_strategy", "System5Strategy"),
    "system6": ("strategies.system6_strategy", "System6Strategy"),
    "system7": ("strategies.system7_strategy", "System7Strategy"),
    "system8": ("strategies.system8_strategy", "System8Strategy"),
}


def get_strategy(system_name: str) -> object:
    """指定システムの Strategy インスタンスを取得 (遅延生成)。

    Args:
            system_name: "system1" ～ "system7"
    Returns:
            StrategyBase 実装インスタンス
    Raises:
            KeyError: 未対応 system_name
    """

    key = system_name.lower()
    if key in _instances:
        return _instances[key]
    if key not in _CLASS_MAP:
        raise KeyError(f"unknown strategy: {system_name}")
    module_name, class_name = _CLASS_MAP[key]
    try:
        module = __import__(module_name, fromlist=[class_name])
        cls = getattr(module, class_name)
        inst = cls()
        _instances[key] = inst
        _create_counts[key] = _create_counts.get(key, 0) + 1
        _log(f"create {key} count={_create_counts[key]}")
        return inst
    except Exception as e:  # pragma: no cover - 防御
        _log(f"failed to create {key}: {e}")
        raise


def get_creation_counts() -> dict[str, int]:  # 監視/テスト向け
    return dict(_create_counts)


__all__ = ["get_strategy", "get_creation_counts"]
