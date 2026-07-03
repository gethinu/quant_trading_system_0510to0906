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

# headline (ntfy X-Title) の許容最大長。iPhone/Android 通知の見やすさ優先。
# 参考の理想 format: "📈 07-02 49 signals / BUY:39 SELL:10 / $37K" (42 chars)。
# 40-50 文字前後を想定し 50 を hard limit として validation する。
_HEADLINE_MAX_LEN = 50

# 概算 pricing (USD / 1M tokens)。厳密課金ではなく dashboard 表示用の目安。
# Haiku 4.5: input $1.00 / output $5.00 per MTok (2026 時点の概算)。
_PRICE_PER_MTOK = {
    "input": 1.00,
    "output": 5.00,
}

_SYSTEM_PROMPT = (
    "あなたは経験豊富な quant analyst です。7 つの systematic strategy "
    "(sys1-7) が出した当日シグナルを、個人投資家が 30 秒で把握できるよう "
    "日本語で 2 段落に要約します (summary 本文のみ日本語)。誇張・投資助言・未来予測は"
    "せず、JSON に含まれる事実 (symbol/side/system/gate 生存率) だけを根拠にしてください。\n\n"
    "★ headline は必ず ASCII (半角英数字と記号) と絵文字のみで作成し、日本語文字は"
    "絶対に含めないこと。ntfy X-Title は非 ASCII を strip するため、日本語 headline は"
    "iPhone 通知で mangled ASCII (例: '7系統49シグナル BUY主流…' が '749BUYSELL10100%3' に"
    "潰れる) になる。以下の書式を厳格に守ること:\n"
    "    <絵文字> <MM-DD> <N> signals / BUY:<x> SELL:<y> / $<Z>K\n"
    "  ルール:\n"
    "    - 区切りは半角 space または ' / ' (スラッシュの前後に必ず space)\n"
    "    - 数字と label の間には ':' か space を必ず入れる (例 'BUY:39' or 'BUY 39')\n"
    "    - notional は千ドル単位で '$37K'、100 万超は '$1.2M'\n"
    "    - 見出し全体は目安 40-50 文字以内 (絵文字含む)\n"
    "    - 絵文字は先頭 1 個のみ、以降は使わない\n"
    "  正しい例:\n"
    "    OK '\U0001F4C8 07-02 49 signals / BUY:39 SELL:10 / $37K'\n"
    "    OK '\U0001F4C9 07-03 12 signals / BUY:3 SELL:9 / $8K'\n"
    "    OK '\U0001F6E1️ 07-04 3 signals / BUY:0 SELL:3 / $1.2M'\n"
    "  誤った例 (絶対に生成しない):\n"
    "    NG '7系統49シグナル、BUY主流・SELL10件・生存率100%が3系統' (日本語 → mangled)\n"
    "    NG '\U0001F4C849signalsBUY39SELL10$37K' (区切りなしで cram、読めない)\n"
    "    NG 'Mega tech buy day, SPY hedge on!' (統計値が無い)"
)

# JSON に含まれない symbol を捏造しないための ticker 抽出 (大文字 1-5 文字)。
_TICKER_RE = re.compile(r"\b[A-Z]{1,5}\b")
# ticker と紛らわしい一般大文字語 (system 記述等) は cross-check から除外。
_TICKER_STOPWORDS = {
    "BUY", "SELL", "AI", "SPY", "USD", "JPY", "SMA", "ROC", "RSI", "ADX",
    "WARN", "OK", "NET", "ETF", "US", "PICKS",
}


# ---- headline validation / synthesis --------------------------------------
# NOTE(2026-07-03 mangled-title fix, phase 2): narrator が日本語 headline を
# 返しても iPhone 通知は ASCII-strip で gibberish になる。LLM 出力を信じずに
# post-process で validate し、format 違反なら _synth_headline() で決定論的に
# 差し替える。summary/per_symbol_reasons は温存する (headline だけ書き換え)。
def _headline_char_ok(ch: str) -> bool:
    """headline 用に許可する文字 (印字可能 ASCII + 絵文字ブロック)。"""
    cp = ord(ch)
    if 0x20 <= cp <= 0x7E:  # printable ASCII incl. space
        return True
    if 0x2600 <= cp <= 0x27BF:  # misc symbols + dingbats
        return True
    if 0x1F300 <= cp <= 0x1FAFF:  # emoji SMP ranges
        return True
    if cp == 0xFE0F:  # variation selector-16 (emoji presentation)
        return True
    return False


