"""System test: alpaca-next ダッシュの「サマリー + 2 層化」契約.

2026-07-27 の UI 再構成で入れた不変条件を固定する。

背景
----
それまでのダッシュは事実を全部並べていたので、スマホ (375px) で Alpaca タブが
縦 8,868px = 約 11 画面あり、「今どういう状態か」を掴むのに端まで scroll する
必要があった。特に **期限超過の建玉** (max_hold を過ぎたのに残っている =
exit が詰まっている) は保有ポジション表の最下部まで行かないと分からなかった。

そこで:
    1. 最上部に「状態」サマリー 1 ブロック (当日損益 / 保有と期限超過 /
       鮮度と run_id / 赤アラート件数) を置く。
    2. 長い表 (system 別・決済履歴・エクイティ・銘柄別) は Collapsible で
       既定は畳む。**情報は消さず** 見出しに要約値を出して 2 層にする。

ここで固定したいのは「縦が縮んだこと」そのものではなく、縮んだ **理由** —
サマリーが存在し、表が既定で閉じ、閉じている間は DOM に出さない (lazy mount) —
の 3 点。どれか 1 つでも崩れると 11 画面に逆戻りする。

TS/TSX は Node 実行環境がないので、既存の test_dashboard_ui_contract.py と
同じく source 部分文字列 assertion で契約を固定する。
"""

from __future__ import annotations

from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]
NEXT_ROOT = REPO_ROOT / "apps" / "dashboards" / "alpaca-next"

PAGE = NEXT_ROOT / "app" / "page.tsx"
STATUS_SUMMARY = NEXT_ROOT / "components" / "StatusSummary.tsx"
COLLAPSIBLE = NEXT_ROOT / "components" / "Collapsible.tsx"
ALPACA_SECTION = NEXT_ROOT / "components" / "AlpacaSection.tsx"
FRESHNESS_BANNER = NEXT_ROOT / "components" / "FreshnessBanner.tsx"
STATUS_LIB = NEXT_ROOT / "lib" / "status.ts"
FRESHNESS_LIB = NEXT_ROOT / "lib" / "freshness.ts"
FORMAT_LIB = NEXT_ROOT / "lib" / "format.ts"


def _read(p: Path) -> str:
    assert p.exists(), f"{p} 消失"
    return p.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def page_text() -> str:
    return _read(PAGE)


@pytest.fixture(scope="module")
def summary_text() -> str:
    return _read(STATUS_SUMMARY)


@pytest.fixture(scope="module")
def collapsible_text() -> str:
    return _read(COLLAPSIBLE)


@pytest.fixture(scope="module")
def alpaca_text() -> str:
    return _read(ALPACA_SECTION)


@pytest.fixture(scope="module")
def status_lib_text() -> str:
    return _read(STATUS_LIB)


class TestSummaryIsMountedAtTop:
    """★ 最上部の「状態」サマリーが存在すること."""

    def test_page_renders_status_summary(self, page_text: str):
        assert "StatusSummary" in page_text, (
            "StatusSummary が page から消えた. 最上部サマリーが無いと"
            "「今どういう状態か」を掴むのに全表を scroll する状態に逆戻りする"
        )

    def test_summary_receives_snapshot_and_run_id(self, page_text: str):
        """サマリーは口座 snapshot と配信側の run_id 双方を受けること。"""
        assert "snapshot={alpaca}" in page_text
        assert "run_id" in page_text


class TestSummaryShowsTheFiveFacts:
    """★ サマリーに出す 5 点 (欠けたら「一目で分かる」が壊れる)."""

    def test_today_pnl(self, summary_text: str):
        assert "今日の損益" in summary_text

    def test_position_count(self, summary_text: str):
        assert "保有ポジション" in summary_text

    def test_overdue_count(self, summary_text: str):
        """今の問題 (exit が詰まっている) を一目で出すのが主目的。"""
        assert (
            "期限超過" in summary_text
        ), "期限超過の件数がサマリーから消えた. これはこの再構成の主目的"
        assert "status.overdue" in summary_text

    def test_freshness_and_run_id(self, summary_text: str):
        assert "データ鮮度" in summary_text
        assert "runId" in summary_text

    def test_red_alert_count(self, summary_text: str):
        assert "赤アラート" in summary_text
        assert "'red'" in summary_text


class TestSummaryDoesNotFabricateNumbers:
    """★ 出せない数字は出さない (0 と不明は別物)."""

    def test_today_pnl_guarded_by_measured(self, summary_text: str):
        """measured=false なら金額を出さず「未計測」と言い切ること。"""
        assert "p.measured" in summary_text, (
            "measured ガードが消えた. 基準の取れない当日損益を数字で"
            "出すと 2026-07-15/07-19 の「幻の当日損益」が復活する"
        )
        assert "未計測" in summary_text

    def test_today_pnl_states_its_baseline(self, summary_text: str):
        """どの終値を基準にした数字かを併記すること。"""
        assert "baseline_session" in summary_text

    def test_freshness_not_asserted_before_check(self, summary_text: str):
        """ブラウザで突き合わせるまで「最新」と断言しないこと。"""
        assert "checked" in summary_text, (
            "checked ガードが消えた. 静的 export の初回 paint で"
            "「最新」と断言すると 2026-07-22 の stale 事故が見えなくなる"
        )


