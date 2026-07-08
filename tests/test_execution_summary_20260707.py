"""execution summary formatter (recon → ntfy title/body) の検証。"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.publishers.execution_summary import (  # noqa: E402
    build_body,
    build_title,
    format_execution_summary,
)
from common.publishers.ntfy import _sanitize_ascii_title  # noqa: E402


def _recon() -> dict:
    return {
        "date": "2026-07-08",
        "inputs": {"signals": True, "paper_orders": True, "exit_orders": True},
        "portfolio": {
            "universe_target": 4123,
            "signals": 49,
            "orders_generated": 34,
            "entry_submitted": 27,
            "entry_filled": 25,
            "entry_failed": 1,
            "long_entry_submitted": 18,
            "short_entry_submitted": 9,
            "exit_submitted": 14,
            "exit_close": 5,
            "exit_protect": 9,
            "account_equity": 10120.0,
            "drop_breakdown": {"below_min_notional": 6, "short": 4, "fail": 1},
        },
        "systems": {
            "system1": {
                "long": {
                    "signals": 12,
                    "generated": 8,
                    "entry_submitted": 7,
                    "filled": 7,
                    "skipped": 1,
                    "failed": 0,
                },
                "short": {
                    "signals": 0,
                    "generated": 0,
                    "entry_submitted": 0,
                    "filled": 0,
                    "skipped": 0,
                    "failed": 0,
                },
                "exit": {"submitted": 2, "close": 0, "protect": 2},
                "funnel": None,
            },
            "system2": {
                "long": {
                    "signals": 0,
                    "generated": 0,
                    "entry_submitted": 0,
                    "filled": 0,
                    "skipped": 0,
                    "failed": 0,
                },
                "short": {
                    "signals": 9,
                    "generated": 5,
                    "entry_submitted": 3,
                    "filled": 3,
                    "skipped": 1,
                    "failed": 1,
                },
                "exit": {"submitted": 1, "close": 1, "protect": 0},
                "funnel": None,
            },
        },
    }


def test_title_is_ascii_plus_emoji_and_has_counts():
    title = build_title(_recon())
    # entry_failed>0 なので warning emoji
    assert "07-08" in title
    assert "sig49" in title
    assert "entry27" in title
    assert "exit14" in title
    # sanitize しても壊れない (ASCII+emoji のみ)
    assert _sanitize_ascii_title(title) == title


def test_body_has_funnel_and_per_system_and_drops():
    body = build_body(_recon())
    assert "Tgt 4123 → sig 49 → gen 34 → entry 27 → fill 25" in body
    assert "exit 14 (close 5 / protect 9)" in body
    assert "LONG entry 18 / SHORT entry 9" in body
    assert "資産 $10,120" in body
    # per-system: s1 long, s2 short
    assert "s1L 12→7" in body
    assert "s2S 9→3" in body
    # drop 内訳
    assert "below_min_notional 6" in body


def test_missing_inputs_noted():
    recon = _recon()
    recon["inputs"] = {"signals": True, "paper_orders": False, "exit_orders": False}
    body = build_body(recon)
    assert "入力欠損" in body
    assert "paper_orders" in body


def test_format_returns_tuple():
    title, body = format_execution_summary(_recon())
    assert isinstance(title, str) and isinstance(body, str)
    assert title and body
