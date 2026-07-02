"""AI narrator layer (Claude API, Haiku default, fail-safe)。

当日シグナル JSON (schema v1.0) を受け取り、quant analyst 風の 2 段落解説
(headline + summary + per_symbol_reasons) を生成する。Vercel dashboard の
NarrativeCard と ntfy/email 配信に載せるための "人間向けナラティブ" 層。

設計方針:
    - **fail-safe**: ANTHROPIC_API_KEY 未設定・SDK 未インストール・API 失敗の
      いずれでも例外を投げず空 dict + WARN log を返す。pipeline は narrative
      無しで継続できる (publish は既存 body を使う)。
    - **hallucination 対策**: narrator が言及した symbol を signals JSON と
      cross-check し、乖離 (JSON に無い symbol の割合) が閾値超なら
      決定論的な fallback template に差し替える。
    - **cost 記録**: usage tokens から概算 USD を計算し dict に載せる。

env:
    ANTHROPIC_API_KEY  (required, 無ければ fail-safe で空 dict)
    NARRATOR_MODEL     (optional, default claude-haiku-4-5-20251001)
"""

from __future__ import annotations

import logging
import os
import re
import time
from typing import Any

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
_MAX_TOKENS = 800
# narrator が言及した symbol のうち signals JSON に無いものの割合がこれを超えたら
# hallucination とみなし fallback template に差し替える。
_HALLUCINATION_THRESHOLD = 0.30

# 概算 pricing (USD / 1M tokens)。厳密課金ではなく dashboard 表示用の目安。
# Haiku 4.5: input $1.00 / output $5.00 per MTok (2026 時点の概算)。
_PRICE_PER_MTOK = {
    "input": 1.00,
    "output": 5.00,
}

_SYSTEM_PROMPT = (
    "あなたは経験豊富な quant analyst です。7 つの systematic strategy "
    "(sys1-7) が出した当日シグナルを、個人投資家が 30 秒で把握できるよう "
    "日本語で 2 段落に要約します。誇張・投資助言・未来予測はせず、JSON に "
    "含まれる事実 (symbol/side/system/gate 生存率) だけを根拠にしてください。"
)

# JSON に含まれない symbol を捏造しないための ticker 抽出 (大文字 1-5 文字)。
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
# ticker と紛らわしい一般大文字語 (system 記述等) は cross-check から除外。
_TICKER_STOPWORDS = {
    "BUY", "SELL", "AI", "SPY", "USD", "JPY", "SMA", "ROC", "RSI", "ADX",
    "WARN", "OK", "NET", "ETF", "US", "PICKS",
}


