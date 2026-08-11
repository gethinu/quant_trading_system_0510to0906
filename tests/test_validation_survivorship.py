"""Tests for survivorship audit / point-in-time universe / guard."""

from __future__ import annotations

import logging

import pandas as pd
import pytest

from common.validation.survivorship import (
    PointInTimeUniverse,
    SurvivorshipError,
    audit_survivorship,
    survivorship_guard,
)


def _write_membership(path, rows):
    pd.DataFrame(rows).to_csv(path, index=False)


def test_audit_no_membership_file_is_biased(tmp_path):
    audit = audit_survivorship(["AAA", "BBB"], root=tmp_path)
    assert audit.biased is True
    assert audit.membership_file_present is False
    assert audit.universe_size == 2


def test_audit_with_delisted_symbols_is_survivorship_free(tmp_path):
    mpath = tmp_path / "data" / "universe_membership.csv"
    mpath.parent.mkdir(parents=True)
    _write_membership(
        mpath,
        [
            {"symbol": "AAA", "list_date": "2010-01-01", "delist_date": ""},
            {"symbol": "DEAD", "list_date": "2010-01-01", "delist_date": "2015-06-30"},
        ],
    )
    audit = audit_survivorship(["AAA", "DEAD"], root=tmp_path)
    assert audit.survivorship_free is True
    assert audit.biased is False


def test_point_in_time_members_asof():
    df = pd.DataFrame(
        [
            {"symbol": "AAA", "list_date": "2010-01-01", "delist_date": ""},
            {"symbol": "DEAD", "list_date": "2010-01-01", "delist_date": "2015-06-30"},
            {"symbol": "NEW", "list_date": "2018-01-01", "delist_date": ""},
        ]
    )
    pit = PointInTimeUniverse(
        membership=pd.DataFrame(
            {
                "symbol": df["symbol"],
                "list_date": pd.to_datetime(df["list_date"]),
                "delist_date": pd.to_datetime(df["delist_date"], errors="coerce"),
            }
        )
    )
    # DEAD was a member in 2014 (removes survivorship bias), gone by 2016.
    assert pit.members_asof("2014-01-01") == {"AAA", "DEAD"}
    assert pit.members_asof("2016-01-01") == {"AAA"}
    assert pit.members_asof("2019-01-01") == {"AAA", "NEW"}
    assert pit.is_survivorship_free() is True


def test_guard_off_is_silent(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        audit = survivorship_guard(["AAA"], mode="off", root=tmp_path)
    assert audit.biased is True
    assert caplog.records == []  # OFF must be byte-parity silent


def test_guard_warn_logs_when_biased(tmp_path, caplog):
    with caplog.at_level(logging.WARNING):
        survivorship_guard(["AAA"], mode="warn", root=tmp_path)
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert len(warnings) == 1
    assert "SURVIVORSHIP-BIASED" in warnings[0].getMessage()


def test_guard_enforce_raises_when_biased(tmp_path):
    with pytest.raises(SurvivorshipError):
        survivorship_guard(["AAA"], mode="enforce", root=tmp_path)
