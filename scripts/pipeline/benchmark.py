"""Lightweight Benchmark for performance measurement.

Extracted from run_all_systems_today.py for better modularity.
"""

from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path
from typing import Any


class LightweightBenchmark:
    """軽量ベンチマーク（時間計測のみ、--benchmark フラグで有効化）。"""

    def __init__(self, enabled: bool = False) -> None:
        self.enabled = enabled
        self.phases: dict[str, dict[str, float]] = {}
        self._current_phase: str | None = None
        self._start_time: float | None = None
        self._global_start: float | None = None
        self.extras: dict[str, Any] = {}

    def start_phase(self, phase_name: str) -> None:
        """フェーズ開始時刻を記録。"""
        if not self.enabled:
            return
        import time

        if self._global_start is None:
            self._global_start = time.perf_counter()
        self._current_phase = phase_name
        self._start_time = time.perf_counter()

    def end_phase(self) -> None:
        """フェーズ終了時刻を記録。"""
        if not self.enabled:
            return
        if self._current_phase is None or self._start_time is None:
            return
        import time

        end_time = time.perf_counter()
        duration = end_time - self._start_time
        self.phases[self._current_phase] = {
            "start": self._start_time - (self._global_start or 0.0),
            "end": end_time - (self._global_start or 0.0),
            "duration_sec": round(duration, 6),
        }
        self._current_phase = None
        self._start_time = None

    def get_report(self) -> dict[str, Any]:
        """ベンチマークレポートを取得。"""
        if not self.enabled:
            return {
                "enabled": False,
                "phases": {},
                "phase_times": {},
                "total_duration_sec": 0.0,
                "total_time": 0.0,
            }

        phase_times = {
            name: float(values.get("duration_sec", 0.0))
            for name, values in self.phases.items()
        }
        total_duration = sum(phase_times.values())
        total_duration = round(total_duration, 6)
        return {
            "enabled": True,
            "timestamp": datetime.now().isoformat(),
            "phases": self.phases,
            "phase_times": phase_times,
            "total_duration_sec": total_duration,
            "total_time": total_duration,
            "extras": self.extras,
        }

    def save_report(self, output_path: str | Path) -> None:
        """レポートをJSONで保存。"""
        if not self.enabled:
            return
        path = Path(output_path)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            json.dump(self.get_report(), f, ensure_ascii=False, indent=2)

    def add_extra_section(self, name: str, payload: Any) -> None:
        """追加セクションを付加。"""
        if not self.enabled:
            return
        try:
            self.extras[str(name)] = payload
        except Exception:
            pass
