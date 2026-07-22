"""Exit (手仕舞い) の *実績* 台帳を broker fill から再構成する pure module。

背景 / なぜ必要か
-----------------
既存の exit 経路は ``scripts/paper_exit_check.py`` が **exit の意図 (proposal)** を
``results_csv/exit_orders_YYYYMMDD.json`` に書くだけで、その後 *実際に約定したか*
*いくら儲かった/損したか* を durable に残す場所がどこにも無かった。
結果として「exit が計測されていない」= 実現損益 (realized P&L) が系のどこにも
存在しない状態になっていた。

この module は Alpaca の ``/v2/account/activities/FILL`` (= 約定の ground truth)
から round-trip を再構成し、**実現損益**と**計測できたか否か**を明示的に返す。

設計方針 (silent success を作らない)
------------------------------------
- 数字を「0 で埋めない」。計測できなければ ``measured=False`` + 理由を返す。
- fill から再構成した建玉と broker の実 position が食い違ったら握り潰さず
  ``LotDiscrepancy`` として列挙する (ticker rename / fill 欠落の検出)。
- 損益基準を混ぜない。realized (確定) と unrealized (含み) は別物として扱い、
  この module は **realized のみ**を扱う。
- I/O 無し。network も file も触らない (test しやすさのため)。
"""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from decimal import Decimal
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

# 建玉突合の許容誤差。端株 (fractional share) があるので完全一致は要求しない。
QTY_EPSILON = Decimal("0.0001")

# 「その約定はどの立会日のものか」は必ず US 東部時間で決める。
# UTC 日付で切ると冬時間の時間外 (19:00-20:00 EST = 00:00-01:00 UTC 翌日) が
# 翌日に飛び、日次実現損益が 1 日ずれる。
MARKET_TZ = ZoneInfo("America/New_York")

# 立会の進行状態。exit の「意図したのに約定していない」を *まだ執行機会が来ていない*
# 分まで失敗として数えないために使う (= 朝の時点で毎日 20 件の偽陽性を出さない)。
SESSION_BEFORE_OPEN = "before_open"
SESSION_OPEN = "open"
SESSION_CLOSED = "closed"
SESSION_UNKNOWN = "unknown"


def session_date_of(timestamp: str) -> str:
    """ISO8601 (UTC) の約定時刻 -> その約定が属する立会日 (``YYYY-MM-DD``, ET)。

    parse できない値は握り潰さず先頭 10 文字を返す (情報を捨てるより粗くても残す)。
    """
    raw = str(timestamp)
    try:
        stamp = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return raw[:10]
    if stamp.tzinfo is None:
        stamp = stamp.replace(tzinfo=timezone.utc)
    return str(stamp.astimezone(MARKET_TZ).date())


class ExitLedgerError(ValueError):
    """fill payload が想定形と違う (silent skip せず上げる)。"""


# ---------------------------------------------------------------------------
# データ型
# ---------------------------------------------------------------------------


@dataclass
class Fill:
    """Alpaca FILL activity 1 件 (partial_fill / fill を区別せず同列に扱う)。"""

    symbol: str
    side: str  # "buy" | "sell" | "sell_short"
    qty: Decimal
    price: Decimal
    transaction_time: str  # ISO8601 UTC
    order_id: str | None = None
    activity_id: str | None = None

    @property
    def signed_qty(self) -> Decimal:
        """買い = 正、売り (sell / sell_short) = 負。"""
        return self.qty if self.side == "buy" else -self.qty


@dataclass
class OpenLot:
    """未決済の建玉 1 枚 (FIFO の 1 要素)。"""

    symbol: str
    qty: Decimal  # 符号つき: 正 = long, 負 = short
    price: Decimal
    opened_at: str


