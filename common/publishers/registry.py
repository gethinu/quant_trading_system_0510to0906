"""PublisherRegistry — primary → backup の chain 実行 (Phase 1)。

- primary (ntfy) を送信、成功なら終了。
- primary 失敗 (または always_secondary=True) で secondary (email) を発火。
- すべて失敗なら status="failed" を返し、呼び出し側が signals JSON の
  ``meta.publish_status`` に記録して Vercel dashboard 側で検知できるようにする。

Phase 2/3 では primary/secondary を per-subscriber の publisher list に
一般化する (同じ chain ロジックを list に拡張するだけ)。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import logging
from typing import Any

from common.publishers.base import Publisher, PublishResult

logger = logging.getLogger(__name__)


@dataclass
class RegistryResult:
    status: str  # "ok" | "partial" | "failed"
    results: list[PublishResult] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "results": [r.as_dict() for r in self.results],
        }


class PublisherRegistry:
    def __init__(
        self,
        primary: Publisher,
        secondary: Publisher | None = None,
        *,
        always_secondary: bool = False,
    ) -> None:
        self.primary = primary
        self.secondary = secondary
        self.always_secondary = always_secondary

    def publish(
        self, signals_json: dict[str, Any], *, dry_run: bool = False
    ) -> RegistryResult:
        results: list[PublishResult] = []

        primary_res = self.primary.send(signals_json, dry_run=dry_run)
        results.append(primary_res)
        logger.info(
            "[%s] primary %s -> %s (%s)",
            "OK" if primary_res.ok else "FAIL",
            self.primary.name,
            primary_res.target,
            primary_res.status_code,
        )

        # secondary は「primary 失敗時」または「always_secondary」で発火
        need_secondary = self.secondary is not None and (
            self.always_secondary or not primary_res.ok
        )
        if need_secondary and self.secondary is not None:
            sec_res = self.secondary.send(signals_json, dry_run=dry_run)
            results.append(sec_res)
            logger.info(
                "[%s] secondary %s -> %s (%s) %s",
                "OK" if sec_res.ok else "FAIL",
                self.secondary.name,
                sec_res.target,
                sec_res.status_code,
                "(fallback)" if not primary_res.ok else "(always)",
            )

        n_ok = sum(1 for r in results if r.ok)
        if n_ok == 0:
            status = "failed"
        elif n_ok == len(results):
            status = "ok"
        else:
            status = "partial"
        return RegistryResult(status=status, results=results)
