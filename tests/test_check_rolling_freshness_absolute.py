"""Regression: freshness guard が「rolling も upstream も同時凍結」を検知すること。

背景 (2026-07-19 audit — blind spot a):
    ``scripts/check_rolling_freshness.py`` は rolling を upstream(full_backup) と
    相対比較するだけだったため、cache step が確定日を取得できず **両方が同じ古い日で
    凍結** すると lag=0 = fresh に見えた。2026-07-12..14 のダッシュ凍結
    (キャッシュ全体停滞) はこの盲点そのもの。

    追加した絶対チェック: upstream の最新日が NYSE カレンダーの直近取引日から
    ``--max-abs-lag-bdays`` (既定 2, vendor EOD-lag を吸収) を超えて遅れていたら
    exit 2 (soft WARN) で surface する。
"""

from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import scripts.check_rolling_freshness as crf  # noqa: E402


def _write_csv(d: Path, sym: str, last_date: str) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / f"{sym}.csv").write_text(
        f"Date,Close\n2026-01-02,100\n{last_date},101\n", encoding="utf-8"
    )


def test_total_freeze_is_detected(tmp_path: Path):
    """rolling も upstream も 07-01 で凍結 → 相対 lag=0 でも絶対チェックで exit 2。"""
    rolling = tmp_path / "rolling"
    upstream = tmp_path / "full"
    for sym in ("AAA", "BBB"):
        _write_csv(rolling, sym, "2026-07-01")
        _write_csv(upstream, sym, "2026-07-01")

    rc = crf.main(
        [
            "--rolling-dir",
            str(rolling),
            "--upstream-dir",
            str(upstream),
            "--today",
            "2026-07-19",  # 直近取引日 = 2026-07-17 (金); 07-01 から ~12 営業日遅れ
        ]
    )
    assert rc == 2


def test_fresh_upstream_does_not_false_fire(tmp_path: Path):
    """upstream が直近取引日と一致 → 絶対チェックは発火せず 0。"""
    rolling = tmp_path / "rolling"
    upstream = tmp_path / "full"
    uni = tmp_path / "universe.txt"
    uni.write_text("AAA\nBBB\n", encoding="utf-8")
    for sym in ("AAA", "BBB"):
        _write_csv(rolling, sym, "2026-07-17")
        _write_csv(upstream, sym, "2026-07-17")

    rc = crf.main(
        [
            "--rolling-dir",
            str(rolling),
            "--upstream-dir",
            str(upstream),
            "--universe-file",
            str(uni),
            "--today",
            "2026-07-17",  # 金曜、取引日
        ]
    )
    assert rc == 0


def test_within_tolerance_does_not_fire(tmp_path: Path):
    """vendor EOD-lag 相当 (2 営業日以内) は誤検知しない。"""
    rolling = tmp_path / "rolling"
    upstream = tmp_path / "full"
    uni = tmp_path / "universe.txt"
    uni.write_text("AAA\n", encoding="utf-8")
    # 直近取引日 07-17(金) に対し upstream 07-15(水) = 2 営業日遅れ = 許容内。
    _write_csv(rolling, "AAA", "2026-07-15")
    _write_csv(upstream, "AAA", "2026-07-15")
    rc = crf.main(
        [
            "--rolling-dir",
            str(rolling),
            "--upstream-dir",
            str(upstream),
            "--universe-file",
            str(uni),
            "--today",
            "2026-07-17",
        ]
    )
    assert rc == 0
