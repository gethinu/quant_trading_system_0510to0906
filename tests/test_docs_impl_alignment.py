"""Docs vs impl alignment tests (D2 / D4).

このテストは、docs/systems/システムN.txt 記述と実装 (core/systemN.py) の
定数値が一致していることを保証する。

対象:
    - D2: System4 の RSI4 除外閾値 (MAX_RSI4_THRESHOLD)
    - D4: System6 の HV50 範囲 (HV50_BOUNDS_PERCENT)

背景:
    tests/DIVERGENCE_ANALYSIS_20260702.md で確認された D2/D4 の乖離は、
    2026-07-02 に docs 側を impl 準拠に update した。以後、code 側の閾値
    変更時に docs も追随する invariant を pytest で担保する。

方針:
    core.systemN のモジュール import は runtime dependency (例: `ta`) を
    引きずるため、本 alignment テストでは **source file を regex で parse**
    して定数値を抽出する。これによりオフライン環境でも安定して回る。
"""

from __future__ import annotations

from pathlib import Path
import re

import pytest

# プロジェクトルート
ROOT = Path(__file__).resolve().parents[1]

DOCS_DIR = ROOT / "docs" / "systems"
SYSTEM4_DOC = DOCS_DIR / "システム4.txt"
SYSTEM6_DOC = DOCS_DIR / "システム6.txt"

CORE_DIR = ROOT / "core"
SYSTEM4_SRC = CORE_DIR / "system4.py"
SYSTEM6_SRC = CORE_DIR / "system6.py"


# ---------------------------------------------------------------------------
# Helpers: parse impl constants directly from source (avoid runtime deps)
# ---------------------------------------------------------------------------


def _read(p: Path) -> str:
    assert p.exists(), f"missing file: {p}"
    return p.read_text(encoding="utf-8")


def _parse_scalar(src: str, name: str) -> float:
    """`NAME = <number>` from source; ignores trailing comments."""
    m = re.search(
        rf"^\s*{re.escape(name)}\s*(?::\s*[^\s=]+)?\s*=\s*([0-9]+(?:\.[0-9]+)?)",
        src,
        re.MULTILINE,
    )
    assert m is not None, f"cannot find scalar constant {name} in source"
    return float(m.group(1))


def _parse_tuple2(src: str, name: str) -> tuple[float, float]:
    """`NAME = (a, b)` from source."""
    m = re.search(
        rf"^\s*{re.escape(name)}\s*(?::\s*[^\s=]+)?\s*=\s*"
        r"\(\s*([0-9]+(?:\.[0-9]+)?)\s*,\s*([0-9]+(?:\.[0-9]+)?)\s*\)",
        src,
        re.MULTILINE,
    )
    assert m is not None, f"cannot find tuple constant {name} in source"
    return float(m.group(1)), float(m.group(2))


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture(scope="module")
def system4_doc_text() -> str:
    return _read(SYSTEM4_DOC)


@pytest.fixture(scope="module")
def system6_doc_text() -> str:
    return _read(SYSTEM6_DOC)


@pytest.fixture(scope="module")
def system4_src_text() -> str:
    return _read(SYSTEM4_SRC)


@pytest.fixture(scope="module")
def system6_src_text() -> str:
    return _read(SYSTEM6_SRC)


# ---------------------------------------------------------------------------
# D2: System4 RSI4 threshold alignment
# ---------------------------------------------------------------------------


class TestSystem4RSI4Alignment:
    """D2: System4 の RSI4 除外閾値が docs と impl で一致することを保証。"""

    def test_impl_constant_value(self, system4_src_text: str) -> None:
        """impl 側の MAX_RSI4_THRESHOLD が 30.0 であること (regression guard)。"""
        val = _parse_scalar(system4_src_text, "MAX_RSI4_THRESHOLD")
        assert val == 30.0, (
            f"MAX_RSI4_THRESHOLD changed to {val}; docs "
            "(docs/systems/システム4.txt) も一緒に update してください。"
        )

    def test_docs_mentions_rsi4_exclusion(self, system4_doc_text: str) -> None:
        """docs に RSI4 除外条件が明記されていること。"""
        assert ("RSI4" in system4_doc_text) or (
            "4日RSI" in system4_doc_text
        ), "docs に RSI4 / 4日RSI の記述が見当たりません"
        exclusion_pattern = re.compile(r"(RSI4|4日RSI).{0,80}30", re.DOTALL)
        assert exclusion_pattern.search(system4_doc_text), (
            "docs/systems/システム4.txt に RSI4 と閾値 30 の関連記述が"
            "見当たりません。impl (MAX_RSI4_THRESHOLD=30.0) と乖離。"
        )

    def test_docs_impl_value_match(
        self, system4_doc_text: str, system4_src_text: str
    ) -> None:
        """docs に書かれた閾値と impl 定数が数値レベルで一致すること。"""
        impl_val = _parse_scalar(system4_src_text, "MAX_RSI4_THRESHOLD")
        m = re.search(
            r"MAX_RSI4_THRESHOLD\s*\(?\s*=\s*([0-9]+(?:\.[0-9]+)?)",
            system4_doc_text,
        )
        assert m is not None, (
            "docs にMAX_RSI4_THRESHOLD の数値参照が見当たりません。"
            "「実装参照: ... MAX_RSI4_THRESHOLD (= 30.0)」形式で明記してください。"
        )
        doc_value = float(m.group(1))
        assert doc_value == impl_val, (
            f"docs の閾値 {doc_value} と impl の "
            f"MAX_RSI4_THRESHOLD={impl_val} が乖離しています。"
        )


