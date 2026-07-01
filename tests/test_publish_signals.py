"""publish_signals / signal_export / publishers の unit test (mock webhook)。

実際の HTTP は投げず、DiscordPublisher.publish(dry_run=True) が返す payload を
検証することで送信内容 (embed / footer の run_id / WARN badge) を確認する。
"""

from __future__ import annotations

import json

import pandas as pd
import pytest

from common.publishers import DiscordPublisher, SignalMessage, build_publisher
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
                    {"symbol": "AAPL", "side": "BUY", "entry_price": 289.24,
                     "weight": 0.2, "rank": 1, "reason": "SMA200 breakout"},
                    {"symbol": "MSFT", "side": "BUY", "entry_price": 512.6,
                     "weight": 0.18, "rank": 2, "reason": "ROC200"},
                ],
                "n_candidates_input": 20,
                "n_signals_output": 2,
                "gate_survival_ratio": 0.10,
            },
            "sys6": {  # 生存率 < 0.05 -> WARN
                "signals": [
                    {"symbol": "XOM", "side": "SELL", "entry_price": 118.2,
                     "weight": 0.05, "rank": 1, "reason": "6-day high"},
                ],
                "n_candidates_input": 100,
                "n_signals_output": 1,
                "gate_survival_ratio": 0.01,
            },
            "sys7": {
                "signals": [
                    {"symbol": "SPY", "side": "SELL", "entry_price": 641.8,
                     "weight": 0.06, "rank": 1, "reason": "hedge"},
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


def test_signal_message_accessors():
    msg = SignalMessage(payload=_sample_payload())
    assert msg.date == "2026-07-01"
    assert msg.run_id == "20260701_061523_abc123"
    assert msg.total_signals == 4
    assert msg.hedge and msg.hedge["symbol"] == "SPY"
    assert msg.has_warnings() is True  # sys6 生存率 0.01 < 0.05


def test_summary_lines_contain_signals_and_warn():
    msg = SignalMessage(payload=_sample_payload())
    lines = msg.system_summary_lines()
    joined = "\n".join(lines)
    assert "sys1" in joined and "AAPL BUY $289.24 (rank 1)" in joined
    assert "⚠️ WARN" in joined  # sys6


def test_discord_dry_run_payload():
    pub = DiscordPublisher(webhook_url="https://discord.com/api/webhooks/x/y")
    res = pub.publish(SignalMessage(payload=_sample_payload()), dry_run=True)
    assert res.ok is True
    payload = json.loads(res.detail)
    assert "embeds" in payload and len(payload["embeds"]) == 1
    embed = payload["embeds"][0]
    # WARN があるので title 先頭に ⚠️
    assert embed["title"].startswith("⚠️")
    # footer に run_id が載る (重複配信検出用)
    assert "20260701_061523_abc123" in embed["footer"]["text"]
    # Systems / Portfolio field が存在
    names = [f["name"] for f in embed["fields"]]
    assert "Systems" in names and "Portfolio" in names


def test_discord_no_warn_color_ok():
    payload = _sample_payload()
    # 全 system の生存率を閾値以上に上げる
    for sys_cfg in payload["systems"].values():
        sys_cfg["gate_survival_ratio"] = 0.5
    pub = DiscordPublisher(webhook_url="https://discord.com/api/webhooks/x/y")
    res = pub.publish(SignalMessage(payload=payload), dry_run=True)
    embed = json.loads(res.detail)["embeds"][0]
    assert not embed["title"].startswith("⚠️")
    assert embed["color"] == 0x4ADE80  # OK color


def test_discord_unconfigured_non_dry_run():
    pub = DiscordPublisher(webhook_url="")
    res = pub.publish(SignalMessage(payload=_sample_payload()), dry_run=False)
    assert res.ok is False
    assert "未設定" in res.detail


def test_build_publisher_factory_and_unknown():
    assert build_publisher("discord").name == "discord"
    assert build_publisher("webhook").name == "webhook"
    with pytest.raises(ValueError):
        build_publisher("carrier_pigeon")


def test_build_signals_json_from_dataframe():
    final_df = pd.DataFrame(
        [
            {"system": "System1", "symbol": "AAPL", "side": "long",
             "entry_price": 289.24, "shares": 10, "score": 5.0, "rank": 1,
             "reason": "SMA200 breakout"},
            {"system": "system7", "symbol": "SPY", "side": "short",
             "entry_price": 641.8, "shares": 3, "score": 1.0, "rank": 1},
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
    assert payload["date"] == "2026-07-01"
    assert payload["meta"]["run_id"] == "testrun_1"
    assert payload["systems"]["sys1"]["n_signals_output"] == 1
    assert payload["systems"]["sys1"]["n_candidates_input"] == 3
    # side long -> BUY, short -> SELL
    assert payload["systems"]["sys1"]["signals"][0]["side"] == "BUY"
    assert payload["systems"]["sys7"]["signals"][0]["side"] == "SELL"
    # hedge = sys7 SPY SELL
    assert payload["portfolio"]["hedge"]["symbol"] == "SPY"
    # weight は notional 比率で 0..1
    w = payload["systems"]["sys1"]["signals"][0]["weight"]
    assert w is None or 0.0 <= w <= 1.0


def test_generate_run_id_format():
    rid = generate_run_id()
    parts = rid.split("_")
    assert len(parts) == 3
    assert len(parts[0]) == 8 and len(parts[1]) == 6  # date_time
