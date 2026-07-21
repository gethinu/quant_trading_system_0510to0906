"""モーニング・オペ・ブリーフ — ホスト実行・ntfy 配信 (paper 限定・発注しない)。

これまで Cowork のスケジュールタスク (毎朝 8:00 JST) で回していたブリーフは、
サンドボックスで走るため ``C:\\Repos\\...`` のログや memory に一切届かず、
中身が空になっていた。本スクリプトは **ホスト側で走らせて全データに届かせ**、
既存の ``common.publishers.ntfy.NtfyPublisher`` (UTF-8 安全) でスマホへ push する。

設計思想 = **例外ファースト**:
    赤信号と要アクションを先頭に、緑は畳む。何も壊れておらず手作業も無い朝は
    3〜4 行の「全緑・手は不要」で終える。全部を毎朝フル表示しない (前日差分＋赤のみ)。
    長さで信頼を失わないことを最優先にする。

セクション (ネタが無ければセクションごと省略):
    1. 夜間アラート  : quant self_monitor (worst/CRIT/WARN) + mt5 端末突合 (drift) +
                       zombie 監査 (PARTIAL_SLEEVE / ZOMBIE)。
    2. 今日の要アクション: mt5 HUMAN_TASK_QUEUE の **新規** OPEN + 赤起因のアクション。
                       (定常バックログ全件は出さない = 差分のみ)
    3. PJ ステータス : quant / mt5 / tribe / 家族ボード。動きが無い PJ は出さない
                       (前日差分＋赤のみ)。polymarket は CLOSED なので出さない。
    4. 天気 1 行     : 平日=東京・国際展示場駅(有明) / 週末=千葉市 で自動切替。
    5. 提案/ニュース : ネタがある時だけ (ホスト側 deterministic では既定で省略)。

前日差分:
    生成したブリーフを ``logs/morning_brief/brief_YYYYMMDD.json`` に保存し、翌日の
    差分計算に使う (repo 本体は汚さない = logs/ 配下)。

Exit codes: 0=OK, 2=WARN あり, 3=CRIT/赤あり (ntfy 送信可否とは独立)。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import date, datetime
import json
import os
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PRIMARY_ROOT_DEFAULT = r"C:\Repos\quant_trading_system_0510to0906"
MT5_ROOT_DEFAULT = r"C:\Repos\mt5_Bundle-of-edges"
TRIBE_ROOT_DEFAULT = r"C:\Repos\swimmy-fx-tribe"

# 天気: 平日=東京・国際展示場駅周辺(江東区有明) / 週末=千葉県千葉市
#
# 【地点観測ベースへ切替 2026-07-22】以前は open-meteo(グリッド予報)を使っていたが、
# 有明はグリッドが東京湾セルに落ち海洋影響で日中最高を過小評価した(実測36℃に対し
# 33℃止まり)。地点観測ベースに切替: **気象庁(JMA)公式**を直接叩く。
#   - 天気・降水確率・**当日最高気温** = 府県天気予報(地点予報)
#       forecast/data/forecast/{pref}.json (05時発表 = 当日最高/降水確率を含む)
#   - **当日最低気温** = AMeDAS 地点観測 (朝の時点で当日最低は既に観測済 = 確定値)
#       amedas/data/point/{amedas_id}/{YYYYMMDD}_{HH}.json
# 予報(最高)と観測(最低)の役割分担で、実測に最も近い range を作る。
# pref=府県コード, area=一次細分区(天気/降水確率), temp_point=気温地点, amedas=観測点。
JMA_WEEKDAY = {
    "name": "東京・有明(国際展示場)",
    "pref": "130000",  # 東京都
    "area": "130010",  # 東京地方 (23区含む=江東区)
    "temp_point": "44132",  # 気温地点=東京
    "amedas": "44132",  # AMeDAS=東京 (北の丸公園, 陸上観測)
}
JMA_WEEKEND = {
    "name": "千葉市",
    "pref": "120000",  # 千葉県
    "area": "120010",  # 北西部 (千葉市)
    "temp_point": "45212",  # 気温地点=千葉
    "amedas": "45212",  # AMeDAS=千葉
}

# JMA telop コード -> 短い日本語 (forecast weatherCodes)。telops.json は 404 のため
# 静的に埋め込む(コードは安定)。未収載コードは先頭桁で粗くフォールバック
# (1xx=晴/2xx=くもり/3xx=雨/4xx=雪) して生 code を出さない。
_JMA_TELOP = {
    "100": "晴れ",
    "101": "晴れ時々くもり",
    "102": "晴れ一時雨",
    "103": "晴れ時々雨",
    "104": "晴れ一時雪",
    "105": "晴れ時々雪",
    "110": "晴れのち時々くもり",
    "111": "晴れのちくもり",
    "112": "晴れのち一時雨",
    "113": "晴れのち時々雨",
    "114": "晴れのち雨",
    "115": "晴れのち一時雪",
    "116": "晴れのち時々雪",
    "117": "晴れのち雪",
    "119": "晴れのち雨か雷雨",
    "123": "晴れ(山沿い雷雨)",
    "125": "晴れ午後は雷雨",
    "126": "晴れ昼頃から雨",
    "127": "晴れ夕方から雨",
    "128": "晴れ夜は雨",
    "130": "朝の内霧のち晴れ",
    "132": "晴れ朝夕くもり",
    "140": "晴れ時々雨で雷",
    "160": "晴れ一時雪か雨",
    "200": "くもり",
    "201": "くもり時々晴れ",
    "202": "くもり一時雨",
    "203": "くもり時々雨",
    "204": "くもり一時雪",
    "205": "くもり時々雪",
    "206": "くもり一時雨か雪",
    "207": "くもり時々雨か雪",
    "208": "くもり一時雨で雷",
    "209": "霧",
    "210": "くもりのち時々晴れ",
    "211": "くもりのち晴れ",
    "212": "くもりのち一時雨",
    "213": "くもりのち時々雨",
    "214": "くもりのち雨",
    "215": "くもりのち一時雪",
    "216": "くもりのち時々雪",
    "217": "くもりのち雪",
    "218": "くもりのち雨か雪",
    "219": "くもりのち雨か雷雨",
    "220": "くもり朝夕一時雨",
    "221": "くもり朝の内一時雨",
    "222": "くもり夕方一時雨",
    "223": "くもり日中時々晴れ",
    "224": "くもり昼頃から雨",
    "225": "くもり夕方から雨",
    "226": "くもり夜は雨",
    "228": "くもり昼頃から雪",
    "231": "くもり(海上海岸は霧)",
    "240": "くもり時々雨で雷",
    "300": "雨",
    "301": "雨時々晴れ",
    "302": "雨時々止む",
    "303": "雨時々雪",
    "304": "雨か雪",
    "306": "大雨",
    "308": "雨で暴風",
    "309": "雨一時雪",
    "311": "雨のち晴れ",
    "313": "雨のちくもり",
    "314": "雨のち時々雪",
    "315": "雨のち雪",
    "316": "雨か雪のち晴れ",
    "317": "雨か雪のちくもり",
    "320": "朝の内雨のち晴れ",
    "321": "朝の内雨のちくもり",
    "323": "雨昼頃から晴れ",
    "324": "雨夕方から晴れ",
    "325": "雨夜は晴れ",
    "328": "雨一時強く降る",
    "329": "雨一時みぞれ",
    "340": "雪か雨",
    "350": "雨で雷",
    "400": "雪",
    "401": "雪時々晴れ",
    "402": "雪時々止む",
    "403": "雪時々雨",
    "405": "大雪",
    "406": "風雪強い",
    "407": "暴風雪",
    "409": "雪一時雨",
    "411": "雪のち晴れ",
    "413": "雪のちくもり",
    "414": "雪のち雨",
    "425": "雪一時強く降る",
    "426": "雪のちみぞれ",
    "427": "雪一時みぞれ",
    "450": "雪で雷",
}
# 降水を示す文字(整合性ガード用)。表示テキストにこれらが無い=「乾いた空」判定。
_PRECIP_CHARS = ("雨", "雪", "雷", "みぞれ", "霧雨")


# --------------------------------------------------------------------------
# データモデル
# --------------------------------------------------------------------------
@dataclass
class ProjectStatus:
    """1 プロジェクトの当朝スナップショット。"""

    key: str
    label: str
    available: bool = False
    red: bool = False  # 赤 (要注意) があるか
    alerts: list[str] = field(default_factory=list)  # 夜間アラート行 (赤/警告)
    actions: list[str] = field(default_factory=list)  # 赤起因の即アクション
    open_ids: list[str] = field(default_factory=list)  # 差分用 (mt5 OPEN タスク)
    open_detail: dict[str, str] = field(default_factory=dict)  # id -> 1行
    status_line: str = ""  # PJ ステータス用の 1 行 (差分キー)
    note: str = ""  # 取得不可などの正直な注記

    def to_dict(self) -> dict:
        return {
            "key": self.key,
            "label": self.label,
            "available": self.available,
            "red": self.red,
            "alerts": self.alerts,
            "open_ids": self.open_ids,
            "status_line": self.status_line,
            "note": self.note,
        }


# --------------------------------------------------------------------------
# 小物
# --------------------------------------------------------------------------
def _latest_dated_json(
    results_dir: Path, prefix: str
) -> tuple[Path | None, int | None]:
    """prefix_YYYYMMDD.json のうち日付最大のものと日付(int)を返す。"""
    best: tuple[int, Path] | None = None
    if not results_dir.exists():
        return None, None
    for f in results_dir.glob(f"{prefix}_*.json"):
        digits = "".join(ch for ch in f.stem[len(prefix) :] if ch.isdigit())[:8]
        if len(digits) != 8:
            continue
        n = int(digits)
        if best is None or n > best[0]:
            best = (n, f)
    if best is None:
        return None, None
    return best[1], best[0]


def _load_json(path: Path | None) -> dict | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _truncate(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[: n - 1] + "…"


# --------------------------------------------------------------------------
# quant アダプタ — self_monitor JSON を読む
# --------------------------------------------------------------------------
def adapter_quant(primary_root: Path, today_yyyymmdd: int) -> ProjectStatus:
    st = ProjectStatus(key="quant", label="quant")
    logs = primary_root / "logs"
    sm_path, sm_date = _latest_dated_json(logs, "self_monitor")
    data = _load_json(sm_path)
    if data is None:
        st.note = "self_monitor JSON 未検出"
        st.status_line = "self_monitor 未検出"
        st.red = True
        st.alerts.append("[quant] self_monitor JSON が見つからない (監視自体が停止?)")
        return st

    st.available = True
    worst = str(data.get("worst", "?")).lower()
    checks = data.get("checks", []) or []
    total_signals = None
    for c in checks:
        if c.get("name") == "signals":
            total_signals = (c.get("data") or {}).get("total_signals")

    # self_monitor 自体の鮮度 (当日ぶんが無ければ端末ダウン等を疑う)
    stale = sm_date is not None and sm_date < today_yyyymmdd
    bad_checks = [c for c in checks if str(c.get("status")).lower() in ("warn", "crit")]

    if worst in ("warn", "crit") or bad_checks:
        st.red = worst == "crit" or any(
            str(c.get("status")).lower() == "crit" for c in bad_checks
        )
        for c in bad_checks:
            mark = str(c.get("status")).upper()
            st.alerts.append(
                f"[quant] {mark} {c.get('name')}: {_truncate(str(c.get('detail')), 90)}"
            )
    if stale:
        st.red = True
        st.alerts.append(
            f"[quant] self_monitor が当日ぶん未更新 (最新={sm_date}, 今日={today_yyyymmdd}) — 06:00 run/端末を確認"
        )

    sig_txt = f"{total_signals}sig" if total_signals is not None else "sig?"
    date_txt = str(sm_date) if sm_date else "?"
    st.status_line = f"worst={worst} · {sig_txt} · self_monitor={date_txt}"
    return st


# --------------------------------------------------------------------------
# mt5 アダプタ — 端末突合 + zombie 監査 + HUMAN_TASK_QUEUE
# --------------------------------------------------------------------------
def _run_zombie_audit(mt5_root: Path, timeout: float) -> dict:
    """audit_terminal_zombies.py を read-only(--json) で走らせ集計を返す。

    MQL5/Logs の INIT census のみ読む (端末は起こさない・.chr 不使用)。失敗しても
    ブリーフは落とさず {'ok': False, ...} を返す。
    """
    script = mt5_root / "scripts" / "audit_terminal_zombies.py"
    if not script.exists():
        return {"ok": False, "why": "script 不在"}
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    try:
        proc = subprocess.run(
            [sys.executable, str(script), "--json", "--days", "3"],
            cwd=str(mt5_root),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "why": f"run 例外 {type(exc).__name__}"}
    # auditor は --json でも JSON 配列の後ろに `[TERMINAL_ZOMBIES: ...]` の
    # deterministic 行を必ず付ける。json.loads(全文) は落ちるので raw_decode で
    # 先頭の JSON 配列だけを取り出す (末尾の marker 行は無視)。
    try:
        findings, _ = json.JSONDecoder().raw_decode(proc.stdout.lstrip())
    except Exception:
        return {"ok": False, "why": "json parse 失敗"}
    if not isinstance(findings, list):
        return {"ok": False, "why": "json 形式想定外"}

    def _c(v: str) -> list[dict]:
        return [f for f in findings if str(f.get("verdict")) == v]

    bad = _c("ZOMBIE") + _c("LEAKING") + _c("UNDECLARED")
    partial = _c("PARTIAL_SLEEVE")
    phantom = _c("PHANTOM")
    return {
        "ok": True,
        "bad": bad,
        "partial": partial,
        "phantom": phantom,
        "bad_n": len(bad),
        "partial_n": len(partial),
        "phantom_n": len(phantom),
    }


def _parse_open_tasks(queue_md: Path) -> dict[str, str]:
    """HUMAN_TASK_QUEUE.md の "## OPEN" テーブルから id -> 1行 を抽出。"""
    out: dict[str, str] = {}
    if not queue_md.exists():
        return out
    try:
        text = queue_md.read_text(encoding="utf-8")
    except Exception:
        return out
    lines = text.splitlines()
    in_open = False
    for line in lines:
        if line.startswith("## "):
            in_open = line.startswith("## OPEN")
            continue
        if not in_open:
            continue
        if not line.strip().startswith("|"):
            continue
        cells = [c.strip() for c in line.strip().strip("|").split("|")]
        if len(cells) < 2:
            continue
        tid = cells[0]
        # ヘッダ行 / 区切り行 を弾く
        if tid.lower() in ("id", "") or set(tid) <= set("-: "):
            continue
        out[tid] = _truncate(cells[1], 80)
    return out


def adapter_mt5(mt5_root: Path, zombie_timeout: float) -> ProjectStatus:
    st = ProjectStatus(key="mt5", label="mt5")
    # (1) 端末突合 (drift)
    recon = _load_json(mt5_root / "logs" / "status_terminal_reconcile_latest.json")
    drift_txt = "drift?"
    hard = soft = None
    recon_date = None
    if recon:
        st.available = True
        summ = recon.get("summary", {}) or {}
        hard = int(summ.get("hard_drift", 0) or 0)
        soft = int(summ.get("soft_drift", 0) or 0)
        recon_date = str(recon.get("generated_at", ""))[:10]
        drift_txt = f"drift hard={hard}/soft={soft}"
        if hard > 0:
            st.red = True
            st.alerts.append(f"[mt5] 🔴 hard drift {hard} leg — 端末突合レポート要確認")
        elif soft > 0:
            st.alerts.append(f"[mt5] ⚠️ soft drift {soft} leg")
    else:
        st.note = "端末突合 JSON 未検出"

    # (2) zombie / PARTIAL_SLEEVE 監査 (read-only census)
    z = _run_zombie_audit(mt5_root, zombie_timeout)
    zt = "zombie?"
    if z.get("ok"):
        st.available = True
        zt = (
            f"zombie bad={z['bad_n']}/partial={z['partial_n']}/phantom={z['phantom_n']}"
        )
        if z["bad_n"] > 0:
            st.red = True
            names = ",".join(sorted({f.get("strategy", "?") for f in z["bad"]}))
            st.alerts.append(
                f"[mt5] 🔴 ZOMBIE/UNDECLARED {z['bad_n']} ({_truncate(names, 60)}) — DETACH"
            )
        if z["partial_n"] > 0:
            st.red = True
            names = ",".join(sorted({f.get("strategy", "?") for f in z["partial"]}))
            st.alerts.append(
                f"[mt5] 🔴 PARTIAL_SLEEVE {z['partial_n']} ({_truncate(names, 60)}) — 脚欠落"
            )
        # PHANTOM (claims-live だが census 窓に INIT 無し) は quiet 脚を拾いやすく
        # 毎朝の loud alert にすると煩い。status_line の count に畳んで差分でだけ出す。
    else:
        zt = f"zombie 未取得({z.get('why', '?')})"
        if not st.note:
            st.note = zt

    # (3) HUMAN_TASK_QUEUE OPEN
    open_tasks = _parse_open_tasks(mt5_root / "docs" / "HUMAN_TASK_QUEUE.md")
    st.open_ids = list(open_tasks.keys())
    st.open_detail = open_tasks
    open_n = len(open_tasks)

    # 赤(PARTIAL_SLEEVE/ZOMBIE)の strategy に対応する OPEN タスクを即アクション化。
    # 赤が消えるまで毎朝出る = 直すべき手作業を取りこぼさない。
    red_strats: set[str] = set()
    if z.get("ok"):
        red_strats = {f.get("strategy", "") for f in (z["partial"] + z["bad"])}
    for sid in sorted(s for s in red_strats if s):
        matched = [tid for tid in open_tasks if sid in tid]
        for tid in matched:
            st.actions.append(f"[mt5] 🔴{sid} → OPEN {tid}: {open_tasks.get(tid, '')}")

    parts = []
    if recon is not None:
        parts.append(drift_txt + (f"@{recon_date}" if recon_date else ""))
    if z.get("ok"):
        parts.append(zt)
    parts.append(f"OPEN {open_n}")
    st.status_line = " · ".join(parts)
    return st


# --------------------------------------------------------------------------
# tribe アダプタ — swimmy-fx-tribe の forward Go/No-Go を1行で
# --------------------------------------------------------------------------
def _parse_forward_status(text: str) -> dict | None:
    """forward_status.txt を1行ステータス用の dict へ。

    1 行目の形式は安定 (生成元: swimmy-fx-tribe の
    ``src/lisp/school/school-validation.lisp`` の format 文字列)::

        Forward Go/No-Go: LIVE_READY=0 RUNNING=7 FAIL=0 BLOCKED_OOS=48 total=55 | ...
        updated: 07/21 08:27 JST / 23:27 UTC reason: report

    LIVE_READY が「live 昇格済/候補」数 = deploy シグナル。0 なら「deploy 無し」。
    """
    if "Forward Go/No-Go" not in text:
        return None

    def _int(key: str) -> int | None:
        m = re.search(rf"\b{key}=(\d+)", text)
        return int(m.group(1)) if m else None

    out: dict = {
        "live_ready": _int("LIVE_READY"),
        "running": _int("RUNNING"),
        "blocked_oos": _int("BLOCKED_OOS"),
        "total": _int("total"),
    }
    m = re.search(r"updated:\s*(.+?)(?:\s+reason:|$)", text, flags=re.MULTILINE)
    if m:
        out["updated"] = m.group(1).strip()
    return out


def adapter_tribe(tribe_root: Path, today: date) -> ProjectStatus:
    """swimmy-fx-tribe。forward の Go/No-Go を決定的な1行に落とす。

    ソース = ``data/reports/forward_status.txt`` (host-local, tribe の forward job が
    毎日更新)。read-only・tribe repo は一切変更しない。ライブ swimmy.db は触らない。
    """
    st = ProjectStatus(key="tribe", label="tribe")
    status_path = tribe_root / "data" / "reports" / "forward_status.txt"
    if not status_path.exists():
        st.note = "forward_status.txt 未検出 (data/reports は host-local) — forward job を確認"
        return st
    try:
        text = status_path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        st.note = "forward_status.txt 読取失敗"
        return st
    parsed = _parse_forward_status(text)
    if not parsed or parsed.get("live_ready") is None:
        st.note = "forward_status.txt 形式想定外 (Forward Go/No-Go 行なし)"
        return st

    st.available = True
    live = int(parsed["live_ready"])
    running = parsed.get("running")
    blocked = parsed.get("blocked_oos")
    total = parsed.get("total")

    # 鮮度: mtime が数日古ければ stale と正直に注記(数値の信頼度を落とす)。
    stale_days = 0
    try:
        mdate = date.fromtimestamp(status_path.stat().st_mtime)
        stale_days = (today - mdate).days
    except Exception:
        stale_days = 0

    # LIVE_READY>0 = forward が live 昇格候補を提示 → deploy 判断が要る (warn)。
    # 定常(=0)は「deploy 無し・flag OFF」でノイズ無し。
    if live > 0:
        st.alerts.append(
            f"[tribe] 🟡 LIVE_READY={live} — forward が live 昇格候補を提示 (deploy 判断)"
        )

    deploy_txt = f"live {live}" + ("(deploy無)" if live == 0 else "")
    funnel = f"forward RUN{running}/blkOOS{blocked}/total{total}"
    stale_txt = f" ⚠{stale_days}d古" if stale_days >= 2 else ""
    st.status_line = f"{deploy_txt} · {funnel}{stale_txt}"
    if stale_days >= 2:
        st.note = f"forward_status.txt が {stale_days}d 未更新 (値が最新でない可能性)"
    return st


# --------------------------------------------------------------------------
# 家族ボード アダプタ — 実体(読めるソース)がまだ無い PJ
# --------------------------------------------------------------------------
# ソースが決まったらここに読取を実装し note/status_line を埋めれば復活する。
FAMILY_BOARD_SOURCE: str | None = (
    None  # 例: 共有ボードの API URL / ローカル export path
)


def adapter_family() -> ProjectStatus:
    """家族ボード。実体が無いうちは *沈黙* する。

    毎朝「ソース無し」を出すのは純粋なノイズなので、available=False かつ note="" に
    して行ごと抑止する (gaps の正直注記にも出さない)。FAMILY_BOARD_SOURCE が設定
    されたら読取を実装する。
    """
    st = ProjectStatus(key="family", label="家族ボード")
    st.available = False
    st.note = ""  # 実体ができるまで沈黙 (毎朝の「ソース無し」ノイズを止める)
    return st


# --------------------------------------------------------------------------
# 天気 — 気象庁(JMA)公式: 府県天気予報(地点予報) + AMeDAS(地点観測)
# --------------------------------------------------------------------------
_JMA_UA = {"User-Agent": "quant-morning-brief/1.0 (host-side ops)"}


def _jma_weather_text(code: str | None) -> str | None:
    """JMA telop code -> 短い日本語。未収載は先頭桁で粗くフォールバック。"""
    if not code:
        return None
    if code in _JMA_TELOP:
        return _JMA_TELOP[code]
    coarse = {"1": "晴れ", "2": "くもり", "3": "雨", "4": "雪"}.get(str(code)[:1])
    return coarse  # None なら呼び側で扱う (生 code は出さない)


def _text_is_dry(text: str) -> bool:
    """表示テキストが「乾いた空」(雨/雪/雷を含まない) か。"""
    return not any(ch in text for ch in _PRECIP_CHARS)


def _weather_consistent(text: str | None, pop: float | None) -> bool:
    """天気テキストと降水確率が内部矛盾していないか。

    信頼を落とす最大の失敗は「晴れなのに降水確率が極端に高い」。乾いた空
    (雨/雪/雷なし)なのに pop>=70% なら矛盾とみなす(数値を伏せる判断に使う)。
    JMA 単一ソースなので通常は起きないが、ガードは安全網として維持する。
    """
    if text and _text_is_dry(text) and pop is not None and pop >= 70:
        return False
    return True


def _format_weather(
    name: str,
    code: str | None,
    tmax: float | None,
    tmin: float | None,
    pop: float | None,
) -> str | None:
    """日次値から 1 行を組む(HTTP しない純関数=テスト可能)。

    - 最高は予報・最低は観測(呼び側で解決済)。矛盾検知時は数値を伏せて正直に。
    - 最低が取れない場合は range をやめ「最高X℃」だけ出す(観測欠損を捏造しない)。
    """
    desc = _jma_weather_text(code)
    if desc is None and tmax is None:
        return None  # 天気も気温も無い = 実質取得失敗
    label = desc or "天気不明"
    if not _weather_consistent(desc, pop):
        # 誤った数字を自信満々に出すより、矛盾を正直に告げる方が信頼される。
        return f"{name}: 天気取得の整合性エラー (天気={label} と降水確率{pop}%が矛盾のため数値非表示)"
    if tmax is not None and tmin is not None:
        temp = f" {round(tmin)}〜{round(tmax)}℃"
    elif tmax is not None:
        temp = f" 最高{round(tmax)}℃"  # 最低(観測)欠損 → range を捏造しない
    elif tmin is not None:
        temp = f" 最低{round(tmin)}℃"
    else:
        temp = ""
    rain = f" 降水{round(pop)}%" if pop is not None else ""
    return f"{name}: {label}{temp}{rain}"


def _http_json(url: str, timeout: float):
    """GET -> JSON。失敗は None (黙って別値を出さない=呼び側で欠損扱い)。"""
    try:
        import requests

        r = requests.get(url, headers=_JMA_UA, timeout=timeout)
        if r.status_code != 200:
            return None
        return r.json()
    except Exception:
        return None


def _parse_jma_forecast(
    blocks: list, area: str, temp_point: str, today_iso: str
) -> dict:
    """JMA 府県予報 JSON(list)から当日の {code,text,pop,tmax,report} を抜く純関数。

    tmax = 気温地点の当日値の最大 (05時発表の temps は当日 min スロットが max と同値の
    placeholder になる既知仕様のため、当日エントリの max を最高気温として採る)。
    tmin は当日ぶんが信頼できない(観測で別途取る)ので返さない。
    """
    out: dict = {"code": None, "text": None, "pop": None, "tmax": None, "report": None}
    if not isinstance(blocks, list) or not blocks:
        return out
    b = blocks[0]
    out["report"] = b.get("reportDatetime")

    def _area(ts: dict, code: str) -> dict | None:
        for a in ts.get("areas", []):
            if a.get("area", {}).get("code") == code:
                return a
        return None

    def _today_ints(ts: dict, a: dict | None, key: str) -> list[int]:
        if not a or not a.get(key):
            return []
        tdef = ts.get("timeDefines", [])
        return [
            int(v)
            for t, v in zip(tdef, a[key])
            if str(t).startswith(today_iso) and str(v).lstrip("-").isdigit()
        ]

    ts = b.get("timeSeries", [])
    if len(ts) >= 1:  # 天気コード (先頭 = 当日)
        a = _area(ts[0], area)
        if a and a.get("weatherCodes"):
            out["code"] = str(a["weatherCodes"][0])
            out["text"] = _jma_weather_text(out["code"])
    if len(ts) >= 2:  # 降水確率 (当日ぶんの最大)
        pops = _today_ints(ts[1], _area(ts[1], area), "pops")
        if pops:
            out["pop"] = max(pops)
    if len(ts) >= 3:  # 最高気温 (当日ぶんの最大値)
        temps = _today_ints(ts[2], _area(ts[2], temp_point), "temps")
        if temps:
            out["tmax"] = max(temps)
    return out


def _jma_forecast_today(
    pref: str, area: str, temp_point: str, today_iso: str, timeout: float
) -> dict:
    """府県予報を取得して当日ぶんを parse (HTTP は _http_json、parse は純関数)。"""
    j = _http_json(
        f"https://www.jma.go.jp/bosai/forecast/data/forecast/{pref}.json", timeout
    )
    return _parse_jma_forecast(
        j if isinstance(j, list) else [], area, temp_point, today_iso
    )


def _amedas_today_min(amedas_id: str, today_compact: str, timeout: float) -> dict:
    """AMeDAS 地点観測から当日の {tmin, current} を取る(観測値=確定した最低)。

    3 時間ごとのファイル(00,03,...)を過去ぶんだけ辿る。未来ブロックは 404 で、
    200 の後に 404 が来たら以降は無いので打ち切る(無駄打ち回避)。
    """
    out: dict = {"tmin": None, "current": None}
    temps: list[float] = []
    seen200 = False
    for hh in ("00", "03", "06", "09", "12", "15", "18", "21"):
        url = (
            f"https://www.jma.go.jp/bosai/amedas/data/point/"
            f"{amedas_id}/{today_compact}_{hh}.json"
        )
        j = _http_json(url, timeout)
        if not isinstance(j, dict) or not j:
            if seen200:
                break  # 200 の後の欠落 = 未来ブロック → 打ち切り
            continue
        seen200 = True
        for k in sorted(j):
            t = j[k].get("temp")
            if isinstance(t, list) and t and t[0] is not None:
                temps.append(float(t[0]))
                out["current"] = float(t[0])
    if temps:
        out["tmin"] = min(temps)
    return out


def fetch_weather(is_weekend: bool, today: date, timeout: float = 10.0) -> str | None:
    """当日の天気 1 行を JMA 公式(予報=最高/降水/天気, 観測=最低)で組む。

    フォールバックは *黙って別ソース/古い値を出さない*:
      - 府県予報が取れない → None (呼び側で「取得失敗(JMA)」と正直に注記)
      - 最高だけ取れ最低(観測)が欠損 → range をやめ「最高X℃」だけ(捏造しない)
    値のソースは stdout ([weather] 行)に必ず残す (launch ログから追える)。
    """
    loc = JMA_WEEKEND if is_weekend else JMA_WEEKDAY
    name = loc["name"]
    today_iso = today.strftime("%Y-%m-%d")
    today_compact = today.strftime("%Y%m%d")

    fc = _jma_forecast_today(
        loc["pref"], loc["area"], loc["temp_point"], today_iso, timeout
    )
    if fc["tmax"] is None and fc["code"] is None:
        print(f"[weather] {name} src=JMA-forecast 取得失敗 (府県予報 null)")
        return None  # 予報が取れない = 別ソースに黙って乗り換えず正直に失敗

    am = _amedas_today_min(loc["amedas"], today_compact, timeout)
    tmax = fc["tmax"]
    tmin = am["tmin"]

    # ソースを必ずログ (どの値がどのソースか後から追える)
    print(
        f"[weather] {name} src=JMA report={fc['report']} "
        f"code={fc['code']}(forecast {loc['area']}) "
        f"tmax={tmax}(forecast {loc['temp_point']}) "
        f"tmin={tmin}(AMeDAS obs {loc['amedas']}) "
        f"current={am['current']} pop={fc['pop']}(forecast max)"
        + ("" if tmin is not None else " [WARN AMeDAS最低欠損→最高のみ表示]")
    )
    return _format_weather(name, fc["code"], tmax, tmin, fc["pop"])


# --------------------------------------------------------------------------
# 差分 (前日ブリーフ) の読み込み
# --------------------------------------------------------------------------
def _load_prev_brief(state_dir: Path, today: date) -> dict | None:
    """今日より前の最新 brief_YYYYMMDD.json を返す (週末/欠測を跨いでも動く)。"""
    if not state_dir.exists():
        return None
    best: tuple[str, Path] | None = None
    today_key = today.strftime("%Y%m%d")
    for f in state_dir.glob("brief_*.json"):
        digits = "".join(ch for ch in f.stem if ch.isdigit())[:8]
        if len(digits) != 8 or digits >= today_key:
            continue
        if best is None or digits > best[0]:
            best = (digits, f)
    if best is None:
        return None
    return _load_json(best[1])


# --------------------------------------------------------------------------
# ブリーフ組み立て (例外ファースト)
# --------------------------------------------------------------------------
def build_brief(
    projects: list[ProjectStatus],
    weather: str | None,
    today: date,
    prev: dict | None,
) -> tuple[str, str, str, bool, bool]:
    """(title, body, worst, has_red, has_warn) を返す。"""
    date_str = today.strftime("%m-%d (%a)")

    prev_lines = {
        p["key"]: p.get("status_line", "") for p in (prev or {}).get("projects", [])
    }
    prev_open = set()
    for p in (prev or {}).get("projects", []):
        if p.get("key") == "mt5":
            prev_open = set(p.get("open_ids", []))
    first_run = prev is None

    # --- 1. 夜間アラート (赤/警告を全 PJ から集約, 赤を先頭) ---
    alerts: list[str] = []
    for p in projects:
        alerts.extend(p.alerts)
    has_red = any(p.red for p in projects)
    has_warn = bool(alerts) and not has_red

    # --- 2. 今日の要アクション (赤起因 + 新規 OPEN タスク) ---
    actions: list[str] = []
    for p in projects:
        actions.extend(p.actions)
    mt5 = next((p for p in projects if p.key == "mt5"), None)
    new_open: list[str] = []
    if mt5 and mt5.available:
        if first_run:
            # 初回は baseline を作るだけ (全件を新規扱いして氾濫させない)
            pass
        else:
            new_open = [i for i in mt5.open_ids if i not in prev_open]
        for tid in new_open[:5]:
            actions.append(f"[mt5] 新規 OPEN: {tid} — {mt5.open_detail.get(tid, '')}")

    # --- 3. PJ ステータス (赤 or 前日から変化した PJ のみ) ---
    pj_lines: list[str] = []
    for p in projects:
        if not p.available:
            continue
        changed = p.status_line != prev_lines.get(p.key, None)
        if p.red or changed or first_run:
            flag = "🔴" if p.red else "•"
            delta = ""
            if p.key == "mt5" and new_open:
                delta = f" (+{len(new_open)}新規OPEN)"
            pj_lines.append(f"{flag} {p.label}: {p.status_line}{delta}")

    # --- worst 判定 ---
    worst = "crit" if has_red else ("warn" if has_warn else "ok")

    # --- 本文組み立て ---
    out: list[str] = []
    quiet = not alerts and not actions and not pj_lines

    if quiet:
        # 何も壊れておらず手作業も無い朝 = 3〜4 行で終える
        out.append(f"☀️ モーニング・ブリーフ {date_str} — 全緑・手は不要")
        avail = [p.label for p in projects if p.available]
        out.append(f"監視: {', '.join(avail)} すべて異常なし")
        if weather:
            out.append(f"🌤 {weather}")
    else:
        head_flag = "🔴" if has_red else ("⚠️" if has_warn else "☀️")
        out.append(f"{head_flag} モーニング・ブリーフ {date_str}")
        if alerts:
            out.append("")
            out.append("■ 夜間アラート")
            out.extend(alerts)
        if actions:
            out.append("")
            out.append("■ 今日の要アクション")
            out.extend(actions)
        if pj_lines:
            out.append("")
            out.append("■ PJ ステータス (差分・赤のみ)")
            out.extend(pj_lines)
        if weather:
            out.append("")
            out.append(f"🌤 {weather}")

    # 取得できなかったセクション/ソースを正直に注記
    gaps = [f"{p.label}: {p.note}" for p in projects if not p.available and p.note]
    if weather is None:
        gaps.append("天気: 取得失敗 (JMA 地点予報 — 別ソースは出さない)")
    if gaps:
        out.append("")
        out.append("■ 未取得 (正直な注記)")
        out.extend(f"– {g}" for g in gaps)

    body = "\n".join(out)

    # --- title (ASCII+emoji, send_text 側で sanitize される) ---
    n_alerts = len(alerts)
    n_actions = len(actions)
    if has_red:
        title = f"MorningBrief {today:%m-%d}: RED {n_alerts} alert / {n_actions} todo"
    elif has_warn:
        title = f"MorningBrief {today:%m-%d}: WARN {n_alerts} / {n_actions} todo"
    elif quiet:
        title = f"MorningBrief {today:%m-%d}: OK (all green)"
    else:
        title = f"MorningBrief {today:%m-%d}: OK / {n_actions} todo"

    return title, body, worst, has_red, has_warn


# --------------------------------------------------------------------------
# ntfy 送信
# --------------------------------------------------------------------------
def send_ntfy(title: str, body: str, urgent: bool, dry_run: bool) -> bool:
    if dry_run:
        print("--- ntfy (dry-run) ---")
        print(f"X-Title: {title}")
        print(body)
        return True
    try:
        from common.publishers.ntfy import NtfyPublisher

        pub = NtfyPublisher()
        if not pub.is_configured():
            print("[ntfy] NTFY_TOPIC 未設定のため送信スキップ")
            return False
        tags = "rotating_light,warning" if urgent else "sunrise"
        res = pub.send_text(title, body, tags=tags, priority=(5 if urgent else None))
        print(
            f"[ntfy] 送信 ok={getattr(res, 'ok', '?')} detail={getattr(res, 'detail', '?')}"
        )
        return bool(getattr(res, "ok", False))
    except Exception as exc:  # noqa: BLE001
        print(f"[ntfy] 送信失敗: {exc}")
        return False


# --------------------------------------------------------------------------
# 保存 (前日差分用)
# --------------------------------------------------------------------------
def save_state(
    state_dir: Path, today: date, projects: list[ProjectStatus], body: str
) -> None:
    state_dir.mkdir(parents=True, exist_ok=True)
    key = today.strftime("%Y%m%d")
    record = {
        "date": today.isoformat(),
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "projects": [p.to_dict() for p in projects],
    }
    (state_dir / f"brief_{key}.json").write_text(
        json.dumps(record, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (state_dir / f"brief_{key}.txt").write_text(body, encoding="utf-8")


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--primary-root", default=os.getenv("QTS_REPO_ROOT", PRIMARY_ROOT_DEFAULT)
    )
    parser.add_argument(
        "--mt5-root", default=os.getenv("MT5_REPO_ROOT", MT5_ROOT_DEFAULT)
    )
    parser.add_argument("--tribe-root", default=TRIBE_ROOT_DEFAULT)
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (既定: today local)"
    )
    parser.add_argument("--zombie-timeout", type=float, default=90.0)
    parser.add_argument("--no-weather", action="store_true")
    parser.add_argument(
        "--dry-run", action="store_true", help="ntfy を送らず本文を表示"
    )
    parser.add_argument("--no-notify", action="store_true", help="ntfy 送信を無効化")
    parser.add_argument("--state-dir", default=None)
    args = parser.parse_args(argv)

    if args.date:
        today = datetime.strptime(args.date, "%Y-%m-%d").date()
    else:
        today = date.today()
    today_yyyymmdd = int(today.strftime("%Y%m%d"))
    is_weekend = today.weekday() >= 5

    primary = Path(args.primary_root)
    mt5_root = Path(args.mt5_root)
    tribe_root = Path(args.tribe_root)
    state_dir = (
        Path(args.state_dir) if args.state_dir else primary / "logs" / "morning_brief"
    )

    # --- 各アダプタ (どれが失敗してもブリーフは落とさない) ---
    projects: list[ProjectStatus] = []
    for fn in (
        lambda: adapter_quant(primary, today_yyyymmdd),
        lambda: adapter_mt5(mt5_root, args.zombie_timeout),
        lambda: adapter_tribe(tribe_root, today),
        adapter_family,
    ):
        try:
            projects.append(fn())
        except Exception as exc:  # noqa: BLE001
            print(f"[warn] adapter 失敗: {exc}")

    weather = None if args.no_weather else fetch_weather(is_weekend, today)

    prev = _load_prev_brief(state_dir, today)
    title, body, worst, has_red, has_warn = build_brief(projects, weather, today, prev)

    print("=" * 60)
    print(f"X-Title: {title}")
    print("-" * 60)
    print(body)
    print("=" * 60)

    # 保存は送信の前に (送信失敗しても差分 baseline は残す)
    try:
        save_state(state_dir, today, projects, body)
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] state 保存失敗: {exc}")

    if not args.no_notify:
        send_ntfy(title, body, urgent=(has_red or has_warn), dry_run=args.dry_run)
    else:
        print("[ntfy] --no-notify のため送信スキップ")

    return 3 if has_red else (2 if has_warn else 0)


if __name__ == "__main__":
    raise SystemExit(main())
