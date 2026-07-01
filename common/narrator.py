"""AI narrator layer — 当日シグナルを Claude API で自然文解説する (事業差別化の核)。

「signals だけ」を配信する他社に対し、当日シグナルを 2 段落の narrative +
各 symbol の 1 行 reason に翻訳して付加価値を出す。subscriber tier の礎。

設計方針:
  - **fail-safe**: ``ANTHROPIC_API_KEY`` 未設定なら早期 return で空 narrative +
    WARN log。narrate() は例外を投げず、常に ``NarrativeResult`` を返す
    (pipeline を止めない)。
  - **hallucination 対策**: narrator が言及する symbol が signals_json に
    存在するか cross-check。乖離があれば warnings に記録し、乖離 >30% なら
    template fallback narrative に切替える。
  - **cost 実測**: Claude API ``usage`` から入出力トークン単価で cost_usd を算出。

output schema (``NarrativeResult.as_dict()`` / ``meta.narrative`` へ merge):
    {
      "headline": "本日はメガテック集中買い、SPY hedge on",
      "summary": "sys1 が... sys7 catastrophe hedge...",
      "per_symbol_reasons": {"AAPL": "SMA200 breakout + 3日連続volume拡大", ...},
      "model": "claude-haiku-4-5-20251001",
      "cost_usd": 0.006,
      "elapsed_seconds": 4.2,
      "warnings": ["symbol X mentioned but not in signals"],
      "configured": true,
      "fallback": false
    }
"""

from __future__ import annotations

from dataclasses import dataclass, field
import json
import logging
import os
import re
import time
from typing import Any

from common.publishers.base import SignalMessage

logger = logging.getLogger(__name__)

DEFAULT_MODEL = "claude-haiku-4-5-20251001"
DEFAULT_MAX_TOKENS = 800

# 乖離 (言及 symbol のうち signals に無い割合) がこれを超えたら fallback。
HALLUCINATION_FALLBACK_RATIO = 0.30

# model prefix -> (input $/1M, output $/1M)。prefix 一致で最初にヒットした料金を使う。
_PRICING: dict[str, tuple[float, float]] = {
    "claude-haiku-4-5": (1.0, 5.0),
    "claude-haiku": (1.0, 5.0),
    "claude-sonnet-5": (3.0, 15.0),
    "claude-sonnet-4-6": (3.0, 15.0),
    "claude-sonnet": (3.0, 15.0),
    "claude-opus-4-8": (5.0, 25.0),
    "claude-opus": (5.0, 25.0),
    "claude-fable-5": (10.0, 50.0),
}

# ticker cross-check で無視する全大文字トークン (英単語 / 取引用語)。
_TICKER_STOPWORDS = {
    "BUY", "SELL", "WARN", "USD", "AI", "US", "API", "ETF", "IPO",
    "CEO", "GDP", "FX", "OK", "A", "I", "SMA", "EMA", "RSI", "ATR",
    "HEDGE", "LONG", "SHORT", "TODAY", "NEW", "ALL",
}
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")


@dataclass
class NarrativeResult:
    """narrate() の返却値。常に生成され、pipeline から meta.narrative へ merge する。"""

    headline: str = ""
    summary: str = ""
    per_symbol_reasons: dict[str, str] = field(default_factory=dict)
    model: str = ""
    cost_usd: float = 0.0
    elapsed_seconds: float = 0.0
    warnings: list[str] = field(default_factory=list)
    configured: bool = False
    fallback: bool = False

    def is_empty(self) -> bool:
        return not self.headline and not self.summary

    def as_dict(self) -> dict[str, Any]:
        return {
            "headline": self.headline,
            "summary": self.summary,
            "per_symbol_reasons": self.per_symbol_reasons,
            "model": self.model,
            "cost_usd": round(self.cost_usd, 6),
            "elapsed_seconds": round(self.elapsed_seconds, 2),
            "warnings": self.warnings,
            "configured": self.configured,
            "fallback": self.fallback,
        }


def _price_for(model: str) -> tuple[float, float]:
    for prefix, price in _PRICING.items():
        if model.startswith(prefix):
            return price
    # 未知 model は haiku 相当で見積る (cost を過小報告しないよう控えめ)。
    return _PRICING["claude-haiku-4-5"]


def _collect_symbols(signals_json: dict[str, Any]) -> set[str]:
    """signals_json 内の全 symbol (systems の signals + hedge) を大文字集合で返す。"""
    symbols: set[str] = set()
    for cfg in (signals_json.get("systems") or {}).values():
        for s in cfg.get("signals", []) or []:
            sym = s.get("symbol")
            if sym:
                symbols.add(str(sym).upper())
    hedge = (signals_json.get("portfolio") or {}).get("hedge") or {}
    if hedge.get("symbol"):
        symbols.add(str(hedge["symbol"]).upper())
    return symbols