class TestCollapsibleCollapsesByDefaultAndLazyMounts:
    """★ 縦が縮んだ理由そのもの."""

    def test_default_is_closed(self, collapsible_text: str):
        assert (
            "defaultOpen = false" in collapsible_text
        ), "Collapsible の既定が開に変わった. 全表が開いた状態に戻る"

    def test_children_not_mounted_until_opened(self, collapsible_text: str):
        """閉じている間は children を DOM に出さない (lazy mount)。

        閉じたまま DOM に置くと決済履歴 40 行 + 保有 61 行が残り、
        静的 HTML も scroll コストも縮まない。
        """
        assert "mounted" in collapsible_text, "lazy mount が消えた"
        assert "{mounted ? (" in collapsible_text

    def test_stays_mounted_after_first_open(self, collapsible_text: str):
        """2 回目以降は hidden で隠すだけ (表の絞り込み状態を捨てない)。"""
        assert "hidden={!open}" in collapsible_text

    def test_meta_summary_visible_while_closed(self, collapsible_text: str):
        """閉じていても状態が読めるよう見出しに要約値を出す (情報を消さない)。"""
        assert "meta" in collapsible_text

    def test_exposes_expanded_state_for_a11y(self, collapsible_text: str):
        assert "aria-expanded" in collapsible_text


class TestLongTablesAreCollapsed:
    """★ 長い表が既定で畳まれ、かつ中身は消えていないこと."""

    @pytest.mark.parametrize(
        "title",
        ['title="保有ポジション"', 'title="エクイティ"', 'title="エクスポージャ"'],
    )
    def test_section_is_wrapped_in_collapsible(self, alpaca_text: str, title: str):
        assert title in alpaca_text, f"{title} の Collapsible が消えた"

    def test_realized_history_is_collapsed(self, alpaca_text: str):
        """決済履歴 (最長の表) が畳まれていること。"""
        assert "実現損益 · exit 履歴" in alpaca_text

    def test_no_section_defaults_to_open(self, alpaca_text: str):
        """どれか 1 つでも defaultOpen にすると縦が戻る。"""
        assert "defaultOpen" not in alpaca_text, (
            "AlpacaSection のどこかが defaultOpen で開いている. "
            "既定で開くセクションを足すと 11 画面に逆戻りする"
        )

    def test_detail_tables_still_exist(self, alpaca_text: str):
        """畳んだだけで情報は消していないこと (2 層化であって削除ではない)。"""
        for comp in (
            "PositionsTable",
            "ClosedTradesTable",
            "ExposureBlock",
            "EquityPanel",
        ):
            assert (
                comp in alpaca_text
            ), f"{comp} が消えた. 畳むのであって消してはいけない"

    def test_overdue_count_visible_while_collapsed(self, alpaca_text: str):
        """保有ポジションは閉じていても期限超過件数を badge で出すこと。"""
        assert "badge" in alpaca_text
        assert "期限超過" in alpaca_text


class TestPreservedBehaviours:
    """★ 再構成で落としてはいけない既存機能."""

    def test_freshness_banner_still_rendered(self, summary_text: str):
        """2026-07-22 incident 対策の stale バナーは維持。"""
        assert "FreshnessBanner" in summary_text

    def test_freshness_banner_is_presentational(self):
        """判定は lib に、表示は banner に。banner 側に閾値を残さない。"""
        banner = _read(FRESHNESS_BANNER)
        assert "PUBLISH_HOUR_JST" not in banner, (
            "鮮度の判定ロジックが banner に残っている. "
            "lib/freshness.ts と二重定義になり食い違う"
        )
        assert "PUBLISH_HOUR_JST" in _read(FRESHNESS_LIB)

    def test_equity_range_switcher_preserved(self, alpaca_text: str):
        """期間切替 (1D/1W/1M/3M/ALL) は維持。"""
        assert "RANGE_ORDER" in alpaca_text
        assert "'1D'" in alpaca_text and "'ALL'" in alpaca_text

    def test_realized_and_unrealized_stay_separate(self, alpaca_text: str):
        """実現 / 含みは別物として分離したまま (合算しない)。"""
        assert "含み損益" in alpaca_text
        assert "実現損益" in alpaca_text
        assert "合算していません" in alpaca_text

    def test_formatters_shared_between_layers(
        self, alpaca_text: str, summary_text: str
    ):
        """サマリーと詳細で同じ数字の表記が食い違わないこと。"""
        assert "@/lib/format" in alpaca_text
        assert "@/lib/format" in summary_text
        assert FORMAT_LIB.exists()


class TestOverdueDerivation:
    """★ 「期限超過」の定義が仕様どおりであること."""

    def test_overdue_is_negative_days_remaining(self, status_lib_text: str):
        """days_remaining < 0 が超過。0 は本日満期でまだ超過ではない。"""
        assert "days_remaining != null && p.days_remaining < 0" in status_lib_text

    def test_due_today_counted_separately(self, status_lib_text: str):
        assert "days_remaining === 0" in status_lib_text

    def test_pending_exit_distinguished(self, status_lib_text: str):
        """本日の exit 発注に入っている分は「詰まり」と混ぜないこと。"""
        assert "intended_pending" in status_lib_text
        assert "執行待ち" in status_lib_text

    def test_unmeasured_is_amber_not_zero(self, status_lib_text: str):
        """出せないものは 0 ではなく amber で「不明」と出すこと。"""
        assert "0 ではなく不明" in status_lib_text
