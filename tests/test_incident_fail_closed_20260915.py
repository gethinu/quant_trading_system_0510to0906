from __future__ import annotations

from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]


def test_daily_follow_blocks_unchanged_signal_artifact():
    text = (ROOT / "scripts" / "daily_main_follow.ps1").read_text(encoding="utf-8-sig")
    assert "$signalRunBefore = Get-SignalRunId $signalPath" in text
    assert "$signalRunAfter = Get-SignalRunId $signalPath" in text
    assert "$signalRunAfter -ne $signalRunBefore" in text
    assert "refusing stale-artifact publish" in text


def test_snapshot_script_imports_from_unrelated_cwd(tmp_path):
    script = ROOT / "scripts" / "export_alpaca_snapshot.py"
    proc = subprocess.run(
        [sys.executable, str(script), "--help"],
        cwd=tmp_path,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
