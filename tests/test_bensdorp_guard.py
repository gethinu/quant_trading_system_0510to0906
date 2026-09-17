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
    assert "common/system_constants.py" in protected
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
    monkeypatch.setattr(guard, "verify_ref", lambda _head: [])
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


def test_unrelated_old_branch_does_not_need_candidate_manifest(monkeypatch):
    monkeypatch.setattr(guard, "protected_changes", lambda _base, _head: [])

    def should_not_run(_head):
        raise AssertionError("verify_ref must not run for an unrelated old branch")

    monkeypatch.setattr(guard, "verify_ref", should_not_run)
    assert guard.command_gate("base", "old-head", "[]") == 0


def test_approved_protected_change_must_match_candidate_manifest(monkeypatch):
    monkeypatch.setattr(
        guard, "protected_changes", lambda _base, _head: ["core/system2.py"]
    )
    monkeypatch.setattr(
        guard, "verify_ref", lambda _head: ["protected source changed: core/system2.py"]
    )
    labels = json.dumps([guard.APPROVAL_LABEL])
    assert guard.command_gate("base", "head", labels) == 1


def test_protected_changes_diff_from_merge_base(monkeypatch):
    calls = []

    def fake_git(*args):
        calls.append(args)
        if args[0] == "merge-base":
            return "merge-base-sha\n"
        if args[0] == "diff":
            return ""
        raise AssertionError(args)

    monkeypatch.setattr(guard, "_git", fake_git)
    assert guard.protected_changes("moving-base-tip", "old-pr-head") == []
    assert calls[0] == ("merge-base", "moving-base-tip", "old-pr-head")
    assert calls[1][3:5] == ("merge-base-sha", "old-pr-head")


def test_verify_ref_compares_manifest_with_git_object_payload(monkeypatch):
    payload = guard._current_manifest_payload()
    monkeypatch.setattr(guard, "_load_manifest_at", lambda _ref: payload)
    monkeypatch.setattr(guard, "_manifest_payload_at", lambda _ref: payload)
    assert guard.verify_ref("candidate") == []


def test_guard_control_plane_is_self_protected_after_bootstrap():
    assert set(guard.CONTROL_PLANE_FILES) == {
        ".github/workflows/bensdorp-integrity.yml",
        "tools/check_bensdorp_guard.py",
        "config/bensdorp_guard_manifest.json",
    }


def test_github_gate_uses_pull_request_target_and_never_checks_out_head():
    workflow = (guard.ROOT / ".github/workflows/bensdorp-integrity.yml").read_text(
        encoding="utf-8"
    )
    assert "pull_request_target:" in workflow
    assert "\n  pull_request:\n" not in workflow
    assert "Checkout trusted base" in workflow
    assert "Fetch untrusted PR head as Git object only" in workflow
    assert "types: [opened, synchronize, reopened, labeled, unlabeled]" in workflow
    assert "--head FETCH_HEAD" in workflow
