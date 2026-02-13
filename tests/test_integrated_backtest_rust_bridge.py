from __future__ import annotations

import os
from unittest.mock import patch

from common.integrated_backtest_rust_bridge import (
    run_rust_backtest_core,
    should_use_rust_engine,
)


def test_should_use_rust_engine_python_mode():
    with patch.dict(os.environ, {"INTEGRATED_BACKTEST_ENGINE": "python"}):
        assert should_use_rust_engine(engine=None) is False


def test_should_use_rust_engine_requires_binary_when_explicit():
    with patch.dict(os.environ, {"INTEGRATED_BACKTEST_ENGINE": "rust"}):
        try:
            should_use_rust_engine(engine=None, rust_bin="C:/missing/path.exe")
        except RuntimeError:
            return
    raise AssertionError("RuntimeError was not raised")


def test_run_rust_backtest_core_returns_none_when_auto_and_missing():
    payload = {
        "dates": [],
        "systems_order": [],
        "initial_capital": 1000.0,
        "allocations": {},
        "long_share": 0.5,
        "short_share": 0.5,
        "allow_gross_leverage": False,
        "opportunities": [],
    }
    df = run_rust_backtest_core(
        payload,
        engine="auto",
        rust_bin="C:/missing/path.exe",
    )
    assert df is None
