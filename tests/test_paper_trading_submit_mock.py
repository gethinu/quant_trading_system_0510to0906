"""paper_trading_submit.py の safeguard を mock で検証する (live API 呼び出しなし)。

検証項目:
    - ALPACA_PAPER=false で fail-fast (submit されない)
    - --confirm 無しは dry-run (submit されない)
    - --yes bypass で対話なしに全注文 submit
    - preview 突合 (一致で OK / 乖離で abort)
    - Ctrl+C interrupt で残注文 skip
すべて offline (submit_paper_order を mock)。
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

import scripts.paper_trading_submit as sub
from common.alpaca_trading import signals_json_to_orders

_DATE = "20260701"


@pytest.fixture(autouse=True)
def _paper_env(monkeypatch):
    """既定で Paper 環境を保証 (個別テストで上書き可)。"""
    monkeypatch.setenv("ALPACA_PAPER", "true")
    monkeypatch.setenv("ALPACA_API_BASE_URL", "https://paper-api.alpaca.markets")


@pytest.fixture
def mock_submit(monkeypatch):
    """submit_paper_order を mock 化し呼び出しを記録する。"""
    m = MagicMock(return_value=MagicMock(order_id="mock-id", status="accepted"))
    monkeypatch.setattr(sub, "submit_paper_order", m)
    return m


def _write_matching_preview(preview_dir: Path, equity: int) -> Path:
    """mock signals JSON から実 plan と一致する preview を書き出す。"""
    signals_json = json.loads(sub.resolve_signals_json.__globals__["_MOCK_JSON"].read_text("utf-8"))
    plan = signals_json_to_orders(signals_json, account_equity=equity)
    preview_dir.mkdir(parents=True, exist_ok=True)
    path = preview_dir / f"orders_preview_{_DATE}_{equity}.json"
    path.write_text(json.dumps(plan.to_preview_dict()), encoding="utf-8")
    return path


def test_fail_fast_when_not_paper(monkeypatch, mock_submit):
    monkeypatch.setenv("ALPACA_PAPER", "false")
    rc = sub.main(
        ["--demo-json", "--account-equity", "10000", "--confirm", "--yes", "--skip-reconcile"]
    )
    assert rc == 2
    mock_submit.assert_not_called()


def test_no_confirm_is_dry_run(mock_submit):
    rc = sub.main(["--demo-json", "--account-equity", "10000"])
    assert rc == 0
    mock_submit.assert_not_called()


def test_yes_bypasses_prompt_and_submits_all(monkeypatch, mock_submit):
    # input() が呼ばれたら失敗 (対話が走っていない証明)
    monkeypatch.setattr("builtins.input", lambda *a: pytest.fail("input should be bypassed"))
    rc = sub.main(
        ["--demo-json", "--account-equity", "10000", "--confirm", "--yes", "--skip-reconcile"]
    )
    assert rc == 0
    # medium tier = 11 注文すべて submit
    assert mock_submit.call_count == 11


def test_fractional_order_passes_notional_not_qty(mock_submit):
    sub.main(["--demo-json", "--account-equity", "10000", "--confirm", "--yes", "--skip-reconcile"])
    # 全 fractional なので notional kwarg が渡り qty=0
    first = mock_submit.call_args_list[0]
    assert first.kwargs["notional"] is not None
    assert first.args[1] == 0  # qty 引数は 0 (notional 発注)


def test_reconcile_ok_allows_submit(tmp_path, mock_submit):
    _write_matching_preview(tmp_path, 10000)
    rc = sub.main(
        [
            "--demo-json", "--account-equity", "10000", "--confirm", "--yes",
            "--preview-dir", str(tmp_path),
        ]
    )
    assert rc == 0
    assert mock_submit.call_count == 11


def test_reconcile_divergence_aborts(tmp_path, mock_submit):
    # 空の orders を持つ preview → 乖離 → abort
    bad = tmp_path / f"orders_preview_{_DATE}_10000.json"
    bad.write_text(json.dumps({"orders": []}), encoding="utf-8")
    rc = sub.main(
        [
            "--demo-json", "--account-equity", "10000", "--confirm", "--yes",
            "--preview-dir", str(tmp_path),
        ]
    )
    assert rc == 2
    mock_submit.assert_not_called()


def test_missing_preview_aborts(tmp_path, mock_submit):
    rc = sub.main(
        [
            "--demo-json", "--account-equity", "10000", "--confirm", "--yes",
            "--preview-dir", str(tmp_path),  # preview 無し
        ]
    )
    assert rc == 2
    mock_submit.assert_not_called()


def test_keyboard_interrupt_halts_remaining(monkeypatch, mock_submit):
    # 2 件目で Ctrl+C → 残りは skip
    mock_submit.side_effect = [
        MagicMock(order_id="1", status="accepted"),
        KeyboardInterrupt(),
    ]
    rc = sub.main(
        ["--demo-json", "--account-equity", "10000", "--confirm", "--yes", "--skip-reconcile"]
    )
    # KeyboardInterrupt は捕捉され、途中終了 (failed=0 なので rc=0)
    assert rc == 0
    assert mock_submit.call_count == 2  # 3 件目以降は呼ばれない
