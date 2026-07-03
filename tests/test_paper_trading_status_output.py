"""scripts/paper_trading_status.py の出力 schema 契約 test.

status script は read-only。実発注は絶対にしない。
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paper_trading_status.py"


def _run(tmp_path: Path, *extra_args: str) -> tuple[int, dict]:
    out = tmp_path / "paper_status_20260703.json"
    args = [
        sys.executable,
        str(SCRIPT),
        "--no-alpaca",
        "--date", "2026-07-03",
        "--output-json", str(out),
        "--results-dir", str(tmp_path),
    ]
    args.extend(extra_args)
    proc = subprocess.run(args, capture_output=True, text=True, cwd=str(ROOT))
    if out.exists():
        data = json.loads(out.read_text(encoding="utf-8"))
    else:
        data = {}
    return proc.returncode, data


def test_status_writes_valid_json(tmp_path: Path):
    rc, data = _run(tmp_path)
    assert rc == 0
    assert data.get("version") == "1.0"
    assert data.get("date") == "2026-07-03"
    assert "positions" in data
    assert isinstance(data["positions"], list)


def test_status_has_spy_context(tmp_path: Path):
    rc, data = _run(tmp_path)
    assert rc == 0
    # SPY.csv が存在すれば float、無ければ None が入る (どちらも許容)
    assert "spy_high" in data
    assert "spy_max70" in data


def test_status_no_alpaca_yields_empty_positions(tmp_path: Path):
    rc, data = _run(tmp_path)
    assert rc == 0
    assert data.get("count") == 0
    assert data.get("positions") == []
