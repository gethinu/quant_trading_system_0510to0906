"""PublisherRegistry — primary → backup の chain 実行 (Phase 1)。

- primary (ntfy) を送信、成功なら終了。
- primary 失敗 (または always_secondary=True) で secondary (email) を発火。
- すべて失敗なら status="failed" を返し、呼び出し側が signals JSON の
  ``meta.publish_status`` に記録して Vercel dashboard 側で検知できるようにする。

Phase 2/3 では primary/secondary を per-subscriber の publisher list に
一般化する (同じ chain ロジックを list に拡張するだけ)。

NOTE (F2 P0#4 audit fix, 2026-07-03):
    以前は ``self.primary.send()`` / ``self.secondary.send()`` を try/except で
    包んでおらず、send 内部の rendering 例外 (malformed payload で f-string が
    崩れる、requests.post が raise する、非 ASCII header で構築が失敗する等)
    が publish() から直接 raise していた。この場合:
        * primary raise → secondary は一切呼ばれず fallback chain が死ぬ
        * ``meta.publish_status`` に "failed" が記録されず、dashboard も気付けない
        * その日の通知が完全に無音で消える
    修正後は、各 publisher の send() を try/except でラップし、例外を
    ``PublishResult(ok=False, detail="publisher_exception: ...")`` に変換して
    chain 継続を保証する (ntfy 落ちても Email backup へ確実に fallback)。
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


def _safe_send(
    publisher: Publisher,
    signals_json: dict[str, Any],
    *,
    dry_run: bool,
    role: str,
) -> PublishResult:
    """publisher.send() を例外安全に呼ぶ薄いラッパ。

    どの publisher も send() の render/network 例外を内部で捕まえて
    ok=False を返す設計だが、実装ミス (テンプレ f-string 崩れ、requests
    module 未 import 等) で例外が漏れることが実際に起きた。それが漏れると
    fallback chain 全体が死ぬので、ここで最終防波堤を張る。
    """
    try:
        return publisher.send(signals_json, dry_run=dry_run)
    except Exception as exc:  # noqa: BLE001 - chain 継続のため広く捕まえる
        logger.exception(
            "publisher %s (%s) raised uncaught exception: %s",
            publisher.name,
            role,
            exc,
        )
        # target=publisher.name にとどめる: 内部 secret (ntfy topic 等)
        # を含まないよう、識別子のみ露出する。
        return PublishResult(
            publisher=publisher.name,
            ok=False,
            status_code=None,
            detail=f"publisher_exception: {type(exc).__name__}: {exc}"[:500],
            target=publisher.name,
        )


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

        primary_res = _safe_send(
            self.primary, signals_json, dry_run=dry_run, role="primary"
        )
        results.append(primary_res)
        log_level = logging.INFO if primary_res.ok else logging.WARNING
        logger.log(
            log_level,
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
            sec_res = _safe_send(
                self.secondary, signals_json, dry_run=dry_run, role="secondary"
            )
            results.append(sec_res)
            log_level = logging.INFO if sec_res.ok else logging.WARNING
            logger.log(
                log_level,
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
