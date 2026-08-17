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
from types import SimpleNamespace

from common.alpaca_trading import PositionSnapshot
from scripts.paper_exit_check import (
    _collect_entry_orders_index,
    _hydrate_from_alpaca_coids,
    _load_ticker_rename_aliases,
)

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "paper_exit_check.py"


def _run(tmp_path: Path, *extra_args: str) -> tuple[int, dict]:
    out = tmp_path / "exit_orders_20260703.json"
    args = [
        sys.executable,
        str(SCRIPT),
        "--no-alpaca",
        "--date",
        "2026-07-03",
        "--output-json",
        str(out),
        "--results-dir",
        str(tmp_path),
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


def test_ticker_rename_config_includes_mf_alias_for_exit_resolution():
    """open_auto_run 側でも MF の entry metadata を UBXG から解決できる。"""
    assert _load_ticker_rename_aliases()["MF"] == "UBXG"


def test_required_renamed_symbol_is_loaded_beyond_normal_lookback(tmp_path: Path):
    """35日超の UBXG entry も、MF alias が必要なら artifact から補える。"""
    for i in range(1, 31):
        (tmp_path / f"paper_orders_202608{i:02d}.json").write_text(
            '{"orders": []}', encoding="utf-8"
        )
    (tmp_path / "paper_orders_20260713.json").write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "symbol": "UBXG",
                        "system": "system3",
                        "entry_date": "2026-07-13",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    index = _collect_entry_orders_index(tmp_path, required_symbols={"UBXG"})
    assert index["UBXG"] == {"system": "system3", "entry_date": "2026-07-13"}


def test_alpaca_entry_coid_uses_rename_alias_when_broker_symbol_changed():
    """live path でも UBXG order metadata を MF position に補完する。"""
    order = SimpleNamespace(symbol="UBXG", client_order_id="system3-UBXG-20260713")
    client = SimpleNamespace(get_orders=lambda _request: [order])
    snap = PositionSnapshot(symbol="MF", qty=100, side="long", avg_entry_price=4.7)

    _hydrate_from_alpaca_coids([snap], client, symbol_aliases={"MF": "UBXG"})

    assert snap.system == "system3"
    assert snap.entry_date == "2026-07-13"
