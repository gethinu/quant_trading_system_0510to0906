"""Regression tests for F2 audit P0#2: _side_from_row must never silently default.

Historical bug (fixed 2026-07-03):
    ``common/alpaca_trading.py::_side_from_row`` mapped an unrecognised or
    missing ``side`` value to ``"sell"`` via a silent default::

        return "buy" if raw == "long" else "sell"

    Any schema drift on the signals frame — a missing ``side`` column, a typo,
    a new value like ``"exit"`` — therefore submitted a SHORT sell order. Paper
    only for now, but the same code will fire live once autotrade flips.

Coverage:
    * Known aliases (buy / long / sell / short / sell_short) map deterministically.
    * Missing ``side`` raises ``InvalidSideError`` (used to silently return "sell").
    * Unknown side raises ``InvalidSideError`` (used to silently return "sell").
    * ``signals_to_orders`` skips per-row on ``InvalidSideError`` and keeps the
      rest of the batch alive — a single bad row must not kill the whole run,
      but must also never silently become a short.
"""

from __future__ import annotations

import pandas as pd
import pytest

from common.alpaca_trading import InvalidSideError, _side_from_row, signals_to_orders

# ---------------------------------------------------------------------------
# Known aliases
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("buy", "buy"),
        ("BUY", "buy"),
        ("Buy", "buy"),
        ("long", "buy"),
        ("LONG", "buy"),
        (" long ", "buy"),  # whitespace tolerated
        ("sell", "sell"),
        ("SELL", "sell"),
        ("short", "sell"),
        ("Short", "sell"),
        ("sell_short", "sell"),
    ],
)
def test_side_from_row_maps_known_aliases(raw: str, expected: str) -> None:
    assert _side_from_row(pd.Series({"side": raw, "symbol": "AAPL"})) == expected


# ---------------------------------------------------------------------------
# Silent defaults are dead
# ---------------------------------------------------------------------------


def test_side_from_row_raises_on_missing_side_column() -> None:
    """Missing 'side' key used to silently return 'sell'. Now it raises."""
    with pytest.raises(InvalidSideError) as excinfo:
        _side_from_row(pd.Series({"symbol": "AAPL", "system": "system1"}))
    # The operator must be able to identify the offending row from the log line.
    msg = str(excinfo.value)
    assert "AAPL" in msg
    assert "system1" in msg


def test_side_from_row_raises_on_empty_side_value() -> None:
    with pytest.raises(InvalidSideError):
        _side_from_row(pd.Series({"side": "", "symbol": "AAPL"}))
    with pytest.raises(InvalidSideError):
        _side_from_row(pd.Series({"side": "   ", "symbol": "AAPL"}))


def test_side_from_row_raises_on_unknown_value() -> None:
    """An unknown value ('exit', 'reverse', typo) used to silently return 'sell'."""
    for bad in ("exit", "reverse", "hodl", "flat", "sel"):
        with pytest.raises(InvalidSideError) as excinfo:
            _side_from_row(pd.Series({"side": bad, "symbol": "AAPL"}))
        assert bad in str(excinfo.value).lower()


# ---------------------------------------------------------------------------
# Batch behaviour: skip bad row, keep the batch alive
# ---------------------------------------------------------------------------


def test_signals_to_orders_skips_invalid_side_and_keeps_batch_alive() -> None:
    """A single bad row must not kill the batch, and must never become a short."""
    df = pd.DataFrame(
        [
            {
                "symbol": "AAPL",
                "system": "system1",
                "side": "long",
                "shares": 10,
                "entry_price": 195.0,
                "entry_date": "2026-06-30",
            },
            {
                # Historical bug: this row would silently become a SHORT sell.
                "symbol": "MSFT",
                "system": "system1",
                "side": "wat?",
                "shares": 5,
                "entry_price": 400.0,
                "entry_date": "2026-06-30",
            },
            {
                # And this row: missing `side` entirely.
                "symbol": "GOOG",
                "system": "system1",
                "shares": 3,
                "entry_price": 180.0,
                "entry_date": "2026-06-30",
            },
            {
                "symbol": "TSLA",
                "system": "system1",
                "side": "long",
                "shares": 2,
                "entry_price": 250.0,
                "entry_date": "2026-06-30",
            },
        ]
    )
    orders = signals_to_orders(df, account_equity=100_000.0, dry_run=True)
    symbols = {o.symbol for o in orders}
    sides = {o.symbol: o.side for o in orders}

    # Bad rows were skipped (not defaulted to sell!)
    assert "MSFT" not in symbols, (
        "F2 P0#2 regression: MSFT with side='wat?' was submitted — the historical "
        "silent default would have made it a SHORT sell."
    )
    assert "GOOG" not in symbols, (
        "F2 P0#2 regression: GOOG with missing side was submitted — the historical "
        "silent default would have made it a SHORT sell."
    )

    # Good rows still got through — batch stayed alive around the bad row.
    assert symbols == {"AAPL", "TSLA"}
    assert sides["AAPL"] == "buy"
    assert sides["TSLA"] == "buy"


def test_signals_to_orders_still_produces_no_orders_when_all_rows_bad() -> None:
    """A batch of all-bad rows returns [] — critically, without any silent shorts."""
    df = pd.DataFrame(
        [
            {"symbol": "AAPL", "system": "system1", "side": "?", "shares": 10},
            {"symbol": "MSFT", "system": "system1", "shares": 5},
        ]
    )
    orders = signals_to_orders(df, account_equity=100_000.0, dry_run=True)
    assert orders == []
