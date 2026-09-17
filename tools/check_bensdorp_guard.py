from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "config" / "bensdorp_guard_manifest.json"
CONFIG_PATH = ROOT / "config" / "config.yaml"
APPROVAL_LABEL = "bensdorp-strategy-change-approved"
APPROVAL_ENV = "BENSDORP_STRATEGY_CHANGE_APPROVED"

PROTECTED_FILES = tuple(
    [f"core/system{i}.py" for i in range(1, 8)]
    + [f"strategies/system{i}_strategy.py" for i in range(1, 8)]
    + [
        "common/system_setup_predicates.py",
        "common/trade_management.py",
        "common/profit_protection.py",
        "strategies/constants.py",
    ]
)


def _sha256(path: Path) -> str:
    # Normalize line endings so the frozen fingerprint is stable on Windows/Linux.
    data = path.read_bytes().replace(b"\r\n", b"\n").replace(b"\r", b"\n")
    return hashlib.sha256(data).hexdigest()


def _load_yaml_text(text: str) -> dict[str, Any]:
    value = yaml.safe_load(text) or {}
    if not isinstance(value, dict):
        raise ValueError("config YAML root must be a mapping")
    return value


def _protected_config_subset(data: dict[str, Any]) -> dict[str, Any]:
    strategies = data.get("strategies") or {}
    ui = data.get("ui") or {}
    risk = data.get("risk") or {}
    return {
        "strategies": {f"system{i}": strategies.get(f"system{i}") for i in range(1, 8)},
        "ui": {
            "long_allocations": ui.get("long_allocations"),
            "short_allocations": ui.get("short_allocations"),
        },
        "risk": {
            key: risk.get(key) for key in ("risk_pct", "max_positions", "max_pct")
        },
    }


def _current_manifest_payload() -> dict[str, Any]:
    config_data = _load_yaml_text(CONFIG_PATH.read_text(encoding="utf-8"))
    return {
        "version": 1,
        "approval_label": APPROVAL_LABEL,
        "protected_files": {
            path: (_sha256(ROOT / path) if (ROOT / path).is_file() else "__MISSING__")
            for path in sorted(PROTECTED_FILES)
        },
        "protected_config": _protected_config_subset(config_data),
    }


def _load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise FileNotFoundError(f"missing manifest: {MANIFEST_PATH}")
    value = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise ValueError("Bensdorp guard manifest must be a JSON object")
    return value


def verify_manifest() -> list[str]:
    expected = _load_manifest()
    current = _current_manifest_payload()
    errors: list[str] = []

    if expected.get("version") != current["version"]:
        errors.append("manifest version mismatch")
    if expected.get("approval_label") != APPROVAL_LABEL:
        errors.append("approval label changed")

    expected_files = expected.get("protected_files") or {}
    current_files = current["protected_files"]
    if set(expected_files) != set(current_files):
        missing = sorted(set(current_files) - set(expected_files))
        extra = sorted(set(expected_files) - set(current_files))
        if missing:
            errors.append(f"manifest missing protected paths: {missing}")
        if extra:
            errors.append(f"manifest has unexpected protected paths: {extra}")

    for path, actual_hash in current_files.items():
        expected_hash = expected_files.get(path)
        if expected_hash != actual_hash:
            errors.append(f"protected source changed: {path}")

    if expected.get("protected_config") != current["protected_config"]:
        errors.append("protected Bensdorp config values changed")

    return errors


