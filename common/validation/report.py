"""Durable validation report objects + dated persistence.

Reports are written with a date stamp to ``results_csv/`` (machine-readable JSON
+ a flat CSV of fold Sharpes) and a human-readable line to ``logs/``, matching
the project convention of keeping important decisions in durable, dated files.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import datetime as _dt
import json
import logging
import os

import pandas as pd

logger = logging.getLogger(__name__)


@dataclass
class ValidationReport:
    label: str
    created_at: str
    n_trials: int
    cpcv: dict = field(default_factory=dict)
    bootstrap: dict = field(default_factory=dict)
    deflated_sharpe: dict = field(default_factory=dict)
    survivorship: dict = field(default_factory=dict)
    fold_sharpes: list = field(default_factory=list)
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    def verdict(self) -> str:
        dsr = self.deflated_sharpe.get("deflated_sharpe")
        passed = self.deflated_sharpe.get("passed")
        boot_p = self.bootstrap.get("p_value_le_zero")
        parts = []
        if dsr is not None:
            parts.append(f"DSR={dsr:.3f}({'PASS' if passed else 'FAIL'})")
        if boot_p is not None:
            parts.append(f"bootstrap P(SR<=0)={boot_p:.3f}")
        surv = self.survivorship.get("biased")
        if surv is not None:
            parts.append("survivorship=BIASED" if surv else "survivorship=ok")
        return " | ".join(parts) if parts else "no-metrics"

    def save(
        self,
        results_dir: str | os.PathLike,
        logs_dir: str | os.PathLike | None = None,
        stamp: str | None = None,
    ) -> dict[str, str]:
        """Persist JSON + fold CSV under a dated filename. Returns written paths."""
        stamp = stamp or _dt.datetime.now().strftime("%Y%m%d_%H%M%S")
        os.makedirs(results_dir, exist_ok=True)
        safe = "".join(c if c.isalnum() or c in "-_" else "_" for c in self.label)
        base = f"validation_{safe}_{stamp}"
        json_path = os.path.join(results_dir, base + ".json")
        with open(json_path, "w", encoding="utf-8") as fh:
            json.dump(self.to_dict(), fh, ensure_ascii=False, indent=2, default=str)

        written = {"json": json_path}
        if self.fold_sharpes:
            csv_path = os.path.join(results_dir, base + "_folds.csv")
            pd.DataFrame(self.fold_sharpes).to_csv(csv_path, index=False)
            written["folds_csv"] = csv_path

        line = f"{self.created_at} [{self.label}] {self.verdict()}"
        if logs_dir:
            os.makedirs(logs_dir, exist_ok=True)
            log_path = os.path.join(logs_dir, "validation_reports.log")
            with open(log_path, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
            written["log"] = log_path
        logger.info(line)
        return written