@dataclass
class ClosedTrade:
    """決済済み round-trip 1 本 (entry lot と exit fill の付き合わせ結果)。"""

    symbol: str
    side: str  # "long" | "short"
    qty: Decimal  # 常に正 (決済された株数)
    entry_time: str
    entry_price: Decimal
    exit_time: str
    exit_price: Decimal
    realized_pl: Decimal
    system: str | None = None
    exit_reason: str | None = None
    exit_order_id: str | None = None

    @property
    def entry_session(self) -> str:
        """entry が属する立会日 (ET)。"""
        return session_date_of(self.entry_time)

    @property
    def exit_session(self) -> str:
        """exit が属する立会日 (ET)。日次実現損益はこれで束ねる。"""
        return session_date_of(self.exit_time)

    @property
    def holding_days(self) -> int:
        """entry から exit までの暦日数 (立会日ベース)。"""
        from datetime import date

        try:
            a = date.fromisoformat(self.entry_session)
            b = date.fromisoformat(self.exit_session)
        except ValueError:
            return 0
        return (b - a).days

    @property
    def realized_pl_pct(self) -> Decimal | None:
        """entry notional に対する実現損益率 (%)。entry が 0 なら None。"""
        notional = self.entry_price * self.qty
        if notional == 0:
            return None
        return self.realized_pl / notional * Decimal(100)

    def to_row(self) -> dict[str, Any]:
        pct = self.realized_pl_pct
        return {
            "symbol": self.symbol,
            "side": self.side,
            "qty": float(self.qty),
            "system": self.system,
            "entry_time": self.entry_time,
            "entry_session": self.entry_session,
            "entry_price": float(self.entry_price),
            "exit_time": self.exit_time,
            "exit_session": self.exit_session,
            "exit_price": float(self.exit_price),
            "holding_days": self.holding_days,
            "realized_pl": round(float(self.realized_pl), 2),
            "realized_pl_pct": round(float(pct), 3) if pct is not None else None,
            "exit_reason": self.exit_reason,
            "exit_order_id": self.exit_order_id,
        }


@dataclass
class LotDiscrepancy:
    """fill 再構成の建玉 と broker の実 position が食い違った symbol。

    これが 1 件でもあれば、その symbol の realized P&L は信用できない
    (= 未計測)。黙って捨てず必ず表に出す。
    """

    symbol: str
    reconstructed_qty: Decimal
    broker_qty: Decimal
    reason: str

    def to_row(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "reconstructed_qty": float(self.reconstructed_qty),
            "broker_qty": float(self.broker_qty),
            "reason": self.reason,
        }


@dataclass
class LedgerResult:
    """再構成の全結果。``measured`` が False の時は数字を信用しないこと。"""

    closed_trades: list[ClosedTrade] = field(default_factory=list)
    open_lots: dict[str, list[OpenLot]] = field(default_factory=dict)
    discrepancies: list[LotDiscrepancy] = field(default_factory=list)
    fills_seen: int = 0
    coverage_start: str | None = None
    coverage_end: str | None = None

    @property
    def measured(self) -> bool:
        """約定 ground truth を掴めているか (= 実現損益を計算する土台がある)。

        建玉の食い違いは *symbol 単位* の問題なので全体の計測可否は落とさない。
        全体が信用できるかは :attr:`complete` を見ること。
        """
        return self.fills_seen > 0

    @property
    def complete(self) -> bool:
        """取りこぼしゼロか。1 件でも食い違えば False。"""
        return self.measured and not self.discrepancies

    @property
    def unmeasured_symbols(self) -> list[str]:
        """この symbol の実現損益は信用できない、という list。"""
        return sorted({d.symbol for d in self.discrepancies})

    def measurement_reasons(self) -> list[str]:
        """計測できていない / 取りこぼしている理由を人間可読で列挙 (空 = 完全)。"""
        reasons: list[str] = []
        if self.fills_seen == 0:
            reasons.append(
                "no_fill_activities: broker から約定履歴が 1 件も取れていない"
            )
        if self.discrepancies:
            syms = ", ".join(self.unmeasured_symbols[:10])
            more = (
                ""
                if len(self.unmeasured_symbols) <= 10
                else f" (他 {len(self.unmeasured_symbols) - 10} 件)"
            )
            reasons.append(
                f"lot_mismatch: 再構成建玉が broker position と不一致 [{syms}]{more}"
            )
        return reasons