def _git(*args: str) -> str:
    proc = subprocess.run(
        ["git", *args],
        cwd=ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        raise RuntimeError(proc.stderr.strip() or "git command failed")
    return proc.stdout


def _git_show(ref: str, path: str) -> str | None:
    proc = subprocess.run(
        ["git", "show", f"{ref}:{path}"],
        cwd=ROOT,
        check=False,
        text=True,
        encoding="utf-8",
        errors="replace",
        capture_output=True,
    )
    if proc.returncode != 0:
        return None
    return proc.stdout


def _manifest_exists_at(ref: str) -> bool:
    return _git_show(ref, "config/bensdorp_guard_manifest.json") is not None


def _protected_config_changed(base: str, head: str) -> bool:
    before = _git_show(base, "config/config.yaml")
    after = _git_show(head, "config/config.yaml")
    if before is None or after is None:
        return before != after
    return _protected_config_subset(
        _load_yaml_text(before)
    ) != _protected_config_subset(_load_yaml_text(after))


def protected_changes(base: str, head: str) -> list[str]:
    names = set(
        line.strip()
        for line in _git(
            "diff", "--name-only", "--diff-filter=ACMRD", base, head, "--"
        ).splitlines()
        if line.strip()
    )
    changed = sorted(names.intersection(PROTECTED_FILES))

    if "config/config.yaml" in names and _protected_config_changed(base, head):
        changed.append("config/config.yaml::bensdorp-sections")

    manifest_rel = "config/bensdorp_guard_manifest.json"
    # Bootstrap PR is allowed to add the first manifest. After that, manifest
    # edits are themselves approval-gated so the baseline cannot drift quietly.
    if manifest_rel in names and _manifest_exists_at(base):
        changed.append(manifest_rel)

    return sorted(set(changed))


def _labels_from_json(raw: str | None) -> set[str]:
    if not raw:
        return set()
    value = json.loads(raw)
    if not isinstance(value, list):
        raise ValueError("labels JSON must be an array")
    return {str(item) for item in value}


def _print_changes(changes: list[str]) -> None:
    print("Bensdorp protected strategy surface changed:")
    for path in changes:
        print(f"  - {path}")


def command_verify() -> int:
    errors = verify_manifest()
    if not errors:
        print("Bensdorp strategy fingerprint: OK")
        return 0
    print("Bensdorp strategy fingerprint: FAILED", file=sys.stderr)
    for error in errors:
        print(f"  - {error}", file=sys.stderr)
    print(
        "Do not update the baseline without explicit strategy-change approval.",
        file=sys.stderr,
    )
    return 1


def command_gate(base: str, head: str, labels_json: str | None) -> int:
    changes = protected_changes(base, head)
    if not changes:
        print("Bensdorp PR gate: no protected strategy changes")
        return 0

    _print_changes(changes)
    labels = _labels_from_json(labels_json)
    if APPROVAL_LABEL in labels:
        print(f"Bensdorp PR gate: approved by label '{APPROVAL_LABEL}'")
        return 0

    print(
        f"Bensdorp PR gate: BLOCKED. Add label '{APPROVAL_LABEL}' only after explicit owner approval.",
        file=sys.stderr,
    )
    return 1


def command_local_gate(base: str, head: str) -> int:
    errors = verify_manifest()
    changes = protected_changes(base, head)
    if errors:
        print("Bensdorp local gate: fingerprint mismatch", file=sys.stderr)
        for error in errors:
            print(f"  - {error}", file=sys.stderr)
    if not changes and not errors:
        print("Bensdorp local gate: no protected changes")
        return 0

    if changes:
        _print_changes(changes)
    if os.environ.get(APPROVAL_ENV) == "1" and not errors:
        print(f"Bensdorp local gate: explicit {APPROVAL_ENV}=1 override accepted")
        return 0

    print(
        f"Bensdorp local gate: BLOCKED. Explicit approval requires {APPROVAL_ENV}=1 after baseline review.",
        file=sys.stderr,
    )
    return 1


def command_write_manifest() -> int:
    if MANIFEST_PATH.exists() and os.environ.get(APPROVAL_ENV) != "1":
        print(
            f"Refusing to rewrite an existing Bensdorp baseline without {APPROVAL_ENV}=1.",
            file=sys.stderr,
        )
        return 1
    payload = _current_manifest_payload()
    MANIFEST_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(f"Wrote Bensdorp guard manifest: {MANIFEST_PATH}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Protect Bensdorp System1-7 strategy logic"
    )
    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser(
        "verify", help="verify current sources/config against the frozen manifest"
    )
    sub.add_parser(
        "write-manifest", help="write or explicitly refresh the frozen manifest"
    )

    gate = sub.add_parser(
        "gate", help="PR approval gate for protected strategy changes"
    )
    gate.add_argument("--base", required=True)
    gate.add_argument("--head", default="HEAD")
    gate.add_argument("--labels-json", default="[]")

    local = sub.add_parser(
        "local-gate", help="pre-push gate for protected strategy changes"
    )
    local.add_argument("--base", default="origin/main")
    local.add_argument("--head", default="HEAD")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "verify":
        return command_verify()
    if args.command == "write-manifest":
        return command_write_manifest()
    if args.command == "gate":
        return command_gate(args.base, args.head, args.labels_json)
    if args.command == "local-gate":
        return command_local_gate(args.base, args.head)
    raise AssertionError(f"unhandled command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
