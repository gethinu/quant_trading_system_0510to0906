"""System test: daily_pipeline.ps1 の CLI 契約と exit code 契約を固定する.

Phase 1 audit gap:
    - `daily_pipeline.ps1` の grep hit が tests/ 内で 0
    - step1 が `cache_daily_polygon.py --start {d} --end {d}` を叩く形態が
      2026-07-02 flatten bug の呼び出しトリガだった. この契約に変更が入れば
      検知する.

制約: PowerShell そのものを Linux で実行できないため, ps1 の中身 (regex + AST)
と, ps1 の呼び出す python script (main() 経路) の integration を assert する.
CLI 契約が壊れれば ps1 起動不能 = flatten を含む pipeline 全体の regression 検知.
"""

from __future__ import annotations

from pathlib import Path

import pytest

PS1 = Path(__file__).resolve().parents[2] / "scripts" / "daily_pipeline.ps1"


@pytest.fixture(scope="module")
def ps1_text() -> str:
    assert PS1.exists(), f"{PS1} が存在しない = pipeline 消失"
    return PS1.read_text(encoding="utf-8-sig", errors="replace")


class TestDailyPipelineCliContract:
    """ps1 の中身から expected CLI 契約を抽出して固定する."""

    def test_step1_cache_command_shape(self, ps1_text: str):
        """★ step1 cache 契約: `scripts/cache_daily_polygon.py --auto-latest` の形態.

        履歴: 旧契約は `--start {Date} --end {Date}` の 1 日 fetch だったが、
        定例 06:00 JST run では「今日」が US EOD 前で Polygon 403 空振り → cache
        exit=2 になっていた。#138 で ps1 を `--auto-latest` (full_backup 最新日の翌
        取引日〜直近 NYSE 取引日を自動対象) に切替。この契約をここで固定する。
        """
        # 対応 python が呼ばれている
        assert (
            "scripts\\cache_daily_polygon.py" in ps1_text
            or "scripts/cache_daily_polygon.py" in ps1_text
        ), "step1 で cache_daily_polygon.py が呼ばれていない"
        # --auto-latest 形態 (今日固定 fetch をやめ、確定済み range を自動解決)
        assert "--auto-latest" in ps1_text, (
            "cache step が --auto-latest 形態でない。旧 --start/--end 形態に戻ると "
            "06:00 JST 定例 run で当日 EOD 前 403 → cache exit=2 が再発する。"
        )

    def test_step2_signals_command_shape(self, ps1_text: str):
        assert (
            "apps\\app_today_signals.py" in ps1_text
            or "apps/app_today_signals.py" in ps1_text
        )
        assert "--headless" in ps1_text
        assert "--output-json" in ps1_text

    def test_step3_coverage_monitor(self, ps1_text: str):
        assert (
            "scripts\\daily_polygon_monitor.py" in ps1_text
            or "scripts/daily_polygon_monitor.py" in ps1_text
        )

    def test_step4_narrator_optional(self, ps1_text: str):
        assert (
            "scripts\\generate_narrative.py" in ps1_text
            or "scripts/generate_narrative.py" in ps1_text
        )
        # narrator は fail-safe (exit!=0 でも WARN 止まり)
        assert "SkipNarrator" in ps1_text
        assert "narrative 無しで継続" in ps1_text

    def test_step5_publish_signals(self, ps1_text: str):
        assert (
            "scripts\\publish_signals.py" in ps1_text
            or "scripts/publish_signals.py" in ps1_text
        )

    def test_exit_code_contract_documented(self, ps1_text: str):
        """Exit codes: 0=全 OK, 2=一部失敗 (WARN), 1=致命的エラー."""
        assert "Exit codes:" in ps1_text
        # ドキュメンテーション + 実装両方
        assert "0=" in ps1_text and "2=" in ps1_text and "1=" in ps1_text

    def test_project_root_resolution(self, ps1_text: str):
        """ProjectRoot が pipeline 内で resolve される (sys.path 依存回避)."""
        assert "$ProjectRoot" in ps1_text

    def test_skipcache_flag_exists(self, ps1_text: str):
        """SkipCache 変数が定義されている (backfill 中の pipeline 再実行など)."""
        assert "SkipCache" in ps1_text

    def test_env_file_auto_loaded_before_error_action(self, ps1_text: str):
        """★ 2026-07-02 hygiene: .env auto-load block が $ErrorActionPreference より前に存在.

        Task Scheduler tick では ANTHROPIC_API_KEY / NTFY_TOPIC が親プロセス
        から継承されない環境が多い. .env parse を pipeline 冒頭に置くことで
        narrator skip / publish 失敗 の再発を防ぐ.
        """
        # .env parse block の存在
        assert "$EnvFile" in ps1_text, ".env auto-load block が消失している"
        assert "Test-Path $EnvFile" in ps1_text
        # 既存 env は上書きしない (guard)
        assert (
            'Test-Path "Env:$k"' in ps1_text
        ), "既存 env 優先ガードが必要 (env 覆いを防ぐ)"
        # order: .env auto-load が $ErrorActionPreference より前に位置する
        idx_env = ps1_text.find("$EnvFile = Join-Path $ProjectRoot")
        idx_eap = ps1_text.find('$ErrorActionPreference = "Continue"')
        assert idx_env > 0 and idx_eap > 0, "block が欠落"
        assert idx_env < idx_eap, (
            ".env auto-load は $ErrorActionPreference より前で走る必要がある "
            "(そうしないと step 直前で env が揃わない)"
        )