# ---------------------------------------------------------------------------
# fill parsing
# ---------------------------------------------------------------------------


def parse_fill(raw: Mapping[str, Any]) -> Fill:
    """Alpaca activity dict -> Fill。必須 key 欠落は握り潰さず raise。"""
    try:
        symbol = str(raw["symbol"]).upper()
        side = str(raw["side"]).lower()
        qty = Decimal(str(raw["qty"]))
        price = Decimal(str(raw["price"]))
        tm = str(raw["transaction_time"])
    except (KeyError, TypeError, ArithmeticError) as exc:
        raise ExitLedgerError(f"FILL activity の parse に失敗: {raw!r}") from exc
    if side not in ("buy", "sell", "sell_short"):
        raise ExitLedgerError(f"未知の side={side!r} (activity={raw!r})")
    if qty <= 0:
        raise ExitLedgerError(f"qty<=0 の FILL: {raw!r}")
    return Fill(
        symbol=symbol,
        side=side,
        qty=qty,
        price=price,
        transaction_time=tm,
        order_id=str(raw.get("order_id") or "") or None,
        activity_id=str(raw.get("id") or "") or None,
    )


def parse_fills(rows: Iterable[Mapping[str, Any]]) -> list[Fill]:
    """activity dict 群 -> Fill list (transaction_time 昇順に整列)。"""
    fills = [parse_fill(r) for r in rows]
    fills.sort(key=lambda f: (f.transaction_time, f.activity_id or ""))
    return fills


# ---------------------------------------------------------------------------
# round-trip 再構成 (FIFO)
# ---------------------------------------------------------------------------


def reconstruct_round_trips(fills: Sequence[Fill]) -> LedgerResult:
    """時系列 fill から FIFO で round-trip を組み、実現損益を確定させる。

    long / short 両対応。反対売買が入った時に古い lot から消し込み、
    消し込んだ分だけ ``ClosedTrade`` を生成する。建玉が反転する fill
    (例: long 100 を 150 売る) も残り 50 を新規 short lot として扱う。
    """
    result = LedgerResult(fills_seen=len(fills))
    if fills:
        result.coverage_start = fills[0].transaction_time
        result.coverage_end = fills[-1].transaction_time

    books: dict[str, deque[OpenLot]] = {}

    for f in fills:
        book = books.setdefault(f.symbol, deque())
        remaining = f.signed_qty

        # 反対側の lot がある限り消し込む
        while remaining != 0 and book and (book[0].qty > 0) != (remaining > 0):
            lot = book[0]
            take = min(abs(lot.qty), abs(remaining))
            direction = Decimal(1) if lot.qty > 0 else Decimal(-1)
            # long: (exit - entry) * qty / short: (entry - exit) * qty
            realized = (f.price - lot.price) * take * direction
            result.closed_trades.append(
                ClosedTrade(
                    symbol=f.symbol,
                    side="long" if direction > 0 else "short",
                    qty=take,
                    entry_time=lot.opened_at,
                    entry_price=lot.price,
                    exit_time=f.transaction_time,
                    exit_price=f.price,
                    realized_pl=realized,
                    exit_order_id=f.order_id,
                )
            )
            lot.qty -= direction * take
            remaining += direction * take
            if lot.qty == 0:
                book.popleft()

        if remaining != 0:
            book.append(
                OpenLot(
                    symbol=f.symbol,
                    qty=remaining,
                    price=f.price,
                    opened_at=f.transaction_time,
                )
            )

    result.open_lots = {sym: list(book) for sym, book in books.items() if book}
    return result


def net_open_qty(result: LedgerResult) -> dict[str, Decimal]:
    """symbol -> 再構成された正味建玉 (符号つき)。0 の symbol は落とす。"""
    out: dict[str, Decimal] = {}
    for sym, lots in result.open_lots.items():
        total = sum((lot.qty for lot in lots), Decimal(0))
        if total != 0:
            out[sym] = total
    return out


