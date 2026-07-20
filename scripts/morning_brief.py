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
# 座標について: 国際展示場駅(実測 139.7939)は open-meteo/JMA グリッドでは東京湾側
# のセル(丸め先 139.8125)に落ち、海洋影響で日中最高気温を ~3℃ 過小評価する
# (実測 36℃ の日に湾側セルは 30.3℃、江東区の陸側セル=139.75 は 33.0℃ と、陸側が
# 実測にも Yahoo/tenki 等の陸上予報にも近い)。ラベルは「有明」のまま、代表点だけ
# 江東区の陸側グリッドへわずかに内陸へ寄せる (経度 139.775 → 139.75 セル)。
WEEKDAY_LOC = ("東京・有明(国際展示場)", 35.635, 139.775)
WEEKEND_LOC = ("千葉市", 35.6073, 140.1063)

# open-meteo の既定 (best_match) は JMA の気温/天気コードに *別モデルの* 降水確率を
# 継ぎ接ぎするため、「晴れ(code 1)なのに降水100%」という自己矛盾を吐く(実際に
# 2026-07-21 の配信で発生)。日本域は JMA モデルに固定し、天気コード・気温・降水を
# 1 つの整合したモデルから取る。JMA は降水確率(%)を持たず precipitation_sum(mm)を
# 返すので、雨は mm ベースで表示する。JMA が欠測なら降水確率を持つ単一モデル
# (gfs_seamless)へフォールバック(それでもモデルを跨いで継ぎ接ぎはしない)。
_WEATHER_MODELS = ("jma_seamless", "gfs_seamless")
# 「晴れ/快晴/晴れ時々曇」= WMO 0..2。ここに強い降水シグナルが同居したら矛盾。
_CLEAR_CODES = frozenset({0, 1, 2})

# WMO weather_code -> 短い日本語 (open-meteo daily.weather_code)
_WMO = {
    0: "快晴",
    1: "晴れ",
    2: "晴れ時々曇",
    3: "曇り",
    45: "霧",
    48: "着氷性の霧",
    51: "霧雨(弱)",
    53: "霧雨",
    55: "霧雨(強)",
    56: "着氷性霧雨",
    57: "着氷性霧雨(強)",
    61: "雨(弱)",
    63: "雨",
    65: "雨(強)",
    66: "着氷性の雨",
    67: "着氷性の雨(強)",
    71: "雪(弱)",
    73: "雪",
    75: "雪(強)",
    77: "細氷",
    80: "にわか雨(弱)",
    81: "にわか雨",
    82: "にわか雨(激)",
    85: "にわか雪",
    86: "にわか雪(強)",
    95: "雷雨",
    96: "雷雨(雹)",
    99: "雷雨(激・雹)",
}


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
# 天気 — open-meteo (無料・キー不要)
# --------------------------------------------------------------------------
def _weather_consistent(code: int, pop: float | None, psum: float | None) -> bool:
    """天気コードと降水シグナルが内部矛盾していないか。

    信頼を落とす最大の失敗は「晴れなのに降水100%」。天気コードが晴れ系(0..2)
    なのに降水確率が高い/降水量が多い場合は矛盾とみなす(数値を出さない判断に使う)。
    """
    if code in _CLEAR_CODES:
        if pop is not None and pop >= 70:
            return False
        if psum is not None and psum >= 5.0:
            return False
    return True


def _format_weather(
    name: str,
    code: int | None,
    tmax: float | None,
    tmin: float | None,
    pop: float | None,
    psum: float | None,
) -> str | None:
    """生の日次値から 1 行を組む(HTTP しない純関数=テスト可能)。

    矛盾を検知したら数値を伏せて「整合性エラー」を正直に返す。晴れ/雨いずれの
    表示も同一モデル由来なので、best_match のような継ぎ接ぎ矛盾は構造的に起きない。
    """
    if code is None:
        return None
    desc = _WMO.get(int(code), f"code{code}")
    if not _weather_consistent(int(code), pop, psum):
        # 誤った数字を自信満々に出すより、矛盾を正直に告げる方が信頼される。
        return (
            f"{name}: 天気取得の整合性エラー (天気={desc} と降水が矛盾のため数値非表示)"
        )
    temp = ""
    if tmax is not None and tmin is not None:
        temp = f" {round(tmin)}〜{round(tmax)}℃"
    # 降水: pop(%) があればそれ、無ければ JMA の precipitation_sum(mm)。
    if pop is not None:
        rain = f" 降水{round(pop)}%"
    elif psum is not None:
        rain = f" 降水{round(psum)}mm" if psum > 0 else " 降水なし"
    else:
        rain = ""
    return f"{name}: {desc}{temp}{rain}"


def fetch_weather(is_weekend: bool, timeout: float = 10.0) -> str | None:
    name, lat, lon = WEEKEND_LOC if is_weekend else WEEKDAY_LOC
    try:
        import requests
    except Exception:
        return None
    # 単一の整合モデルを順に試す(継ぎ接ぎしない)。最初に気温が取れたモデルを採用。
    for model in _WEATHER_MODELS:
        url = (
            "https://api.open-meteo.com/v1/forecast"
            f"?latitude={lat}&longitude={lon}"
            "&daily=weather_code,temperature_2m_max,temperature_2m_min,"
            "precipitation_probability_max,precipitation_sum"
            f"&timezone=Asia%2FTokyo&forecast_days=1&models={model}"
        )
        try:
            r = requests.get(url, timeout=timeout)
            if r.status_code != 200:
                continue
            d = r.json().get("daily", {})

            def _first(key: str):
                v = d.get(key)
                return v[0] if isinstance(v, list) and v else None

            code = _first("weather_code")
            tmax = _first("temperature_2m_max")
            tmin = _first("temperature_2m_min")
            pop = _first("precipitation_probability_max")
            psum = _first("precipitation_sum")
            if tmax is None or tmin is None:
                continue  # このモデルは欠測 → 次の単一モデルへ
            return _format_weather(name, code, tmax, tmin, pop, psum)
        except Exception:
            continue
    return None


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
        gaps.append("天気: 取得失敗 (open-meteo)")
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

    weather = None if args.no_weather else fetch_weather(is_weekend)

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
