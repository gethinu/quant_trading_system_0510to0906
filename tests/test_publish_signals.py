"""publishers (ntfy/email) + registry + signal_export の unit test。

実 HTTP は投げず dry_run=True の payload を検証する。registry は fake publisher
で primary/secondary の chain (fallback / always) と status 判定を検証する。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from common.publishers import (
    EmailPublisher,
    NtfyPublisher,
    PublisherRegistry,
    SignalMessage,
    build_publisher,
)
from common.publishers.base import Publisher, PublishResult
from common.signal_export import build_signals_json, generate_run_id


def _sample_payload() -> dict:
    return {
        "version": "1.0",
        "date": "2026-07-01",
        "generated_at": "2026-07-01T06:15:23+09:00",
        "provider": "polygon",
        "systems": {
            "sys1": {
                "signals": [
                    {
                        "symbol": "AAPL",
                        "side": "BUY",
                        "entry_price": 289.24,
                        "weight": 0.2,
                        "rank": 1,
                        "reason": "SMA200 breakout",
                    },
                    {
                        "symbol": "MSFT",
                        "side": "BUY",
                        "entry_price": 512.6,
                        "weight": 0.18,
                        "rank": 2,
                        "reason": "ROC200",
                    },
                ],
                "n_candidates_input": 20,
                "n_signals_output": 2,
                "gate_survival_ratio": 0.10,
            },
            "sys6": {  # 生存率 < 0.05 -> WARN
                "signals": [
                    {
                        "symbol": "XOM",
                        "side": "SELL",
                        "entry_price": 118.2,
                        "weight": 0.05,
                        "rank": 1,
                        "reason": "6-day high",
                    },
                ],
                "n_candidates_input": 100,
                "n_signals_output": 1,
                "gate_survival_ratio": 0.01,
            },
            "sys7": {
                "signals": [
                    {
                        "symbol": "SPY",
                        "side": "SELL",
                        "entry_price": 641.8,
                        "weight": 0.06,
                        "rank": 1,
                        "reason": "hedge",
                    },
                ],
                "n_candidates_input": 1,
                "n_signals_output": 1,
                "gate_survival_ratio": 1.0,
            },
        },
        "portfolio": {
            "total_signals": 4,
            "total_notional_usd": 50000.0,
            "hedge": {"symbol": "SPY", "side": "SELL", "entry_price": 641.8},
        },
        "meta": {
            "cli_version": "0.1.0",
            "run_id": "20260701_061523_abc123",
            "elapsed_seconds": 47.3,
        },
    }


# --- SignalMessage ---------------------------------------------------------


def test_signal_message_accessors_and_warn():
    msg = SignalMessage(payload=_sample_payload())
    assert msg.date == "2026-07-01"
    assert msg.run_id == "20260701_061523_abc123"
    assert msg.total_signals == 4
    assert msg.hedge and msg.hedge["symbol"] == "SPY"
    assert msg.has_warnings() is True  # sys6 0.01 < 0.05
    joined = "\n".join(msg.system_summary_lines())
    assert "AAPL BUY $289.24 (rank 1)" in joined
    assert "⚠️ WARN" in joined


# --- ntfy ------------------------------------------------------------------


def test_ntfy_dry_run_headers_and_action():
    pub = NtfyPublisher(topic="quant-test-abc", priority=4)
    assert pub.is_configured() is True
    res = pub.send(_sample_payload(), dry_run=True)
    assert res.ok is True
    dump = json.loads(res.detail)
    assert dump["endpoint"].endswith("/quant-test-abc")
    h = dump["headers"]
    # WARN があるので priority=5 (urgent) + warning tag
    assert h["X-Priority"] == "5"
    assert "warning" in h["X-Tags"]
    assert "chart_with_upwards_trend" in h["X-Tags"]
    # dashboard へ jump する Action header
    assert "quant-trading-monitor.vercel.app" in h["X-Actions"]
    assert "AAPL BUY $289.24" in dump["body"]
    assert "20260701_061523_abc123" in dump["body"]  # run_id footer


def test_ntfy_priority_when_no_warn():
    payload = _sample_payload()
    for s in payload["systems"].values():
        s["gate_survival_ratio"] = 0.5
    pub = NtfyPublisher(topic="t", priority=4)
    h = json.loads(pub.send(payload, dry_run=True).detail)["headers"]
    assert h["X-Priority"] == "4"
    assert "warning" not in h["X-Tags"]


def test_ntfy_unconfigured():
    pub = NtfyPublisher(topic="")
    assert pub.is_configured() is False
    res = pub.send(_sample_payload(), dry_run=False)
    assert res.ok is False and "NTFY_TOPIC" in res.detail


# --- email (SendGrid) ------------------------------------------------------


def test_email_dry_run_payload():
    pub = EmailPublisher(
        api_key="SG.x", from_email="bot@ex.com", to_emails="a@ex.com,b@ex.com"
    )
    assert pub.is_configured() is True
    res = pub.send(_sample_payload(), dry_run=True)
    body = json.loads(res.detail)
    assert body["from"]["email"] == "bot@ex.com"
    assert [p["email"] for p in body["personalizations"][0]["to"]] == [
        "a@ex.com",
        "b@ex.com",
    ]
    types = [c["type"] for c in body["content"]]
    assert "text/plain" in types and "text/html" in types
    assert "2026-07-01" in body["subject"]


def test_email_unconfigured():
    pub = EmailPublisher(api_key="", from_email="", to_emails="")
    assert pub.is_configured() is False
    res = pub.send(_sample_payload(), dry_run=False)
    assert res.ok is False


# --- registry chain --------------------------------------------------------


class _FakePublisher(Publisher):
    def __init__(self, name: str, ok: bool):
        self.name = name
        self._ok = ok
        self.called = False

    def is_configured(self) -> bool:
        return True

    def send(self, signals_json, *, dry_run=False) -> PublishResult:
        self.called = True
        return PublishResult(publisher=self.name, ok=self._ok, detail="fake")


def test_registry_primary_ok_skips_secondary():
    p, s = _FakePublisher("ntfy", True), _FakePublisher("email", True)
    reg = PublisherRegistry(primary=p, secondary=s)  # fallback only
    res = reg.publish(_sample_payload())
    assert res.status == "ok"
    assert p.called and not s.called  # primary 成功で secondary 発火せず


def test_registry_fallback_on_primary_fail():
    p, s = _FakePublisher("ntfy", False), _FakePublisher("email", True)
    reg = PublisherRegistry(primary=p, secondary=s)
    res = reg.publish(_sample_payload())
    assert res.status == "partial"  # primary fail + secondary ok
    assert p.called and s.called


def test_registry_all_fail():
    p, s = _FakePublisher("ntfy", False), _FakePublisher("email", False)
    reg = PublisherRegistry(primary=p, secondary=s)
    res = reg.publish(_sample_payload())
    assert res.status == "failed"


def test_registry_always_secondary():
    p, s = _FakePublisher("ntfy", True), _FakePublisher("email", True)
    reg = PublisherRegistry(primary=p, secondary=s, always_secondary=True)
    res = reg.publish(_sample_payload())
    assert res.status == "ok"
    assert p.called and s.called  # 両方常時送信


# --- factory + signal_export ----------------------------------------------


def test_build_publisher_factory_and_unknown():
    assert build_publisher("ntfy", topic="t").name == "ntfy"
    assert build_publisher("email").name == "email"
    with pytest.raises(ValueError):
        build_publisher("carrier_pigeon")


def test_build_signals_json_from_dataframe():
    final_df = pd.DataFrame(
        [
            {
                "system": "System1",
                "symbol": "AAPL",
                "side": "long",
                "entry_price": 289.24,
                "shares": 10,
                "score": 5.0,
                "rank": 1,
                "reason": "SMA200 breakout",
            },
            {
                "system": "system7",
                "symbol": "SPY",
                "side": "short",
                "entry_price": 641.8,
                "shares": 3,
                "score": 1.0,
                "rank": 1,
            },
        ]
    )
    per_system = {
        "System1": pd.DataFrame([{"symbol": "AAPL"}, {"symbol": "X"}, {"symbol": "Y"}]),
        "System7": pd.DataFrame([{"symbol": "SPY"}]),
    }
    payload = build_signals_json(
        final_df, per_system, date_str="2026-07-01", run_id="testrun_1"
    )
    assert payload["version"] == "1.0"
    assert payload["meta"]["run_id"] == "testrun_1"
    assert payload["systems"]["sys1"]["n_signals_output"] == 1
    assert payload["systems"]["sys1"]["n_candidates_input"] == 3
    assert payload["systems"]["sys1"]["signals"][0]["side"] == "BUY"
    assert payload["systems"]["sys7"]["signals"][0]["side"] == "SELL"
    assert payload["portfolio"]["hedge"]["symbol"] == "SPY"


def test_generate_run_id_format():
    parts = generate_run_id().split("_")
    assert len(parts) == 3 and len(parts[0]) == 8 and len(parts[1]) == 6
