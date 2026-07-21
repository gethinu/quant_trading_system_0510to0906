"""Tests for scripts/check_dashboard_freshness.py.

Pins the publish-gap detector that catches the 2026-07-22 failure mode: the
pipeline generated (and ntfy'd) fresh signals, but the dashboard publish was
lost when the wrapper died -> results_csv ahead of data/ -> STALE.
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pytest

_MOD_PATH = (
    Path(__file__).resolve().parents[2] / "scripts" / "check_dashboard_freshness.py"
)

_spec = importlib.util.spec_from_file_location("check_dashboard_freshness", _MOD_PATH)
assert _spec and _spec.loader
cdf = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(cdf)


def _write_signal(directory: Path, date_compact: str, total: int = 3) -> None:
    directory.mkdir(parents=True, exist_ok=True)
    payload = {
        "version": "1.0",
        "date": f"{date_compact[0:4]}-{date_compact[4:6]}-{date_compact[6:8]}",
        "generated_at": f"{date_compact[0:4]}-{date_compact[4:6]}-{date_compact[6:8]}T06:00:00+09:00",
        "portfolio": {"total_signals": total},
        "systems": {},
    }
    (directory / f"today_signals_{date_compact}.json").write_text(
        json.dumps(payload), encoding="utf-8"
    )


class TestNewestSignalDate:
    def test_missing_dir_returns_none(self, tmp_path: Path):
        assert cdf.newest_signal_date(tmp_path / "nope") is None

    def test_empty_dir_returns_none(self, tmp_path: Path):
        assert cdf.newest_signal_date(tmp_path) is None

    def test_picks_max_date(self, tmp_path: Path):
        _write_signal(tmp_path, "20260720")
        _write_signal(tmp_path, "20260722")
        _write_signal(tmp_path, "20260721")
        assert cdf.newest_signal_date(tmp_path) == 20260722

    def test_ignores_non_matching_files(self, tmp_path: Path):
        _write_signal(tmp_path, "20260721")
        (tmp_path / "pipeline_20260799.json").write_text("{}", encoding="utf-8")
        (tmp_path / "narrative_20260799.json").write_text("{}", encoding="utf-8")
        assert cdf.newest_signal_date(tmp_path) == 20260721

    def test_numeric_not_lexical_ordering(self, tmp_path: Path):
        # both same length here, but confirm int compare is used
        _write_signal(tmp_path, "20260702")
        _write_signal(tmp_path, "20260701")
        assert cdf.newest_signal_date(tmp_path) == 20260702


class TestCheckFreshness:
    def test_fresh_when_data_matches_results(self, tmp_path: Path):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260722")
        r = cdf.check_freshness(results, data)
        assert r["status"] == "fresh"

    def test_stale_when_results_ahead_of_data(self, tmp_path: Path):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260721")
        r = cdf.check_freshness(results, data)
        assert r["status"] == "stale"
        assert r["results_date"] == 20260722
        assert r["data_date"] == 20260721
        assert r["results_date_str"] == "2026-07-22"
        assert r["data_date_str"] == "2026-07-21"

    def test_stale_when_data_dir_empty_but_results_present(self, tmp_path: Path):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        data.mkdir()
        assert cdf.check_freshness(results, data)["status"] == "stale"

    def test_fresh_when_no_results_to_publish(self, tmp_path: Path):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        results.mkdir()
        _write_signal(data, "20260721")
        assert cdf.check_freshness(results, data)["status"] == "fresh"

    def test_unknown_when_both_empty(self, tmp_path: Path):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        results.mkdir()
        data.mkdir()
        assert cdf.check_freshness(results, data)["status"] == "unknown"

    def test_data_never_ahead_is_not_flagged(self, tmp_path: Path):
        # served newer than generated (e.g. results purged) -> not stale
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260721")
        _write_signal(data, "20260722")
        assert cdf.check_freshness(results, data)["status"] == "fresh"


class TestMainExitCodes:
    def test_exit_0_when_fresh(self, tmp_path: Path, capsys):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260722")
        rc = cdf.main(["--results-dir", str(results), "--data-dir", str(data)])
        assert rc == 0

    def test_exit_2_when_stale(self, tmp_path: Path, capsys):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260721")
        rc = cdf.main(["--results-dir", str(results), "--data-dir", str(data)])
        assert rc == 2
        out = capsys.readouterr().out
        assert "STALE" in out

    def test_json_output_is_parseable(self, tmp_path: Path, capsys):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260721")
        cdf.main(
            [
                "--results-dir",
                str(results),
                "--data-dir",
                str(data),
                "--json",
            ]
        )
        line = capsys.readouterr().out.strip().splitlines()[0]
        parsed = json.loads(line)
        assert parsed["status"] == "stale"

    def test_notify_not_called_without_flag(self, tmp_path: Path, monkeypatch):
        # ensure no accidental network: _notify_stale must not run without --notify
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260721")
        called = {"n": 0}

        def _boom(_result):
            called["n"] += 1

        monkeypatch.setattr(cdf, "_notify_stale", _boom)
        rc = cdf.main(["--results-dir", str(results), "--data-dir", str(data)])
        assert rc == 2
        assert called["n"] == 0

    def test_notify_called_with_flag(self, tmp_path: Path, monkeypatch):
        results = tmp_path / "results_csv"
        data = tmp_path / "data"
        _write_signal(results, "20260722")
        _write_signal(data, "20260721")
        called = {"n": 0}
        monkeypatch.setattr(cdf, "_notify_stale", lambda _r: called.__setitem__("n", 1))
        cdf.main(
            [
                "--results-dir",
                str(results),
                "--data-dir",
                str(data),
                "--notify",
            ]
        )
        assert called["n"] == 1


if __name__ == "__main__":
    raise SystemExit(pytest.main([__file__, "-v"]))
