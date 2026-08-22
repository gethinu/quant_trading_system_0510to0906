"""broker が受け付けない銘柄を entry 失敗ではなく skip に分類する検証。

2026-08-18 の run で `sys3 JZ buy` が Alpaca の code 42210000
(asset not tradable) で **failed** として記録された。実測でも
``JZ: tradable=False`` (上場廃止等)。

発注して失敗させると:
  - 「本当に失敗した entry」がノイズに埋もれる (exit 側と同じ問題)
  - 無駄な発注往復が起きる
ので、submit 境界で ``untradable`` として skip 分類する
(既存の already_held / standing_cap と同じ扱い)。

**不明 (None) は skip しない**: broker 到達失敗を「取引不可」と解釈すると、
一時的な障害で全 entry が止まる。False の時だけ弾く。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import common.alpaca_trading as at  # noqa: E402


class _Asset:
    def __init__(self, tradable, fractionable=True):
        self.tradable = tradable
        self.fractionable = fractionable


class _Client:
    def __init__(self, mapping):
        self.mapping = mapping
        self.calls = []

    def get_asset(self, symbol):
        self.calls.append(symbol)
        if symbol not in self.mapping:
            raise RuntimeError("unknown asset")
        return self.mapping[symbol]


@pytest.fixture(autouse=True)
def _clear_caches():
    at._FRACTIONABLE_CACHE.clear()
    at._TRADABLE_CACHE.clear()
    yield
    at._FRACTIONABLE_CACHE.clear()
    at._TRADABLE_CACHE.clear()


def test_untradable_symbol_is_reported_as_false():
    c = _Client({"JZ": _Asset(False, False)})
    assert at.get_asset_tradable(c, "JZ") is False


def test_tradable_symbol_is_reported_as_true():
    c = _Client({"AAPL": _Asset(True, True)})
    assert at.get_asset_tradable(c, "AAPL") is True


def test_lookup_failure_is_unknown_not_untradable():
    """broker 到達失敗を「取引不可」と混同しない (全 entry を止めないため)。"""
    c = _Client({})
    assert at.get_asset_tradable(c, "X") is None


def test_tradable_and_fractionable_share_one_asset_call():
    """追加の API 往復を作らない (同じ get_asset 応答を共有)。"""
    c = _Client({"AAPL": _Asset(True, True)})
    assert at.get_asset_tradable(c, "AAPL") is True
    assert at.get_asset_fractionable(c, "AAPL") is True
    assert c.calls == ["AAPL"]


def test_repeated_lookups_are_cached():
    c = _Client({"AAPL": _Asset(True, True)})
    for _ in range(3):
        at.get_asset_tradable(c, "AAPL")
    assert c.calls == ["AAPL"]


def test_empty_symbol_is_unknown():
    c = _Client({})
    assert at.get_asset_tradable(c, "") is None
    assert c.calls == []


def test_missing_tradable_attribute_is_unknown():
    """asset に tradable が無い SDK 差異でも False と断定しない。"""

    class NoFlag:
        fractionable = True

    c = _Client({"X": NoFlag()})
    assert at.get_asset_tradable(c, "X") is None


def test_submit_path_skips_untradable_before_ordering():
    """submit 境界で untradable が skip 分類され、発注が試行されないこと。"""
    src = (ROOT / "common" / "alpaca_trading.py").read_text(encoding="utf-8")
    assert 'po.skip_reason = "untradable:not_tradable_at_broker"' in src
    # skip は submit の *前* に置く (発注してから失敗させない)
    skip_at = src.index('po.skip_reason = "untradable:not_tradable_at_broker"')
    plan_at = src.index("mode, qty, notional, reason = plan_order_execution(")
    assert skip_at < plan_at
    # False の時だけ弾く (unknown で全 entry を止めない)
    assert "get_asset_tradable(client, po.symbol) is False" in src