def _is_valid_headline(headline: str) -> bool:
    """新 format 制約 (ASCII+emoji, <= _HEADLINE_MAX_LEN 字) を満たすか。"""
    if not headline:
        return False
    if len(headline) > _HEADLINE_MAX_LEN:
        return False
    return all(_headline_char_ok(ch) for ch in headline)


def _synth_headline(signals_json: dict) -> str:
    """signals JSON から決定論的に headline を組み立てる (fallback / post-process)。

    format: '<絵文字> <MM-DD> <N> signals / BUY:<x> SELL:<y> / $<Z>K'
    - portfolio.total_signals から N
    - 各 system の signals から BUY/SELL 集計
    - portfolio.total_notional_usd から $ZK
    - 絵文字: WARN 有りなら ⚠️、SELL 主体なら 📉、それ以外 📈
    - date は 'YYYY-MM-DD' なら 'MM-DD' を抽出、それ以外は省略
    """
    date_raw = str(signals_json.get("date", "")).strip()
    mmdd = date_raw[5:10] if len(date_raw) >= 10 and date_raw[4] == "-" else ""

    systems = signals_json.get("systems", {}) or {}
    portfolio = signals_json.get("portfolio", {}) or {}
    total = int(portfolio.get("total_signals", 0) or 0)

    buy, sell, warn = 0, 0, False
    for cfg in systems.values():
        try:
            if float(cfg.get("gate_survival_ratio", 1.0)) < 0.05:
                warn = True
        except (TypeError, ValueError):
            pass
        for s in cfg.get("signals", []) or []:
            side = str(s.get("side") or "").upper()
            if side == "BUY":
                buy += 1
            elif side == "SELL":
                sell += 1

    notional = float(portfolio.get("total_notional_usd", 0) or 0)
    if notional >= 1_000_000:
        money = f"${notional / 1_000_000:.1f}M"
    elif notional >= 1_000:
        money = f"${notional / 1_000:.0f}K"
    elif notional > 0:
        money = f"${notional:.0f}"
    else:
        money = ""

    if warn:
        emoji = "⚠️"  # ⚠️
    elif sell > buy:
        emoji = "\U0001F4C9"  # 📉
    else:
        emoji = "\U0001F4C8"  # 📈

    parts = [emoji]
    if mmdd:
        parts.append(mmdd)
    parts.append(f"{total} signals")
    core = " ".join(parts)

    tail_bits = []
    if buy or sell:
        tail_bits.append(f"BUY:{buy} SELL:{sell}")
    if money:
        tail_bits.append(money)
    if tail_bits:
        return (core + " / " + " / ".join(tail_bits))[:_HEADLINE_MAX_LEN]
    return core[:_HEADLINE_MAX_LEN]


