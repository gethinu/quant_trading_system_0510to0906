"""SignalNarrator (common/narrator.py) の unit test。

実 API は投げず、``anthropic`` SDK を fake module で差し替えて narrate() を
検証する。3 系統:
    1. 正常系: mock API から headline/summary/cost を組み立てる。
    2. fail-safe: ANTHROPIC_API_KEY 無しで空 dict + WARN。
    3. hallucination: JSON に無い symbol を大量に言及 -> fallback template。

publisher 統合 (narrative を X-Title / email HTML に載せる) も dry_run で検証。
"""

from __future__ import annotations

import sys
import types

import pytest

from common.narrator import SignalNarrator


# --- fake anthropic SDK -----------------------------------------------------
class _FakeUsage:
    def __init__(self, in_tok: int, out_tok: int) -> None:
        self.input_tokens = in_tok
        self.output_tokens = out_tok


class _FakeBlock:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeResponse:
    def __init__(self, text: str, in_tok: int = 1200, out_tok: int = 300) -> None:
        self.content = [_FakeBlock(text)]
        self.usage = _FakeUsage(in_tok, out_tok)


class _FakeMessages:
    def __init__(self, text: str) -> None:
        self._text = text
        self.last_kwargs: dict | None = None

    def create(self, **kwargs):
        self.last_kwargs = kwargs
        return _FakeResponse(self._text)


class _FakeClient:
    def __init__(self, text: str) -> None:
        self.messages = _FakeMessages(text)


def _install_fake_anthropic(monkeypatch, response_text: str) -> _FakeClient:
    """sys.modules に fake anthropic を差し込み、生成した client を返す。"""
    client_holder: dict = {}

    def _anthropic_factory(api_key=None):
        client = _FakeClient(response_text)
        client_holder["client"] = client
        return client

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _anthropic_factory  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)
    return client_holder  # dict filled on narrate()


def _sample_signals() -> dict:
    return {
        "version": "1.0",
        "date": "2026-07-01",
        "provider": "polygon",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "AAPL", "side": "BUY", "entry_price": 289.2,
                     "weight": 0.18, "rank": 1, "reason": "SMA200 breakout"},
                    {"symbol": "MSFT", "side": "BUY", "entry_price": 512.6,
                     "weight": 0.15, "rank": 2, "reason": "ROC200 momentum"},
                ],
                "n_candidates_input": 21, "n_signals_output": 2,
                "gate_survival_ratio": 0.143,
            },
            "sys7": {
                "signals": [
                    {"symbol": "SPY", "side": "SELL", "entry_price": 641.8,
                     "weight": 0.06, "rank": 1, "reason": "Catastrophe hedge"},
                ],
                "n_candidates_input": 1, "n_signals_output": 1,
                "gate_survival_ratio": 1.0,
            },
        },
        "portfolio": {
            "total_signals": 3,
            "total_notional_usd": 50000.0,
            "hedge": {"symbol": "SPY", "side": "SELL", "entry_price": 641.8},
        },
        "meta": {"run_id": "test", "cli_version": "0.1.0"},
    }


