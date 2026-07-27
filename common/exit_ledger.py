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

# --- system 帰属の「根拠の強さ」 ---------------------------------------------
# どの system が建てた玉かを *何を根拠に* 決めたか。強い順に並べてある。
# 強さを残さないと「symbol 単位の当て推量」と「trade 単位の ground truth」が
# 表示上 同じ顔をしてしまうため、必ず trade ごとに記録する。
SYSTEM_SOURCE_ENTRY_ORDER = "entry_order"  # entry 注文の client_order_id (trade 単位)
SYSTEM_SOURCE_ORDER_FILE = "order_file"  # paper_orders_*.json (symbol 単位)
SYSTEM_SOURCE_SYMBOL_MAP = "symbol_map"  # symbol_system_map.json (symbol 単位)

SYSTEM_SOURCE_LABEL = {
    SYSTEM_SOURCE_ENTRY_ORDER: "entry 注文の client_order_id (trade 単位の確定根拠)",
    SYSTEM_SOURCE_ORDER_FILE: "発注記録 paper_orders_*.json (symbol 単位)",
    SYSTEM_SOURCE_SYMBOL_MAP: "symbol_system_map.json (symbol 単位・時点情報なし)",
}

# --- なぜ system 不明なのか ---------------------------------------------------
# 「unknown」で終わらせず種別を残す。どれも *推測で埋めない* ことが前提。
UNKNOWN_NO_ENTRY_ORDER_ID = "no_entry_order_id"
UNKNOWN_ENTRY_ORDER_NOT_FOUND = "entry_order_not_found"
UNKNOWN_ENTRY_ORDER_UNTAGGED = "entry_order_untagged"

UNKNOWN_REASON_LABEL = {
    UNKNOWN_NO_ENTRY_ORDER_ID: (
        "約定に order_id が無く entry 注文を特定できない (broker 側の記録欠落)"
    ),
    UNKNOWN_ENTRY_ORDER_NOT_FOUND: (
        "entry 注文が broker の注文履歴に見つからない (履歴の保持期間外など)"
    ),
    UNKNOWN_ENTRY_ORDER_UNTAGGED: (
        "entry 注文の client_order_id に system tag が無い"
        " (system tag を付ける前の発注。事後に判別する術が無い)"
    ),
}


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
    # この玉を建てた注文。system 帰属の ground truth を round-trip まで運ぶ。
    order_id: str | None = None
    # 建てた時点の生の symbol (rename 前)。canonical と違う時だけ意味を持つ。
    raw_symbol: str | None = None


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
    entry_order_id: str | None = None
    # ticker rename で symbol が変わった場合の *元の* symbol 群 (canonical 以外)。
    # 「EKSO の決済なのに 07-06 の CHRN の値段」が追えなくならないよう残す。
    symbol_aliases: list[str] = field(default_factory=list)
    # system を「何を根拠に」付けたか / 付かなかったのは「なぜ」か。
    # 片方だけが埋まる (system があれば source、無ければ unknown_reason)。
    system_source: str | None = None
    system_unknown_reason: str | None = None

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
            "entry_order_id": self.entry_order_id,
            "system_source": self.system_source,
            "system_unknown_reason": self.system_unknown_reason,
            "symbol_aliases": list(self.symbol_aliases),
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


def normalize_rename_map(rows: Iterable[Mapping[str, Any]] | None) -> dict[str, str]:
    """rename 定義 -> ``alias(大文字) -> canonical(大文字)``。

    自己参照 (alias == canonical) と、alias が別の alias を指す **連鎖** は落とす。
    連鎖を許すと A->B->C の解決順で結果が変わり、静かに間違った統合が起きるため。
    """
    raw: dict[str, str] = {}
    for row in rows or []:
        alias = str(row.get("alias", "") or "").strip().upper()
        canonical = str(row.get("canonical", "") or "").strip().upper()
        if not alias or not canonical or alias == canonical:
            continue
        raw[alias] = canonical
    # canonical 側がさらに alias になっている (= 連鎖) 対は採用しない。
    return {a: c for a, c in raw.items() if c not in raw}


def canonical_symbol(symbol: str, aliases: Mapping[str, str] | None) -> str:
    """rename マップを 1 段だけ適用した symbol (マップが無ければそのまま)。"""
    sym = str(symbol).upper()
    if not aliases:
        return sym
    return str(aliases.get(sym, sym)).upper()


