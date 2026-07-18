"""Regression: cache_daily_polygon.py --auto-latest 契約.

背景 (2026-07-15):
    daily_pipeline.ps1 (#138) は cache step を `--auto-latest` で叩くよう変更済みだが、
    cache_daily_polygon.py 側の実装が origin/main に landing しておらず (--start/--end
    required のまま)、argparse が `error: the following arguments are required:
    --start, --end` を出して **exit=2** で毎日 cache step が失敗していた。

    本テストは:
      1. argparse が --auto-latest を受理し、--start/--end 無しでも SystemExit しない
      2. main(["--auto-latest"]) が (full_backup 最新なら) exit=0 で skip する
      3. --start/--end も --auto-latest も無ければ argparse exit=2 でなく rc=1 で loud fail
      4. resolve_auto_range が「既に最新」で None を返す (fetch skip)
    を固定する。
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pandas as pd

import scripts.cache_daily_polygon as cdp


def test_argparser_accepts_auto_latest_without_start_end() -> None:
    """--auto-latest 単独で parse できる (旧: --start/--end required で SystemExit した)."""
    parser = cdp.build_arg_parser()
    args = parser.parse_args(["--auto-latest"])
    assert args.auto_latest is True
    assert args.start is None and args.end is None


def test_main_auto_latest_skips_when_up_to_date(monkeypatch) -> None:
    """full_backup が最新 (resolve→None) のとき exit=0 で skip。cache exit=2 回帰の核心."""
    monkeypatch.setattr(
        "config.settings.get_settings",
        lambda create_dirs=True: SimpleNamespace(),
    )
    monkeypatch.setattr(cdp, "resolve_auto_range", lambda settings, **kw: None)

    rc = cdp.main(["--auto-latest"])
    assert rc == 0


def test_main_missing_range_is_loud_rc1_not_argparse_exit2(monkeypatch) -> None:
    """--start/--end も --auto-latest も無ければ rc=1 (silent argparse exit=2 にしない)."""
    rc = cdp.main([])
    assert rc == 1


def test_resolve_auto_range_returns_none_when_full_backup_latest(
    tmp_path: Path, monkeypatch
) -> None:
    """full_backup 最新日 == 直近取引日なら翌取引日 > end となり None (fetch 不要)."""
    full_dir = tmp_path / "full_backup"
    full_dir.mkdir(parents=True)
    pd.DataFrame(
        {"Date": ["2026-07-13", "2026-07-14"], "Close": [100.0, 101.0]}
    ).to_csv(full_dir / "SPY.csv", index=False)

    settings = SimpleNamespace(cache=SimpleNamespace(full_dir=str(full_dir)))

    fixed_end = pd.Timestamp("2026-07-14")
    monkeypatch.setattr(
        "common.utils_spy.get_latest_nyse_trading_day",
        lambda ts=None: fixed_end,
    )
    monkeypatch.setattr(
        "common.utils_spy.get_next_nyse_trading_day",
        lambda ts: pd.Timestamp("2026-07-15"),  # 07-14 の翌取引日
    )

    rng = cdp.resolve_auto_range(settings)
    assert rng is None


def test_resolve_auto_range_returns_range_when_behind(
    tmp_path: Path, monkeypatch
) -> None:
    """full_backup が遅れていれば (start, end) を返し start<=end。"""
    full_dir = tmp_path / "full_backup"
    full_dir.mkdir(parents=True)
    pd.DataFrame(
        {"Date": ["2026-07-09", "2026-07-10"], "Close": [100.0, 101.0]}
    ).to_csv(full_dir / "SPY.csv", index=False)

    settings = SimpleNamespace(cache=SimpleNamespace(full_dir=str(full_dir)))

    fixed_end = pd.Timestamp("2026-07-14")

    def fake_latest(ts=None):
        if ts is None:
            return fixed_end
        t = pd.Timestamp(ts).normalize()
        return t if t < fixed_end else fixed_end

    monkeypatch.setattr("common.utils_spy.get_latest_nyse_trading_day", fake_latest)
    monkeypatch.setattr(
        "common.utils_spy.get_next_nyse_trading_day",
        lambda ts: pd.Timestamp("2026-07-13"),  # 07-10 の翌取引日
    )

    rng = cdp.resolve_auto_range(settings)
    assert rng is not None
    start, end = rng
    assert start <= end == fixed_end.date()