class TestPipelineScriptExistence:
    """ps1 の各 step が呼ぶ script が実在すること."""

    @pytest.mark.parametrize(
        "relpath",
        [
            "scripts/cache_daily_polygon.py",
            "apps/app_today_signals.py",
            "scripts/daily_polygon_monitor.py",
            "scripts/generate_narrative.py",
            "scripts/publish_signals.py",
        ],
    )
    def test_script_file_exists(self, relpath: str):
        target = PS1.parent.parent / relpath
        assert (
            target.exists()
        ), f"{relpath} が存在しない. daily_pipeline.ps1 が起動時に失敗する."


class TestCachePolygonMainAcceptsPs1Contract:
    """ps1 が渡す CLI 引数を python 側 main() が受理することの契約検証."""

    def test_main_accepts_start_end_same_date(self, monkeypatch, tmp_path):
        """★ flatten bug 経路そのものの assertion:
        ps1 と全く同一 CLI 引数形態を main() に渡して exit 0 で終わることを確認.
        """
        from types import SimpleNamespace

        import pandas as pd

        import scripts.cache_daily_polygon as cdp

        data_cache = tmp_path / "data_cache"
        (data_cache / "full_backup").mkdir(parents=True)
        (data_cache / "base").mkdir(parents=True)

        fake_settings = SimpleNamespace(
            DATA_CACHE_DIR=str(data_cache),
            cache=SimpleNamespace(
                full_dir=str(data_cache / "full_backup"),
                round_decimals=4,
            ),
        )
        monkeypatch.setattr(
            "config.settings.get_settings",
            lambda create_dirs=True: fake_settings,
        )
        monkeypatch.setattr(
            "common.cache_manager.get_settings",
            lambda create_dirs=True: fake_settings,
        )
        monkeypatch.setattr(
            cdp,
            "get_polygon_grouped_daily",
            lambda ds: pd.DataFrame(
                {
                    "Open": [100.0],
                    "High": [101.0],
                    "Low": [99.0],
                    "Close": [100.5],
                    "Volume": [1_000_000],
                },
                index=pd.Index(["AAPL"], name="symbol"),
            ),
        )

        # ps1 step1 と等価な呼び出し
        rc = cdp.main(
            [
                "--start",
                "2026-07-02",
                "--end",
                "2026-07-02",
                "--sleep",
                "0",
            ]
        )
        assert rc == 0, "ps1 の step1 が渡す CLI 引数で main() が失敗する"
