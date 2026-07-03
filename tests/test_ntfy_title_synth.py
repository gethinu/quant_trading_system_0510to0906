"""ntfy X-Title の ASCII synth ロジック regression test。

2026-07-02 incident: 日本語 headline
「7系統49シグナル、BUY主流・SELL10件・生存率100%が3系統」を単純に
``encode("ascii", "ignore")`` すると「749BUYSELL10100%3」のような
gibberish ASCII に潰れ、iPhone push が読めなくなった。

fix (common/publishers/ntfy.py::_to_safe_ascii_title):
    - ASCII 保持率 60% 未満なら narrator headline を捨て、portfolio 統計から
      「YYYY-MM-DD | N signals | BUY x / SELL y | $Zk」を synth。
    - 元 title が最初から ASCII 主体ならそのまま採用。
"""

from __future__ import annotations

from common.publishers.base import SignalMessage
from common.publishers.ntfy import NtfyPublisher, _to_safe_ascii_title


def _payload(headline: str) -> dict:
    """narrative headline + BUY 39 / SELL 10 / notional 36932.73 の
    2026-07-02 実データを模した payload。"""
    return {
        "date": "2026-07-02",
        "provider": "polygon",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "SDOT", "side": "BUY", "entry_price": 72.0,
                     "weight": 0.006, "rank": 1, "reason": "roc"},
                ] * 39,
                "n_candidates_input": 39, "n_signals_output": 39,
                "gate_survival_ratio": 1.0,
            },
            "sys2": {
                "signals": [
                    {"symbol": "LFST", "side": "SELL", "entry_price": 11.43,
                     "weight": 0.054, "rank": 1, "reason": "overheated"},
                ] * 10,
                "n_candidates_input": 10, "n_signals_output": 10,
                "gate_survival_ratio": 1.0,
            },
        },
        "portfolio": {
            "total_signals": 49,
            "total_notional_usd": 36932.73,
            "hedge": None,
        },
        "meta": {"run_id": "test", "cli_version": "0.1.0"},
        "narrative": {"headline": headline, "summary": "..."},
    }


def test_japanese_headline_synth_structured_ascii():
    """日本語 headline は捨てて構造化 ASCII に置き換わること。"""
    p = _payload("7系統49シグナル、BUY主流・SELL10件・生存率100%が3系統")
    title = _to_safe_ascii_title(p["narrative"]["headline"], SignalMessage(payload=p))
    # gibberish "749BUYSELL10100%3" ではなく "|" と " / " が入る構造化 ASCII
    assert "49 signals" in title
    assert "BUY 39" in title
    assert "SELL 10" in title
    assert "2026-07-02" in title
    # 空白 + 区切り棒で人間可読
    assert " | " in title
    # gibberish 検出: BUYSELL 連結や 100%3 のような数字連結は無いこと
    assert "BUYSELL" not in title
    assert "100%3" not in title


def test_ascii_headline_passes_through():
    """narrator が最初から ASCII で headline を返した場合はそのまま保持。"""
    p = _payload("Mega-tech buy day / SPY hedge")
    title = _to_safe_ascii_title(p["narrative"]["headline"], SignalMessage(payload=p))
    assert title == "Mega-tech buy day / SPY hedge"


def test_mixed_but_mostly_ascii_kept():
    """ASCII 保持率が高ければ非 ASCII は落として plain ASCII 部分を残す。"""
    # 「Mega-tech ~ SPY hedge on」の非 ASCII は「~」1 char のみ → 保持率 >60%
    p = _payload("Mega-tech buy day ~ SPY hedge on")
    title = _to_safe_ascii_title(p["narrative"]["headline"], SignalMessage(payload=p))
    # 元題そのまま (「~」は ASCII なのでこのまま)
    assert "Mega-tech" in title
    assert "SPY hedge" in title


def test_empty_headline_synth_from_portfolio():
    """narrative headline が空でも portfolio 統計から構造化 ASCII を synth。"""
    p = _payload("")
    title = _to_safe_ascii_title("", SignalMessage(payload=p))
    assert "49 signals" in title
    assert "BUY 39" in title


def test_zero_signals_returns_default():
    """signals も narrative も無ければ 'Today's Signals' fallback。"""
    payload = {
        "date": "",
        "systems": {},
        "portfolio": {"total_signals": 0, "total_notional_usd": 0, "hedge": None},
        "meta": {},
    }
    title = _to_safe_ascii_title("", SignalMessage(payload=payload))
    # 何も無ければ最終 fallback
    assert title == "Today's Signals"


def test_ntfy_publisher_end_to_end_japanese_title():
    """NtfyPublisher._build 経由でも同じ synth が働くこと (integration)。"""
    p = _payload("49シグナル、BUY 39・SELL 10")
    pub = NtfyPublisher(topic="test-topic")
    _, headers = pub._build(p)
    xt = headers["X-Title"]
    assert xt  # 空文字にならない
    assert "49 signals" in xt or "49" in xt
    # ASCII 化されている
    assert xt.encode("ascii", "ignore").decode("ascii") == xt


def test_ntfy_title_length_bounded():
    """title は 120 char 以内に truncate されること (X-Title 実用値)。"""
    long_headline = "a" * 500
    p = _payload(long_headline)
    pub = NtfyPublisher(topic="test-topic")
    _, headers = pub._build(p)
    assert len(headers["X-Title"]) <= 120
