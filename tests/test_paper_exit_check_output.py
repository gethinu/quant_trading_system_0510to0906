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
    _load_ticker_renames,
    _resolve_rename_aliases,
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
    assert data.get("time_exit_due") == 0
    assert data.get("time_exit_unsubmitted") == 0
    assert data.get("execution_health") == "ok"


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


def _mf(qty: float = 100) -> PositionSnapshot:
    return PositionSnapshot(symbol="MF", qty=qty, side="long", avg_entry_price=4.7)


def test_ticker_rename_config_includes_mf_alias_for_exit_resolution():
    """open_auto_run 側でも MF の entry metadata を UBXG から解決できる。"""
    row = _load_ticker_renames()["MF"]
    assert row["canonical"] == "UBXG"
    assert row["qty"] == 100


def test_rename_alias_requires_holding_qty_to_match_config():
    """config を書いただけでは効かない。保有株数が qty と一致した時だけ採用する。"""
    renames = {"MF": {"canonical": "UBXG", "qty": 100.0}}
    assert _resolve_rename_aliases([_mf(100)], renames) == {"MF": "UBXG"}


def test_rename_alias_is_rejected_when_holding_qty_diverged(capsys):
    """部分決済 / 買い増し / 別物なら alias を捨て、unmanaged のまま残す。"""
    renames = {"MF": {"canonical": "UBXG", "qty": 100.0}}
    assert _resolve_rename_aliases([_mf(50)], renames) == {}
    # silent に落とさない (stale config が見えなくならないように)
    assert "qty 不一致" in capsys.readouterr().out


def test_rename_row_without_qty_evidence_is_not_loaded(tmp_path: Path):
    """qty は採用根拠そのもの。無い行は alias を作らない。"""
    p = tmp_path / "ticker_renames.json"
    p.write_text(
        json.dumps(
            {
                "renames": [
                    {"alias": "AAA", "canonical": "BBB"},
                    {"alias": "CCC", "canonical": "DDD", "qty": 0},
                    {"alias": "EEE", "canonical": "FFF", "qty": 7},
                ]
            }
        ),
        encoding="utf-8",
    )
    assert set(_load_ticker_renames(p)) == {"EEE"}


def test_unheld_alias_does_not_widen_artifact_lookback():
    """保有していない alias は採用しない (過去 artifact を無用に遡らせない)。"""
    renames = {"CHRN": {"canonical": "EKSO", "qty": 119.0}}
    assert _resolve_rename_aliases([_mf(100)], renames) == {}


def test_rename_config_keeps_ledger_uniqueness_invariant():
    """build_exit_ledger 側の二段ゲートの記述を exit 側の都合で消さない。

    この config は main では build_exit_ledger と共有される。片方の consumer の
    説明だけに書き換えると、merge 時にもう片方の不変条件が消える。
    """
    data = json.loads(
        (ROOT / "config" / "ticker_renames.json").read_text(encoding="utf-8")
    )
    safety = data["safety"]
    assert "一意に打ち消し合う" in safety
    assert "build_exit_ledger" in safety
    assert "paper_exit_check" in safety


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


def test_operational_gate_rejects_due_dry_run(monkeypatch, tmp_path: Path):
    """期限 exit の案だけ作って broker 未送信なら日次運用は成功扱いしない。"""
    from common.alpaca_trading import PreparedExit
    from scripts import paper_exit_check as pec

    due = PreparedExit(
        symbol="DUE",
        system="system2",
        qty=1,
        side="sell",
        order_type="market",
        reason="time_based",
        holding_days=3,
        max_holding_days=2,
    )
    monkeypatch.setattr(pec, "build_exit_orders_from_positions", lambda *a, **k: [due])
    out = tmp_path / "exit_orders_20260703.json"
    rc = pec.main(
        [
            "--no-alpaca",
            "--date",
            "2026-07-03",
            "--output-json",
            str(out),
            "--results-dir",
            str(tmp_path),
            "--fail-on-unsubmitted-time-exit",
        ]
    )
    data = json.loads(out.read_text(encoding="utf-8"))
    # 2026-08-25: exit=3 は broker_unreachable が先に使っているため、この
    # opt-in ゲートは別コード 4 で surface する (docs/RUNNER_RETIREMENT_20260822.md)。
    assert rc == 4
    assert data["time_exit_due"] == 1
    assert data["time_exit_unsubmitted"] == 1
    assert data["execution_health"] == "blocked_unsubmitted_time_exit"


def test_manual_dry_run_remains_zero_without_operational_gate(
    monkeypatch, tmp_path: Path
):
    """明示ゲート無しの手動シミュレーションは従来互換で exit=0。"""
    from common.alpaca_trading import PreparedExit
    from scripts import paper_exit_check as pec

    due = PreparedExit(
        symbol="DUE",
        system="system2",
        qty=1,
        side="sell",
        order_type="market",
        reason="time_based",
    )
    monkeypatch.setattr(pec, "build_exit_orders_from_positions", lambda *a, **k: [due])
    rc = pec.main(
        [
            "--no-alpaca",
            "--date",
            "2026-07-03",
            "--output-json",
            str(tmp_path / "manual.json"),
            "--results-dir",
            str(tmp_path),
        ]
    )
    assert rc == 0
