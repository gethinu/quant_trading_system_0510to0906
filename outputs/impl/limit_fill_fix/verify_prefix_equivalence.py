#!/usr/bin/env python3
"""Prove that the ``prefix`` arm of ``revalidate_limit_fill.py`` really is the
pre-fix engine, by running the **actual pre-fix code** and comparing.

``revalidate_limit_fill.py`` reconstructs the inflated baseline by monkeypatching
``StrategyBase._limit_entry_filled`` to return ``True``. That is only a valid
stand-in if the fix commit changed nothing else on the entry path. Commit
``960487c`` touched only ``strategies/`` (plus tests/docs), so the pre-fix
runtime is exactly *pre-fix ``strategies/`` + today's ``common``/``core``/
``config``*.

This script materialises that: ``git archive e00e1c3 strategies`` is unpacked to
a scratch dir which is put at the front of ``sys.path`` (cwd stays the repo, so
``data_cache`` and every relative path still resolve to the real data). Then it
runs ``simulate_trades_with_risk`` three ways over the same candidates and
asserts:

    pre-fix tree  ==  fixed tree + ForceFill   (the reconstruction is exact)
    pre-fix tree  !=  fixed tree               (the fix actually bites)

Usage
-----
    python outputs/impl/limit_fill_fix/verify_prefix_equivalence.py \
        --prefix-tree <scratch>/prefix_tree --limit 400
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import subprocess
import sys
import warnings

warnings.filterwarnings("ignore")
logging.getLogger("streamlit").setLevel(logging.ERROR)

_ROOT = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), *[".."] * 3)
)

SUBJECTS = ["System3", "System5", "System6"]

# Runs inside a child process so the two `strategies` packages never share an
# interpreter (module identity would otherwise leak between arms).
CHILD = r"""
import json, os, sys, warnings, logging
warnings.filterwarnings("ignore"); logging.getLogger("streamlit").setLevel(logging.ERROR)
mode, root, prefix_tree, limit, normalize = sys.argv[1:6]
limit = int(limit); normalize = normalize == "1"
os.chdir(root)
if mode == "prefix_tree":
    sys.path.insert(0, prefix_tree)   # pre-fix strategies/ wins over the repo copy
sys.path.insert(1 if mode == "prefix_tree" else 0, root)

from common.universe import load_universe_file
from common.utils_spy import get_spy_data_cached, get_spy_with_indicators
from common.integrated_backtest import build_system_states
from common.backtest_utils import simulate_trades_with_risk
import strategies.base_strategy as bs

def _norm(p):
    return os.path.normcase(os.path.abspath(p))

assert _norm(os.path.dirname(os.path.dirname(bs.__file__))) == _norm(
    prefix_tree if mode == "prefix_tree" else root
), f"strategies package came from {bs.__file__}"
has_guard = hasattr(bs.StrategyBase, "_limit_entry_filled")
if mode == "prefix_tree":
    assert not has_guard, "pre-fix tree unexpectedly has _limit_entry_filled"
else:
    assert has_guard, "fixed tree is missing _limit_entry_filled"
if mode == "forcefill":
    bs.StrategyBase._limit_entry_filled = lambda self, df, i, p, s: True

syms = load_universe_file()[:limit]
spy = get_spy_with_indicators(get_spy_data_cached())
states = build_system_states(syms, spy_df=spy)
if normalize:
    from common.system_candidates_utils import normalize_candidates_by_date
    for st in states:
        if st.candidates_by_date and any(
            isinstance(v, list) for v in st.candidates_by_date.values()
        ):
            st.candidates_by_date = normalize_candidates_by_date(st.candidates_by_date)

out = {}
for st in states:
    if st.name not in ("System3", "System5", "System6"):
        continue
    tr, _ = simulate_trades_with_risk(
        st.candidates_by_date, st.prepared, 100000.0, st.strategy, side=st.side
    )
    if tr is None or tr.empty:
        out[st.name] = {"n": 0, "pnl": 0.0, "digest": ""}
        continue
    cols = [c for c in ("symbol", "entry_date", "exit_date", "entry_price",
                        "exit_price", "shares", "pnl") if c in tr.columns]
    d = tr[cols].astype(str).agg("|".join, axis=1).sort_values().str.cat(sep="\n")
    import hashlib
    out[st.name] = {
        "n": int(len(tr)),
        "pnl": round(float(tr["pnl"].astype(float).sum()), 2),
        "digest": hashlib.sha256(d.encode()).hexdigest()[:16],
    }
print("__RESULT__" + json.dumps(out))
"""


def run_arm(mode, prefix_tree, limit, normalize):
    child = os.path.join(prefix_tree, "_child_verify.py")
    with open(child, "w", encoding="utf-8") as fh:
        fh.write(CHILD)
    proc = subprocess.run(
        [
            sys.executable,
            child,
            mode,
            _ROOT,
            prefix_tree,
            str(limit),
            "1" if normalize else "0",
        ],
        capture_output=True,
        text=True,
        cwd=_ROOT,
    )
    for line in proc.stdout.splitlines():
        if line.startswith("__RESULT__"):
            return json.loads(line[len("__RESULT__") :])
    print(proc.stdout[-4000:])
    print(proc.stderr[-4000:], file=sys.stderr)
    raise SystemExit(f"arm {mode} produced no result (rc={proc.returncode})")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--prefix-tree", required=True)
    ap.add_argument("--limit", type=int, default=400)
    ap.add_argument("--normalize", action="store_true")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    arms = {}
    for mode in ("prefix_tree", "forcefill", "fixed"):
        print(f"running arm: {mode} ...", flush=True)
        arms[mode] = run_arm(mode, args.prefix_tree, args.limit, args.normalize)

    ok = True
    lines = []
    for name in SUBJECTS:
        p = arms["prefix_tree"].get(name, {})
        f = arms["forcefill"].get(name, {})
        x = arms["fixed"].get(name, {})
        exact = p == f
        bites = p != x
        ok = ok and exact
        lines.append(
            f"{name}: pre-fix n={p.get('n')} pnl={p.get('pnl')} digest={p.get('digest')}"
            f" | forcefill n={f.get('n')} pnl={f.get('pnl')} digest={f.get('digest')}"
            f" | fixed n={x.get('n')} pnl={x.get('pnl')} digest={x.get('digest')}"
            f" -> reconstruction_exact={exact} fix_changes_result={bites}"
        )
    print("\n".join(lines))
    print("VERDICT:", "EXACT" if ok else "MISMATCH")

    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(
                {
                    "limit": args.limit,
                    "normalize": args.normalize,
                    "arms": arms,
                    "reconstruction_exact": ok,
                },
                fh,
                indent=2,
            )
    return 0 if ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
