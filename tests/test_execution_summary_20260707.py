"""execution summary formatter (recon → ntfy title/body) の検証。"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.publishers.execution_summary import (  # noqa: E402
    build_body,
    build_title,
    format_execution_summary,
)
from common.publishers.ntfy import (  # noqa: E402
    NtfyPublisher,
    _latin1_safe_headers,
    _sanitize_ascii_title,
)


def _recon() -> dict:
    return {
        "date": "2026-07-08",
        "inputs": {"signals": True, "paper_orders": True, "exit_orders": True},
        "portfolio": {
            "universe_target": 4123,
            "signals": 49,
            "orders_generated": 34,
            "entry_submitted": 27,
            "entry_filled": 25,
            "entry_failed": 1,
            "long_entry_submitted": 18,
            "short_entry_submitted": 9,
            "exit_submitted": 14,
            "exit_close": 5,
            "exit_protect": 9,
            "account_equity": 10120.0,
            "drop_breakdown": {"below_min_notional": 6, "short": 4, "fail": 1},
        },
        "systems": {
            "system1": {
                "long": {
                    "signals": 12,
                    "generated": 8,
                    "entry_submitted": 7,
                    "filled": 7,
                    "skipped": 1,
                    "failed": 0,
                },
                "short": {
                    "signals": 0,
                    "generated": 0,
                    "entry_submitted": 0,
                    "filled": 0,
                    "skipped": 0,
                    "failed": 0,
                },
                "exit": {"submitted": 2, "close": 0, "protect": 2},
                "funnel": None,
            },
            "system2": {
                "long": {
                    "signals": 0,
                    "generated": 0,
                    "entry_submitted": 0,
                    "filled": 0,
                    "skipped": 0,
                    "failed": 0,
                },
                "short": {
                    "signals": 9,
                    "generated": 5,
                    "entry_submitted": 3,
                    "filled": 3,
                    "skipped": 1,
                    "failed": 1,
                },
                "exit": {"submitted": 1, "close": 1, "protect": 0},
                "funnel": None,
            },
        },
    }


def test_title_is_ascii_plus_emoji_and_has_counts():
    title = build_title(_recon())
    # entry_failed>0 なので warning emoji
    assert "07-08" in title
    assert "sig49" in title
    assert "entry27" in title
    assert "exit14" in title
    # sanitize しても壊れない (ASCII+emoji のみ)
    assert _sanitize_ascii_title(title) == title


def test_body_has_funnel_and_per_system_and_drops():
    body = build_body(_recon())
    assert "Tgt 4123 → sig 49 → gen 34 → entry 27 → fill 25" in body
    assert "exit 14 (close 5 / protect 9)" in body
    assert "LONG entry 18 / SHORT entry 9" in body
    assert "資産 $10,120" in body
    # per-system: s1 long, s2 short
    assert "s1L 12→7" in body
    assert "s2S 9→3" in body
    # drop 内訳
    assert "below_min_notional 6" in body


def test_missing_inputs_noted():
    recon = _recon()
    recon["inputs"] = {"signals": True, "paper_orders": False, "exit_orders": False}
    body = build_body(recon)
    assert "入力欠損" in body
    assert "paper_orders" in body


def test_format_returns_tuple():
    title, body = format_execution_summary(_recon())
    assert isinstance(title, str) and isinstance(body, str)
    assert title and body


def test_latin1_safe_headers_strips_emoji_title():
    """emoji 入り X-Title は latin-1 エンコード可能に落とされること。

    2026-07-13 regression: build_title は「⚠️ 07-13 exec …」のように先頭 emoji を
    付ける。HTTP ヘッダーは latin-1 encode されるため、この title を素で
    requests に渡すと 'latin-1' codec can't encode で送信が丸ごと失敗する。
    """
    title = build_title(_recon())  # entry_failed=1 → "⚠️ …"
    # 素の title は latin-1 に載らない (バグの前提を固定)
    import pytest

    with pytest.raises(UnicodeEncodeError):
        title.encode("latin-1")

    safe = _latin1_safe_headers({"X-Title": title, "X-Tags": "bar_chart,warning"})
    # 落とした後は latin-1 でエンコードでき、count 情報は残る
    safe["X-Title"].encode("latin-1")  # raises しない
    assert "sig49" in safe["X-Title"]
    assert "entry27" in safe["X-Title"]
    assert "exit14" in safe["X-Title"]
    # ASCII tags はそのまま
    assert safe["X-Tags"] == "bar_chart,warning"


def test_send_text_emoji_title_does_not_raise(monkeypatch):
    """send_text が emoji title でも例外を投げず ntfy へ POST できること。

    修正前は requests.post のヘッダー encode で latin-1 例外が漏れ、
    exec summary 通知が 4 retry 全滅していた (open_auto_run 2026-07-13)。
    """
    captured: dict = {}

    class _Resp:
        status_code = 200
        text = "ok"

    def _fake_post(url, data=None, headers=None, timeout=None):
        # requests 本来の latin-1 ヘッダー encode を再現し、非 latin-1 が
        # 残っていれば例外を投げる (= バグが再発したらここで落ちる)。
        for value in (headers or {}).values():
            str(value).encode("latin-1")
        captured["headers"] = headers
        captured["url"] = url
        return _Resp()

    import requests

    monkeypatch.setattr(requests, "post", _fake_post)

    pub = NtfyPublisher(topic="unit-test-topic")
    title, body = format_execution_summary(_recon())  # title に emoji を含む
    result = pub.send_text(title, body, tags="bar_chart,warning")

    assert result.ok is True
    assert result.status_code == 200
    # POST に渡った X-Title は latin-1 safe
    captured["headers"]["X-Title"].encode("latin-1")
