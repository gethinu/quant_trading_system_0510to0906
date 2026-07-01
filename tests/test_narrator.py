"""SignalNarrator の単体テスト (mock Anthropic API)。

- happy path: JSON schema parse + cost 見積り + cross-check
- fail-safe: ANTHROPIC_API_KEY 無しで空 narrative + WARN (pipeline 継続)
- hallucination: signals に無い symbol 言及の warning / 乖離 >30% で fallback
- API error: 例外時も fallback narrative を返し pipeline を止めない
- live smoke: ANTHROPIC_API_KEY があるときだけ実 API を叩く (無ければ skip)
"""

from __future__ import annotations

import json
import os

import pytest

from common.narrator import (
    DEFAULT_MODEL,
    NarrativeResult,
    SignalNarrator,
)


# --------------------------------------------------------------------------
# fake Anthropic client
# --------------------------------------------------------------------------
class _Block:
    def __init__(self, text: str) -> None:
        self.type = "text"
        self.text = text


class _Usage:
    def __init__(self, input_tokens: int, output_tokens: int) -> None:
        self.input_tokens = input_tokens
        self.output_tokens = output_tokens
        self.cache_read_input_tokens = 0
        self.cache_creation_input_tokens = 0


class _Message:
    def __init__(self, text: str, usage: _Usage) -> None:
        self.content = [_Block(text)]
        self.usage = usage


class _FakeMessages:
    def __init__(self, text: str, usage: _Usage, raise_exc: Exception | None) -> None:
        self._text = text
        self._usage = usage
        self._raise = raise_exc
        self.calls: list[dict] = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self._raise is not None:
            raise self._raise
        return _Message(self._text, self._usage)


class _FakeClient:
    def __init__(
        self,
        text: str = "",
        usage: _Usage | None = None,
        raise_exc: Exception | None = None,
    ) -> None:
        self.messages = _FakeMessages(text, usage or _Usage(1000, 500), raise_exc)


def _signals(symbols=("AAPL", "MSFT"), hedge_symbol="SPY") -> dict:
    systems = {
        "sys1": {
            "signals": [
                {
                    "symbol": s,
                    "side": "BUY",
                    "entry_price": 100.0 + i,
                    "weight": 0.5,
                    "rank": i + 1,
                    "reason": "breakout",
                }
                for i, s in enumerate(symbols)
            ],
            "n_candidates_input": 10,
            "n_signals_output": len(symbols),
            "gate_survival_ratio": 0.5,
        },
        "sys7": {
            "signals": [
                {
                    "symbol": hedge_symbol,
                    "side": "SELL",
                    "entry_price": 400.0,
                    "weight": 1.0,
                    "rank": 1,
                    "reason": "hedge",
                }
            ],
            "n_candidates_input": 1,
            "n_signals_output": 1,
            "gate_survival_ratio": 1.0,
        },
    }
    return {
        "version": "1.0",
        "date": "2026-07-01",
        "provider": "polygon",
        "systems": systems,
        "portfolio": {
            "total_signals": len(symbols) + 1,
            "total_notional_usd": 10000.0,
            "hedge": {"symbol": hedge_symbol, "side": "SELL", "entry_price": 400.0},
        },
        "meta": {"cli_version": "0.1.0", "run_id": "20260701_060000_abc123"},
    }


def _json_reply(headline, summary, reasons) -> str:
    return json.dumps(
        {"headline": headline, "summary": summary, "per_symbol_reasons": reasons},
        ensure_ascii=False,
    )


# --------------------------------------------------------------------------
# happy path
# --------------------------------------------------------------------------
def test_narrate_parses_schema_and_marks_configured():
    reply = _json_reply(
        "メガテック集中買い",
        "sys1 が AAPL と MSFT を選好。sys7 は SPY でヘッジ。",
        {"AAPL": "SMA200 breakout", "MSFT": "出来高拡大"},
    )
    narrator = SignalNarrator(client=_FakeClient(reply, _Usage(1000, 500)))
    assert narrator.is_configured() is True

    res = narrator.narrate(_signals())
    assert isinstance(res, NarrativeResult)
    assert res.headline == "メガテック集中買い"
    assert "AAPL" in res.per_symbol_reasons
    assert res.per_symbol_reasons["MSFT"] == "出来高拡大"
    assert res.configured is True
    assert res.fallback is False
    assert res.warnings == []
    assert res.model == DEFAULT_MODEL
    assert res.elapsed_seconds >= 0.0