def reconcile_with_broker(
    result: LedgerResult,
    broker_positions: Mapping[str, Any],
    *,
    epsilon: Decimal = QTY_EPSILON,
) -> list[LotDiscrepancy]:
    """再構成建玉 と broker の実 position を突合し、食い違いを列挙する。

    ``broker_positions`` は ``{symbol: qty}`` (符号つき; short は負)。
    差分は ``result.discrepancies`` にも格納される (呼び出し側の利便のため)。

    典型的な食い違い:
      - ticker rename (旧 symbol の建玉が残り、新 symbol が broker 側だけに居る)
      - fill activity の取りこぼし (page 抜け / 期間外)
      - corporate action (分割・併合) による株数変化
    """
    recon = net_open_qty(result)
    broker = {
        str(k).upper(): Decimal(str(v))
        for k, v in broker_positions.items()
        if Decimal(str(v)) != 0
    }

    discrepancies: list[LotDiscrepancy] = []
    for sym in sorted(set(recon) | set(broker)):
        a = recon.get(sym, Decimal(0))
        b = broker.get(sym, Decimal(0))
        if abs(a - b) <= epsilon:
            continue
        if b == 0:
            reason = "reconstructed_only: fill 上は建玉が残るが broker に position 無し (ticker rename / corporate action の疑い)"
        elif a == 0:
            reason = "broker_only: broker に position があるが fill 履歴から再構成できない (fill 取りこぼしの疑い)"
        else:
            reason = (
                "qty_mismatch: 株数が一致しない (分割 / 部分約定の取りこぼしの疑い)"
            )
        discrepancies.append(
            LotDiscrepancy(symbol=sym, reconstructed_qty=a, broker_qty=b, reason=reason)
        )

    result.discrepancies = discrepancies
    return discrepancies


# ---------------------------------------------------------------------------
# 集計
# ---------------------------------------------------------------------------


def realized_by_day(trades: Iterable[ClosedTrade]) -> dict[str, Decimal]:
    """exit の立会日 (ET) -> 実現損益合計。"""
    out: dict[str, Decimal] = {}
    for t in trades:
        day = t.exit_session
        out[day] = out.get(day, Decimal(0)) + t.realized_pl
    return out


def realized_cumulative(by_day: Mapping[str, Decimal]) -> list[dict[str, Any]]:
    """日次実現損益 -> 累計付きの時系列 (日付昇順)。"""
    running = Decimal(0)
    rows: list[dict[str, Any]] = []
    for day in sorted(by_day):
        running += by_day[day]
        rows.append(
            {
                "t": day,
                "realized_pl": round(float(by_day[day]), 2),
                "realized_pl_cum": round(float(running), 2),
            }
        )
    return rows


def summarize_realized(trades: Sequence[ClosedTrade]) -> dict[str, Any]:
    """勝率 / 平均勝ち負け / 総実現損益。trade が 0 本なら数字は None (0 で埋めない)。"""
    if not trades:
        return {
            "n_trades": 0,
            "total_realized_pl": None,
            "win_rate_pct": None,
            "n_wins": 0,
            "n_losses": 0,
            "avg_win": None,
            "avg_loss": None,
            "best": None,
            "worst": None,
        }
    wins = [t for t in trades if t.realized_pl > 0]
    losses = [t for t in trades if t.realized_pl < 0]
    total = sum((t.realized_pl for t in trades), Decimal(0))
    best = max(trades, key=lambda t: t.realized_pl)
    worst = min(trades, key=lambda t: t.realized_pl)
    return {
        "n_trades": len(trades),
        "total_realized_pl": round(float(total), 2),
        "win_rate_pct": round(len(wins) / len(trades) * 100.0, 1),
        "n_wins": len(wins),
        "n_losses": len(losses),
        "avg_win": (
            round(float(sum((t.realized_pl for t in wins), Decimal(0)) / len(wins)), 2)
            if wins
            else None
        ),
        "avg_loss": (
            round(
                float(sum((t.realized_pl for t in losses), Decimal(0)) / len(losses)), 2
            )
            if losses
            else None
        ),
        "best": {
            "symbol": best.symbol,
            "realized_pl": round(float(best.realized_pl), 2),
        },
        "worst": {
            "symbol": worst.symbol,
            "realized_pl": round(float(worst.realized_pl), 2),
        },
    }


