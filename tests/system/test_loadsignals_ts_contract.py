"""System test: apps/dashboards/alpaca-next/lib/loadSignals.ts の選択契約を固定する.

Phase 1 audit gap:
    - 2026-07-02 に 07-02 stub file (未来日) が 07-01 real data (49 signals) を
      lexically 大きい方として picked し dashboard 上の real signal を隠す事象
      発生. 原因は `.sort()` の string 昇順 + `files[length-1]` (末尾) 選択.

契約:
    - 日付 8 桁 (YYYYMMDD) を filename から抽出して数値降順で並べる
    - 空 stub file を弾く (MIN_USABLE_BYTES または portfolio.total_signals=0 等)
    - Lexical string sort を無条件で末尾拾いする実装が復活しないことを固定

TS を Node で実行する CI 構成が無いため, 正規化された source (regex + 部分文字列)
の presence assertion で契約を固定する.
"""

from __future__ import annotations

from pathlib import Path

import pytest

LOADSIGNALS = (
    Path(__file__).resolve().parents[2]
    / "apps" / "dashboards" / "alpaca-next" / "lib" / "loadSignals.ts"
)


@pytest.fixture(scope="module")
def ts_text() -> str:
    assert LOADSIGNALS.exists(), f"{LOADSIGNALS} が存在しない = dashboard loader 消失"
    return LOADSIGNALS.read_text(encoding="utf-8", errors="replace")


class TestLoadSignalsSelectionContract:
    """loadSignals.ts の '最新かつ非空' 選択契約."""

    def test_date_extract_helper_present(self, ts_text: str):
        """YYYYMMDD 8 桁を filename から数値化する helper が存在."""
        assert "extractSignalDate" in ts_text, \
            "date 抽出 helper が消失. lexical string sort 復活のリスク"
        # regex は today_signals_(\d{8})\.json を捉える
        assert r"today_signals_(\d{8})\.json" in ts_text

    def test_no_naive_lexical_sort(self, ts_text: str):
        """★ 2026-07-02 bug 直接固定:
        `.sort()` 引数無し (lexical) + `files[length-1]` (末尾) の
        naive 実装が復活していないこと.
        """
        # sort に comparator を渡している行がある (numeric 降順)
        assert "b.d - a.d" in ts_text or "b.date - a.date" in ts_text, (
            "date 数値降順 comparator が消失. lexical sort 復活の疑い"
        )
        # 末尾拾い (files[files.length - 1]) が復活していない
        assert "files[files.length - 1]" not in ts_text, (
            "末尾拾い (lexically 最後) 実装は復活禁止. "
            "未来日 stub が real data を隠すため"
        )

    def test_empty_stub_filter_present(self, ts_text: str):
        """空 stub file を弾く filter (MIN_USABLE_BYTES / total_signals guard)."""
        assert "isUsableSignalFile" in ts_text, \
            "stub file guard が消失. stale stub が real data を上書きするリスク"
        assert "MIN_USABLE_BYTES" in ts_text
        # portfolio.total_signals もしくは signals 配列を見ている
        assert "total_signals" in ts_text or "signals" in ts_text

    def test_directory_preference_order_unchanged(self, ts_text: str):
        """data/ を results_csv/ よりも優先 (Vercel build 時は data/ のみ存在)."""
        # tryDirs は data → results_csv → mock の順を維持
        d = ts_text.find("process.cwd(), 'data'")
        r = ts_text.find("REPO_ROOT, 'results_csv'")
        m = ts_text.find("process.cwd(), 'mock'")
        assert d > 0 and r > 0 and m > 0
        assert d < r < m, (
            "tryDirs の優先順序が変わった. Vercel build は data/ のみ存在するため "
            "data → results_csv → mock の順を維持する必要がある"
        )
