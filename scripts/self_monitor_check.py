"""自己監視アラート — daily / open-run / publish の silent-failure を日次で検知し ntfy 1 通に集約。

過去に踏んだ「0 シグナル」「ダッシュボード日付固着 (07-07)」の *silent* 失敗を、毎朝 1 回
まとめて検査する dead-man's-switch。各チェックの合否を **1 通のサマリ ntfy** にして送る
(NtfyPublisher = UTF-8-safe。素の str POST の latin-1 死を回避)。異常があれば urgent(5)。

検査項目 (source of truth は primary repo。C:\\tmp\\qts-main-run の logs/results_csv/data_cache
は primary への junction なので --repo-root だけ見れば良い):

    1. [daily]     06:00 デイリー (main 追従) が走ったか。
                   today_signals_YYYYMMDD.json / pipeline_YYYYMMDD.json の当日更新有無・mtime。
                   最新ファイルが古い (mtime age > --max-age-hours) → CRIT。
    1b.[pipeline]  daily_pipeline_*.log を解析し cache step が exit=0 で完走したか +
                   pipeline が『完了』まで到達したか (途中 stall = silent hang を検出)。
                   --auto-latest cache の本番実証に使う (走行 worktree 側の log を見る)。
    1c.[data_fresh] full_backup 参照銘柄 (SPY) の最新日を NYSE 最新取引日と突合。
                   full_backup は日次 fetch の着地点なので、その絶対 staleness を見れば
                   freshness_guard の盲点 (rolling/full_backup 同時凍結を fresh と誤判定)
                   も含め cache 凍結を検出できる (>4 営業日遅れ → CRIT)。rolling の前進は
                   universe scoped な freshness_guard の担当 (下記 check_data_advance 参照)。
    2. [signals]   シグナルが潤沢か。portfolio.total_signals が 0 → CRIT、
                   閾値 (--min-signals) 未満 → WARN (データ鮮度異常の疑い)。
    3. [publish]   Vercel publish が成功したか。monitor-webapp ブランチに当日 commit があるか
                   (git log)。古ければ dashboard 固着の疑い → CRIT。
    4. [open_run]  オープン自動発注 run が走り entry が fill したか。
                   最新 logs/open_run_<date>/completion_recon.json + paper_orders_*.json。
                   abort(market_closed) は良性、それ以外の abort / entry 0 は WARN。

Exit codes: 0=全 OK, 2=WARN あり, 3=CRIT あり。
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import subprocess
import sys
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

PRIMARY_ROOT_DEFAULT = r"C:\Repos\quant_trading_system_0510to0906"

# status の重大度順 (worst を集約するのに使う)
_SEVERITY = {"ok": 0, "info": 0, "skip": 1, "warn": 2, "crit": 3}
_MARK = {"ok": "OK", "info": "..", "skip": "--", "warn": "WARN", "crit": "CRIT"}


@dataclass
class CheckResult:
    name: str
    status: str  # ok / info / skip / warn / crit
    detail: str
    data: dict[str, Any] = field(default_factory=dict)

    def line(self) -> str:
        return f"[{_MARK.get(self.status, '??')}] {self.name}: {self.detail}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "status": self.status,
            "detail": self.detail,
            "data": self.data,
        }


# --------------------------------------------------------------------------
# small utils
# --------------------------------------------------------------------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _mtime_age_hours(path: Path) -> float | None:
    try:
        mt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (_now() - mt).total_seconds() / 3600.0
    except Exception:
        return None


def _latest_dated_json(
    results_dir: Path, prefix: str
) -> tuple[Path | None, int | None]:
    """prefix_YYYYMMDD.json のうち日付が最大のものと、その日付 (int) を返す。"""
    best: tuple[int, Path] | None = None
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


def _load_json(path: Path | None) -> dict[str, Any] | None:
    if path is None or not path.exists():
        return None
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


# --------------------------------------------------------------------------
# checks
# --------------------------------------------------------------------------
def check_daily(results_dir: Path, max_age_hours: float) -> CheckResult:
    """today_signals / pipeline の最新ファイルの鮮度で 06:00 デイリーの実行を判定。"""
    sig_path, sig_date = _latest_dated_json(results_dir, "today_signals")
    pipe_path, pipe_date = _latest_dated_json(results_dir, "pipeline")
    if sig_path is None:
        return CheckResult(
            "daily", "crit", "today_signals_*.json が 1 つも無い (デイリー未実行の疑い)"
        )
    age = _mtime_age_hours(sig_path)
    data = {
        "today_signals": sig_path.name,
        "today_signals_date": sig_date,
        "mtime_age_hours": round(age, 1) if age is not None else None,
        "pipeline": pipe_path.name if pipe_path else None,
        "pipeline_date": pipe_date,
    }
    if age is None:
        return CheckResult(
            "daily", "warn", f"{sig_path.name} の mtime を取得できない", data
        )
    if age > max_age_hours:
        return CheckResult(
            "daily",
            "crit",
            f"最新 {sig_path.name} が {age:.1f}h 前 (> {max_age_hours:.0f}h): "
            "06:00 デイリーが走っていない疑い",
            data,
        )
    return CheckResult(
        "daily",
        "ok",
        f"{sig_path.name} を {age:.1f}h 前に生成 (date={sig_date})",
        data,
    )


def check_signals(results_dir: Path, min_signals: int) -> CheckResult:
    """最新 today_signals の総シグナル数で潤沢さを判定 (0=CRIT / 薄=WARN)。"""
    sig_path, sig_date = _latest_dated_json(results_dir, "today_signals")
    sig = _load_json(sig_path)
    if sig is None:
        return CheckResult(
            "signals", "crit", "today_signals JSON を読めない (0 signals 事故の疑い)"
        )
    portfolio = sig.get("portfolio", {}) or {}
    total = portfolio.get("total_signals")
    if total is None:
        # 明示フィールドが無ければ systems から数える
        total = 0
        for blk in (sig.get("systems") or {}).values():
            if isinstance(blk, dict):
                total += len(blk.get("signals") or [])
    data = {
        "date": sig.get("date"),
        "total_signals": total,
        "universe_target": portfolio.get("universe_target"),
        "min_signals": min_signals,
    }
    if total <= 0:
        return CheckResult(
            "signals",
            "crit",
            f"total_signals={total} (0 シグナル: データ鮮度異常)",
            data,
        )
    if total < min_signals:
        return CheckResult(
            "signals",
            "warn",
            f"total_signals={total} < 閾値{min_signals}: 薄い (データ鮮度異常の疑い)",
            data,
        )
    return CheckResult(
        "signals", "ok", f"total_signals={total} (>= {min_signals})", data
    )


def check_publish(
    repo_root: Path, branch: str, max_age_hours: float, data_dir: Path
) -> CheckResult:
    """monitor-webapp ブランチの最新 commit 時刻で Vercel publish の当日実行を判定。"""
    committed_iso: str | None = None
    subject: str | None = None
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo_root), "log", "-1", "--format=%cI\x1f%s", branch],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        if proc.returncode == 0 and proc.stdout.strip():
            committed_iso, _, subject = proc.stdout.strip().partition("\x1f")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "publish", "warn", f"git log 取得失敗: {exc}", {"branch": branch}
        )

    # 副: dashboard data dir の最新ファイル日付も参考に
    dash_path, dash_date = _latest_dated_json(data_dir, "today_signals")
    data = {
        "branch": branch,
        "last_commit_iso": committed_iso,
        "last_commit_subject": subject,
        "dashboard_data_date": dash_date,
    }
    if not committed_iso:
        return CheckResult(
            "publish", "warn", f"{branch} の commit を取得できない", data
        )
    try:
        ct = datetime.fromisoformat(committed_iso)
        age = (datetime.now(tz=ct.tzinfo) - ct).total_seconds() / 3600.0
        data["last_commit_age_hours"] = round(age, 1)
    except Exception:
        return CheckResult(
            "publish", "warn", f"commit 時刻を解釈できない: {committed_iso}", data
        )
    if age > max_age_hours:
        return CheckResult(
            "publish",
            "crit",
            f"{branch} 最新 commit が {age:.1f}h 前 (> {max_age_hours:.0f}h): "
            "Vercel publish 停止/ダッシュ固着の疑い",
            data,
        )
    return CheckResult(
        "publish", "ok", f"{branch} を {age:.1f}h 前に更新 ('{subject}')", data
    )


def check_open_run(
    logs_dir: Path, results_dir: Path, max_age_hours: float
) -> CheckResult:
    """最新 open_run_<date> の completion_recon で自動発注 run の実行/約定を判定。"""
    dirs = sorted(logs_dir.glob("open_run_*"), reverse=True)
    dirs = [d for d in dirs if d.is_dir()]
    if not dirs:
        return CheckResult("open_run", "info", "open_run_* ディレクトリがまだ無い")
    newest = dirs[0]
    recon = _load_json(newest / "completion_recon.json") or {}
    done = (newest / "DONE.lock").exists()
    age = _mtime_age_hours(newest / "completion_recon.json") or _mtime_age_hours(newest)
    run_date = recon.get("date") or newest.name.replace("open_run_", "")
    abort = recon.get("abort")
    mode = recon.get("mode")
    submitted = recon.get("entry_submitted")
    data = {
        "dir": newest.name,
        "run_date": run_date,
        "mode": mode,
        "abort": abort,
        "done_lock": done,
        "entry_submitted": submitted,
        "entry_status": recon.get("entry_status"),
        "final_positions": recon.get("final_positions"),
        "age_hours": round(age, 1) if age is not None else None,
    }

    # 良性 abort (市場休場) は OK 扱い
    if abort == "market_closed":
        return CheckResult(
            "open_run", "ok", f"{run_date}: market closed で正常 skip", data
        )
    if abort == "drawdown_flatten":
        return CheckResult(
            "open_run",
            "warn",
            f"{run_date}: drawdown breaker 発火で flatten/中止",
            data,
        )
    if abort:
        return CheckResult("open_run", "warn", f"{run_date}: ABORT ({abort})", data)

    # abort 無し = entry まで到達したはず
    if age is not None and age > max_age_hours:
        return CheckResult(
            "open_run",
            "warn",
            f"最新 open_run が {age:.1f}h 前 (> {max_age_hours:.0f}h): ランナー停止の疑い",
            data,
        )
    if mode == "dry_run":
        return CheckResult("open_run", "info", f"{run_date}: dry_run (発注なし)", data)
    try:
        n_sub = int(submitted or 0)
    except (TypeError, ValueError):
        n_sub = 0
    if n_sub <= 0:
        return CheckResult(
            "open_run",
            "warn",
            f"{run_date}: 実 run だが entry_submitted={submitted}",
            data,
        )
    return CheckResult(
        "open_run",
        "ok",
        f"{run_date}: entry_submitted={n_sub} status={recon.get('entry_status')} done={done}",
        data,
    )


def check_pipeline_run(daily_log_dir: Path, max_age_hours: float) -> CheckResult:
    """最新 daily_pipeline_*.log を解析し、cache step の exit と pipeline 完走を判定。

    07-19 の --auto-latest 初本番実証で必要な (a) cache step が exit=0 で完走したか +
    pipeline が『完了』まで到達したか (途中 stall=07-18 の silent hang を検出) を見る。
    daily_pipeline のログは走行 worktree (既定 C:\\tmp\\qts-daily-main\\logs) 側に出る。
    """
    if not daily_log_dir.exists():
        return CheckResult("pipeline", "skip", f"daily log dir 不在: {daily_log_dir}")
    logs = [p for p in daily_log_dir.glob("daily_pipeline_*.log")]
    if not logs:
        return CheckResult(
            "pipeline", "crit", "daily_pipeline_*.log が無い (デイリー未実行の疑い)"
        )
    newest = max(logs, key=lambda p: p.stat().st_mtime)
    age = _mtime_age_hours(newest)
    try:
        text = newest.read_text(encoding="utf-8-sig", errors="replace")
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "pipeline", "warn", f"log 読取失敗: {exc}", {"log": newest.name}
        )
    m_cache = re.search(r"\[cache\]\s*終了\s*\(exit=(-?\d+)\)", text)
    cache_exit = int(m_cache.group(1)) if m_cache else None
    completed = "Daily Pipeline 完了" in text
    steps = re.findall(r"-----\s*\[([^\]]+)\]\s*(?:開始|終了)", text)
    last_step = steps[-1] if steps else None
    data = {
        "log": newest.name,
        "age_hours": round(age, 1) if age is not None else None,
        "cache_exit": cache_exit,
        "completed": completed,
        "last_step": last_step,
    }
    if age is not None and age > max_age_hours:
        return CheckResult(
            "pipeline",
            "crit",
            f"🔴 最新 daily_pipeline log が {age:.1f}h 前 (> {max_age_hours:.0f}h): "
            "06:00 デイリーが走っていない疑い",
            data,
        )
    if not completed:
        return CheckResult(
            "pipeline",
            "crit",
            f"🔴 {newest.name} が『完了』未到達 (stall at [{last_step}]) "
            "= silent hang の疑い",
            data,
        )
    if cache_exit is None:
        return CheckResult(
            "pipeline", "warn", "pipeline 完了だが cache exit を検出できず", data
        )
    if cache_exit != 0:
        return CheckResult(
            "pipeline",
            "warn",
            f"pipeline 完了だが cache exit={cache_exit} "
            "(--auto-latest 未完走 / 旧コード or fetch 失敗の疑い)",
            data,
        )
    return CheckResult(
        "pipeline",
        "ok",
        "🟢 --auto-latest cache 完走 (exit=0) + pipeline 完了",
        data,
    )


def _csv_last_date(path: Path) -> str | None:
    """CSV の最終行 (= 最新日) から YYYY-MM-DD を末尾読みで安価に取得。

    full_backup は Date が第1列、rolling は index,Date,... と列順が異なるため、
    列位置ではなく日付パターンで拾う (どちらの schema でも正しく取れる)。
    """
    try:
        with path.open("rb") as fh:
            fh.seek(0, 2)
            size = fh.tell()
            fh.seek(max(0, size - 4096))
            tail = fh.read().decode("utf-8", errors="replace").strip().splitlines()
        if not tail:
            return None
        m = re.search(r"\d{4}-\d{2}-\d{2}", tail[-1])
        return m.group(0) if m else tail[-1].split(",")[0].strip()
    except Exception:
        return None


def check_data_advance(data_cache_dir: Path, ref: str = "SPY") -> CheckResult:
    """full_backup 参照銘柄 (SPY) の最新日を NYSE 最新取引日と突合 (frozen cache 検出)。

    ``full_backup`` は日次 fetch の着地点で、SPY を含む全銘柄が必ずここへ更新される。
    その絶対 staleness (full_backup vs 実市場 = NYSE 最新取引日) を見れば、freshness_guard
    の盲点 (rolling/full_backup 同時凍結を『fresh』と誤判定) も含めて cache 停滞を検出できる
    ── full_backup が凍結すれば必ずここで拾える。

    rolling の前進 (rolling vs full_backup) は universe scoped な freshness_guard
    (check_rolling_freshness.py) の担当。ここでは **敢えて rolling を読まない**:
    SPY は non-universe ETF で rolling へは再構築されず毎日 stale drift するため、
    rolling/SPY.csv を参照すると恒常的な誤 WARN 表示になっていた (2026-07-19 の是正)。
    full_backup 基準なら SPY を含む非 universe 参照銘柄でも正しく前進を確認できる。
    """
    fb = data_cache_dir / "full_backup" / f"{ref}.csv"
    fb_date = _csv_last_date(fb)
    data: dict[str, Any] = {"ref": ref, "full_backup_last": fb_date}
    if fb_date is None:
        return CheckResult(
            "data_fresh", "skip", f"full_backup/{ref}.csv を読めない", data
        )
    try:
        import pandas as pd

        from common.utils_spy import get_latest_nyse_trading_day

        now = pd.Timestamp.now(tz="America/New_York").tz_localize(None).normalize()
        latest_nyse = pd.Timestamp(get_latest_nyse_trading_day(now)).normalize()
        fb_ts = pd.Timestamp(fb_date).normalize()
        lag = max(0, int(pd.bdate_range(fb_ts, latest_nyse).size) - 1)
        data["latest_nyse"] = str(latest_nyse.date())
        data["lag_business_days"] = lag
    except Exception as exc:  # noqa: BLE001
        return CheckResult(
            "data_fresh",
            "info",
            f"full_backup {ref}={fb_date} (NYSE 突合不可: {exc})",
            data,
        )
    tail = f"full_backup {ref}={fb_date} / 市場最新={latest_nyse.date()}"
    if lag <= 1:
        return CheckResult(
            "data_fresh", "ok", f"データ前進 OK ({tail}, lag {lag} 営業日)", data
        )
    if lag <= 4:
        return CheckResult(
            "data_fresh",
            "warn",
            f"full_backup が市場より {lag} 営業日遅れ (cache 停滞疑い) — {tail}",
            data,
        )
    return CheckResult(
        "data_fresh",
        "crit",
        f"🔴 full_backup が市場より {lag} 営業日遅れ (cache 凍結) — {tail}",
        data,
    )


# --------------------------------------------------------------------------
# aggregate + notify
# --------------------------------------------------------------------------
def _aggregate(results: list[CheckResult]) -> str:
    worst = "ok"
    for r in results:
        if _SEVERITY.get(r.status, 0) > _SEVERITY.get(worst, 0):
            worst = r.status
    return worst


def _notify(
    date_str: str, results: list[CheckResult], worst: str, dry_run: bool
) -> bool:
    n_bad = sum(1 for r in results if r.status in ("warn", "crit"))
    n_ok = sum(1 for r in results if r.status in ("ok", "info"))
    if worst == "crit":
        head = f"SelfMonitor {date_str}: CRIT ({n_bad} issue)"
    elif worst == "warn":
        head = f"SelfMonitor {date_str}: WARN ({n_bad} issue)"
    else:
        head = f"SelfMonitor {date_str}: OK ({n_ok}/{len(results)})"
    body = head + "\n" + "\n".join(r.line() for r in results)
    urgent = worst in ("warn", "crit")

    if dry_run:
        print("--- ntfy (dry-run) ---")
        print(body)
        return True
    try:
        from common.publishers.ntfy import NtfyPublisher

        pub = NtfyPublisher()
        if not pub.is_configured():
            print("[ntfy] NTFY_TOPIC 未設定のため送信スキップ")
            return False
        tags = "rotating_light,warning" if urgent else "white_check_mark"
        res = pub.send_text(head, body, tags=tags, priority=(5 if urgent else None))
        print(f"[ntfy] 送信 ok={getattr(res, 'ok', '?')}")
        return bool(getattr(res, "ok", False))
    except Exception as exc:  # noqa: BLE001
        print(f"[ntfy] 送信失敗: {exc}")
        return False


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (表示用。default: today local)"
    )
    parser.add_argument(
        "--repo-root",
        default=os.getenv("QTS_REPO_ROOT", PRIMARY_ROOT_DEFAULT),
        help="results_csv/logs を持つ primary repo (junction 元)",
    )
    parser.add_argument(
        "--min-signals",
        type=int,
        default=10,
        help="これ未満なら薄シグナル WARN (default 10)",
    )
    parser.add_argument(
        "--max-age-hours",
        type=float,
        default=26.0,
        help="daily / publish の鮮度上限 h (これ超で CRIT。default 26)",
    )
    parser.add_argument(
        "--openrun-max-age-hours",
        type=float,
        default=96.0,
        help="open_run の鮮度上限 h (週末を跨ぐので既定 96)",
    )
    parser.add_argument(
        "--monitor-branch",
        default="claude/monitor-webapp",
        help="Vercel publish 先ブランチ",
    )
    parser.add_argument(
        "--daily-log-dir",
        default=os.getenv("QTS_DAILY_LOG_DIR", r"C:\tmp\qts-daily-main\logs"),
        help="daily_pipeline_*.log の出力先 (走行 worktree 側。cache exit 判定に使う)",
    )
    parser.add_argument("--output-json", default=None, help="判定サマリ JSON の出力先")
    parser.add_argument(
        "--dry-run", action="store_true", help="ntfy を送らず本文を表示"
    )
    parser.add_argument(
        "--no-notify", action="store_true", help="ntfy 送信を完全に無効化"
    )
    args = parser.parse_args(argv)

    date_str = args.date or datetime.now().strftime("%Y-%m-%d")
    repo = Path(args.repo_root)
    results_dir = repo / "results_csv"
    logs_dir = repo / "logs"
    data_dir = repo / "apps" / "dashboards" / "alpaca-next" / "data"
    daily_log_dir = Path(args.daily_log_dir)
    data_cache_dir = repo / "data_cache"

    results = [
        check_daily(results_dir, args.max_age_hours),
        check_pipeline_run(daily_log_dir, args.max_age_hours),
        check_data_advance(data_cache_dir),
        check_signals(results_dir, args.min_signals),
        check_publish(repo, args.monitor_branch, args.max_age_hours, data_dir),
        check_open_run(logs_dir, results_dir, args.openrun_max_age_hours),
    ]
    worst = _aggregate(results)

    for r in results:
        print(r.line())
    print(f"=> worst={worst.upper()}")

    record = {
        "version": "1.1",
        "date": date_str,
        "generated_at": _now().isoformat(timespec="seconds"),
        "repo_root": str(repo),
        "worst": worst,
        "checks": [r.to_dict() for r in results],
    }
    out_path = (
        Path(args.output_json)
        if args.output_json
        else logs_dir / f"self_monitor_{date_str.replace('-', '')}.json"
    )
    try:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(
            json.dumps(record, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[write] {out_path}")
    except Exception as exc:  # noqa: BLE001
        print(f"[warn] JSON 書き出し失敗 (無視): {exc}")

    if not args.no_notify:
        _notify(date_str, results, worst, dry_run=args.dry_run)

    return {"ok": 0, "info": 0, "skip": 0, "warn": 2, "crit": 3}.get(worst, 0)


if __name__ == "__main__":
    raise SystemExit(main())