# --- 1. 正常系 --------------------------------------------------------------
def test_narrate_success(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    resp_text = (
        "HEADLINE: メガテック集中買い、SPY hedge on\n"
        "sys1 は AAPL と MSFT を SMA200 breakout で買い。\n"
        "sys7 は SPY short で catastrophe hedge を発火。"
    )
    _install_fake_anthropic(monkeypatch, resp_text)

    narrator = SignalNarrator()
    assert narrator.is_configured() is True

    result = narrator.narrate(_sample_signals())

    assert result, "narrate は非空 dict を返すべき"
    assert result["headline"] == "メガテック集中買い、SPY hedge on"
    assert "AAPL" in result["summary"] or "sys1" in result["summary"]
    assert result["model"] == "claude-haiku-4-5-20251001"
    assert result["cost_usd"] > 0
    assert result["elapsed_seconds"] >= 0
    # per_symbol_reasons は signals JSON の reason を反映
    assert result["per_symbol_reasons"].get("AAPL") == "SMA200 breakout"
    assert not result.get("fallback")


def test_narrate_uses_model_override(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    _install_fake_anthropic(monkeypatch, "HEADLINE: x\nbody AAPL SPY")
    narrator = SignalNarrator(model="claude-sonnet-5")
    result = narrator.narrate(_sample_signals())
    assert result["model"] == "claude-sonnet-5"


# --- 2. fail-safe -----------------------------------------------------------
def test_narrate_no_api_key_returns_empty(monkeypatch, caplog):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    narrator = SignalNarrator()
    assert narrator.is_configured() is False

    with caplog.at_level("WARNING"):
        result = narrator.narrate(_sample_signals())

    assert result == {}
    assert any("ANTHROPIC_API_KEY" in rec.message for rec in caplog.records)


def test_narrate_sdk_missing_returns_empty(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # anthropic import を失敗させる
    monkeypatch.setitem(sys.modules, "anthropic", None)
    narrator = SignalNarrator()
    with caplog.at_level("WARNING"):
        result = narrator.narrate(_sample_signals())
    assert result == {}


def test_narrate_api_exception_returns_empty(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

    class _BoomMessages:
        def create(self, **kwargs):
            raise RuntimeError("boom")

    class _BoomClient:
        def __init__(self, api_key=None):
            self.messages = _BoomMessages()

    fake_mod = types.ModuleType("anthropic")
    fake_mod.Anthropic = _BoomClient  # type: ignore[attr-defined]
    monkeypatch.setitem(sys.modules, "anthropic", fake_mod)

    with caplog.at_level("WARNING"):
        result = SignalNarrator().narrate(_sample_signals())
    assert result == {}


# --- 3. hallucination -------------------------------------------------------
def test_narrate_hallucination_falls_back(monkeypatch, caplog):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")
    # JSON に無い symbol (TSLA/COIN/AMZN/GOOG/NFLX) を大量に言及
    resp_text = (
        "HEADLINE: TSLA COIN AMZN 急騰\n"
        "本日は TSLA と COIN と AMZN と GOOG と NFLX が急騰し全面高。"
    )
    _install_fake_anthropic(monkeypatch, resp_text)

    with caplog.at_level("WARNING"):
        result = SignalNarrator().narrate(_sample_signals())

    assert result.get("fallback") is True
    assert "across" in result["headline"]  # "N signals across M systems"
    assert any("hallucination" in rec.message.lower() for rec in caplog.records)


def test_hallucination_ratio_direct():
    narrator = SignalNarrator()
    signals = _sample_signals()
    clean = {"headline": "AAPL MSFT strong", "summary": "SPY hedge", "per_symbol_reasons": {}}
    assert narrator._hallucination_ratio(clean, signals) == 0.0
    dirty = {"headline": "TSLA COIN NFLX", "summary": "", "per_symbol_reasons": {}}
    assert narrator._hallucination_ratio(dirty, signals) > 0.30


# --- publisher 統合 ---------------------------------------------------------
def test_ntfy_includes_narrative(monkeypatch):
    from common.publishers.ntfy import NtfyPublisher

    payload = _sample_signals()
    payload["narrative"] = {
        "headline": "Mega-tech buy day",
        "summary": "sys1 concentrates AAPL/MSFT. sys7 hedges SPY.",
    }
    pub = NtfyPublisher(topic="test-topic")
    body, headers = pub._build(payload)
    # ASCII headline は X-Title に採用される
    assert headers["X-Title"] == "Mega-tech buy day"
    # body にも narrative が載る
    assert "sys1 concentrates" in body


def test_email_includes_narrative():
    from common.publishers.email import EmailPublisher

    payload = _sample_signals()
    payload["narrative"] = {
        "headline": "メガテック集中買い",
        "summary": "sys1 は AAPL/MSFT を買い。",
    }
    pub = EmailPublisher(api_key="k", from_email="a@b.com", to_emails="c@d.com")
    result = pub.send(payload, dry_run=True)
    assert result.ok
    assert "メガテック集中買い" in result.detail
    assert "AI narrator" in result.detail


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
