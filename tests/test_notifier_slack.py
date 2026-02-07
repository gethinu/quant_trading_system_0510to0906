from __future__ import annotations

from common import notifier


def test_slack_send_text_only_once(monkeypatch):
    call_count = 0

    class DummyClient:
        def __init__(self, token: str):
            pass

        def chat_postMessage(self, *, channel: str, text: str, blocks=None):
            nonlocal call_count
            call_count += 1

    monkeypatch.setenv("SLACK_BOT_TOKEN", "x")
    monkeypatch.setenv("SLACK_CHANNEL", "C123")
    monkeypatch.delenv("PYTEST_CURRENT_TEST", raising=False)
    monkeypatch.setattr(notifier, "WebClient", DummyClient)

    n = notifier.SimpleSlackNotifier()
    assert n._slack_send_text("hello") is True
    assert call_count == 1


def test_create_notifier_falls_back_to_discord(monkeypatch):
    monkeypatch.delenv("SLACK_BOT_TOKEN", raising=False)
    monkeypatch.setenv("DISCORD_WEBHOOK_URL", "https://discord.com/api/webhooks/test")

    n = notifier.create_notifier(platform="slack", fallback=True)

    assert isinstance(n, notifier.Notifier)
    assert n.platform == "discord"