def select_applicable_renames(
    rows: Sequence[Mapping[str, Any]],
    candidates: Sequence[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """手で書いた rename 定義のうち、**約定履歴が独立に裏づけた対だけ**を採用する。

    ``candidates`` は :func:`pair_rename_candidates` の出力 = 「建玉の残差が
    *一意に* 打ち消し合う対」。config に書いてあっても、この裏づけが無い対は
    採用しない。設定ファイルを書き換えるだけでは架空の round-trip を作れない、
    という保証がこの関数の存在理由。

    戻り値は ``(applied, rejected)``。``rejected`` には理由が入る。
    """
    proposed = {
        frozenset(
            (
                str(c.get("from_symbol", "")).upper(),
                str(c.get("to_symbol", "")).upper(),
            )
        ): c
        for c in candidates
    }
    applied: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    for row in rows:
        alias = str(row.get("alias", "") or "").strip().upper()
        canonical = str(row.get("canonical", "") or "").strip().upper()
        entry = dict(row)
        entry["alias"] = alias
        entry["canonical"] = canonical
        if not alias or not canonical:
            entry["rejected_reason"] = "incomplete: alias / canonical が空"
            rejected.append(entry)
            continue
        if alias == canonical:
            entry["rejected_reason"] = "self_reference: alias と canonical が同じ"
            rejected.append(entry)
            continue
        match = proposed.get(frozenset((alias, canonical)))
        if match is None:
            entry["rejected_reason"] = (
                "unique_offset_not_found: 約定履歴から再構成した建玉の残差が"
                " 一意に打ち消し合う対として現れない (裏づけ無しなので統合しない)"
            )
            rejected.append(entry)
            continue
        entry["observed_qty"] = match.get("qty")
        entry["corroboration"] = match.get("evidence")
        applied.append(entry)
    return applied, rejected


def reconstruct_round_trips(
    fills: Sequence[Fill], *, symbol_aliases: Mapping[str, str] | None = None
) -> LedgerResult:
    """時系列 fill から FIFO で round-trip を組み、実現損益を確定させる。

    long / short 両対応。反対売買が入った時に古い lot から消し込み、
    消し込んだ分だけ ``ClosedTrade`` を生成する。建玉が反転する fill
    (例: long 100 を 150 売る) も残り 50 を新規 short lot として扱う。

    ``symbol_aliases`` (alias -> canonical) を渡すと、ticker rename で分断された
    約定を **同じ建玉として** 消し込む。旧 symbol で建てて新 symbol で決済した
    round-trip はこれが無いと永久に決済されず、実現損益が台帳から抜け落ちる。
    合成した trade には元の symbol を :attr:`ClosedTrade.symbol_aliases` に残す。
    """
    result = LedgerResult(fills_seen=len(fills))
    if fills:
        result.coverage_start = fills[0].transaction_time
        result.coverage_end = fills[-1].transaction_time

    books: dict[str, deque[OpenLot]] = {}

    for f in fills:
        key = canonical_symbol(f.symbol, symbol_aliases)
        book = books.setdefault(key, deque())
        remaining = f.signed_qty

        # 反対側の lot がある限り消し込む
        while remaining != 0 and book and (book[0].qty > 0) != (remaining > 0):
            lot = book[0]
            take = min(abs(lot.qty), abs(remaining))
            direction = Decimal(1) if lot.qty > 0 else Decimal(-1)
            # long: (exit - entry) * qty / short: (entry - exit) * qty
            realized = (f.price - lot.price) * take * direction
            aliases = sorted({lot.raw_symbol or key, f.symbol.upper()} - {key})
            result.closed_trades.append(
                ClosedTrade(
                    symbol=key,
                    side="long" if direction > 0 else "short",
                    qty=take,
                    entry_time=lot.opened_at,
                    entry_price=lot.price,
                    exit_time=f.transaction_time,
                    exit_price=f.price,
                    realized_pl=realized,
                    exit_order_id=f.order_id,
                    entry_order_id=lot.order_id,
                    symbol_aliases=aliases,
                )
            )
            lot.qty -= direction * take
            remaining += direction * take
            if lot.qty == 0:
                book.popleft()

        if remaining != 0:
            book.append(
                OpenLot(
                    symbol=key,
                    qty=remaining,
                    price=f.price,
                    opened_at=f.transaction_time,
                    order_id=f.order_id,
                    raw_symbol=f.symbol.upper(),
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
    symbol_aliases: Mapping[str, str] | None = None,
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
    # broker 側も同じ canonical に寄せてから突合する。片側だけ寄せると
    # rename 対が「復元にはあるが broker に無い」と永久に食い違い続ける。
    broker: dict[str, Decimal] = {}
    for k, v in broker_positions.items():
        qty = Decimal(str(v))
        if qty == 0:
            continue
        key = canonical_symbol(str(k), symbol_aliases)
        broker[key] = broker.get(key, Decimal(0)) + qty
    broker = {k: v for k, v in broker.items() if v != 0}

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


def summarize_by_exit_reason(trades: Sequence[ClosedTrade]) -> list[dict[str, Any]]:
    """exit 理由別の件数 (**全件**が母数)。理由が残っていないものは ``None`` キー。

    dashboard の履歴表は直近 N 件しか載せないので、そこで数えた理由別件数は
    *表示分の内訳* にしかならない。「全 652 本」と並べて出すと母数が違う数字が
    同じ画面に並ぶことになるため、全件基準の内訳をここで別に出す。

    なお exit 理由 (``exit_reason``) と system 帰属 (``system``) は**別の軸**。
    「理由が記録なし」と「system が unknown」は互いに独立で、二重計上ではない。
    """
    buckets: dict[str | None, list[ClosedTrade]] = {}
    for t in trades:
        buckets.setdefault(t.exit_reason, []).append(t)
    rows = [
        {
            "reason": reason,
            "n_trades": len(rows_),
            "realized_pl": round(
                float(sum((r.realized_pl for r in rows_), Decimal(0))), 2
            ),
        }
        for reason, rows_ in buckets.items()
    ]
    return sorted(rows, key=lambda r: (-r["n_trades"], str(r["reason"])))


# ---------------------------------------------------------------------------
# system 帰属 (どの system が建てた玉か)
# ---------------------------------------------------------------------------


def attribute_systems(
    trades: Sequence[ClosedTrade],
    *,
    system_by_order_id: Mapping[str, str] | None = None,
    known_order_ids: Iterable[str] | None = None,
    order_file_system_map: Mapping[str, str] | None = None,
    symbol_system_map: Mapping[str, str] | None = None,
) -> None:
    """各 round-trip に system を帰属させ、**その根拠**も一緒に残す (in-place)。

    優先順位 (根拠が強い順):

    1. ``system_by_order_id`` — *entry 注文* の ``client_order_id`` から解決した
       system。round-trip 単位で確定するので取り違えが起きない **ground truth**。
    2. ``order_file_system_map`` — ``paper_orders_*.json`` の発注記録 (symbol 単位)。
    3. ``symbol_system_map`` — ``data/symbol_system_map.json`` (symbol 単位)。

    2 と 3 は *symbol 単位* なので「同じ銘柄を別の system が別の時期に扱った」
    ケースを取り違えうる。だから付けた system だけでなく
    :attr:`ClosedTrade.system_source` に **どの根拠で付けたか** を必ず残し、
    ground truth と symbol 単位の推定を表示上も区別できるようにする。

    どれでも解決できないものは **推測で埋めない**。``system`` は ``None`` のまま、
    :attr:`ClosedTrade.system_unknown_reason` に *なぜ不明か* を入れる。

    引数
    ----
    system_by_order_id:
        order_id -> system。``client_order_id`` の解析は呼び出し側の責務
        (この module を broker SDK から独立に保つため)。
    known_order_ids:
        「注文履歴として観測できた」order_id の集合。
        *履歴に無い* (照会不能) と *履歴にはあるが system tag が無い* を
        区別するために使う。省略時は ``system_by_order_id`` の key を使う。
    """
    by_order = {str(k): str(v) for k, v in (system_by_order_id or {}).items() if v}
    known = (
        {str(x) for x in known_order_ids}
        if known_order_ids is not None
        else set(by_order)
    )
    order_file = {
        str(k).upper(): str(v) for k, v in (order_file_system_map or {}).items() if v
    }
    sym_map = {
        str(k).upper(): str(v) for k, v in (symbol_system_map or {}).items() if v
    }

    for t in trades:
        t.system = None
        t.system_source = None
        t.system_unknown_reason = None

        oid = t.entry_order_id
        if oid and oid in by_order:
            t.system = by_order[oid]
            t.system_source = SYSTEM_SOURCE_ENTRY_ORDER
            continue

        # entry 注文からは取れなかった。まず「なぜ取れなかったか」を確定させる。
        if not oid:
            unknown_reason = UNKNOWN_NO_ENTRY_ORDER_ID
        elif oid in known:
            unknown_reason = UNKNOWN_ENTRY_ORDER_UNTAGGED
        else:
            unknown_reason = UNKNOWN_ENTRY_ORDER_NOT_FOUND

        sym = t.symbol.upper()
        fallback = order_file.get(sym)
        if fallback:
            t.system = fallback
            t.system_source = SYSTEM_SOURCE_ORDER_FILE
            continue
        fallback = sym_map.get(sym)
        if fallback:
            t.system = fallback
            t.system_source = SYSTEM_SOURCE_SYMBOL_MAP
            continue

        t.system_unknown_reason = unknown_reason


def summarize_attribution(trades: Sequence[ClosedTrade]) -> dict[str, Any]:
    """system 帰属の内訳 = 「どれだけを何を根拠に付けられたか / 残りはなぜ不明か」。

    dashboard が unknown を *黙って* 一塊にしないための材料。unknown は理由別に
    件数・銘柄・実現損益まで出す (金額を伴わない「不明」は軽く見えてしまうため)。
    """
    n = len(trades)
    by_source: dict[str, int] = {}
    unknown: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        if t.system:
            by_source[t.system_source or "unspecified"] = (
                by_source.get(t.system_source or "unspecified", 0) + 1
            )
        else:
            unknown.setdefault(t.system_unknown_reason or "unspecified", []).append(t)

    n_unknown = sum(len(v) for v in unknown.values())
    n_ground_truth = by_source.get(SYSTEM_SOURCE_ENTRY_ORDER, 0)
    return {
        "n_trades": n,
        "n_attributed": n - n_unknown,
        "n_unknown": n_unknown,
        "n_ground_truth": n_ground_truth,
        "ground_truth_pct": round(n_ground_truth / n * 100.0, 1) if n else None,
        "by_source": [
            {
                "source": src,
                "label": SYSTEM_SOURCE_LABEL.get(src, src),
                "n_trades": cnt,
            }
            for src, cnt in sorted(by_source.items(), key=lambda kv: -kv[1])
        ],
        "unknown_by_reason": [
            {
                "reason": reason,
                "label": UNKNOWN_REASON_LABEL.get(reason, reason),
                "n_trades": len(rows),
                "realized_pl": round(
                    float(sum((r.realized_pl for r in rows), Decimal(0))), 2
                ),
                "symbols": sorted({r.symbol for r in rows}),
                "entry_sessions": sorted({r.entry_session for r in rows}),
            }
            for reason, rows in sorted(unknown.items(), key=lambda kv: -len(kv[1]))
        ],
    }


def pair_rename_candidates(
    discrepancies: Sequence[LotDiscrepancy],
    *,
    epsilon: Decimal = QTY_EPSILON,
) -> list[dict[str, Any]]:
    """建玉の食い違いを「旧 symbol -> 新 symbol」の ticker rename 候補に組む。

    ``reconstructed_only`` (fill 上は建玉が残るのに broker に無い) と
    ``broker_only`` / 逆符号の残玉は、ticker rename なら必ず **対** で現れ、
    残株数がちょうど打ち消し合う。その対を拾って提示する。

    これは **仮説であって断定ではない**。株数一致は状態証拠にすぎないので、
    自動で round-trip を合成したり実現損益に混ぜたりは *しない*
    (架空の損益を作らないため)。人間が判断できるよう表に出すのが目的。
    突き合わせ先が一意に定まらない場合は候補にしない (当て推量を避ける)。
    """
    residuals = [
        (d, d.reconstructed_qty - d.broker_qty)
        for d in discrepancies
        if abs(d.reconstructed_qty - d.broker_qty) > epsilon
    ]
    out: list[dict[str, Any]] = []
    for src, r in residuals:
        if r <= 0:
            continue  # 残玉が居座っている側 (= 旧 symbol) だけを起点にする
        matches = [d for d, r2 in residuals if abs(r2 + r) <= epsilon]
        if len(matches) != 1:
            continue  # 一意でなければ組まない
        dst = matches[0]
        out.append(
            {
                "from_symbol": src.symbol,
                "to_symbol": dst.symbol,
                "qty": float(r),
                "evidence": (
                    f"{src.symbol} に {float(r)} 株の残玉があり "
                    f"{dst.symbol} 側の不足と株数が一致 (rename / corporate action の疑い)"
                ),
                "confirmed": False,
            }
        )
    return sorted(out, key=lambda x: x["from_symbol"])


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
