"""2026-08-20 の「全部成功なのに全部失敗に見えた」3 バグの回帰テスト。

その夜の実測 (logs/open_run_20260820*, results_csv/):
  - flatten の close 41 件中 **39 件が HTTP 200 / order_id=None** (非同期受理)、
    2 件が HTTP 422 (CDTX/FOLD = INACTIVE asset)。
    -> 旧コードは ``st == 200 and oid`` を成功条件にして ``ok=0 failed=41``。
  - ``ok=0`` -> ``market_ids=[]`` -> ``wait_exit_fills`` が即 return し、
    非同期 fill の前に建玉を撮って ``positions 41->41``。
  - entry 47/47 が fill 済みなのに ``paper_orders`` の status は submit 時点の
    ``"OrderStatus.PENDING_NEW"`` のまま。recon は ``str(...).lower()`` で
    ``"orderstatus.pending_new"`` にしてから ``{"filled", ...}`` と突合するため
    ``entry_filled`` は **構造的に 0 から動けなかった**。

ここで固定する契約:
  1. HTTP 2xx の close は order_id の有無に関わらず「受理」。
  2. 422 等の実エラーは受理にしない (真のエラー経路は温存)。
  3. order_id が無くても、受理 symbol の建玉が消えるまで待ってから verify する。
  4. order status は enum / "OrderStatus.FILLED" / "FILLED" のどれでも
     "filled" に正規化され、recon が fill として数える。
"""

from __future__ import annotations

import argparse
import importlib.util
import json
from pathlib import Path
import sys
from types import SimpleNamespace

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.order_status import (  # noqa: E402
    is_filled,
    is_working,
    normalize_order_status,
)
from scripts.build_execution_recon import build_recon  # noqa: E402


