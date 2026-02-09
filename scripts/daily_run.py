from __future__ import annotations

from collections.abc import Callable, Iterable
from dataclasses import dataclass
import io
import logging
import sys

# Windows スケジューラー実行時の cp932 エンコードエラーを回避
if sys.platform == "win32":
    try:
        sys.stdout.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
        sys.stderr.reconfigure(encoding="utf-8")  # type: ignore[attr-defined]
    except (AttributeError, io.UnsupportedOperation):
        # Python < 3.7 または reconfigure 不可の場合
        import codecs

        sys.stdout = codecs.getwriter("utf-8")(sys.stdout.buffer, "replace")  # type: ignore
        sys.stderr = codecs.getwriter("utf-8")(sys.stderr.buffer, "replace")  # type: ignore

from common.logging_utils import setup_logging
from config.settings import Settings, get_settings

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class StepResult:
    name: str
    success: bool
    detail: str | None = None


def _run_step(name: str, func: Callable[[], object]) -> StepResult:
    logger.info("開始: %s", name)
    try:
        result = func()
    except Exception as exc:  # pragma: no cover - defensive logging
        logger.exception("失敗: %s", name)
        return StepResult(name=name, success=False, detail=str(exc))
    logger.info("完了: %s", name)
    detail = str(result) if result not in (None, "") else None
    return StepResult(name=name, success=True, detail=detail)


def _task_cache_daily_data() -> None:
    from scripts import cache_daily_data

    cache_daily_data._cli_main()


# 注: compute_today_signals は schedulers/runner.py で 07:00 に独立実行されます
# daily_run ではデータ更新のみを行い、シグナル生成は07:00に一本化


def _task_build_metrics_report() -> str:
    try:
        from tools.build_metrics_report import build_metrics_report
    except Exception as exc:  # pragma: no cover - optional dependency guard
        logger.warning("metrics レポート生成モジュールを読み込めませんでした: %s", exc)
        return "skipped"
    path = build_metrics_report()
    return f"report={path}" if path else "no report"


def _task_notify_metrics() -> str:
    try:
        from tools.notify_metrics import notify_metrics
    except Exception as exc:  # pragma: no cover - optional dependency guard
        logger.warning("notify_metrics が未実装のためスキップします: %s", exc)
        return "skipped"
    notify_metrics()
    return "notified"


def run_daily_pipeline(settings: Settings) -> list[StepResult]:
    steps: Iterable[tuple[str, Callable[[], object]]] = [
        ("cache_daily_data", _task_cache_daily_data),
        # compute_today_signals は 07:00 の独立タスクで実行（重複を避けるため除外）
        ("build_metrics_report", _task_build_metrics_report),
        ("notify_metrics", _task_notify_metrics),
    ]
    results: list[StepResult] = []
    for name, func in steps:
        results.append(_run_step(name, func))
    return results


def main() -> int:
    settings = get_settings(create_dirs=True)
    setup_logging(settings)
    results = run_daily_pipeline(settings)
    all_ok = all(r.success for r in results)
    for res in results:
        status = "✅" if res.success else "❌"
        logger.info("%s %s %s", status, res.name, res.detail or "")
    return 0 if all_ok else 1


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
