"""recon JSON から execution summary の通知 title/body を組み立てる (pure)。

submit 後 (Step5b/5c 完了後) に「実際に何件発注されたか」を 1 通で伝える整列表を
生成する。従来の ntfy 本文 (signal 予告: narrator 2 段落 + system別 signal 行) とは
別レイヤで、signals→生成→entry→fill→exit を system×side で並べ、drop 内訳を出す。

body は日本語 OK (ntfy body は UTF-8 表示可)。title は ASCII+emoji のみ
(iPhone X-Title が非 ASCII を strip するため、送信側 send_text でも sanitize される)。
"""

from __future__ import annotations

from typing import Any

# 単一サイド運用の system → サイド記号 (表示用)。両サイド持つ system は無い想定だが
# recon は side 別に持つので、signals/generated があるサイドを実際には出力する。
_SIDE_MARK = {"long": "L", "short": "S"}


def _n(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _mmdd(date_str: str) -> str:
    s = str(date_str or "")
    if len(s) >= 10 and s[4] == "-":
        return s[5:10]
    return s


def build_title(recon: dict[str, Any]) -> str:
    """ASCII+emoji の X-Title。例: '📊 07-08 exec sig49 entry27 exit14'。"""
    p = recon.get("portfolio", {}) or {}
    mmdd = _mmdd(recon.get("date", ""))
    sig = _n(p.get("signals"))
    entry = _n(p.get("entry_submitted"))
    exit_ = _n(p.get("exit_submitted"))
    fail = _n(p.get("entry_failed"))
    emoji = "⚠️" if fail > 0 else "📊"
    head = f"{emoji} {mmdd} exec" if mmdd else f"{emoji} exec"
    return f"{head} sig{sig} entry{entry} exit{exit_}"


def build_body(recon: dict[str, Any]) -> str:
    """整列された execution summary 本文 (UTF-8, 日本語可)。"""
    p = recon.get("portfolio", {}) or {}
    lines: list[str] = []

    tgt = p.get("universe_target")
    tgt_s = str(_n(tgt)) if tgt is not None else "?"
    sig = _n(p.get("signals"))
    gen = _n(p.get("orders_generated"))
    entry = _n(p.get("entry_submitted"))
    fill = _n(p.get("entry_filled"))
    exit_sub = _n(p.get("exit_submitted"))
    exit_close = _n(p.get("exit_close"))
    exit_protect = _n(p.get("exit_protect"))

    # 1 行目: 全体 funnel
    lines.append(
        f"Tgt {tgt_s} → sig {sig} → gen {gen} → entry {entry} → fill {fill}"
    )
    lines.append(
        f"exit {exit_sub} (close {exit_close} / protect {exit_protect})"
    )

    # long/short 充足 + 残高
    le = _n(p.get("long_entry_submitted"))
    se = _n(p.get("short_entry_submitted"))
    equity = p.get("account_equity")
    equity_s = f"  資産 ${float(equity):,.0f}" if equity is not None else ""
    lines.append(f"LONG entry {le} / SHORT entry {se}{equity_s}")

    # system 別
    systems = recon.get("systems", {}) or {}
    if systems:
        lines.append("─ system別 sig→entry fill/ex ─")
        for name in sorted(
            systems.keys(),
            key=lambda x: int(x[6:]) if x[6:].isdigit() else 99,
        ):
            data = systems[name]
            num = name[6:] if name.startswith("system") else name
            for side in ("long", "short"):
                sb = data.get(side, {}) or {}
                s_sig = _n(sb.get("signals"))
                s_gen = _n(sb.get("generated"))
                if s_sig == 0 and s_gen == 0:
                    continue
                s_entry = _n(sb.get("entry_submitted"))
                s_fill = _n(sb.get("filled"))
                s_skip = _n(sb.get("skipped"))
                s_fail = _n(sb.get("failed"))
                ex = _n((data.get("exit", {}) or {}).get("submitted"))
                mark = _SIDE_MARK.get(side, "?")
                tail = f" fill{s_fill}" if s_fill else ""
                tail += f" ex{ex}" if ex else ""
                if s_skip or s_fail:
                    drops = []
                    if s_skip:
                        drops.append(f"skip{s_skip}")
                    if s_fail:
                        drops.append(f"fail{s_fail}")
                    tail += f" ({','.join(drops)})"
                lines.append(f"s{num}{mark} {s_sig}→{s_entry}{tail}")

    # drop 内訳
    drops = p.get("drop_breakdown", {}) or {}
    if drops:
        parts = " · ".join(f"{k} {v}" for k, v in sorted(drops.items()))
        lines.append(f"⚠ drop: {parts}")

    # 入力欠損の注記 (dry-run 等で paper_orders/exit_orders が無い場合)
    inputs = recon.get("inputs", {}) or {}
    missing = [k for k in ("signals", "paper_orders", "exit_orders") if not inputs.get(k)]
    if missing:
        lines.append(f"※ 入力欠損: {', '.join(missing)} (部分 recon)")

    return "\n".join(lines)


def format_execution_summary(recon: dict[str, Any]) -> tuple[str, str]:
    """(title, body) を返す。"""
    return build_title(recon), build_body(recon)
