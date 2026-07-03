"""narrator.py の headline validation + synth の regression test (v2)。

2026-07-02 の subscriber pitch review で narrator の headline が長すぎて
ntfy X-Title に収まらず ASCII gibberish (「749BUYSELL10100%3」) に潰れる
問題が発覚。fix は 2 段構え:
    1. LLM prompt (`_SYSTEM_PROMPT` + `_build_user_prompt`) に format 制約
       (ASCII+emoji, 25-50 字, ' / ' 区切り) を明示。
    2. narrator.narrate() が LLM 出力を post-validation し、format 違反
       (日本語混入 or 長すぎ) なら `_synth_headline` で決定論的に synth。

test 対象:
    - _is_valid_headline: 日本語混入は False、絵文字+ASCII は True。
    - _synth_headline: portfolio 統計から書式通りの ASCII+emoji を返す。
    - _SYSTEM_PROMPT / _build_user_prompt: 25-50 字制約と / 区切りの明示継続。
"""

from __future__ import annotations

from common.narrator import (
    SignalNarrator,
    _HEADLINE_MAX_LEN,
    _SYSTEM_PROMPT,
    _is_valid_headline,
    _synth_headline,
)


def _payload_2026_07_02() -> dict:
    """2026-07-02 実データ相当 (49 signals / BUY 39 / SELL 10 / $37K)。"""
    return {
        "version": "1.0",
        "date": "2026-07-02",
        "provider": "polygon",
        "systems": {
            "sys1": {
                "signals": [
                    {"symbol": "AAPL", "side": "BUY", "entry_price": 289.2,
                     "weight": 0.18, "rank": i, "reason": "roc"}
                    for i in range(39)
                ],
                "n_candidates_input": 39, "n_signals_output": 39,
                "gate_survival_ratio": 1.0,
            },
            "sys2": {
                "signals": [
                    {"symbol": "LFST", "side": "SELL", "entry_price": 11.43,
                     "weight": 0.054, "rank": i, "reason": "overheat"}
                    for i in range(10)
                ],
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
    }


# --- validator -------------------------------------------------------------
class TestIsValidHeadline:
    def test_valid_emoji_ascii(self):
        assert _is_valid_headline("📈 07-02 49 signals / BUY:39 SELL:10 / $37K")

    def test_valid_plain_ascii(self):
        assert _is_valid_headline("07-02 49 signals / BUY:39 SELL:10")

    def test_reject_japanese(self):
        # 2026-07-02 の実例: 日本語混入 → mangled ASCII に潰れるので reject。
        assert not _is_valid_headline(
            "7系統49シグナル、BUY主流・SELL10件・生存率100%が3系統"
        )

    def test_reject_over_length(self):
        long = "📈 " + "x" * _HEADLINE_MAX_LEN
        assert not _is_valid_headline(long)

    def test_reject_empty(self):
        assert not _is_valid_headline("")


# --- synth -----------------------------------------------------------------
class TestSynthHeadline:
    def test_synth_format_2026_07_02(self):
        h = _synth_headline(_payload_2026_07_02())
        # 期待 format: "📈 07-02 49 signals / BUY:39 SELL:10 / $37K"
        assert h.startswith("📈")
        assert "07-02" in h
        assert "49 signals" in h
        assert "BUY:39" in h
        assert "SELL:10" in h
        assert "$37K" in h
        # gibberish 検出禁止
        assert "749BUYSELL" not in h

    def test_synth_length_bounded(self):
        h = _synth_headline(_payload_2026_07_02())
        assert len(h) <= _HEADLINE_MAX_LEN

    def test_synth_is_valid_headline(self):
        """synth 結果は必ず _is_valid_headline を通ること (self-check)。"""
        h = _synth_headline(_payload_2026_07_02())
        assert _is_valid_headline(h)

    def test_synth_sell_dominant_uses_down_emoji(self):
        payload = _payload_2026_07_02()
        # BUY を 0 に潰して SELL 主体にする
        payload["systems"]["sys1"]["signals"] = []
        h = _synth_headline(payload)
        assert h.startswith("📉")

    def test_synth_warn_uses_warning_emoji(self):
        payload = _payload_2026_07_02()
        payload["systems"]["sys1"]["gate_survival_ratio"] = 0.01
        h = _synth_headline(payload)
        assert h.startswith("⚠")  # ⚠️ or ⚠

    def test_synth_slash_separator_with_spaces(self):
        """区切り ' / ' (前後 space) が最低 2 個ある。"""
        import re
        h = _synth_headline(_payload_2026_07_02())
        assert " / " in h
        # 数値と label の間に必ず space or ':' (cram を禁止)
        assert re.search(r"\b\d+\s+signals\b", h), h
        assert re.search(r"\bBUY:\d+\b", h), h
        assert re.search(r"\bSELL:\d+\b", h), h
        assert h.count(" / ") >= 2

    def test_synth_million_notional(self):
        payload = _payload_2026_07_02()
        payload["portfolio"]["total_notional_usd"] = 1_250_000.0
        h = _synth_headline(payload)
        assert "$1.2M" in h


# --- system prompt ---------------------------------------------------------
class TestSystemPromptCarriesFormatRules:
    """LLM が日本語を出しにくくする system 指示が prompt に残っていること。"""

    def test_prompt_forbids_japanese_in_headline(self):
        # 「日本語」+ 「絶対に含めない」または「ASCII」の明示
        assert ("日本語" in _SYSTEM_PROMPT) or ("Japanese" in _SYSTEM_PROMPT)
        # ASCII+emoji only の指示
        assert "ASCII" in _SYSTEM_PROMPT

    def test_prompt_shows_correct_example(self):
        # 「📈 07-02 49 signals / BUY:39 SELL:10 / $37K」のような format 例
        assert "signals" in _SYSTEM_PROMPT
        assert "BUY:" in _SYSTEM_PROMPT or "BUY " in _SYSTEM_PROMPT
        assert "/" in _SYSTEM_PROMPT

    def test_prompt_shows_bad_example(self):
        # 誤例 (gibberish) を示して LLM に負の学習をさせている
        assert "mangled" in _SYSTEM_PROMPT or "749BUYSELL" in _SYSTEM_PROMPT or "潰れる" in _SYSTEM_PROMPT


# --- user prompt (_build_user_prompt) -------------------------------------
class TestUserPromptCarriesConstraints:
    def test_headline_marker_still_required(self):
        prompt = SignalNarrator()._build_user_prompt(_payload_2026_07_02())
        assert "HEADLINE:" in prompt

    def test_forbids_hallucinated_symbols(self):
        prompt = SignalNarrator()._build_user_prompt(_payload_2026_07_02())
        assert "JSON" in prompt
        assert ("無い symbol" in prompt) or ("hallucin" in prompt.lower())

    def test_slash_separator_or_char_limit_present(self):
        """/ 区切り or 25/50 字制約のどちらかは user prompt に残っていること。"""
        prompt = SignalNarrator()._build_user_prompt(_payload_2026_07_02())
        assert ("25" in prompt) or ("50" in prompt)
