from pathlib import Path

import pytest

from common.trade_management import SYSTEM_TRADE_RULES
from scripts import export_alpaca_snapshot as ex

ROOT = Path(__file__).resolve().parents[1]
COMPONENT = (
    ROOT / "apps" / "dashboards" / "alpaca-next" / "components" / "AlpacaSection.tsx"
)
LEGACY_EXPORTER = ROOT / "scripts" / "export_alpaca_snapshot_legacy.py"


def test_trailing_snapshot_uses_resting_broker_stop_first():
    rules = SYSTEM_TRADE_RULES["system1"]
    stop, target = ex._estimate_stop_target(
        side="long",
        avg_entry=100.0,
        rules=rules,
        atr={int(rules.stop_atr_period): 99.0},
        actual_stop=91.25,
        hwm=120.0,
    )
    assert stop == pytest.approx(91.25)
    assert target is None


def test_trailing_snapshot_uses_hwm_not_atr_when_broker_price_absent():
    s1 = SYSTEM_TRADE_RULES["system1"]
    s4 = SYSTEM_TRADE_RULES["system4"]
    stop1, _ = ex._estimate_stop_target(
        side="long",
        avg_entry=100.0,
        rules=s1,
        atr={int(s1.stop_atr_period): 50.0},
        hwm=120.0,
    )
    stop4, _ = ex._estimate_stop_target(
        side="long",
        avg_entry=100.0,
        rules=s4,
        atr={int(s4.stop_atr_period): 50.0},
        hwm=120.0,
    )
    assert stop1 == pytest.approx(90.0)  # HWM * 0.75
    assert stop4 == pytest.approx(96.0)  # HWM * 0.80


def test_trailing_snapshot_refuses_fake_atr_or_one_cent_fallback():
    rules = SYSTEM_TRADE_RULES["system1"]
    stop, _ = ex._estimate_stop_target(
        side="long",
        avg_entry=6.77,
        rules=rules,
        atr={int(rules.stop_atr_period): 10.0},
    )
    assert stop is None


def test_non_trailing_stops_keep_canonical_execution_math():
    rules = SYSTEM_TRADE_RULES["system3"]
    stop, _ = ex._estimate_stop_target(
        side="long",
        avg_entry=100.0,
        rules=rules,
        atr={int(rules.stop_atr_period): 2.0},
    )
    assert stop is not None


def test_non_trailing_estimator_does_not_recurse_while_wrapper_is_installed():
    """build_snapshot temporarily installs the wrapper; delegation must stay on the original."""
    rules = SYSTEM_TRADE_RULES["system3"]
    original = ex._legacy._estimate_stop_target
    ex._legacy._estimate_stop_target = ex._estimate_stop_target
    try:
        stop, _ = ex._estimate_stop_target(
            side="long",
            avg_entry=100.0,
            rules=rules,
            atr={int(rules.stop_atr_period): 2.0},
        )
    finally:
        ex._legacy._estimate_stop_target = original
    assert stop is not None


def test_dashboard_surfaces_authoritative_protection_states():
    text = COMPONENT.read_text(encoding="utf-8")
    assert "trail ${pct} ✓ broker · stop ${stop}" in text
    assert "soft trail ${pct} ⚠ · stop ${stop}" in text
    assert "UNPROTECTED · trail ${pct}" in text
    assert "pending arm" in text
    assert "broker observation unmeasured" in text
    assert "OPEN orders → broker HWM → soft HWM" in text


def test_strategy_trailing_widths_are_canonical_and_not_hardcoded_in_ui():
    assert SYSTEM_TRADE_RULES["system1"].trailing_stop_pct == pytest.approx(0.25)
    assert SYSTEM_TRADE_RULES["system4"].trailing_stop_pct == pytest.approx(0.20)
    component = COMPONENT.read_text(encoding="utf-8")
    assert "p.trailing_stop_pct * 100" in component
    assert "trail 25%" not in component
    assert "trail 20%" not in component


def test_preserved_exporter_still_exports_trailing_intent():
    legacy = LEGACY_EXPORTER.read_text(encoding="utf-8")
    assert '"trailing_stop_pct": (' in legacy
