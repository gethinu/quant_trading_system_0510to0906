"""Regression tests for F2 audit P0#5: zero-orders must NOT be silent success.

Historical bug (fixed 2026-07-03):
    ``scripts/paper_trading_submit.py`` ``_submit_from_json`` printed
    ``完了: 生成=0 送信=0 失敗=0`` and returned exit code 0 whenever
    ``signals_json_to_orders`` returned ``[]``, regardless of whether the input
    JSON was actually empty (real flat book) or contained signals that were
    all silently dropped due to schema drift / min_notional / tier-key
    mismatch. Daily pipeline treated the run as a successful submit and
    subscribers thought orders had been sent.

Coverage:
    * Input JSON with signals but produced orders=0 → exit code 3 + WARN log +
      ``status="no_orders_generated"`` written to output JSON.
    * Input JSON with 0 signals (true flat book) → exit code 0 + status marker
      ``no_input_signals`` (silent success is fine here).
    * CSV path also produces exit code 3 when input signals > 0 but orders=0.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
from unittest import mock

import pandas as pd
import pytest


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts import paper_trading_submit as pts  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write_json(tmp_path: Path, payload: dict) -> Path:
    p = tmp_path / "signals.json"
    p.write_text(json.dumps(payload), encoding="utf-8")
    return p


def _read_output(tmp_path: Path) -> dict:
    out = tmp_path / "orders.json"
    assert out.exists()
    return json.loads(out.read_text(encoding="utf-8"))


def _mk_args(
    tmp_path: Path,
    signals_json_path: Path,
    *,
    confirm: bool = False,
    tier: str = "small",
) -> argparse.Namespace:
    return argparse.Namespace(
        date=None,
        signals_csv=None,
        signals_json=str(signals_json_path),
        tier=tier,
        output_json=str(tmp_path / "orders.json"),
        min_notional=5.0,
        no_fractional=False,
        equity=10_000.0,
        demo=False,
        confirm=confirm,
        yes=False,
    )


# ---------------------------------------------------------------------------
# _count_input_signals unit
# ---------------------------------------------------------------------------


def test_count_input_signals_counts_all_systems() -> None:
    payload = {
        "systems": {
            "sys1": {"signals": [{"symbol": "AAPL"}, {"symbol": "MSFT"}]},
            "sys2": {"signals": [{"symbol": "TSLA"}]},
        }
    }
    assert pts._count_input_signals(payload) == 3


def test_count_input_signals_handles_empty_and_none() -> None:
    assert pts._count_input_signals({}) == 0
    assert pts._count_input_signals({"systems": None}) == 0
    assert pts._count_input_signals({"systems": {}}) == 0
    assert pts._count_input_signals({"systems": {"sys1": {}}}) == 0
    assert pts._count_input_signals(None) == 0  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# JSON path: input signals > 0 but orders = 0 -> anomaly
# ---------------------------------------------------------------------------


def test_json_path_input_signals_but_zero_orders_returns_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """The regression: input had signals but orders came back empty."""
    src = _write_json(
        tmp_path,
        {
            "date": "2026-07-03",
            "systems": {
                "sys1": {
                    "signals": [
                        {"symbol": "AAPL", "side": "BUY", "entry_price": 100.0, "weight": 0.5},
                        {"symbol": "MSFT", "side": "BUY", "entry_price": 200.0, "weight": 0.5},
                    ]
                }
            },
        },
    )
    args = _mk_args(tmp_path, src, confirm=False)

    # Force signals_json_to_orders to return [] to simulate the drift.
    with mock.patch.object(pts, "signals_json_to_orders", return_value=[]):
        rc = pts._submit_from_json(args)

    assert rc == 3, "input signals > 0 & orders = 0 must NOT be silent exit 0"

    out = _read_output(tmp_path)
    # meta is spread at the top level of the payload (see paper_trading_dryrun._write_orders_json).
    assert out["status"] == "no_orders_generated"
    assert out["input_signals"] == 2
    assert out["count"] == 0

    captured = capsys.readouterr().out
    assert "[WARN]" in captured
    assert "1 件も order が生成されませんでした" in captured


def test_json_path_zero_input_signals_returns_exit_0(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """True flat book — no input, no output — is legit silent success."""
    src = _write_json(
        tmp_path,
        {"date": "2026-07-03", "systems": {"sys1": {"signals": []}}},
    )
    args = _mk_args(tmp_path, src, confirm=False)

    with mock.patch.object(pts, "signals_json_to_orders", return_value=[]):
        rc = pts._submit_from_json(args)

    assert rc == 0
    out = _read_output(tmp_path)
    assert out["status"] == "no_input_signals"
    assert out["input_signals"] == 0


def test_json_path_successful_orders_returns_exit_0(tmp_path: Path) -> None:
    """Happy path: input signals produced orders, all submitted -> exit 0."""
    src = _write_json(
        tmp_path,
        {
            "date": "2026-07-03",
            "systems": {
                "sys1": {
                    "signals": [
                        {"symbol": "AAPL", "side": "BUY", "entry_price": 100.0, "weight": 1.0},
                    ]
                }
            },
        },
    )
    args = _mk_args(tmp_path, src, confirm=False)

    from common.alpaca_trading import PreparedOrder

    fake_order = PreparedOrder(
        symbol="AAPL",
        qty=0,
        side="buy",
        order_type="market",
        client_order_id="system1-AAPL-20260703",
        system="system1",
        entry_date="2026-07-03",
        notional_usd=1000.0,
        tier="small",
        dry_run=True,
    )
    with mock.patch.object(pts, "signals_json_to_orders", return_value=[fake_order]):
        rc = pts._submit_from_json(args)

    assert rc == 0
    out = _read_output(tmp_path)
    assert out["status"] == "ok"
    assert out["input_signals"] == 1
    assert out["count"] == 1


# ---------------------------------------------------------------------------
# CSV path (main) equivalent behavior
# ---------------------------------------------------------------------------


def test_csv_path_input_signals_but_zero_orders_returns_exit_3(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """CSV / dry-run path also must not silently succeed."""
    # a non-empty signals frame that will be dropped to [] by signals_to_orders
    # (simulated via mock — the point is the visibility, not the drop reason).
    csv = tmp_path / "signals.csv"
    csv.write_text(
        "symbol,side,shares,system,entry_price\n"
        "AAPL,buy,10,system1,100.0\n"
        "MSFT,buy,5,system1,200.0\n",
        encoding="utf-8",
    )

    args = argparse.Namespace(
        date=None,
        signals_csv=str(csv),
        signals_json=None,
        tier="small",
        output_json=None,
        min_notional=5.0,
        no_fractional=False,
        equity=10_000.0,
        demo=False,
        confirm=False,
        yes=False,
    )

    with mock.patch.object(pts, "signals_to_orders", return_value=[]):
        rc = pts.main([
            "--signals-csv", str(csv),
        ])

    assert rc == 3, "CSV path also must surface input>0 & planned=0 as exit 3"
    captured = capsys.readouterr().out
    assert "[WARN]" in captured