def summarize_by_system(trades: Sequence[ClosedTrade]) -> dict[str, dict[str, Any]]:
    """system tag 別の実現損益。tag 未解決は ``"unknown"`` に集める (捨てない)。"""
    buckets: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        buckets.setdefault(t.system or "unknown", []).append(t)
    return {k: summarize_realized(v) for k, v in sorted(buckets.items())}


# ---------------------------------------------------------------------------
# exit の意図 (exit_orders_*.json) と 実績 (fill) の突合
# ---------------------------------------------------------------------------


def reconcile_intents_with_fills(
    intents: Sequence[Mapping[str, Any]],
    trades: Sequence[ClosedTrade],
    *,
    session_date: str,
    session_state: str = SESSION_UNKNOWN,
) -> dict[str, Any]:
    """「exit するつもりだった」 vs 「実際に決済された」を突合する。

    ``intents`` は ``exit_orders_YYYYMMDD.json`` の ``exits`` 行。
    ``session_date`` は対象立会日 (``YYYY-MM-DD``, ET)。当該立会日に exit した
    symbol 集合と比較して *意図したのに約定していない* symbol を列挙する。
    これが exit の「取りこぼし」検知そのもの。

    ``session_state`` で **まだ執行機会が来ていない** 分を切り分ける:

    - ``before_open`` / ``open``  : 未約定は ``intended_pending`` (失敗ではない)
    - ``closed`` / ``unknown``    : 未約定は ``intended_not_filled`` (= 取りこぼし)

    ``unknown`` (broker clock が引けない) を「まだ執行前」側に倒さないのは、
    silent success を作らないため。判定不能なら *表に出す* 方に倒す。
    """
    intended: dict[str, str | None] = {}
    for row in intents:
        sym = str(row.get("symbol", "")).upper()
        if not sym:
            continue
        # 同一 symbol に複数 intent がある場合は最初の reason を採用
        intended.setdefault(sym, row.get("reason"))

    filled_syms = {t.symbol for t in trades if t.exit_session == session_date}
    missing = sorted(s for s in intended if s not in filled_syms)
    unexpected = sorted(filled_syms - set(intended))
    pending_phase = session_state in (SESSION_BEFORE_OPEN, SESSION_OPEN)
    rows = [{"symbol": s, "reason": intended[s]} for s in missing]

    return {
        "session_date": session_date,
        "session_state": session_state,
        "n_intended": len(intended),
        "n_filled": len(filled_syms),
        # 立会が終わって初めて「約定しなかった」と断定できる。
        "intended_not_filled": [] if pending_phase else rows,
        # 執行機会がまだ来ていない分 (取りこぼしではないが、黙って消さない)。
        "intended_pending": rows if pending_phase else [],
        "filled_not_intended": list(unexpected),
        "fully_reconciled": pending_phase or not missing,
        # 立会が終わっているか = 「取りこぼし無し」を断定してよいか。
        "evaluated": not pending_phase,
    }


# ---------------------------------------------------------------------------
# 当日損益の基準 (equity basis)
# ---------------------------------------------------------------------------
#
# 【重要 / この system で繰り返し事故になっている点】
#
# Alpaca の ``account.last_equity`` および portfolio-history の *daily (1D)* 系列は、
# 現在の ``account.equity`` および *intraday* 系列とは **会計基準が違う**。
#
# 2026-07 の実測: 上場廃止 (AssetStatus.INACTIVE) の CDTX + FOLD の時価
# 合計 $4,285.87 が daily 系列側にだけ計上されておらず、
#   equity(103,943) - last_equity(99,356) = +4,587
# という「当日損益」が丸ごと幻になっていた (実際の当日変動は +$87)。
#
# したがって ``equity - last_equity`` は **基準の違う 2 つの数を引いている**
# ので当日損益として使ってはいけない。同一基準 (intraday 系列) 同士で引く。
#
# 補正・注釈で誤魔化さない。同一基準の前セッション終値が取れない時は
# 数字を出さず ``measured=False`` を返す (間違った数字より出さない方がマシ)。


