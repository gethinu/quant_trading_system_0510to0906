"""Phase 1 hardening — 常設不変条件ゲート (permanent invariant gates).

診断 Phase1 の実装。ライブ執行パイプラインに常設し、静かに壊れる系の不変条件を
fail-closed で検知する。段階導入のため各ゲートは 3 モードを持つ:

    OFF     … 評価しない
    WARN    … 評価して違反を記録するが ok=True を返す (執行は止めない)
    ENFORCE … 違反したら ok=False を返す (fail-closed = 呼び出し側が執行停止)

既定は WARN。まず 1〜2 営業日 WARN で回して誤検知ゼロを確認し、ゲート単位で
ENFORCE に昇格する (GateConfig)。fail-closed がライブを過剰停止しないための
段階有効化。純関数 + dataclass のみ、I/O なし。

対象不変条件:
  (a) measurement invariant : exit_submitted == exit_close + exit_protect
                              (fired 会計。close/protect は fired 分のみ、
                               armed は別枠 = この恒等式に入らない)
  (a) served-today          : dashboard の served データ日付 == 当日
  (a) snapshot freshness    : alpaca snapshot の mtime が閾値内
  (a) file monotonic        : 累積カウンタ (published files 等) が非減少
  (b) verify alpaca_snapshot: snapshot payload の必須キー / 当日性 / 建玉整合
  (c) funnel monotonic      : rolling >= filter >= setup (pipeline IT 用不変条件)
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping


class GateMode(str, Enum):
    OFF = "off"
    WARN = "warn"
    ENFORCE = "enforce"


DEFAULT_MODE = GateMode.WARN


@dataclass(frozen=True)
class GateConfig:
    """ゲートごとのモード表。未登録キーは default にフォールバック。"""

    default: GateMode = DEFAULT_MODE
    modes: Mapping[str, GateMode] = field(default_factory=dict)

    def mode_for(self, name: str) -> GateMode:
        return self.modes.get(name, self.default)


@dataclass(frozen=True)
class GateResult:
    name: str
    ok: bool
    violated: bool
    mode: GateMode
    detail: str = ""

    @property
    def is_warn(self) -> bool:
        return self.violated and self.mode is GateMode.WARN

    @property
    def is_blocking(self) -> bool:
        return self.violated and self.mode is GateMode.ENFORCE


def _finish(name: str, violated: bool, cfg: GateConfig, detail: str) -> GateResult:
    """生の違反事実 + モードから GateResult を組む。

    fail-closed の核心: ENFORCE かつ violated のときだけ ok=False。
    WARN/OFF では violated でも ok=True だが violated 事実は保持するので
    silent-WARN 監視 (d) が拾える。
    """
    mode = cfg.mode_for(name)
    if mode is GateMode.OFF:
        return GateResult(name, ok=True, violated=False, mode=mode, detail="off")
    ok = not (violated and mode is GateMode.ENFORCE)
    return GateResult(name, ok=ok, violated=violated, mode=mode, detail=detail)


# --- (a) measurement invariant ---------------------------------------------
def check_measurement_invariant(
    portfolio: Mapping[str, Any], cfg: GateConfig
) -> GateResult:
    """fired 会計の恒等式 exit_submitted == exit_close + exit_protect を検証。"""
    sub = int(portfolio.get("exit_submitted") or 0)
    close = int(portfolio.get("exit_close") or 0)
    protect = int(portfolio.get("exit_protect") or 0)
    violated = sub != close + protect
    detail = (
        f"exit_submitted={sub} close={close} protect={protect} "
        f"(close+protect={close + protect})"
    )
    return _finish("measurement_invariant", violated, cfg, detail)


# --- (a) served-today -------------------------------------------------------
def check_served_today(
    served_date: str | None, today: str, cfg: GateConfig
) -> GateResult:
    """serve 中のデータ日付が当日か (stale publish 検知)。YYYYMMDD 文字列。"""
    violated = served_date != today
    detail = f"served={served_date!r} today={today!r}"
    return _finish("served_today", violated, cfg, detail)


# --- (a) snapshot freshness -------------------------------------------------
def check_snapshot_freshness(
    snapshot_mtime: float | None,
    now: float,
    max_age_seconds: float,
    cfg: GateConfig,
) -> GateResult:
    """alpaca snapshot ファイルの鮮度。mtime None or 閾値超過なら違反。"""
    if snapshot_mtime is None:
        return _finish("snapshot_freshness", True, cfg, "snapshot_mtime=None (missing)")
    age = now - snapshot_mtime
    violated = age > max_age_seconds
    detail = f"age={age:.0f}s max={max_age_seconds:.0f}s"
    return _finish("snapshot_freshness", violated, cfg, detail)


# --- (a) file monotonic non-decreasing --------------------------------------
def check_file_monotonic(
    prev_count: int | None, curr_count: int, cfg: GateConfig
) -> GateResult:
    """累積 published-file カウンタが後退していないか。prev=None は違反でない。"""
    if prev_count is None:
        return _finish("file_monotonic", False, cfg, f"first observation curr={curr_count}")
    violated = curr_count < prev_count
    detail = f"prev={prev_count} curr={curr_count}"
    return _finish("file_monotonic", violated, cfg, detail)


# --- (b) verify alpaca_snapshot --------------------------------------------
_REQUIRED_SNAPSHOT_KEYS = ("as_of", "account_equity", "positions")


def verify_alpaca_snapshot(
    snapshot: Mapping[str, Any] | None, today: str, cfg: GateConfig
) -> GateResult:
    """alpaca snapshot payload の妥当性 (必須キー / 当日性 / positions 型)。"""
    if not snapshot:
        return _finish("verify_alpaca_snapshot", True, cfg, "snapshot missing/empty")
    missing = [k for k in _REQUIRED_SNAPSHOT_KEYS if k not in snapshot]
    if missing:
        return _finish("verify_alpaca_snapshot", True, cfg, f"missing keys: {missing}")
    as_of = str(snapshot.get("as_of") or "")
    as_of_ymd = as_of.replace("-", "")[:8]
    if as_of_ymd != today:
        return _finish(
            "verify_alpaca_snapshot", True, cfg, f"as_of={as_of!r} != today={today!r}"
        )
    if not isinstance(snapshot.get("positions"), (list, tuple)):
        return _finish("verify_alpaca_snapshot", True, cfg, "positions is not a list")
    return _finish(
        "verify_alpaca_snapshot", False, cfg,
        f"ok as_of={as_of} positions={len(snapshot['positions'])}",
    )


# --- (c) funnel monotonic (rolling -> filter -> setup) ----------------------
def check_funnel_monotonic(
    rolling: int, filtered: int, setup: int, cfg: GateConfig
) -> GateResult:
    """pipeline 段の非増加性 rolling >= filter >= setup を検証 (IT 用不変条件)。"""
    violated = not (rolling >= filtered >= setup)
    detail = f"rolling={rolling} >= filter={filtered} >= setup={setup}"
    return _finish("funnel_monotonic", violated, cfg, detail)


# --- aggregation ------------------------------------------------------------
@dataclass(frozen=True)
class GateReport:
    results: tuple[GateResult, ...]

    @property
    def ok(self) -> bool:
        """執行してよいか。ENFORCE 違反が 1 つでもあれば False (fail-closed)。"""
        return all(r.ok for r in self.results)

    @property
    def blocking(self) -> tuple[GateResult, ...]:
        return tuple(r for r in self.results if r.is_blocking)

    @property
    def warnings(self) -> tuple[GateResult, ...]:
        """silent-WARN 監視 (d) 対象: 違反したが WARN で握り潰されているもの。"""
        return tuple(r for r in self.results if r.is_warn)

    def summary(self) -> str:
        parts = []
        for r in self.results:
            tag = "OK" if not r.violated else ("BLOCK" if r.is_blocking else "WARN")
            parts.append(f"[{tag}] {r.name}: {r.detail}")
        return "\n".join(parts)


def evaluate_gates(results: list[GateResult]) -> GateReport:
    return GateReport(tuple(results))


class GateBlocked(RuntimeError):
    """ENFORCE ゲート違反で執行を止めるときに呼び出し側が投げる例外。"""

    def __init__(self, report: GateReport) -> None:
        names = ", ".join(r.name for r in report.blocking)
        super().__init__(f"phase1 gate blocked execution: {names}")
        self.report = report


def raise_if_blocked(report: GateReport) -> None:
    """fail-closed のエントリポイント。ENFORCE 違反があれば GateBlocked を投げる。"""
    if not report.ok:
        raise GateBlocked(report)
