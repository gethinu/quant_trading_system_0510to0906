"""Stooq (無料・日足のみ) フォールバック データプロバイダ (stub)。

目的:
    Alpaca 無料 tier (IEX feed) は出来高が過小 (NASDAQ/NYSE 全体の 2〜3%) で
    流動性フィルタが全銘柄を棄却するリスクがある (README "Data source" / 既知の罠1)。
    その場合の **無料フォールバック** として Stooq (日足のみ、出来高は取引所全体ベース) を
    使う選択肢を残しておくための stub。

状態:
    **未実装**。実装するかどうかは smoke test の Volume 桁数を確認した上で
    ユーザが判断する (SIP 有料切替 と本フォールバックのどちらを採るか)。
    ユーザ確認後に別 iteration で中身を埋める。

契約 (実装時に守ること):
    ``get_stooq_data`` は旧 ``get_eodhd_data`` / ``get_alpaca_data`` と
    **同一スキーマ** を返すこと:
        - columns : ["Open", "High", "Low", "Close", "AdjClose", "Volume"]
        - index   : DatetimeIndex (name="Date", tz-naive, 昇順ソート済)
        - dtypes  : OHLC/AdjClose = float64, Volume = int64
        - 失敗/空 : None を返す (例外は送出しない)
"""

from __future__ import annotations

import pandas as pd


def get_stooq_data(symbol: str) -> pd.DataFrame | None:
    """Stooq から日次 OHLCV を取得する (未実装 stub)。

    旧 ``get_eodhd_data`` / ``get_alpaca_data`` の drop-in replacement として
    同一スキーマを返す想定。現時点では未実装。

    Parameters
    ----------
    symbol : str
        取得対象シンボル (``AAPL`` / ``AAPL.US`` 等)。

    Returns
    -------
    pd.DataFrame | None
        実装後は Open/High/Low/Close/AdjClose/Volume を持つ日次 DataFrame。

    Raises
    ------
    NotImplementedError
        常に送出。ユーザ確認後の別 iteration で実装する。
    """
    # TODO: ユーザが stooq フォールバックを選択したら実装する。
    #   候補: `pandas_datareader.stooq` もしくは
    #   https://stooq.com/q/d/l/?s=<sym>.us&i=d の CSV を requests で取得し、
    #   列名を Open/High/Low/Close/Volume に rename、AdjClose=Close、
    #   index を tz-naive DatetimeIndex(name="Date") へ整形して返す。
    raise NotImplementedError(
        "get_stooq_data は未実装です。Alpaca IEX の出来高過小問題への"
        "フォールバックとして、ユーザ確認後に実装予定です。"
    )
