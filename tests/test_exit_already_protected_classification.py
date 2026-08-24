"""exit 発注の「既に保護済み」を失敗から分離することの検証。

2026-08-18 の run で exit 23 件中 **12 件が failed** と記録された。調べると全件が
Alpaca の buying-power 拒否 (code 40310000) で、いずれも
``held_for_orders == existing_qty`` = **建玉が既存の未約定注文で全量予約済み**。
つまり保護は既に掛かっており危険ではないのに、素の failed として数えられ、
**本当の exit 失敗がノイズに埋もれる** 状態だった。

判定は保守的にする (over-claim しない):
  code 40310000 かつ existing_qty > 0 かつ held_for_orders >= existing_qty
だけを ``already_protected`` とし、部分予約や他の資金不足は通常の失敗のまま。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from common.alpaca_trading import PreparedExit, classify_exit_submit_error  # noqa: E402

FULL = (
    "資金不足 (insufficient buying power): "
    '{"available":"0","code":40310000,"existing_qty":"105","held_for_orders":"105"}'
)


def test_fully_reserved_position_is_already_protected():
    assert classify_exit_submit_error(FULL) == "already_protected"


def test_over_reserved_is_also_already_protected():
    """held_for_orders > existing_qty (端株丸め等) も保護済みとみなす。"""
    err = '{"code":40310000,"existing_qty":"12.04","held_for_orders":"12.05"}'
    assert classify_exit_submit_error(err) == "already_protected"


def test_partially_reserved_stays_a_real_failure():
    """一部しか予約されていなければ保護は不完全 = 失敗のまま扱う。"""
    err = '{"code":40310000,"existing_qty":"105","held_for_orders":"40"}'
    assert classify_exit_submit_error(err) is None


def test_zero_position_is_not_classified():
    err = '{"code":40310000,"existing_qty":"0","held_for_orders":"0"}'
    assert classify_exit_submit_error(err) is None


@pytest.mark.parametrize(
    "err",
    [
        None,
        "",
        '無効シンボル (symbol invalid / not tradable): {"code":42210000}',
        "市場休場 (market closed)",
        "発注失敗: connection reset",
    ],
)
def test_other_errors_stay_failures(err):
    assert classify_exit_submit_error(err) is None


def test_missing_quantity_fields_are_not_classified():
    """code だけでは保護済みと断定できない (数量の裏づけを必須にする)。"""
    assert classify_exit_submit_error('{"code":40310000}') is None


def test_negative_short_quantities_are_normalised():
    """ショート建玉は数量が負で返るため絶対値で比較する。"""
    err = '{"code":40310000,"existing_qty":"-119","held_for_orders":"-119"}'
    assert classify_exit_submit_error(err) == "already_protected"


def test_prepared_exit_separates_skip_reason_from_error():
    """skip_reason と error を別フィールドに持ち、下流が区別できること。"""
    po = PreparedExit(
        symbol="ADVB",
        system="system1",
        qty=105,
        side="sell",
        order_type="stop",
        reason="protect_stop",
    )
    row = po.to_row()
    assert "skip_reason" in row and "error" in row
    assert row["skip_reason"] is None

    po.skip_reason = f"already_protected:{FULL}"
    row = po.to_row()
    # error は None のまま = 失敗として集計されない
    assert row["error"] is None
    assert row["skip_reason"].startswith("already_protected:")
    # broker の生文言は監査のため保持する
    assert "40310000" in row["skip_reason"]
