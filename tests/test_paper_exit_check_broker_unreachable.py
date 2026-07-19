"""Regression: exit_check が broker 到達不能を silent success させないこと。

背景 (2026-07-19 audit):
    ``scripts/paper_exit_check.py`` は Alpaca client / positions の取得に失敗すると
    ``snapshots=[]`` で継続し、exit を 1 件も生成しないまま ``return 0`` していた。
    daily_pipeline から見ると「0 exits = 成功 (flat book)」と区別できず、market が
    閉じた後に transient outage が起きると exit が全滑りしても success に見え、
    position が滞留する温床だった (entry 側は既に ``PositionsFetchError`` で
    fail-closed していたのに exit 側だけ silent だった)。

    修正後は broker 到達不能を distinct exit code 3 + WARN + 出力 meta
    ``broker_unreachable=true`` で surface する。``--no-alpaca`` の意図的 offline や
    本当に position 0 件の flat book は 0 のまま (誤検知しない)。
"""

from __future__ import annotations

import json
from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alpaca_trading import PositionsFetchError  # noqa: E402
import scripts.paper_exit_check as pec  # noqa: E402


class _RaisingClient:
    """get_all_positions が transient outage で raise する fake client。"""

    def get_all_positions(self):  # noqa: D401
        raise RuntimeError("simulated Alpaca outage (503)")


class _FlatClient:
    def get_all_positions(self):
        return []


def _run_main(
    tmp_path: Path, monkeypatch, *, extra: list[str] | None = None
) -> tuple[int, dict]:
    out = tmp_path / "exit_orders_20260719.json"
    args = [
        "--date",
        "2026-07-19",
        "--output-json",
        str(out),
        "--results-dir",
        str(tmp_path),
    ]
    if extra:
        args.extend(extra)
    rc = pec.main(args)
    data = json.loads(out.read_text(encoding="utf-8")) if out.exists() else {}
    return rc, data


def test_client_fetch_failure_returns_3_and_flags(tmp_path: Path, monkeypatch):
    """ba.get_client が raise → broker_unreachable, exit 3。"""

    def _boom(*a, **k):
        raise RuntimeError("cannot construct client (no creds / network)")

    monkeypatch.setattr(pec.ba, "get_client", _boom)
    rc, data = _run_main(tmp_path, monkeypatch)
    assert rc == 3
    assert data.get("broker_unreachable") is True
    assert data.get("count") == 0


def test_positions_fetch_failure_returns_3_and_flags(tmp_path: Path, monkeypatch):
    """client は取れたが get_all_positions が raise → broker_unreachable, exit 3。"""
    monkeypatch.setattr(pec.ba, "get_client", lambda *a, **k: _RaisingClient())
    # coid fetchers は到達しない (PositionsFetchError で else ブロックに入らない)。
    rc, data = _run_main(tmp_path, monkeypatch)
    assert rc == 3
    assert data.get("broker_unreachable") is True
    assert data.get("count") == 0


def test_genuine_flat_book_returns_0_not_flagged(tmp_path: Path, monkeypatch):
    """client OK で positions 0 件 = 本当の flat book → 0, 未 flag (誤検知しない)。"""
    monkeypatch.setattr(pec.ba, "get_client", lambda *a, **k: _FlatClient())
    monkeypatch.setattr(pec, "fetch_existing_protect_coids", lambda c: set())
    monkeypatch.setattr(pec, "fetch_existing_exit_coids", lambda c: set())
    monkeypatch.setattr(pec, "_hydrate_from_alpaca_coids", lambda s, c: None)
    rc, data = _run_main(tmp_path, monkeypatch)
    assert rc == 0
    assert data.get("broker_unreachable") is False
    assert data.get("count") == 0


def test_no_alpaca_offline_is_not_flagged(tmp_path: Path, monkeypatch):
    """--no-alpaca の意図的 offline は broker_unreachable ではない (0 のまま)。"""
    rc, data = _run_main(tmp_path, monkeypatch, extra=["--no-alpaca"])
    assert rc == 0
    assert data.get("broker_unreachable") is False


def test_fetch_position_snapshots_raise_on_error_contract():
    """fetch_position_snapshots: raise_on_error で例外を surface / default は [] 後方互換。"""
    # default (False): silent [] を保つ (paper_trading_status など既存 caller 保護)。
    assert pec.fetch_position_snapshots(_RaisingClient()) == []
    # opt-in (True): PositionsFetchError を raise。
    with pytest.raises(PositionsFetchError):
        pec.fetch_position_snapshots(_RaisingClient(), raise_on_error=True)