@dataclass
class SessionPnl:
    """当日損益。``measured`` が False の時 ``total_pl`` は必ず None。"""

    session_date: str | None
    equity_now: float | None
    baseline_equity: float | None
    baseline_session: str | None
    total_pl: float | None
    total_pl_pct: float | None
    realized_pl: float | None
    unrealized_delta: float | None
    basis: str
    measured: bool
    reason: str | None = None

    def to_row(self) -> dict[str, Any]:
        return {
            "session_date": self.session_date,
            "equity_now": self.equity_now,
            "baseline_equity": self.baseline_equity,
            "baseline_session": self.baseline_session,
            "total_pl": self.total_pl,
            "total_pl_pct": self.total_pl_pct,
            "realized_pl": self.realized_pl,
            "unrealized_delta": self.unrealized_delta,
            "basis": self.basis,
            "measured": self.measured,
            "reason": self.reason,
        }


def pick_prev_session_close(
    intraday_by_session: Mapping[str, float],
    session_date: str,
) -> tuple[str | None, float | None]:
    """intraday 系列から *現セッションより前* の直近セッション終値を選ぶ。

    ``intraday_by_session`` は ``{"YYYY-MM-DD": そのセッション最後の equity}``。
    現セッション自身は基準にしない (それだと当日損益が常に 0 になる)。
    """
    prior = [d for d in intraday_by_session if d < session_date]
    if not prior:
        return (None, None)
    day = max(prior)
    return (day, intraday_by_session[day])


def resolve_session_pnl(
    *,
    equity_now: float | None,
    session_date: str | None,
    intraday_by_session: Mapping[str, float],
    realized_pl: float | None = None,
) -> SessionPnl:
    """当日損益を **同一基準** で確定させる。出せない時は数字を出さない。

    basis は常に ``"prev_session_intraday"`` (= 前セッションの intraday 終値)。
    ``last_equity`` / daily-close 系列は基準が違うので一切使わない。
    """
    unavailable = SessionPnl(
        session_date=session_date,
        equity_now=equity_now,
        baseline_equity=None,
        baseline_session=None,
        total_pl=None,
        total_pl_pct=None,
        realized_pl=realized_pl,
        unrealized_delta=None,
        basis="unavailable",
        measured=False,
    )

    if equity_now is None or equity_now <= 0:
        unavailable.reason = "equity_now が取得できない"
        return unavailable
    if not session_date:
        unavailable.reason = "現セッション日付が確定できない (broker clock 未取得)"
        return unavailable
    if not intraday_by_session:
        unavailable.reason = "intraday equity 系列が空 (portfolio-history 取得失敗)"
        return unavailable

    baseline_session, baseline = pick_prev_session_close(
        intraday_by_session, session_date
    )
    if baseline is None or baseline <= 0:
        unavailable.reason = f"同一基準の前セッション終値が無い (現セッション {session_date} より前の intraday point 不在)"
        return unavailable

    total = equity_now - baseline
    realized = realized_pl
    return SessionPnl(
        session_date=session_date,
        equity_now=round(equity_now, 2),
        baseline_equity=round(baseline, 2),
        baseline_session=baseline_session,
        total_pl=round(total, 2),
        total_pl_pct=round(total / baseline * 100.0, 3),
        realized_pl=round(realized, 2) if realized is not None else None,
        unrealized_delta=round(total - realized, 2) if realized is not None else None,
        basis="prev_session_intraday",
        measured=True,
    )