def _load_module():
    path = ROOT / "scripts" / "open_auto_run.py"
    spec = importlib.util.spec_from_file_location("open_auto_run_close_fill_test", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


oar = _load_module()


def _args(**overrides) -> argparse.Namespace:
    base = dict(
        date="2026-08-20",
        min_signals=10,
        poll_timeout=0.0,
        dry_run=False,
        skip_signals=True,
        allow_closed=True,
        force=True,
        flatten_all=True,
        no_publish=True,
        primary_root=".",
        thin_aborts_run=False,
    )
    base.update(overrides)
    return argparse.Namespace(**base)


def _runner(tmp_path, monkeypatch, **overrides):
    monkeypatch.setattr(oar, "ROOT", tmp_path)
    runner = oar.Runner(_args(**overrides))
    monkeypatch.setattr(runner, "_client", lambda: SimpleNamespace())
    return runner


# --------------------------------------------------------------------------
# BUG 1: 非同期 200 (order_id なし) を成功として数える
# --------------------------------------------------------------------------
def _close_resp(symbol, status, order_id=None, body=None):
    """alpaca-py ``ClosePositionResponse`` の形だけ真似た stub。

    実物は ``order_id`` が Optional (既定 None)、``body`` が必須。
    """
    return SimpleNamespace(symbol=symbol, status=status, order_id=order_id, body=body)


def test_async_200_without_order_id_is_accepted():
    """2026-08-20 の 39 件そのもの: 200 かつ order_id=None は成功。"""
    parsed = oar.parse_close_response(_close_resp("ADVB", 200, order_id=None, body={}))
    assert parsed["accepted"] is True
    assert parsed["http_status"] == 200
    assert parsed["symbol"] == "ADVB"
    assert parsed["error"] is None


def test_200_with_order_id_is_accepted_and_id_kept():
    parsed = oar.parse_close_response(_close_resp("GE", 200, order_id="o-1"))
    assert parsed["accepted"] is True
    assert parsed["order_id"] == "o-1"


def test_order_id_recovered_from_body_order():
    """成功時の body は Order。top-level が空でも id を拾えれば fill 監視できる。"""
    parsed = oar.parse_close_response(_close_resp("HP", 200, order_id=None, body=SimpleNamespace(id="body-oid")))
    assert parsed["accepted"] is True
    assert parsed["order_id"] == "body-oid"


def test_422_inactive_asset_is_a_real_error():
    """CDTX/FOLD の実エラー経路は温存する (受理に化けさせない)。"""
    body = SimpleNamespace(code=40410000, message="asset CDTX is not active")
    parsed = oar.parse_close_response(_close_resp("CDTX", 422, body=body))
    assert parsed["accepted"] is False
    assert "not active" in str(parsed["error"])
    assert "40410000" in str(parsed["error"])


def test_missing_status_is_not_accepted():
    parsed = oar.parse_close_response(_close_resp("X", None, body={}))
    assert parsed["accepted"] is False


def test_flatten_counts_tonights_39_accepted_and_2_rejected(tmp_path, monkeypatch):
    """その夜の実データ配分 (200x39 + 422x2) を通すと ok=39 failed=2 になる。"""
    resps = [_close_resp(f"S{i:02d}", 200, order_id=None, body={}) for i in range(39)]
    resps += [
        _close_resp("CDTX", 422, body=SimpleNamespace(code=1, message="not active")),
        _close_resp("FOLD", 422, body=SimpleNamespace(code=1, message="not active")),
    ]
    runner = _runner(tmp_path, monkeypatch)
    runner.results.mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(
        runner,
        "_client",
        lambda: SimpleNamespace(close_all_positions=lambda cancel_orders=True: resps),
    )
    monkeypatch.setattr("common.alpaca_trading.fetch_position_snapshots", lambda _c: [], raising=False)

    runner._flatten_all_stage()

    assert runner.record["flatten_ok"] == 39, "旧実装はここが 0 だった"
    assert runner.record["flatten_failed"] == 2
    # order_id が無くても settle 待ちの対象として symbol は握っている。
    assert len(runner.pending_flat_symbols) == 39
    assert "CDTX" not in runner.pending_flat_symbols

    payload = json.loads(runner.exit_json.read_text(encoding="utf-8"))
    assert payload["submitted"] == 39
    assert payload["failed"] == 2
    assert sum(1 for e in payload["exits"] if e["accepted"]) == 39


# --------------------------------------------------------------------------
# BUG 2: order_id が無くても建玉が消えるまで待ってから verify する
# --------------------------------------------------------------------------
def test_wait_polls_positions_when_no_order_ids(tmp_path, monkeypatch):
    """order_id 0 件でも受理 symbol があれば監視を skip しない (BUG 2 の本丸)。"""
    runner = _runner(tmp_path, monkeypatch)
    runner.pending_flat_symbols = {"ADVB", "GE"}
    polls: list[int] = []
    # 1 回目はまだ建玉が残り、2 回目で消える (非同期 fill の再現)。
    responses = [
        [SimpleNamespace(symbol="ADVB"), SimpleNamespace(symbol="GE")],
        [],
    ]

    def _fetch(_client):
        polls.append(1)
        return responses[min(len(polls) - 1, len(responses) - 1)]

    monkeypatch.setattr("common.alpaca_trading.fetch_position_snapshots", _fetch, raising=False)
    snapped: list[str] = []
    monkeypatch.setattr(runner, "_snapshot_positions", lambda n: snapped.append(n))
    monkeypatch.setattr(oar.time, "sleep", lambda _s: None)
    # deadline を効かせないよう十分な猶予を与える。
    runner.args.poll_timeout = 30.0

    runner.wait_exit_fills([])

    assert polls, "建玉 poll が一度も走っていない = 旧実装の早期 return"
    assert snapped == ["positions_after_close.json"], "verify snapshot が撮られていない"
    assert runner.record["flatten_settled"] == 2
    assert runner.record["flatten_unsettled"] == []


def test_wait_records_unsettled_on_timeout(tmp_path, monkeypatch):
    """settle しなければ黙って成功に倒さず、未解消として記録する。"""
    runner = _runner(tmp_path, monkeypatch)
    runner.pending_flat_symbols = {"ADVB"}
    monkeypatch.setattr(
        "common.alpaca_trading.fetch_position_snapshots",
        lambda _c: [SimpleNamespace(symbol="ADVB")],
        raising=False,
    )
    monkeypatch.setattr(runner, "_snapshot_positions", lambda _n: None)
    monkeypatch.setattr(oar.time, "sleep", lambda _s: None)

    runner.wait_exit_fills([])

    assert runner.record["flatten_settled"] == 0
    assert runner.record["flatten_unsettled"] == ["ADVB"]


def test_wait_still_skips_when_nothing_to_watch(tmp_path, monkeypatch):
    """本当に close 0 のときは従来どおり skip (非退行)。"""
    runner = _runner(tmp_path, monkeypatch)
    runner.pending_flat_symbols = set()
    called: list[str] = []
    monkeypatch.setattr(runner, "_snapshot_positions", lambda n: called.append(n))

    runner.wait_exit_fills([])

    assert called == []


def test_wait_skips_in_dry_run(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, dry_run=True)
    runner.pending_flat_symbols = {"ADVB"}
    called: list[str] = []
    monkeypatch.setattr(runner, "_snapshot_positions", lambda n: called.append(n))

    runner.wait_exit_fills([])

    assert called == []


# --------------------------------------------------------------------------
# BUG 3: status 正規化 + 実 fill への再突合
# --------------------------------------------------------------------------
@pytest.mark.parametrize("raw", ["OrderStatus.FILLED", "FILLED", "filled", "  filled "])
def test_status_variants_normalize_to_filled(raw):
    assert normalize_order_status(raw) == "filled"
    assert is_filled(raw) is True
    assert is_working(raw) is False


def test_status_normalizes_alpaca_enum():
    """enum を直接渡しても素の token になる (書き手側の入口)。"""
    pytest.importorskip("alpaca")
    from alpaca.trading.enums import OrderStatus

    assert normalize_order_status(OrderStatus.FILLED) == "filled"
    assert normalize_order_status(OrderStatus.PENDING_NEW) == "pending_new"
    assert is_working(OrderStatus.PENDING_NEW) is True


@pytest.mark.parametrize("raw", [None, "", "None"])
def test_status_missing_is_empty_and_still_working(raw):
    assert normalize_order_status(raw) == ""
    assert is_working(raw) is True


def test_producer_writes_bare_token_not_enum_repr():
    """artifact に焼き付く status が "OrderStatus.*" でなく素の token であること。

    2026-08-20 の paper_orders_20260820.json は 47 件すべて
    ``"status": "OrderStatus.PENDING_NEW"`` だった。犯人は
    ``common/alpaca_trading.py`` の ``str(getattr(order, "status", ""))`` —
    ``OrderStatus`` は str 継承だが ``__str__`` が Enum 由来なので、明示的な
    ``str()`` は 'filled' ではなく 'OrderStatus.FILLED' を返す。
    """
    pytest.importorskip("alpaca")
    from alpaca.trading.enums import OrderStatus

    order = SimpleNamespace(id="o1", status=OrderStatus.FILLED)
    # 旧実装が焼き付けていた形 (これが recon の突合を外していた)。
    assert str(getattr(order, "status", "") or "") == "OrderStatus.FILLED"
    # 新実装。
    normalized = normalize_order_status(getattr(order, "status", None))
    assert normalized == "filled"
    assert json.loads(json.dumps({"status": normalized}, default=str))["status"] == ("filled")


def _recon_inputs(status):
    signals = {
        "date": "2026-08-20",
        "systems": {"system1": {"signals": [{"symbol": "AAPL", "side": "BUY"}]}},
    }
    paper = {"orders": [{"system": "system1", "side": "buy", "order_id": "o1", "status": status}]}
    return signals, paper


def test_recon_counts_enum_serialized_filled_status():
    """recon は "OrderStatus.FILLED" 形式でも fill として数える。"""
    signals, paper = _recon_inputs("OrderStatus.FILLED")
    recon = build_recon(signals=signals, paper_orders=paper, exit_orders=None)
    assert recon["portfolio"]["entry_submitted"] == 1
    assert recon["portfolio"]["entry_filled"] == 1


def test_recon_does_not_count_pending_new():
    """未 fill を fill に化けさせない (逆側の非退行)。"""
    signals, paper = _recon_inputs("OrderStatus.PENDING_NEW")
    recon = build_recon(signals=signals, paper_orders=paper, exit_orders=None)
    assert recon["portfolio"]["entry_submitted"] == 1
    assert recon["portfolio"]["entry_filled"] == 0


def test_recon_still_counts_plain_lowercase_status():
    signals, paper = _recon_inputs("filled")
    recon = build_recon(signals=signals, paper_orders=paper, exit_orders=None)
    assert recon["portfolio"]["entry_filled"] == 1


def test_reconcile_entry_fills_rewrites_status_from_broker(tmp_path, monkeypatch):
    """submit 時 pending_new -> 実 fill を再 poll して artifact を実状へ寄せる。"""
    pytest.importorskip("alpaca")
    from alpaca.trading.enums import OrderStatus

    runner = _runner(tmp_path, monkeypatch)
    runner.paper_json.parent.mkdir(parents=True, exist_ok=True)
    runner.paper_json.write_text(
        json.dumps(
            {
                "orders": [
                    {
                        "system": "system1",
                        "side": "buy",
                        "order_id": f"o{i}",
                        "status": "OrderStatus.PENDING_NEW",
                    }
                    for i in range(3)
                ]
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        "common.broker_alpaca.get_orders_status_map",
        lambda _c, ids: {oid: OrderStatus.FILLED for oid in ids},
        raising=False,
    )
    monkeypatch.setattr(oar.time, "sleep", lambda _s: None)

    runner.reconcile_entry_fills()

    data = json.loads(runner.paper_json.read_text(encoding="utf-8"))
    assert [o["status"] for o in data["orders"]] == ["filled"] * 3
    assert data["entry_filled"] == 3
    assert runner.record["entry_filled"] == 3

    # そのまま recon に食わせたら fill として数えられる (end-to-end の要)。
    signals = {
        "date": "2026-08-20",
        "systems": {"system1": {"signals": [{"symbol": f"S{i}", "side": "BUY"} for i in range(3)]}},
    }
    recon = build_recon(signals=signals, paper_orders=data, exit_orders=None)
    assert recon["portfolio"]["entry_filled"] == 3


def test_reconcile_entry_fills_keeps_unfilled_unfilled(tmp_path, monkeypatch):
    """broker が未 fill と答えたら fill に数えない。"""
    pytest.importorskip("alpaca")
    from alpaca.trading.enums import OrderStatus

    runner = _runner(tmp_path, monkeypatch)
    runner.paper_json.parent.mkdir(parents=True, exist_ok=True)
    runner.paper_json.write_text(
        json.dumps(
            {
                "orders": [
                    {"system": "system1", "side": "buy", "order_id": "o1"},
                    {"system": "system1", "side": "buy", "order_id": "o2"},
                ]
            }
        ),
        encoding="utf-8",
    )
    smap = {"o1": OrderStatus.FILLED, "o2": OrderStatus.CANCELED}
    monkeypatch.setattr(
        "common.broker_alpaca.get_orders_status_map",
        lambda _c, ids: {oid: smap[oid] for oid in ids},
        raising=False,
    )
    monkeypatch.setattr(oar.time, "sleep", lambda _s: None)

    runner.reconcile_entry_fills()

    data = json.loads(runner.paper_json.read_text(encoding="utf-8"))
    assert data["entry_filled"] == 1
    assert sorted(o["status"] for o in data["orders"]) == ["canceled", "filled"]


def test_reconcile_entry_fills_is_noop_without_artifact(tmp_path, monkeypatch):
    """artifact が無い/壊れていても run を落とさない。"""
    runner = _runner(tmp_path, monkeypatch)
    runner.reconcile_entry_fills()  # 例外なく戻ること
    assert "entry_filled" not in runner.record


def test_reconcile_entry_fills_skips_in_dry_run(tmp_path, monkeypatch):
    runner = _runner(tmp_path, monkeypatch, dry_run=True)
    runner.reconcile_entry_fills()
    assert "entry_filled" not in runner.record


def test_reconcile_entry_fills_bails_out_when_broker_is_blind(tmp_path, monkeypatch):
    """broker が全件無応答なら poll_timeout を空回りせず早期離脱する。

    reconcile は notify/publish の直前に走るので、broker 不達のときに
    既定 300s ぶん待つと dashboard 更新をまるごと遅らせてしまう。
    """
    runner = _runner(tmp_path, monkeypatch, poll_timeout=9999.0)
    runner.paper_json.parent.mkdir(parents=True, exist_ok=True)
    runner.paper_json.write_text(json.dumps({"orders": [{"side": "buy", "order_id": "o1"}]}), encoding="utf-8")
    polls: list[int] = []

    def _blind(_client, ids):
        polls.append(1)
        return {oid: None for oid in ids}  # get_orders_status_map の失敗時の形

    monkeypatch.setattr("common.broker_alpaca.get_orders_status_map", _blind, raising=False)
    monkeypatch.setattr(oar.time, "sleep", lambda _s: None)

    runner.reconcile_entry_fills()

    assert len(polls) == 3, "3 回で諦めるはず (無限に回らない)"
    # status が引けていないので artifact は書き換えない。
    data = json.loads(runner.paper_json.read_text(encoding="utf-8"))
    assert "status" not in data["orders"][0]
