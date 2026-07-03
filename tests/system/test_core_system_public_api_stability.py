"""Public-API stability tripwire for core.system1-7.

The upcoming refactor plan (``docs/CORE_SYSTEM_REFACTOR_PLAN_20260703.md``)
touches internal helpers heavily. This test locks the **module-level public
contract** that every caller in ``apps/``, ``scripts/``, ``common/``,
``strategies/``, and ``tests/`` depends on:

* ``prepare_data_vectorized_systemN``  — data preparation entry point
* ``generate_candidates_systemN``       — ranked-candidate generator
* ``get_total_days_systemN``            — bar-count helper

For every function we assert:

1. Its **name** is still importable from ``core.systemN``.
2. Its **parameter names**, in declaration order, are still identical to the
   contract captured on ``claude/monitor-webapp @ b7ffad1`` (2026-07-03).
   A rename, reorder, or ``*``-gate change breaks callers silently — this
   test breaks loudly instead.
3. Its ``*args`` / ``**kwargs`` sink presence matches.

We deliberately do NOT lock:

* Type annotations (allowed to modernize between ``Optional[X]`` → ``X | None``)
* Default values (allowed to fix defaults during refactor cleanup)
* Return-type annotations
* Docstrings

If the public contract changes for a *good* reason (e.g. Cluster-F consolidates
``prepare_data_vectorized_*`` into a shared helper with an aligned signature),
update the ``EXPECTED`` table in the same PR and note it in the refactor plan.
"""

from __future__ import annotations

import importlib
import inspect

import pytest


# Mapping: system_name -> { function_name -> expected_parameter_names }.
# Ground truth captured from ``inspect.signature`` on ``claude/monitor-webapp
# @ b7ffad1``. ``*args`` / ``**kwargs`` sinks are stored with their sentinel
# so a switch between explicit param and catch-all is caught.
EXPECTED: dict[str, dict[str, list[str]]] = {
    "system1": {
        "prepare_data_vectorized_system1": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "reuse_indicators",
            "symbols",
            "use_process_pool",
            "max_workers",
            "**_kwargs",
        ],
        "generate_candidates_system1": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "batch_size",
            "latest_only",
            "include_diagnostics",
            "diagnostics",
            "**kwargs",
        ],
        "get_total_days_system1": ["data_dict"],
    },
    "system2": {
        "prepare_data_vectorized_system2": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "reuse_indicators",
            "symbols",
            "use_process_pool",
            "max_workers",
            "**kwargs",
        ],
        "generate_candidates_system2": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "batch_size",
            "latest_only",
            "include_diagnostics",
            "diagnostics",
            "**kwargs",
        ],
        "get_total_days_system2": ["data_dict"],
    },
    "system3": {
        "prepare_data_vectorized_system3": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "reuse_indicators",
            "symbols",
            "use_process_pool",
            "max_workers",
            "**kwargs",
        ],
        "generate_candidates_system3": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "batch_size",
            "latest_only",
            "include_diagnostics",
            "**kwargs",
        ],
        "get_total_days_system3": ["data_dict"],
    },
    "system4": {
        "prepare_data_vectorized_system4": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "reuse_indicators",
            "symbols",
            "use_process_pool",
            "max_workers",
            "**_unused_kwargs",
        ],
        "generate_candidates_system4": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "latest_only",
            "include_diagnostics",
            "**_unused_kwargs",
        ],
        "get_total_days_system4": ["data_dict"],
    },
    "system5": {
        "prepare_data_vectorized_system5": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "reuse_indicators",
            "symbols",
            "use_process_pool",
            "max_workers",
            "**kwargs",
        ],
        "generate_candidates_system5": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "batch_size",
            "latest_only",
            "include_diagnostics",
            "diagnostics",
            "**kwargs",
        ],
        "get_total_days_system5": ["data_dict"],
    },
    "system6": {
        "prepare_data_vectorized_system6": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "use_process_pool",
            "max_workers",
            "**kwargs",
        ],
        "generate_candidates_system6": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "batch_size",
            "latest_only",
            "latest_mode_date",
            "include_diagnostics",
            "**kwargs",
        ],
        "get_total_days_system6": ["data_dict"],
    },
    "system7": {
        "prepare_data_vectorized_system7": [
            "raw_data_dict",
            "progress_callback",
            "log_callback",
            "skip_callback",
            "reuse_indicators",
            "**kwargs",
        ],
        "generate_candidates_system7": [
            "prepared_dict",
            "top_n",
            "progress_callback",
            "log_callback",
            "batch_size",
            "latest_only",
            "include_diagnostics",
            "**kwargs",
        ],
        "get_total_days_system7": ["data_dict"],
    },
}


def _signature_param_names(fn) -> list[str]:  # noqa: ANN001
    """Return parameter names in declaration order.

    ``*args`` and ``**kwargs`` sinks are prefixed so a switch between
    explicit parameter and catch-all is detected.
    """

    sig = inspect.signature(fn)
    out: list[str] = []
    for name, p in sig.parameters.items():
        if p.kind == inspect.Parameter.VAR_POSITIONAL:
            out.append(f"*{name}")
        elif p.kind == inspect.Parameter.VAR_KEYWORD:
            out.append(f"**{name}")
        else:
            out.append(name)
    return out


@pytest.mark.parametrize(
    "system_name",
    ["system1", "system2", "system3", "system4", "system5", "system6", "system7"],
)
def test_public_functions_are_importable(system_name: str) -> None:
    module = importlib.import_module(f"core.{system_name}")
    for fn_name in EXPECTED[system_name]:
        assert hasattr(module, fn_name), (
            f"core.{system_name} missing public function {fn_name!r} — "
            "callers in apps/, scripts/, common/, strategies/ will break."
        )


@pytest.mark.parametrize(
    "system_name",
    ["system1", "system2", "system3", "system4", "system5", "system6", "system7"],
)
def test_public_function_signatures_are_stable(system_name: str) -> None:
    module = importlib.import_module(f"core.{system_name}")
    for fn_name, expected_params in EXPECTED[system_name].items():
        fn = getattr(module, fn_name)
        got_params = _signature_param_names(fn)
        assert got_params == expected_params, (
            f"core.{system_name}.{fn_name} signature changed:\n"
            f"  expected: {expected_params}\n"
            f"  got:      {got_params}\n"
            "Update EXPECTED in this test AND note the change in the "
            "refactor plan doc before landing."
        )
