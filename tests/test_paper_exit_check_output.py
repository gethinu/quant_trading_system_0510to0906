"""scripts/paper_exit_check.py の JSON 出力 schema 契約 test.

subscriber サービスイン基準:
    - --no-alpaca で offline 動作すること (CI で SDK 無しでも走る)
    - 出力 JSON に mode / count / exits / positions が必ずある
    - dry_run default で mode="dry_run"
    - --confirm 無しでは submit されない (guard test は test_alpaca_exit_orders.py 側)
    - system 別 rules サマリを systems field に含む (dashboard 用)
"""

from __future__ import annotations

import json
from pathlib import Path
import subprocess
import sys


ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paper_exit_check.py"


def _run(tmp_path: Path, *extra_args: str) -> tuple[int, dict]:
    out = tmp_path / "exit_orders_20260703.json"
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


def test_dry_run_default_writes_json(tmp_path: Path):
    rc, data = _run(tmp_path)
    assert rc == 0
    assert data.get("version") == "1.0"
    assert data.get("date") == "2026-07-03"
    assert data.get("mode") == "dry_run"
    assert "count" in data
    assert "exits" in data
    assert "positions" in data
    assert data.get("submitted") == 0
    assert data.get("failed") == 0


def test_output_schema_has_system_rules_summary(tmp_path: Path):
    rc, data = _run(tmp_path)
    assert rc == 0
    systems = data.get("systems") or {}
    # SYSTEM_TRADE_RULES 定義済 system がすべて出る
    for sys_key in ("system1", "system2", "system3", "system4", "system5", "system6"):
        assert sys_key in systems, f"systems 欠損: {sys_key}"
        rule = systems[sys_key]
        assert "max_holding_days" in rule
        assert "trailing_stop_pct" in rule
        assert "profit_target_type" in rule
        assert "profit_target_value" in rule


def test_offline_mode_yields_no_positions(tmp_path: Path):
    rc, data = _run(tmp_path)
    assert rc == 0
    # no-alpaca なので position 取得は空
    assert data["positions"] == []
    assert data["exits"] == []
    assert data["count"] == 0
