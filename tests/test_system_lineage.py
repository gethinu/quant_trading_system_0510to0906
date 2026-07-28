"""システムの血統 (lineage) レジストリの契約テスト。

System1-7 (Bensdorp 準拠) と System8 (独自開発) を **機械的に** 区別できる状態を
守るためのテスト。血統の記録が消えたり、番号から推測する実装に退行したりすると
ここで落ちる。背景は docs/SYSTEM_LINEAGE.md を参照。
"""

from __future__ import annotations

from pathlib import Path

import pytest

from common.system_constants import (
    LINEAGE_BENSDORP,
    LINEAGE_LABELS,
    LINEAGE_ORIGINAL,
    SYSTEM_CONFIGS,
    SYSTEM_LINEAGE,
    get_system_lineage,
)
from common.system_groups import (
    GROUP_DISPLAY_NAMES,
    LINEAGE_MARKER,
    format_system_label,
    lineage_legend,
    lineage_marker,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
ALL_SYSTEMS = tuple(f"system{i}" for i in range(1, 9))


def test_all_systems_have_a_lineage() -> None:
    """1-8 の全 system が血統を持つ（抜けがあると分類不能になる）。"""
    assert set(SYSTEM_LINEAGE) == set(ALL_SYSTEMS)


def test_system1_to_7_are_bensdorp() -> None:
    for name in (f"system{i}" for i in range(1, 8)):
        assert get_system_lineage(name) == LINEAGE_BENSDORP, name


def test_system8_is_original_not_bensdorp() -> None:
    """System8 は独自開発。ここが bensdorp に変わったら血統の取り違え。"""
    assert get_system_lineage("system8") == LINEAGE_ORIGINAL
    assert get_system_lineage("system8") != LINEAGE_BENSDORP


def test_get_system_lineage_is_case_insensitive() -> None:
    assert get_system_lineage("System8") == LINEAGE_ORIGINAL


def test_get_system_lineage_rejects_unknown() -> None:
    with pytest.raises(KeyError):
        get_system_lineage("system99")


def test_lineage_values_are_known() -> None:
    assert set(SYSTEM_LINEAGE.values()) <= {LINEAGE_BENSDORP, LINEAGE_ORIGINAL}
    assert set(LINEAGE_LABELS) == {LINEAGE_BENSDORP, LINEAGE_ORIGINAL}


def test_system_configs_lineage_matches_registry() -> None:
    """SYSTEM_CONFIGS の lineage が正準マップと食い違わない（二重管理の drift 防止）。"""
    for name, lineage in SYSTEM_LINEAGE.items():
        assert SYSTEM_CONFIGS[name]["lineage"] == lineage, name


def test_marker_only_on_original_lineage() -> None:
    assert lineage_marker("system8") == LINEAGE_MARKER
    for i in range(1, 8):
        assert lineage_marker(f"system{i}") == ""
    # 未知の system にマーカーを付けない（誤って独自扱いしない）。
    assert lineage_marker("unknown") == ""


def test_format_system_label_marks_only_system8() -> None:
    assert format_system_label("system8") == f"System8{LINEAGE_MARKER}"
    assert format_system_label("system1") == "System1"


def test_lineage_legend_explains_the_marker() -> None:
    legend = lineage_legend()
    assert LINEAGE_MARKER in legend
    assert LINEAGE_LABELS[LINEAGE_ORIGINAL] in legend


def test_long_group_label_does_not_equate_system8_with_bensdorp_longs() -> None:
    """System8 を System1/3/5 と同列に並べない（配分プールも別扱い）。"""
    label = GROUP_DISPLAY_NAMES["long"]
    assert "System8" in label
    assert LINEAGE_MARKER in label
    assert "System1,3,5,8" not in label


@pytest.mark.parametrize("system_no", range(1, 9))
def test_source_files_declare_lineage(system_no: int) -> None:
    """core/ と strategies/ の先頭に血統行が残っている（消えない形で残す要件）。"""
    expected = "original" if system_no == 8 else "bensdorp"
    for rel in (
        f"core/system{system_no}.py",
        f"strategies/system{system_no}_strategy.py",
    ):
        head = (REPO_ROOT / rel).read_text(encoding="utf-8")[:1200]
        assert "Lineage:" in head, rel
        assert expected in head, rel


def test_dashboard_lineage_map_matches_python_registry() -> None:
    """ダッシュ側 (format.ts) の写しが Python 側の正準マップと一致する。"""
    ts = (REPO_ROOT / "apps/dashboards/alpaca-next/lib/format.ts").read_text(
        encoding="utf-8"
    )
    for name, lineage in SYSTEM_LINEAGE.items():
        assert f"{name}: '{lineage}'" in ts, name
    assert f"LINEAGE_MARKER = '{LINEAGE_MARKER}'" in ts


def test_lineage_doc_exists() -> None:
    doc = REPO_ROOT / "docs/SYSTEM_LINEAGE.md"
    assert doc.exists()
    text = doc.read_text(encoding="utf-8")
    assert "Bensdorp" in text
    assert "System8" in text