def test_cost_estimation_uses_haiku_pricing():
    # haiku 4.5 = $1/1M in, $5/1M out。1000 in + 500 out = 0.001 + 0.0025 = 0.0035
    narrator = SignalNarrator(client=_FakeClient(_json_reply("h", "s", {}), _Usage(1000, 500)))
    res = narrator.narrate(_signals())
    assert res.cost_usd == pytest.approx(0.0035, abs=1e-9)


def test_as_dict_shape_ready_for_meta_merge():
    narrator = SignalNarrator(client=_FakeClient(_json_reply("h", "s", {"AAPL": "r"})))
    d = narrator.narrate(_signals()).as_dict()
    for key in (
        "headline",
        "summary",
        "per_symbol_reasons",
        "model",
        "cost_usd",
        "elapsed_seconds",
        "warnings",
        "configured",
        "fallback",
    ):
        assert key in d


def test_parse_strips_json_code_fence():
    reply = "```json\n" + _json_reply("見出し", "本文", {"AAPL": "理由"}) + "\n```"
    narrator = SignalNarrator(client=_FakeClient(reply))
    res = narrator.narrate(_signals())
    assert res.headline == "見出し"
    assert res.per_symbol_reasons["AAPL"] == "理由"


# --------------------------------------------------------------------------
# hallucination / cross-check
# --------------------------------------------------------------------------
def test_cross_check_warns_on_unknown_symbol_without_fallback():
    # 5 言及中 1 個 (TSLA) だけ未知 -> ratio 0.2 <= 0.30 -> warning only
    sig = _signals(symbols=("AAPL", "MSFT", "GOOG", "NVDA"))
    reasons = {
        "AAPL": "a",
        "MSFT": "b",
        "GOOG": "c",
        "NVDA": "d",
        "TSLA": "not in signals",
    }
    narrator = SignalNarrator(client=_FakeClient(_json_reply("h", "sys1 selection", reasons)))
    res = narrator.narrate(sig)
    assert res.fallback is False
    assert any("TSLA" in w for w in res.warnings)


def test_hallucination_over_threshold_triggers_fallback():
    # 言及 symbol が全て未知 -> ratio 1.0 > 0.30 -> fallback narrative
    sig = _signals(symbols=("AAPL",))
    reasons = {"FAKE1": "x", "FAKE2": "y"}
    narrator = SignalNarrator(client=_FakeClient(_json_reply("bogus", "bogus body", reasons)))
    res = narrator.narrate(sig)
    assert res.fallback is True
    assert any("fallback" in w for w in res.warnings)
    # fallback narrative は template (systems 数を含む)
    assert "systems" in res.headline


# --------------------------------------------------------------------------
# fail-safe
# --------------------------------------------------------------------------
def test_fail_safe_without_api_key(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    narrator = SignalNarrator(api_key="")  # 明示的に未設定
    assert narrator.is_configured() is False

    res = narrator.narrate(_signals())
    assert res.configured is False
    assert res.is_empty()
    assert any("ANTHROPIC_API_KEY" in w for w in res.warnings)


def test_api_error_returns_fallback_not_raise():
    narrator = SignalNarrator(client=_FakeClient(raise_exc=RuntimeError("boom")))
    res = narrator.narrate(_signals())
    assert res.fallback is True
    assert any("api_error" in w for w in res.warnings)
    assert not res.is_empty()  # template narrative が入る


def test_model_from_env(monkeypatch):
    monkeypatch.setenv("NARRATOR_MODEL", "claude-sonnet-5")
    narrator = SignalNarrator(client=_FakeClient(_json_reply("h", "s", {})))
    assert narrator.model == "claude-sonnet-5"
    # sonnet pricing = $3/1M in, $15/1M out。1000 in + 500 out = 0.003 + 0.0075 = 0.0105
    res = narrator.narrate(_signals())
    assert res.cost_usd == pytest.approx(0.0105, abs=1e-9)


# --------------------------------------------------------------------------
# live smoke (ANTHROPIC_API_KEY があるときだけ)
# --------------------------------------------------------------------------
@pytest.mark.skipif(
    not os.getenv("ANTHROPIC_API_KEY"),
    reason="ANTHROPIC_API_KEY 未設定 (live smoke skip)",
)
def test_live_smoke_real_api():
    narrator = SignalNarrator()  # 実 client
    res = narrator.narrate(_signals())
    assert res.configured is True
    assert not res.is_empty()
    assert res.cost_usd > 0.0
    print(f"\n[live smoke] cost=${res.cost_usd:.6f} model={res.model} headline={res.headline!r}")
