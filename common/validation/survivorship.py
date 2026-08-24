"""Survivorship-bias audit, point-in-time universe interface, and guard.

The backtest universe in this system is built from a *current-membership*
snapshot (``data/universe_auto.txt`` / live NASDAQ Trader listings / today's
cache), and several filters actively drop symbols that are inactive or lack
*recent* data. Applied to historical prices this is textbook survivorship bias:
the delisted losers are structurally absent.

Fully eliminating the bias requires a *dated membership* dataset (which listing
each symbol belonged to on each historical date) that does not currently exist
in the repo. This module therefore provides:

1. :class:`PointInTimeUniverse` - an interface that *consumes* a dated
   membership file (``data/universe_membership.csv``) when present, giving a
   genuinely point-in-time ``members_asof(date)`` used to make the backtest
   survivorship-free.
2. :func:`audit_survivorship` - detects and quantifies the exposure (how much of
   the universe is current-only, whether a membership file exists).
3. :func:`survivorship_guard` - an OFF / WARN / ENFORCE guard (mirroring
   ``phase1_gates``) that makes the bias *explicit* at backtest time instead of
   silent.

All of this is inert unless ``SURVIVORSHIP_GUARD`` is set to ``warn`` / ``enforce``
or a caller invokes it directly.
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
import os

import pandas as pd

from common.validation.flags import survivorship_guard_mode

logger = logging.getLogger(__name__)

MEMBERSHIP_SCHEMA = ("symbol", "list_date", "delist_date")


class SurvivorshipError(RuntimeError):
    """Raised by the guard in ENFORCE mode when the universe is not PIT."""


@dataclass
class PointInTimeUniverse:
    """As-of-date universe membership from a dated membership table.

    The membership CSV has columns ``symbol, list_date, delist_date`` (delist_date
    blank == still listed). ``members_asof(date)`` returns the set of symbols
    that were listed on that date - i.e. it *keeps* symbols that later delisted,
    which is what removes survivorship bias.
    """

    membership: pd.DataFrame

    @classmethod
    def from_csv(cls, path: str | os.PathLike) -> "PointInTimeUniverse":
        df = pd.read_csv(path)
        missing = [c for c in ("symbol", "list_date") if c not in df.columns]
        if missing:
            raise ValueError(f"membership file missing columns: {missing}")
        if "delist_date" not in df.columns:
            df["delist_date"] = pd.NaT
        df["symbol"] = df["symbol"].astype(str).str.upper()
        df["list_date"] = pd.to_datetime(df["list_date"], errors="coerce")
        df["delist_date"] = pd.to_datetime(df["delist_date"], errors="coerce")
        return cls(membership=df)

    def members_asof(self, date) -> set[str]:
        d = pd.Timestamp(date)
        m = self.membership
        listed = m["list_date"].isna() | (m["list_date"] <= d)
        not_delisted = m["delist_date"].isna() | (m["delist_date"] > d)
        return set(m.loc[listed & not_delisted, "symbol"])

    def is_survivorship_free(self) -> bool:
        """True if any symbol has a delist_date (i.e. dead names are retained)."""
        return bool(self.membership["delist_date"].notna().any())


def default_membership_path(root: str | os.PathLike | None = None) -> str:
    root = str(root) if root else os.getcwd()
    return os.path.join(root, "data", "universe_membership.csv")


@dataclass
class SurvivorshipAudit:
    membership_file_present: bool
    membership_path: str
    universe_size: int
    survivorship_free: bool
    reasons: list[str] = field(default_factory=list)

    @property
    def biased(self) -> bool:
        return not self.survivorship_free

    def to_dict(self) -> dict:
        return {
            "membership_file_present": self.membership_file_present,
            "membership_path": self.membership_path,
            "universe_size": self.universe_size,
            "survivorship_free": self.survivorship_free,
            "biased": self.biased,
            "reasons": list(self.reasons),
        }

    def summary(self) -> str:
        state = "survivorship-free" if self.survivorship_free else "SURVIVORSHIP-BIASED"
        return (
            f"[survivorship] {state} | universe={self.universe_size} "
            f"| membership_file={'yes' if self.membership_file_present else 'no'} "
            f"| {'; '.join(self.reasons)}"
        )


def audit_survivorship(
    symbols,
    *,
    membership_path: str | os.PathLike | None = None,
    root: str | os.PathLike | None = None,
) -> SurvivorshipAudit:
    """Assess whether the current backtest universe is survivorship-biased."""
    path = str(membership_path) if membership_path else default_membership_path(root)
    present = os.path.isfile(path)
    reasons: list[str] = []
    survivorship_free = False

    if not present:
        reasons.append(
            "no dated membership file; universe is a current-membership snapshot "
            "applied to historical data (delisted symbols absent)"
        )
    else:
        try:
            pit = PointInTimeUniverse.from_csv(path)
            survivorship_free = pit.is_survivorship_free()
            if survivorship_free:
                n_dead = int(pit.membership["delist_date"].notna().sum())
                reasons.append(
                    f"dated membership present with {n_dead} delisted symbols "
                    "retained -> point-in-time backtest available"
                )
            else:
                reasons.append(
                    "membership file present but contains no delisted symbols; "
                    "still effectively current-only"
                )
        except Exception as exc:  # pragma: no cover - defensive
            reasons.append(f"membership file unreadable ({exc}); treated as biased")

    return SurvivorshipAudit(
        membership_file_present=present,
        membership_path=path,
        universe_size=len(list(symbols)) if symbols is not None else 0,
        survivorship_free=survivorship_free,
        reasons=reasons,
    )


def survivorship_guard(
    symbols,
    *,
    mode: str | None = None,
    membership_path: str | os.PathLike | None = None,
    root: str | os.PathLike | None = None,
) -> SurvivorshipAudit:
    """Explicit OFF/WARN/ENFORCE guard around backtest universe survivorship.

    * ``off``    - returns the audit, logs nothing (default; byte-parity).
    * ``warn``   - logs a WARNING when biased (makes silent bias explicit).
    * ``enforce``- raises :class:`SurvivorshipError` when biased.
    """
    resolved = (mode or survivorship_guard_mode()).lower()
    audit = audit_survivorship(symbols, membership_path=membership_path, root=root)
    if resolved == "off":
        return audit
    if audit.biased:
        if resolved == "enforce":
            raise SurvivorshipError(audit.summary())
        logger.warning(audit.summary())
    else:
        logger.info(audit.summary())
    return audit