class SignalNarrator:
    """Claude API で当日シグナルのナラティブを生成する (fail-safe)。"""

    def __init__(self, model: str | None = None) -> None:
        self.model = model or os.getenv("NARRATOR_MODEL", DEFAULT_MODEL)
        self.api_key = os.getenv("ANTHROPIC_API_KEY")

    def is_configured(self) -> bool:
        return bool(self.api_key)

    # -- public ----------------------------------------------------------
    def narrate(self, signals_json: dict[str, Any]) -> dict[str, Any]:
        """当日シグナル JSON からナラティブ dict を返す。

        returns: {"headline", "summary", "per_symbol_reasons", "model",
                  "cost_usd", "elapsed_seconds"}。
        fail-safe: API key 未設定 / SDK 欠如 / API 失敗なら空 dict + WARN log。
        """
        if not self.is_configured():
            logger.warning("narrator: ANTHROPIC_API_KEY 未設定のためスキップ (空 narrative)")
            return {}

        start = time.monotonic()
        try:
            import anthropic
        except ImportError:
            logger.warning("narrator: anthropic SDK 未インストール (pip install anthropic)")
            return {}

        try:
            client = anthropic.Anthropic(api_key=self.api_key)
            resp = client.messages.create(
                model=self.model,
                max_tokens=_MAX_TOKENS,
                system=_SYSTEM_PROMPT,
                messages=[{"role": "user", "content": self._build_user_prompt(signals_json)}],
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("narrator: Claude API 呼び出し失敗: %s", exc)
            return {}

        elapsed = round(time.monotonic() - start, 3)
        text = self._extract_text(resp)
        headline, summary = self._split_headline_summary(text)
        per_symbol = self._extract_per_symbol_reasons(text, signals_json)

        result = {
            "date": str(signals_json.get("date", "")),
            "headline": headline,
            "summary": summary,
            "per_symbol_reasons": per_symbol,
            "model": self.model,
            "cost_usd": self._estimate_cost(resp),
            "elapsed_seconds": elapsed,
        }

        # hallucination cross-check。乖離が大きければ決定論的 fallback へ。
        ratio = self._hallucination_ratio(result, signals_json)
        if ratio > _HALLUCINATION_THRESHOLD:
            logger.warning(
                "narrator: hallucination 検知 (乖離率 %.0f%% > %.0f%%), fallback template 使用",
                ratio * 100,
                _HALLUCINATION_THRESHOLD * 100,
            )
            fb = self._fallback(signals_json)
            fb["model"] = self.model
            fb["cost_usd"] = result["cost_usd"]
            fb["elapsed_seconds"] = elapsed
            fb["fallback"] = True
            return fb

        return result

    # -- prompt / parsing ------------------------------------------------
    def _build_user_prompt(self, signals_json: dict[str, Any]) -> str:
        import json

        systems = signals_json.get("systems", {}) or {}
        stats_lines = []
        for sys_key in sorted(systems.keys()):
            cfg = systems[sys_key]
            ratio = float(cfg.get("gate_survival_ratio", 0.0))
            n_out = int(cfg.get("n_signals_output", 0))
            stats_lines.append(f"{sys_key}: {n_out} signals, survival {ratio * 100:.0f}%")
        coverage = "\n".join(stats_lines)

        return (
            "以下は本日のシグナル JSON と各 system の gate 生存率です。\n\n"
            "1 行目に <=40 字の headline、続けて 2 段落の summary を書いてください。\n"
            "headline 行は先頭に 'HEADLINE: ' を付けてください。\n"
            "JSON に無い symbol は絶対に登場させないこと。\n\n"
            f"[coverage stats]\n{coverage}\n\n"
            f"[signals JSON]\n{json.dumps(signals_json, ensure_ascii=False)}"
        )

    @staticmethod
    def _extract_text(resp: Any) -> str:
        try:
            parts = []
            for block in resp.content:
                text = getattr(block, "text", None)
                if text:
                    parts.append(text)
            return "\n".join(parts).strip()
        except Exception:  # noqa: BLE001
            return ""

    @staticmethod
    def _split_headline_summary(text: str) -> tuple[str, str]:
        if not text:
            return "", ""
        lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
        headline = ""
        body_start = 0
        for i, ln in enumerate(lines):
            m = re.match(r"^HEADLINE:\s*(.+)$", ln, re.IGNORECASE)
            if m:
                headline = m.group(1).strip()
                body_start = i + 1
                break
        if not headline and lines:
            headline = lines[0]
            body_start = 1
        summary = " ".join(lines[body_start:]).strip()
        return headline, summary

    def _extract_per_symbol_reasons(
        self, text: str, signals_json: dict[str, Any]
    ) -> dict[str, str]:
        """signals JSON に実在する symbol について reason を紐付ける。

        narrator の自由文から個別 reason を厳密に抜くのは難しいので、JSON 側の
        reason を primary とし、確実に signals にある symbol だけを載せる。"""
        reasons: dict[str, str] = {}
        for cfg in (signals_json.get("systems", {}) or {}).values():
            for s in cfg.get("signals", []) or []:
                sym = str(s.get("symbol", "")).strip()
                if sym and sym not in reasons:
                    reason = str(s.get("reason") or "").strip()
                    if reason:
                        reasons[sym] = reason
        return reasons

    # -- hallucination / fallback ---------------------------------------
    @staticmethod
    def _signal_symbols(signals_json: dict[str, Any]) -> set[str]:
        out: set[str] = set()
        for cfg in (signals_json.get("systems", {}) or {}).values():
            for s in cfg.get("signals", []) or []:
                sym = str(s.get("symbol", "")).strip()
                if sym:
                    out.add(sym)
        hedge = (signals_json.get("portfolio", {}) or {}).get("hedge") or {}
        if hedge.get("symbol"):
            out.add(str(hedge["symbol"]))
        return out

    def _hallucination_ratio(
        self, result: dict[str, Any], signals_json: dict[str, Any]
    ) -> float:
        """narrator が言及した ticker のうち signals JSON に無いものの割合。"""
        valid = self._signal_symbols(signals_json)
        text = f"{result.get('headline', '')} {result.get('summary', '')}"
        mentioned = {
            t for t in _TICKER_RE.findall(text) if t not in _TICKER_STOPWORDS
        }
        # per_symbol_reasons のキーも検証対象
        mentioned |= set(result.get("per_symbol_reasons", {}).keys())
        if not mentioned:
            return 0.0
        bogus = {t for t in mentioned if t not in valid}
        return len(bogus) / len(mentioned)

    def _fallback(self, signals_json: dict[str, Any]) -> dict[str, Any]:
        """決定論的テンプレート (「N signals across 7 systems」)。"""
        systems = signals_json.get("systems", {}) or {}
        n_signals = int(
            (signals_json.get("portfolio", {}) or {}).get("total_signals", 0)
        )
        n_sys = sum(
            1 for cfg in systems.values() if (cfg.get("signals") or [])
        )
        hedge = (signals_json.get("portfolio", {}) or {}).get("hedge") or {}
        hedge_str = (
            f"{hedge.get('side')} {hedge.get('symbol')}"
            if hedge.get("symbol")
            else "none"
        )
        return {
            "date": str(signals_json.get("date", "")),
            "headline": f"{n_signals} signals across {n_sys} systems",
            "summary": (
                f"本日は {n_sys} systems から計 {n_signals} signals。"
                f"hedge: {hedge_str}。詳細は各 system の内訳を参照。"
            ),
            "per_symbol_reasons": self._extract_per_symbol_reasons("", signals_json),
        }

    # -- cost ------------------------------------------------------------
    def _estimate_cost(self, resp: Any) -> float:
        try:
            usage = resp.usage
            in_tok = int(getattr(usage, "input_tokens", 0) or 0)
            out_tok = int(getattr(usage, "output_tokens", 0) or 0)
        except Exception:  # noqa: BLE001
            return 0.0
        cost = (
            in_tok / 1_000_000 * _PRICE_PER_MTOK["input"]
            + out_tok / 1_000_000 * _PRICE_PER_MTOK["output"]
        )
        return round(cost, 6)