class SignalNarrator:
    """Claude API で当日シグナルのナラティブを生成する (fail-safe)。"""

    def __init__(self, model=None):
        self.model = model or os.getenv("NARRATOR_MODEL", DEFAULT_MODEL)
        self.api_key = os.getenv("ANTHROPIC_API_KEY")

    def is_configured(self):
        return bool(self.api_key)

    # -- public ----------------------------------------------------------
    def narrate(self, signals_json):
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

        # headline format validation (ASCII+emoji, <= 50 chars)。LLM が prompt
        # 指示に反して日本語や長すぎる headline を返した場合、決定論的に synth
        # したものへ差し替える。summary/per_symbol_reasons は温存。
        headline_replaced = False
        if not _is_valid_headline(headline):
            original = headline
            headline = _synth_headline(signals_json)
            headline_replaced = True
            logger.warning(
                "narrator: headline が format 違反のため synth に差し替え (orig=%r -> new=%r)",
                original[:60],
                headline,
            )

        result = {
            "date": str(signals_json.get("date", "")),
            "headline": headline,
            "summary": summary,
            "per_symbol_reasons": per_symbol,
            "model": self.model,
            "cost_usd": self._estimate_cost(resp),
            "elapsed_seconds": elapsed,
        }
        if headline_replaced:
            result["headline_synth"] = True

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
    def _build_user_prompt(self, signals_json):
        import json

        systems = signals_json.get("systems", {}) or {}
        stats_lines = []
        for sys_key in sorted(systems.keys()):
            cfg = systems[sys_key]
            ratio = float(cfg.get("gate_survival_ratio", 0.0))
            n_out = int(cfg.get("n_signals_output", 0))
            stats_lines.append(f"{sys_key}: {n_out} signals, survival {ratio * 100:.0f}%")
        coverage = "\n".join(stats_lines)

        # subscriber 向けに配信される headline は次の制約に従う:
        #   - ASCII (半角英数字+記号) + 絵文字のみ、日本語文字は絶対禁止
        #   - 目安 40-50 文字以内 (絵文字含む)、hard limit 50 字で post-process 差替
        #   - 区切り「 / 」(スラッシュ前後に半角 space 必須)
        #   - 数字と label の間は ':' or space (例 'BUY:39' / 'BUY 39')
        # narrator が制約を破った場合は narrate() の post-process で決定論的に
        # synth 差替されるので (headline_synth=True flag)、summary/reasons は
        # 温存される。summary 本文は日本語 OK (X-Title でなく body に載る)。
        date_raw = str(signals_json.get("date", "2026-07-02"))
        mmdd_hint = date_raw[5:10] if len(date_raw) >= 10 else "MM-DD"
        n_hint = int((signals_json.get("portfolio", {}) or {}).get("total_signals", 0))
        return (
            "以下は本日のシグナル JSON と各 system の gate 生存率です。\n\n"
            "1 行目に headline、続けて 2 段落の summary を書いてください。\n"
            "headline 行は先頭に 'HEADLINE: ' を付けてください。\n"
            "★ headline は ASCII (半角英数字+記号) と絵文字だけで作成し、日本語文字を"
            "1 文字も含めないこと (iPhone 通知の X-Title は非 ASCII を strip する)。\n"
            "書式 (厳守): '<絵文字> <MM-DD> <N> signals / BUY:<x> SELL:<y> / $<Z>K'\n"
            f"    例: '\U0001F4C8 {mmdd_hint} {n_hint} signals / BUY:X SELL:Y / $ZK'\n"
            "  - スラッシュの前後に必ず半角 space、'BUY:39' の形で数値を明示\n"
            "  - notional は千ドル単位で '$37K'、100 万超は '$1.2M'\n"
            "  - 見出し全体は 50 文字以内、絵文字は先頭 1 個のみ\n"
            "summary 本文 (2 段落) は日本語で構いません。JSON に無い symbol は絶対に"
            "登場させないこと。\n\n"
            f"[coverage stats]\n{coverage}\n\n"
            f"[signals JSON]\n{json.dumps(signals_json, ensure_ascii=False)}"
        )

    @staticmethod
    def _extract_text(resp):
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
    def _split_headline_summary(text):
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

    def _extract_per_symbol_reasons(self, text, signals_json):
        """signals JSON に実在する symbol について reason を紐付ける。

        narrator の自由文から個別 reason を厳密に抜くのは難しいので、JSON 側の
        reason を primary とし、確実に signals にある symbol だけを載せる。"""
        reasons = {}
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
    def _signal_symbols(signals_json):
        out = set()
        for cfg in (signals_json.get("systems", {}) or {}).values():
            for s in cfg.get("signals", []) or []:
                sym = str(s.get("symbol", "")).strip()
                if sym:
                    out.add(sym)
        hedge = (signals_json.get("portfolio", {}) or {}).get("hedge") or {}
        if hedge.get("symbol"):
            out.add(str(hedge["symbol"]))
        return out

    def _hallucination_ratio(self, result, signals_json):
        """narrator が言及した ticker のうち signals JSON に無いものの割合。"""
        valid = self._signal_symbols(signals_json)
        text = f"{result.get('headline', '')} {result.get('summary', '')}"
        mentioned = {
            t for t in _TICKER_RE.findall(text) if t not in _TICKER_STOPWORDS
        }
        mentioned |= set(result.get("per_symbol_reasons", {}).keys())
        if not mentioned:
            return 0.0
        bogus = {t for t in mentioned if t not in valid}
        return len(bogus) / len(mentioned)

    def _fallback(self, signals_json):
        """決定論的テンプレート。headline は新 format (ASCII+emoji) を使用。"""
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
            "headline": _synth_headline(signals_json),
            "summary": (
                f"本日は {n_sys} systems から計 {n_signals} signals。"
                f"hedge: {hedge_str}。詳細は各 system の内訳を参照。"
            ),
            "per_symbol_reasons": self._extract_per_symbol_reasons("", signals_json),
        }

    # -- cost ------------------------------------------------------------
    def _estimate_cost(self, resp):
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
