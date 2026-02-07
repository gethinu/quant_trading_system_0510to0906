# LLM Reference

This folder contains always-referenced, stable summaries for AI-assisted development. Keep these documents updated whenever behavior, interfaces, or outputs change.

## Index
- `SPEC.md`: Product intent, scope, and invariants
- `ARCHITECTURE.md`: System structure and data flow
- `INTERFACES.md`: CLI, UI, API, and output contracts
- `STATE.md`: Persistent state locations and lifecycle

## Update Triggers
- Changes to system logic, diagnostics schema, or allocation contract
- New entrypoints, flags, API endpoints, or outputs
- Changes to cache structure or new stateful directories

## Guard
Pre-push runs `tools/llm_reference_guard.py` to prompt updates when core areas change.
Set `LLM_REF_GUARD_SKIP=1` to bypass for exceptional cases.

## Sources Of Truth
- `docs/README.md`
- `docs/TECHNICAL_SPECS.md`
- `docs/technical/environment_variables.md`
- `.github/copilot-instructions.md`
- `.agent/workflows/project-reference.md`
