"""System test: alpaca-next dashboard UI 契約.

2026-07-02 UI/UX overhaul で 3 つの regression を固定する:

    1. tailwind.config.ts が components/ を scan していること
       (2026-07-02 incident: NarrativeCard.tsx の class が JIT purge され
       銘柄 chip が垂直オーバーフローで崩壊)。

    2. page.tsx の hero が universe (sys1.Tgt.count) だけに依存しない
       こと ("no data" false-negative 対策 - portfolio.total_signals を
       主 KPI として使う)。

    3. NarrativeCard が per_symbol_reasons を flex-wrap で display
       (縦列 overflow 復活禁止)。

TS/TSX は Node 実行環境がないので、source 部分文字列 assertion で契約を固定。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NEXT_ROOT = REPO_ROOT / "apps" / "dashboards" / "alpaca-next"

TAILWIND = NEXT_ROOT / "tailwind.config.ts"
PAGE = NEXT_ROOT / "app" / "page.tsx"
NARRATIVE_CARD = NEXT_ROOT / "components" / "NarrativeCard.tsx"
PIPELINE_SECTION = NEXT_ROOT / "components" / "PipelineSection.tsx"
SIGNALS_SECTION = NEXT_ROOT / "components" / "SignalsSection.tsx"


@pytest.fixture(scope="module")
def tailwind_text() -> str:
    assert TAILWIND.exists(), f"{TAILWIND} 消失"
    return TAILWIND.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_text() -> str:
    assert PAGE.exists(), f"{PAGE} 消失"
    return PAGE.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def narrative_text() -> str:
    assert NARRATIVE_CARD.exists(), f"{NARRATIVE_CARD} 消失"
    return NARRATIVE_CARD.read_text(encoding="utf-8")


class TestTailwindScansComponents:
    """★ 2026-07-02 bug 直接固定."""

    def test_content_includes_components(self, tailwind_text: str):
        """tailwind の content scan に components/ が含まれていること。"""
        # './components/**/*.{ts,tsx}' が入っていること
        assert "./components/" in tailwind_text, (
            "tailwind.config.ts の content に components/ が無い. "
            "NarrativeCard の class が purge され card 崩壊が復活するリスク"
        )

    def test_content_includes_app_and_lib(self, tailwind_text: str):
        """既存 app/ lib/ scan は維持されていること (regression)。"""
        assert "./app/" in tailwind_text
        assert "./lib/" in tailwind_text


class TestPageDoesNotDependOnUniverseOnly:
    """★ "no data" false-negative 対策."""

    def test_hero_uses_total_signals(self, page_text: str):
        """hero KPI は portfolio.total_signals を主として使う。"""
        # 「total」変数か portfolio.total_signals が hero 表示に登場すること
        assert "total_signals" in page_text or "signals?.portfolio" in page_text
        # universe が null でも hero が "no data" にならないこと (旧 fallback を排除)
        # 旧実装: `universe != null ? \`${...} tickers\` : 'no data'`
        assert "'no data'" not in page_text, (
            "'no data' fallback が復活. universe null 時に "
            "signals があっても hero が dead になる"
        )

    def test_hero_shows_buy_sell_split(self, page_text: str):
        """BUY/SELL 内訳が hero に表示されている。"""
        assert "BUY" in page_text and "SELL" in page_text

    def test_universe_still_displayed_as_subinfo(self, page_text: str):
        """universe は主 KPI ではないが sub-info として残っていること。"""
        assert "universe" in page_text.lower()


class TestNarrativeCardWrapsSymbolChips:
    """★ 銘柄 chip の垂直オーバーフロー再発防止."""

    def test_uses_flex_wrap_for_reasons(self, narrative_text: str):
        """per_symbol_reasons は flex-wrap で必ず折り返すこと。"""
        # flex + flex-wrap or Tailwind の `flex-wrap` クラスを含む
        assert "flex-wrap" in narrative_text, (
            "flex-wrap 消失. reasons が縦列 overflow で card 崩壊するリスク"
        )

    def test_wraps_full_summary_in_details(self, narrative_text: str):
        """summary 詳細は <details> accordion で隠すこと (default で summary のみ)。"""
        assert "<details" in narrative_text, (
            "詳細 accordion 消失. narrator の全文が hero 高さ blowout の risk"
        )

    def test_truncate_helper_present(self, narrative_text: str):
        """TL;DR 生成の truncate helper が残っていること。"""
        assert "truncateSummary" in narrative_text


class TestSectionsFactored:
    """PipelineSection / SignalsSection が独立 component 化されていること。"""

    def test_pipeline_section_component_exists(self):
        assert PIPELINE_SECTION.exists()

    def test_signals_section_component_exists(self):
        assert SIGNALS_SECTION.exists()

    def test_page_imports_components(self, page_text: str):
        assert "PipelineSection" in page_text
        assert "SignalsSection" in page_text
        assert "NarrativeCard" in page_text