def _mentioned_symbols(result: NarrativeResult) -> set[str]:
    """narrative が言及する symbol 候補 (per_symbol_reasons key + 本文の ticker 風トークン)。"""
    mentioned: set[str] = {str(k).upper() for k in result.per_symbol_reasons}
    text = f"{result.headline} {result.summary}"
    for tok in _TICKER_RE.findall(text):
        if tok not in _TICKER_STOPWORDS:
            mentioned.add(tok)
    return mentioned


class SignalNarrator:
    """当日シグナルを Claude API で自然文解説する narrator。

    ``ANTHROPIC_API_KEY`` 未設定なら :meth:`is_configured` が False、
    :meth:`narrate` は空 narrative を返す (pipeline 継続)。テストでは
    ``client=`` に fake を注入して API 呼出を mock できる。
    """

    def __init__(
        self,
        model: str | None = None,
        *,
        api_key: str | None = None,
        client: Any | None = None,
        max_tokens: int = DEFAULT_MAX_TOKENS,
    ) -> None:
        self.model = model or os.getenv("NARRATOR_MODEL", DEFAULT_MODEL)
        self.api_key = api_key if api_key is not None else os.getenv("ANTHROPIC_API_KEY")
        self.max_tokens = max_tokens
        self._client = client  # 注入された fake / lazy 生成された Anthropic client

    def is_configured(self) -> bool:
        return bool(self.api_key) or self._client is not None

    # -- client (lazy) ---------------------------------------------------
    def _get_client(self) -> Any:
        if self._client is not None:
            return self._client
        import anthropic  # lazy import: 未 install / 未設定でも import 時に落とさない

        self._client = anthropic.Anthropic(api_key=self.api_key)
        return self._client

    # -- prompts ---------------------------------------------------------
    _SYSTEM_PROMPT = (
        "あなたは経験豊富な quant analyst です。7 つの systematic 戦略 (sys1-7) が"
        "生成した当日シグナルを、投資家向けに簡潔に解説します。誇張や投資助言は避け、"
        "事実 (どの system が何を選んだか、gate 生存率、hedge の有無) に基づいて記述します。"
        "signals に存在しない銘柄には言及しないこと。"
        "\n\n必ず次の JSON のみを返してください (前後に説明文を付けない):\n"
        '{"headline": "<40字以内の見出し>", '
        '"summary": "<2段落の解説>", '
        '"per_symbol_reasons": {"TICKER": "<その銘柄が選ばれた1行の理由>"}}'
    )

    def _build_user_prompt(
        self, signals_json: dict[str, Any], coverage: dict[str, Any] | None
    ) -> str:
        message = SignalMessage(payload=signals_json)
        lines = message.system_summary_lines(top_n=5)
        hedge = message.hedge
        hedge_str = (
            f"{hedge.get('side')} {hedge.get('symbol')} @ {hedge.get('entry_price')}"
            if hedge and hedge.get("symbol")
            else "none"
        )
        parts = [
            f"date: {message.date}",
            f"total_signals: {message.total_signals}",
            f"hedge: {hedge_str}",
            f"warnings(low gate survival): {message.has_warnings()}",
            "",
            "system summary:",
            *(f"  - {ln}" for ln in (lines or ["(no signals today)"])),
        ]
        if coverage:
            parts.append("")
            parts.append("market context (coverage delta):")
            parts.append(f"  {json.dumps(coverage, ensure_ascii=False)[:600]}")
        parts.append("")
        parts.append("上記シグナルを 2 段落で解説し、各 symbol の 1 行 reason を付けてください。")
        return "\n".join(parts)

    # -- public API ------------------------------------------------------
    def narrate(
        self, signals_json: dict[str, Any], coverage: dict[str, Any] | None = None
    ) -> NarrativeResult:
        """signals_json を解説し ``NarrativeResult`` を返す。例外は投げない。"""
        t0 = time.time()

        if not self.is_configured():
            logger.warning(
                "SignalNarrator: ANTHROPIC_API_KEY 未設定のため narrative を skip します "
                "(pipeline は継続)。"
            )
            return NarrativeResult(
                model=self.model,
                configured=False,
                warnings=["ANTHROPIC_API_KEY not set; narrative skipped"],
                elapsed_seconds=time.time() - t0,
            )

        try:
            raw, usage = self._call_api(signals_json, coverage)
        except Exception as exc:  # noqa: BLE001 — narrator は絶対に pipeline を止めない
            logger.warning("SignalNarrator: API 呼出失敗 (%s)。fallback narrative を使用。", exc)
            result = self._fallback(signals_json)
            result.warnings.append(f"api_error: {exc}")
            result.elapsed_seconds = time.time() - t0
            return result

        result = self._parse(raw)
        result.model = self.model
        result.configured = True
        result.cost_usd = self._estimate_cost(usage)
        self._cross_check(result, signals_json)
        result.elapsed_seconds = time.time() - t0
        return result

    # -- internals -------------------------------------------------------
    def _call_api(
        self, signals_json: dict[str, Any], coverage: dict[str, Any] | None
    ) -> tuple[str, Any]:
        client = self._get_client()
        resp = client.messages.create(
            model=self.model,
            max_tokens=self.max_tokens,
            system=self._SYSTEM_PROMPT,
            messages=[
                {"role": "user", "content": self._build_user_prompt(signals_json, coverage)}
            ],
        )
        text = ""
        for block in getattr(resp, "content", []) or []:
            if getattr(block, "type", None) == "text":
                text += block.text
        return text, getattr(resp, "usage", None)

    def _estimate_cost(self, usage: Any) -> float:
        if usage is None:
            return 0.0
        in_price, out_price = _price_for(self.model)
        in_tok = getattr(usage, "input_tokens", 0) or 0
        out_tok = getattr(usage, "output_tokens", 0) or 0
        # cache token も入力側料金で概算 (haiku は cache read ~0.1x だが控えめに full 計上)。
        in_tok += getattr(usage, "cache_read_input_tokens", 0) or 0
        in_tok += getattr(usage, "cache_creation_input_tokens", 0) or 0
        return (in_tok / 1_000_000) * in_price + (out_tok / 1_000_000) * out_price

    @staticmethod
    def _parse(raw: str) -> NarrativeResult:
        """API 出力 (JSON 期待) を parse。壊れていても best-effort で拾う。"""
        text = raw.strip()
        # ```json ... ``` fence を剥がす
        if text.startswith("```"):
            text = re.sub(r"^```[a-zA-Z]*\n?", "", text)
            text = re.sub(r"\n?```$", "", text).strip()
        # 最初の { .. 最後の } を JSON として試す
        start, end = text.find("{"), text.rfind("}")
        if start != -1 and end != -1 and end > start:
            try:
                data = json.loads(text[start : end + 1])
                reasons = data.get("per_symbol_reasons") or {}
                if not isinstance(reasons, dict):
                    reasons = {}
                return NarrativeResult(
                    headline=str(data.get("headline", "")).strip(),
                    summary=str(data.get("summary", "")).strip(),
                    per_symbol_reasons={str(k).upper(): str(v) for k, v in reasons.items()},
                )
            except (ValueError, TypeError):
                pass
        # JSON として読めない場合は全文を summary に (warning は cross_check 前に付与)。
        return NarrativeResult(
            headline="",
            summary=text[:1000],
            warnings=["narrator output was not valid JSON; used raw text as summary"],
        )

    def _cross_check(self, result: NarrativeResult, signals_json: dict[str, Any]) -> None:
        """言及 symbol が signals に存在するか照合し、乖離大なら fallback へ切替える。"""
        known = _collect_symbols(signals_json)
        mentioned = _mentioned_symbols(result)
        if not mentioned:
            return
        missing = sorted(mentioned - known)
        for sym in missing:
            result.warnings.append(f"symbol {sym} mentioned but not in signals")
        ratio = len(missing) / len(mentioned)
        if ratio > HALLUCINATION_FALLBACK_RATIO:
            logger.warning(
                "SignalNarrator: hallucination ratio %.0f%% (>%.0f%%)。fallback へ切替。",
                ratio * 100,
                HALLUCINATION_FALLBACK_RATIO * 100,
            )
            fb = self._fallback(signals_json)
            # cross-check の warnings を保持したまま本文だけ template に差し替える。
            result.headline = fb.headline
            result.summary = fb.summary
            result.per_symbol_reasons = fb.per_symbol_reasons
            result.fallback = True
            result.warnings.append(
                f"hallucination ratio {ratio:.0%} exceeded threshold; used fallback narrative"
            )

    @staticmethod
    def _fallback(signals_json: dict[str, Any]) -> NarrativeResult:
        """API 不可 / hallucination 時の決定的 template narrative。"""
        message = SignalMessage(payload=signals_json)
        systems = message.systems
        n_active = sum(1 for c in systems.values() if (c.get("signals") or []))
        total = message.total_signals
        hedge = message.hedge
        hedge_str = (
            f" {hedge.get('side')} {hedge.get('symbol')} でヘッジ。"
            if hedge and hedge.get("symbol")
            else " ヘッジなし。"
        )
        warn = " gate 生存率が低い system があります。" if message.has_warnings() else ""
        headline = f"{total} signals / {len(systems)} systems"
        summary = (
            f"本日は {len(systems)} systematic 戦略のうち {n_active} 個がシグナルを生成し、"
            f"合計 {total} 件のポジション候補が出ています。{hedge_str}{warn}".strip()
        )
        return NarrativeResult(headline=headline, summary=summary, fallback=True)
