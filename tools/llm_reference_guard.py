from __future__ import annotations

import argparse
import os
import subprocess
import sys

LLM_PREFIX = "docs/llm/"

TRIGGER_PREFIXES = (
    "core/",
    "common/",
    "strategies/",
    "scripts/",
    "apps/",
    "config/",
    "data/",
    "docs/systems/",
    "docs/technical/",
    "docs/operations/",
    "docs/today_signal_scan/",
)

TRIGGER_FILES = (
    "README.md",
    "docs/README.md",
    "docs/TECHNICAL_SPECS.md",
    "docs/technical/environment_variables.md",
    "docs/systems/INDEX.md",
    ".github/copilot-instructions.md",
    ".agent/workflows/project-reference.md",
)


def _normalize(path: str) -> str:
    return path.replace("\\", "/").strip()


def _is_truthy(value: str | None) -> bool:
    if value is None:
        return False
    return value.strip().lower() in {"1", "true", "yes", "on"}


def _collect_files(args: list[str]) -> list[str]:
    if args:
        return args

    def _git_diff(*cmd: str) -> list[str]:
        try:
            output = subprocess.check_output(cmd, text=True, stderr=subprocess.DEVNULL)
        except Exception:
            return []
        return [line for line in output.splitlines() if line.strip()]

    files = _git_diff("git", "diff", "--name-only", "--cached")
    if files:
        return files
    return _git_diff("git", "diff", "--name-only")


def _is_trigger(path: str) -> bool:
    if path.startswith(LLM_PREFIX):
        return False
    if path in TRIGGER_FILES:
        return True
    return any(path.startswith(prefix) for prefix in TRIGGER_PREFIXES)


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        description="Prompt when docs/llm references should be updated.",
    )
    parser.add_argument("files", nargs="*", help="Changed files to inspect.")
    parser.add_argument(
        "--strict",
        action="store_true",
        help="Exit with error when update is required.",
    )
    parser.add_argument(
        "--max-list",
        type=int,
        default=20,
        help="Limit the number of listed files.",
    )
    args = parser.parse_args(argv)

    if _is_truthy(os.getenv("LLM_REF_GUARD_SKIP")):
        return 0

    files = [_normalize(f) for f in _collect_files(args.files)]
    if not files:
        return 0

    changed_llm = any(f.startswith(LLM_PREFIX) for f in files)
    trigger_files = [f for f in files if _is_trigger(f)]

    if trigger_files and not changed_llm:
        print("LLM reference guard: potential updates detected.")
        print("If behavior/interfaces/state changed, update docs/llm/*.")
        print("Changed files (subset):")
        for path in trigger_files[: args.max_list]:
            print(f"  - {path}")
        remaining = len(trigger_files) - args.max_list
        if remaining > 0:
            print(f"  ... {remaining} more")
        print("To bypass: set LLM_REF_GUARD_SKIP=1")
        return 1 if args.strict else 0

    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
