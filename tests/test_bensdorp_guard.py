from __future__ import annotations

import json

import tools.check_bensdorp_guard as guard


def test_manifest_matches_current_bensdorp_sources_and_config():
    assert guard.verify_manifest() == []


def test_protected_surface_contains_all_system1_to_7_implementations():
    protected = set(guard.PROTECTED_FILES)
    for idx in range(1, 8):
        assert f"core/system{idx}.py" in protected
        assert f"strategies/system{idx}_strategy.py" in protected

    assert "common/system_setup_predicates.py" in protected
    assert "common/trade_management.py" in protected
    assert "common/profit_protection.py" in protected
    assert "strategies/constants.py" in protected


def test_manifest_records_reviewable_config_snapshot():
    payload = json.loads(guard.MANIFEST_PATH.read_text(encoding="utf-8"))
    config = payload["protected_config"]
    assert set(config["strategies"]) == {f"system{i}" for i in range(1, 8)}
    assert set(config["ui"]) == {"long_allocations", "short_allocations"}
    assert set(config["risk"]) == {"risk_pct", "max_positions", "max_pct"}


def test_pr_gate_blocks_protected_change_without_owner_approval(monkeypatch):
    monkeypatch.setattr(
        guard, "protected_changes", lambda _base, _head: ["core/system1.py"]
    )
    assert guard.command_gate("base", "head", "[]") == 1


def test_pr_gate_accepts_exact_owner_approval_label(monkeypatch):
    monkeypatch.setattr(
        guard, "protected_changes", lambda _base, _head: ["core/system1.py"]
    )
    labels = json.dumps([guard.APPROVAL_LABEL])
    assert guard.command_gate("base", "head", labels) == 0


def test_local_gate_requires_explicit_environment_override(monkeypatch):
    monkeypatch.setattr(guard, "verify_manifest", lambda: [])
    monkeypatch.setattr(
        guard, "protected_changes", lambda _base, _head: ["core/system7.py"]
    )
    monkeypatch.delenv(guard.APPROVAL_ENV, raising=False)
    assert guard.command_local_gate("base", "head") == 1

    monkeypatch.setenv(guard.APPROVAL_ENV, "1")
    assert guard.command_local_gate("base", "head") == 0


def test_current_guard_branch_has_no_book_logic_change_vs_origin_main():
    assert guard.protected_changes("origin/main", "HEAD") == []


def test_source_fingerprint_is_line_ending_invariant(tmp_path):
    lf = tmp_path / "lf.py"
    crlf = tmp_path / "crlf.py"
    lf.write_bytes(b"a = 1\nb = 2\n")
    crlf.write_bytes(b"a = 1\r\nb = 2\r\n")
    assert guard._sha256(lf) == guard._sha256(crlf)