# ---------------------------------------------------------------------------
# D4: System6 HV50 bounds alignment
# ---------------------------------------------------------------------------


class TestSystem6HV50Alignment:
    """D4: System6 の HV50 範囲 filter が docs と impl で一致することを保証。"""

    def test_impl_constant_value(self, system6_src_text: str) -> None:
        """impl 側の HV50_BOUNDS_PERCENT が (10.0, 40.0) であること。"""
        bounds = _parse_tuple2(system6_src_text, "HV50_BOUNDS_PERCENT")
        assert bounds == (10.0, 40.0), (
            f"HV50_BOUNDS_PERCENT changed to {bounds}; docs "
            "(docs/systems/システム6.txt) も一緒に update してください。"
        )

    def test_docs_mentions_hv50_bounds(self, system6_doc_text: str) -> None:
        """docs に HV50 の 10〜40% 範囲 filter が明記されていること。"""
        assert ("HV50" in system6_doc_text) or (
            "ヒストリカルボラティリティ" in system6_doc_text
        ), (
            "docs/systems/システム6.txt に HV50 / ヒストリカルボラティリティ "
            "の記述が見当たりません。impl (HV50_BOUNDS_PERCENT) と乖離。"
        )
        # 「10〜40%」形式 (〜 / - / ~ 許容)
        bounds_pattern = re.compile(r"10\s*[〜\-~]\s*40\s*%")
        assert bounds_pattern.search(system6_doc_text), (
            "docs/systems/システム6.txt に「10〜40%」形式の HV50 範囲記述が"
            "見当たりません。impl の HV50_BOUNDS_PERCENT=(10.0, 40.0) と乖離。"
        )

    def test_docs_impl_value_match(
        self, system6_doc_text: str, system6_src_text: str
    ) -> None:
        """docs に書かれた bounds tuple と impl 定数が一致すること。"""
        impl_lo, impl_hi = _parse_tuple2(system6_src_text, "HV50_BOUNDS_PERCENT")
        m = re.search(
            r"HV50_BOUNDS_PERCENT\s*\(?\s*=\s*\(\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*,\s*"
            r"([0-9]+(?:\.[0-9]+)?)\s*\)",
            system6_doc_text,
        )
        assert m is not None, (
            "docs にHV50_BOUNDS_PERCENT の tuple 参照が見当たりません。"
            "「実装参照: ... HV50_BOUNDS_PERCENT (= (10.0, 40.0))」形式で"
            "明記してください。"
        )
        doc_lo = float(m.group(1))
        doc_hi = float(m.group(2))
        assert (doc_lo, doc_hi) == (impl_lo, impl_hi), (
            f"docs の HV50 bounds ({doc_lo}, {doc_hi}) と "
            f"impl の HV50_BOUNDS_PERCENT=({impl_lo}, {impl_hi}) が乖離しています。"
        )


# ----------------------------------------------------------------
# ---------------------------------------------------------------------------
# Meta: change log presence (簡易 sanity)
# ---------------------------------------------------------------------------


class TestChangeLogPresence:
    """変更履歴の明示的な marker が docs 末尾に残っていることを保証。"""

    def test_system4_change_log(self, system4_doc_text: str) -> None:
        assert (
            "2026-07-02 impl-alignment update" in system4_doc_text
        ), "docs/systems/システム4.txt に change log marker が見当たりません。"

    def test_system6_change_log(self, system6_doc_text: str) -> None:
        assert (
            "2026-07-02 impl-alignment update" in system6_doc_text
        ), "docs/systems/システム6.txt に change log marker が見当たりません。"
