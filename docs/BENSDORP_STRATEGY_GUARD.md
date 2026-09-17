# Bensdorp System1-7 strategy guard

System1-7 are treated as a protected lineage derived from Laurens Bensdorp's
published trading systems. Routine refactors, performance work, dashboard work,
and execution-plumbing fixes must not silently change their book-derived logic.

## Protected surface

The guard freezes the current source fingerprints for:

- `core/system1.py` ... `core/system7.py`
- `strategies/system1_strategy.py` ... `strategies/system7_strategy.py`
- `common/system_setup_predicates.py`
- `common/system_constants.py`
- `common/trade_management.py`
- `common/profit_protection.py`
- `strategies/constants.py`

It also freezes the semantic values of `config/config.yaml` for System1-7,
the long/short allocation maps, and the shared `risk_pct`, `max_positions`,
and `max_pct` values.

System8 is intentionally excluded because it belongs to the `original` lineage,
not the Bensdorp lineage.

## Approval contract

A pull request that changes the protected surface is blocked unless it carries
the exact label `bensdorp-strategy-change-approved`. That label is reserved for
an explicit owner decision after the book/spec impact has been reviewed.

Local pre-push has the same deliberate friction. A protected change is blocked
unless the manifest still matches and `BENSDORP_STRATEGY_CHANGE_APPROVED=1` is
set for that push.

The frozen manifest is `config/bensdorp_guard_manifest.json`. Rewriting an
existing manifest also requires `BENSDORP_STRATEGY_CHANGE_APPROVED=1`. The
control plane (`tools/check_bensdorp_guard.py`, this manifest, and the dedicated
workflow) is itself approval-gated after the bootstrap merge.

For pull requests, the dedicated workflow uses `pull_request_target`, checks out
only the trusted base commit, and fetches the PR head only as Git objects. The
trusted base copy of the guard evaluates the candidate ref; PR code is never
executed by this security-sensitive job. Changed paths are computed from the PR
merge base, not the moving base-branch tip, so unrelated old PRs are not falsely
classified when main receives a newer guard baseline. Label add/remove events
rerun the gate so approval state is re-evaluated immediately.

## Normal maintenance

Changes outside the protected surface do not need the approval label and do not
need the environment override. The guard is intended to protect strategy
semantics, not to block unrelated maintenance.

The repository's `main` branch is not currently protected by GitHub branch
protection. Therefore this guard is a strong accidental-change detector and CI
contract, but it cannot make an administrator-level direct push impossible.
