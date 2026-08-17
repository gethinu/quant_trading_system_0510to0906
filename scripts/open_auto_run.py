"""定例「オープン自動発注」ランナー (paper 専用・exit->entry / equity 連動 / ntfy)。

`logs/design_open_auto_run_20260708.md` の設計を恒久実装したもの。今日の一回限り
`C:\\tmp\\open_run_20260708.py` (削除済) を汎用化し、以下を段で行う:

    1. [gate]    paper env 断言 + market-open (Alpaca clock)。ここで落ちたら run 全体 ABORT。
                 シグナル数 < 閾値 は **entry 専用ゲート** で、exit は必ず通す
                 (薄データで新規建てはしないが、手仕舞いは止めない)。
    2. [signals] apps/app_today_signals.py --headless --date <d> で当日シグナル生成。
    3. [exit]    scripts/paper_exit_check.py --confirm --yes で計画/protective exit を先に発注。
    4. [wait]    market close (order_type=market) の fill をポーリング → post-exit を確定。
    5. [entry]   scripts/paper_trading_submit.py --signals-json --confirm --yes。
                 main の equity 連動サイジング (mode=equity_linked, deploy_pct=0.5) が
                 Alpaca から equity を自動取得して効く。**exit fill 後**に発注 = 順序担保。
    6. [record]  entry fill をポーリング + 最終ポジション snapshot。
    7. [notify]  scripts/publish_execution_summary.py (非 dry-run) で ntfy 実績通知
                 (UTF-8-safe な NtfyPublisher 経由。素の str POST の latin-1 死を回避)。
    8. [publish] notify が再構成した recon/pipeline を含む当日 data を Vercel へ publish。
    9. [durable] logs/open_run_<date>/ に全成果物と DONE.lock を残す。

安全ガード:
    - paper 固定 (assert_paper_env)。live/実マネーは一切扱わない。
    - market-open gate + 薄シグナル entry SKIP + 冪等ロック (DONE.lock)。
    - exit fill 確認後にのみ entry (exit->entry 順の強制)。

薄シグナルゲートについて (2026-07-27 修正):
    かつては薄シグナルで run 全体を ABORT していたため、2026-07-21..24 の 4 営業日で
    exit_stage() に到達せず時間 exit が停止し 20 建玉が期限超過した。exit は保有
    ポジションのみに依存し today_signals JSON を参照しないため、薄シグナルでも安全に
    実行できる。よって薄シグナルは entry 専用ゲートに降格した。
    切り戻しは --thin-aborts-run / env OPEN_RUN_THIN_ABORTS_RUN=1。

    判定基準の修正 (2026-07-27):
    ゲートの本来の目的は「データがまだ来ていない状態で発注しない」こと
    (design_open_auto_run_20260708.md L15: 06:00 は Polygon 403 / EODHD 401 で
    今日 1 件しか出ない)。しかし判定に使っていたのは ``systems[*].signals`` =
    **portfolio cap 適用後**の本数で、これは
    ``allow_total = max_total_positions(70) - held_total`` で上から抑えられた残枠。
    結果、建玉が 61 まで積み上がると残枠 9 < 閾値 10 が固定化し、
    2026-07-22..27 の 5 営業日連続で entry が SKIP された
    (候補数は 44-48 件で健全なまま = データ欠測ではない)。しかも entry が止まると
    建玉が減らないので残枠も戻らず、cap の最後の 9 枠が構造的に使えない。
    よって判定を **cap 前の候補数** (funnel.candidate_count 合計) に戻した。
    データ欠測時は候補数自体が 0-2 件になるため、本来の保護は維持される。

一回限りランナーが踏んだ 2 バグを恒久修正:
    - subprocess の cp932 UnicodeDecodeError -> encoding="utf-8", errors="replace" +
      子プロセスへ PYTHONUTF8=1 / PYTHONIOENCODING=utf-8 を伝播。
    - proc.stdout が None になり得る -> capture_output(text) で必ず str。かつ (x or "") で保護。

Usage:
    # 疎通確認 (発注しない: exit/entry は dry-run、通知も dry-run、poll skip)
    python scripts/open_auto_run.py --date 2026-07-10 --dry-run

    # 本番 (paper 実発注。Task Scheduler / 手動 GO 両対応)
    python scripts/open_auto_run.py --date 2026-07-10

    # 市場クローズ中でも段を通す (off-hours の疎通テスト)
    python scripts/open_auto_run.py --date 2026-07-10 --dry-run --allow-closed --skip-signals
"""

from __future__ import annotations

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# 段の途中で import が失敗しても runner 自体は落とさない (import は遅延)。
PYEXE = sys.executable
OBSERVABILITY_DEGRADED_EXIT_CODE = 4


def _child_env() -> dict[str, str]:
    """子プロセス用 env: UTF-8 を強制して cp932 デコード事故を根絶する。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _env_flag(name: str, default: bool = False) -> bool:
    """env の truthy 判定 (未設定なら default)。切り戻しスイッチ用。"""
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    return raw.strip().lower() in {"1", "true", "yes", "y", "on"}


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.date = args.date or datetime.now().strftime("%Y-%m-%d")
        self.compact = self.date.replace("-", "")
        self.dry_run = bool(args.dry_run)
        # 薄シグナルは entry のみを止める。exit は必ず通す (下記 signals() 参照)。
        self.entry_allowed = True
        self.out = ROOT / "logs" / f"open_run_{self.compact}"
        self.out.mkdir(parents=True, exist_ok=True)
        self.results = ROOT / "results_csv"
        self.signals_json = self.results / f"today_signals_{self.compact}.json"
        self.exit_json = self.results / f"exit_orders_{self.compact}.json"
        self.paper_json = self.results / f"paper_orders_{self.compact}.json"
        self._log_path = self.out / "run.log"
        self.record: dict[str, object] = {
            "date": self.date,
            "mode": "dry_run" if self.dry_run else "paper_submit",
            "worktree": str(ROOT),
        }

    # -- logging -----------------------------------------------------------
    def log(self, msg: str) -> None:
        ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        line = f"[{ts}] {msg}"
        print(line, flush=True)
        with self._log_path.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def _dump(self, name: str, obj: object) -> None:
        try:
            (self.out / name).write_text(
                json.dumps(obj, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"[warn] dump {name} 失敗 (無視): {exc}")

    # -- subprocess --------------------------------------------------------
    def run_step(self, name: str, argv: list[str]) -> tuple[int, str, str]:
        self.log(f"----- [{name}] python {' '.join(argv)}")
        proc = subprocess.run(
            [PYEXE, *argv],
            cwd=str(ROOT),
            env=_child_env(),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
        out = proc.stdout or ""
        err = proc.stderr or ""
        for ln in out.splitlines():
            self.log(f"  | {ln}")
        if err.strip():
            for ln in err.splitlines():
                self.log(f"  ! {ln}")
        self.log(f"----- [{name}] exit={proc.returncode}")
        (self.out / f"{name}.log").write_text(
            out + "\n---STDERR---\n" + err, encoding="utf-8"
        )
        return proc.returncode, out, err

    # -- ntfy warn (abort 経路用) ------------------------------------------
    def _ntfy_warn(self, title: str, body: str) -> None:
        """ABORT 等を UTF-8-safe な NtfyPublisher で通知。失敗しても無視。"""
        try:
            from common.publishers.ntfy import NtfyPublisher

            pub = NtfyPublisher()
            if not pub.is_configured():
                self.log("[ntfy] NTFY_TOPIC 未設定のため warn 通知スキップ")
                return
            res = pub.send_text(title, body, tags="warning", priority=5)
            self.log(f"[ntfy] warn 送信 ok={getattr(res, 'ok', '?')}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[ntfy] warn 送信失敗 (無視): {exc}")

    # -- gate helpers ------------------------------------------------------
    def _client(self):
        from common import broker_alpaca as ba

        return ba.get_client(paper=True)

    def _assert_paper(self) -> None:
        from common.alpaca_trading import assert_paper_env

        assert_paper_env()  # live なら例外 -> abort

    def _count_signals(self) -> int:
        if not self.signals_json.exists():
            return 0
        try:
            data = json.loads(self.signals_json.read_text(encoding="utf-8"))
        except Exception:
            return 0
        total = 0
        for blk in ((data or {}).get("systems") or {}).values():
            if isinstance(blk, dict):
                sigs = blk.get("signals") or []
                if isinstance(sigs, list):
                    total += len(sigs)
        self.record["signals_json_date"] = (data or {}).get("date")
        return total

    def _count_candidates(self) -> int | None:
        """portfolio cap 適用**前**の候補数 (= シグナル生成の健全性) を返す。

        薄シグナルゲートの本来の目的は「データがまだ来ていない状態で発注しない」こと
        (`logs/design_open_auto_run_20260708.md` L15: 06:00 は Polygon 403 / EODHD 401
        で今日 1 件しか出ない)。ところが判定に使っていた `_count_signals()` は
        ``systems[*].signals`` = **portfolio cap 適用後**の本数で、
        `core/final_allocation.py::_apply_portfolio_caps` が
        ``allow_total = max_total_positions - held_total`` で上から抑えた残数でしかない。

        そのため建玉が積み上がると「データは健全なのに本数が閾値未満」になり、
        entry が毎日止まる (2026-07-22..27 の実測: 候補 44-48 件は健全なまま、
        cap 後だけが 39 -> 8/9 に落ちて `thin_signals:9<10` で 5 営業日連続 SKIP)。

        判定を cap 前の候補数に戻すことで、本来の意図 (データ欠測の検出) を保ったまま
        cap 由来の誤発火だけを消す。``funnel.candidate_count`` が無い旧 JSON では
        ``n_candidates_input`` にフォールバックし、どちらも無ければ None を返して
        呼び出し側が従来どおり cap 後の本数で判定する (後方互換)。
        """
        if not self.signals_json.exists():
            return None
        try:
            data = json.loads(self.signals_json.read_text(encoding="utf-8"))
        except Exception:
            return None
        systems = ((data or {}).get("systems") or {}).values()
        total = 0
        seen = False
        for blk in systems:
            if not isinstance(blk, dict):
                continue
            funnel = blk.get("funnel")
            raw = funnel.get("candidate_count") if isinstance(funnel, dict) else None
            if raw is None:
                raw = blk.get("n_candidates_input")
            if raw is None:
                continue
            try:
                total += int(raw)
            except (TypeError, ValueError):
                continue
            seen = True
        return total if seen else None

    # -- stages ------------------------------------------------------------
    def gate(self) -> bool:
        # paper 断言 (最優先。live なら即 abort)
        try:
            self._assert_paper()
        except Exception as exc:  # noqa: BLE001
            self.log(f"[SAFETY ABORT] paper 断言失敗: {exc}")
            self.record["abort"] = f"not_paper:{exc}"
            return False

        # market-open (Alpaca clock)
        try:
            clock = self._client().get_clock()
            is_open = bool(getattr(clock, "is_open", False))
            self.record["market_is_open"] = is_open
            self.record["clock_next_open"] = str(getattr(clock, "next_open", ""))
            self.log(f"[gate] market_is_open={is_open}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[gate] clock 取得失敗: {exc}")
            is_open = False
            self.record["market_is_open"] = None
        if not is_open and not self.args.allow_closed:
            self.log("[gate] market CLOSED -> ABORT (--allow-closed で無視可)")
            self.record["abort"] = "market_closed"
            self._ntfy_warn(
                f"OpenAutoRun ABORT {self.date}",
                "market closed のため自動発注を中止 (paper)。",
            )
            return False
        return True

    def signals(self) -> bool:
        if self.args.skip_signals:
            self.log(f"[signals] --skip-signals: 既存 {self.signals_json.name} を使用")
        else:
            code, _out, _err = self.run_step(
                "signals",
                [
                    str(ROOT / "apps" / "app_today_signals.py"),
                    "--headless",
                    "--output-json",
                    str(self.signals_json),
                    "--date",
                    self.date,
                ],
            )
            if code != 0:
                self.log(f"[signals] WARN exit={code} (JSON があれば継続)")

        n_out = (
            self._count_signals()
        )  # portfolio cap 適用**後** = 実際に submit する本数
        self.record["signal_count"] = n_out
        # 薄シグナル判定は **cap 前の候補数** (データ健全性) で行う。cap 後の本数は
        # portfolio cap の残枠 (max_total_positions - held_total) に上から抑えられる
        # ため、建玉が積み上がると健全なデータでも閾値未満になり entry が恒久停止する。
        n_raw = self._count_candidates()
        self.record["candidate_count"] = n_raw
        n = n_out if n_raw is None else n_raw
        gate_basis = "signals(post-cap)" if n_raw is None else "candidates(pre-cap)"
        self.record["thin_gate_basis"] = gate_basis
        self.log(
            f"[gate] signal_count={n_out} candidate_count={n_raw} "
            f"-> 判定={n} basis={gate_basis} (threshold={self.args.min_signals}) "
            f"signals_date={self.record.get('signals_json_date')}"
        )
        thin = n < self.args.min_signals
        self.entry_allowed = not thin
        self.record["entry_allowed"] = self.entry_allowed
        if not thin:
            # データは健全だが cap 適用後に 1 件も残らなかった場合、submit しても no-op。
            # 「entry を通したのに 0 件」を黙って通さず、明示的に SKIP として記録する。
            if n_out == 0:
                self.entry_allowed = False
                self.record["entry_allowed"] = False
                self.record["entry_skip_reason"] = "no_submittable_signals_after_caps"
                self.log(
                    "[gate] 候補は健全 (>= 閾値) だが portfolio cap 適用後に 0 件 -> "
                    "entry SKIP (submit するものが無い)。exit は継続する"
                )
            return True

        # 薄シグナルは **entry 専用ゲート**。手仕舞い (exit) を新規シグナルの本数で
        # 止めるのは設計ミスで、実際 2026-07-21..24 の 4 営業日は本ゲートが run 全体を
        # ABORT し、時間 exit が停止して 20 建玉が期限超過した。exit は保有ポジション
        # だけに依存し today_signals JSON を一切参照しないので、薄シグナルでも安全に
        # 通せる。ここでは entry のみを落とす。
        reason = f"thin_signals:{n}<{self.args.min_signals}"
        self.record["entry_skip_reason"] = reason
        if self.args.thin_aborts_run:
            # 切り戻しスイッチ (--thin-aborts-run / OPEN_RUN_THIN_ABORTS_RUN=1)。
            # 旧挙動 = run 全体 ABORT。exit も止まる点に注意。
            self.log(
                f"[gate] 薄シグナル ({n} < {self.args.min_signals}) -> ABORT "
                "(--thin-aborts-run 指定: 旧挙動)"
            )
            self.record["abort"] = reason
            self._ntfy_warn(
                f"OpenAutoRun ABORT {self.date}",
                f"signals={n} < 閾値{self.args.min_signals}: 薄データのため自動発注を中止 (paper)。",
            )
            return False
        self.log(
            f"[gate] 薄シグナル ({n} < {self.args.min_signals}) -> **entry のみ SKIP**。"
            "exit は継続する (手仕舞いはシグナル本数に依存しない)"
        )
        self._ntfy_warn(
            f"OpenAutoRun entry SKIP {self.date}",
            f"signals={n} < 閾値{self.args.min_signals}: 新規 entry を見送り (paper)。"
            "exit (時間/保護) は通常どおり実行します。",
        )
        return True

    def exit_stage(self) -> list[str]:
        """exit を発注し、market-close (即時 fill) の order_id を返す。"""
        if self.args.flatten_all:
            return self._flatten_all_stage()
        argv = [
            str(ROOT / "scripts" / "paper_exit_check.py"),
            "--date",
            self.date,
            "--output-json",
            str(self.exit_json),
        ]
        if not self.dry_run:
            argv += ["--confirm", "--yes"]
        self.run_step("exit", argv)

        market_ids: list[str] = []
        try:
            data = json.loads(self.exit_json.read_text(encoding="utf-8"))
            exits = (data or {}).get("exits") or []
            self.record["exit_count"] = len(exits)
            for e in exits:
                if (
                    str(e.get("order_type")) == "market"
                    and e.get("order_id")
                    and not e.get("dry_run", True)
                ):
                    market_ids.append(str(e.get("order_id")))
            self._dump("exit_orders.json", data)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[exit] exit_orders 解析失敗: {exc}")
        self.log(f"[exit] market-close 注文 {len(market_ids)} 件を fill 監視対象に")
        return market_ids

    def _flatten_all_stage(self) -> list[str]:
        """--flatten-all: 全 position を成行 close + 既存 order を cancel (clean reset)。

        一回限りリセット run 用。Alpaca ネイティブの close_all_positions を使い、
        fractional/整数・long/short を broker 側で正しく処理させる (side/qty 計算の
        自作バグを避ける)。exit_orders.json は既存 schema (exits[].order_type/
        order_id/dry_run) で書き、wait_exit_fills がそのまま fill 監視できるようにする。
        """
        self.log(
            "[exit] --flatten-all: 全ポジションを market close + open order cancel (clean reset)"
        )
        # 事前スナップショット (dry-run でも「何を閉じるか」を durable に残す)
        snaps: list = []
        try:
            from common.alpaca_trading import fetch_position_snapshots

            snaps = fetch_position_snapshots(self._client())
        except Exception as exc:  # noqa: BLE001
            self.log(f"[exit] position 取得失敗: {exc}")
        self._dump(
            "positions_before_flatten.json",
            [
                {
                    "symbol": s.symbol,
                    "qty": s.qty,
                    "side": s.side,
                    "market_value": s.market_value,
                    "system": s.system,
                }
                for s in snaps
            ],
        )

        exits_rows: list[dict] = []
        market_ids: list[str] = []

        if self.dry_run:
            for s in snaps:
                exits_rows.append(
                    {
                        "symbol": s.symbol,
                        "system": s.system,
                        "side": s.side,
                        "qty": s.qty,
                        "order_type": "market",
                        "reason": "flatten_all",
                        "order_id": None,
                        "dry_run": True,
                    }
                )
            self.record["exit_count"] = len(exits_rows)
            payload = {
                "date": self.date,
                "mode": "dry_run",
                "flatten_all": True,
                "count": len(exits_rows),
                "exits": exits_rows,
            }
            self.exit_json.write_text(
                json.dumps(payload, ensure_ascii=False, indent=2, default=str),
                encoding="utf-8",
            )
            self._dump("exit_orders.json", payload)
            self.log(
                f"[exit] dry-run: {len(exits_rows)} ポジションを close する予定 (未発注)"
            )
            return []

        # 実発注: close_all_positions(cancel_orders=True)
        client = self._client()
        try:
            resps = client.close_all_positions(cancel_orders=True)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[exit] close_all_positions 失敗: {exc}")
            resps = []

        ok = 0
        failed = 0
        for r in resps or []:
            sym = getattr(r, "symbol", None)
            st = getattr(r, "status", None)
            raw_oid = getattr(r, "order_id", None)
            oid = str(raw_oid) if raw_oid else None
            if st == 200 and oid:
                ok += 1
                market_ids.append(oid)
            else:
                failed += 1
                self.log(f"[exit] close 失敗 sym={sym} http={st}")
            exits_rows.append(
                {
                    "symbol": sym,
                    "order_type": "market",
                    "reason": "flatten_all",
                    "order_id": oid,
                    "http_status": st,
                    "dry_run": False,
                }
            )
        self.record["exit_count"] = len(exits_rows)
        self.record["flatten_ok"] = ok
        self.record["flatten_failed"] = failed
        payload = {
            "date": self.date,
            "mode": "submitted",
            "flatten_all": True,
            "count": len(exits_rows),
            "submitted": ok,
            "failed": failed,
            "exits": exits_rows,
        }
        self.exit_json.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        self._dump("exit_orders.json", payload)
        self.log(
            f"[exit] flatten-all 発注: ok={ok} failed={failed} -> "
            f"{len(market_ids)} 件を fill 監視"
        )
        return market_ids

    def wait_exit_fills(self, order_ids: list[str]) -> None:
        if self.dry_run or not order_ids:
            self.log("[wait] exit fill 監視スキップ (dry-run または close 0)")
            return
        from common.broker_alpaca import get_orders_status_map

        client = self._client()
        deadline = time.monotonic() + float(self.args.poll_timeout)
        working = {
            "new",
            "accepted",
            "pending_new",
            "partially_filled",
            "held",
            "accepted_for_bidding",
            "pending_replace",
            "calculated",
            "pending_cancel",
        }
        fills: dict[str, str] = {}
        while time.monotonic() < deadline:
            smap = get_orders_status_map(client, order_ids)
            pending = []
            for oid in order_ids:
                st = smap.get(oid)
                s = str(st or "").lower().split(".")[-1]
                fills[oid] = s
                if s in working or s == "" or s == "none":
                    pending.append(oid)
            if not pending:
                self.log(f"[wait] 全 exit close settled ({len(order_ids)} 件)")
                break
            self.log(f"[wait] pending {len(pending)}/{len(order_ids)} ... 3s")
            time.sleep(3)
        else:
            self.log(f"[wait] TIMEOUT ({self.args.poll_timeout}s) pending 残 -> 継続")
        self._dump("close_fills.json", fills)
        self._snapshot_positions("positions_after_close.json")

    def entry_stage(self, eq: float | None) -> None:
        argv = [
            str(ROOT / "scripts" / "paper_trading_submit.py"),
            "--signals-json",
            str(self.signals_json),
            "--output-json",
            str(self.paper_json),
        ]
        # submit 側は Alpaca から equity を自動取得するが、その取得が transient に
        # 失敗すると fallback が既定 $10k になり deploy_budget が桁違いに小さくなる。
        # runner が既に取得済みの実 equity を fallback として渡し、桁落ちを防ぐ。
        if eq is not None and eq > 0:
            argv += ["--equity", str(eq)]
        if not self.dry_run:
            argv += ["--confirm", "--yes"]
        code, out, _err = self.run_step("entry", argv)
        self.record["entry_exit_code"] = code
        try:
            data = json.loads(self.paper_json.read_text(encoding="utf-8"))
            # meta は payload トップレベルに spread される (_write_orders_json)。
            meta = data or {}
            self.record["entry_submitted"] = meta.get("submitted")
            self.record["entry_skipped"] = meta.get("skipped")
            self.record["entry_failed"] = meta.get("failed")
            self.record["entry_status"] = meta.get("status")
            self.record["sizing_mode"] = meta.get("sizing_mode")
            self.record["equity_source"] = meta.get("equity_source")
            self.record["sizing_equity"] = meta.get("account_equity_usd")
            self._dump("paper_orders.json", data)
        except Exception as exc:  # noqa: BLE001
            self.log(f"[entry] paper_orders 解析失敗: {exc}")

    def _snapshot_positions(self, name: str) -> None:
        if self.dry_run:
            return
        try:
            from common.alpaca_trading import fetch_position_snapshots

            snaps = fetch_position_snapshots(self._client())
            rows = [
                {
                    "symbol": s.symbol,
                    "qty": s.qty,
                    "side": s.side,
                    "avg_entry_price": s.avg_entry_price,
                    "market_value": s.market_value,
                    "system": s.system,
                }
                for s in snaps
            ]
            self._dump(name, rows)
            longs = sum(1 for s in snaps if str(s.side).lower() == "long")
            shorts = sum(1 for s in snaps if str(s.side).lower() == "short")
            self.record[name.replace(".json", "")] = {
                "total": len(rows),
                "long": longs,
                "short": shorts,
            }
            self.log(f"[record] {name}: total={len(rows)} L={longs} S={shorts}")
        except Exception as exc:  # noqa: BLE001
            self.log(f"[record] {name} 取得失敗: {exc}")

    def record_stage(self) -> None:
        # entry fill が反映されるまで軽く待ってから最終ポジションを撮る
        if not self.dry_run:
            time.sleep(min(15, float(self.args.poll_timeout)))
        self._snapshot_positions("final_positions.json")

    def equity(self) -> float | None:
        try:
            from common.alpaca_trading import fetch_account_equity

            eq = fetch_account_equity(self._client())
            self.record["account_equity"] = eq
            self.log(f"[equity] account_equity={eq}")
            return eq
        except Exception as exc:  # noqa: BLE001
            self.log(f"[equity] 取得失敗 (無視): {exc}")
            return None

    def notify(self, eq: float | None) -> int:
        # publish_execution_summary は既存 recon_<date>.json を優先ロードして
        # 再ビルドしない。06:00 daily が薄シグナル(0)状態で書いた stale recon が
        # 残っていると、open-run が実発注しても ntfy が 0 と誤報する。stale を消して
        # fresh な today_signals/paper_orders/exit_orders から必ず再ビルドさせる。
        stale = self.results / f"recon_{self.compact}.json"
        if stale.exists():
            try:
                stale.unlink()
                self.log(f"[notify] stale recon を削除し再ビルド強制: {stale.name}")
            except Exception as exc:  # noqa: BLE001
                # ここで続行すると publish_execution_summary が残存 recon を優先し、
                # 今夜の発注結果ではなく古い集計を正常配信してしまう。通知を失敗扱いに
                # して main の observability degraded (rc=4) へ伝播させる。
                self.log(f"[notify] stale recon 削除失敗: {exc}")
                self.record["notify_status"] = "stale_recon_unlink_failed"
                self.record["notify_stale_recon_error"] = f"{type(exc).__name__}: {exc}"
                self.record["notify_exit_code"] = 1
                return 1
        argv = [
            str(ROOT / "scripts" / "publish_execution_summary.py"),
            "--date",
            self.date,
        ]
        if eq is not None:
            argv += ["--account-equity", str(eq)]
        if self.dry_run:
            argv += ["--dry-run"]
        try:
            code, _out, _err = self.run_step("notify", argv)
        except Exception as exc:  # noqa: BLE001 - publish は必ず後続させる
            self.log(f"[notify] 実行失敗: {exc}")
            code = 1
        self.record["notify_exit_code"] = int(code)
        return int(code)

    def publish(self) -> int:
        """post-entry の Alpaca snapshot を再生成し、PRIMARY worktree から Vercel
        monitor へ data/ を publish (commit+push claude/monitor-webapp)。

        - snapshot は read-only GET (export_alpaca_snapshot.py)。entry/record の後に
          撮るので post-entry のポジションを反映する。
        - Vercel publish は PRIMARY worktree (monitor-webapp を checkout 済) の
          scripts/publish_data_to_vercel.ps1 を叩く。data/ のみ stage されるので
          ユーザーの未コミット変更は巻き込まない (script 側の -- $RelData 制約)。
        - dry-run は snapshot 生成のみ (commit/push しない)。
        """
        if self.args.no_publish:
            self.log("[publish] --no-publish: publish stage skip")
            self.record["publish"] = "skipped_no_publish"
            self.record["publish_exit_code"] = 0
            return 0
        # 1) post-entry snapshot 再生成 (read-only)
        try:
            snapshot_code, _out, _err = self.run_step(
                "snapshot",
                [
                    str(ROOT / "scripts" / "export_alpaca_snapshot.py"),
                    "--date",
                    self.date,
                ],
            )
        except Exception as exc:  # noqa: BLE001 - Vercel publish は必ず試す
            self.log(f"[publish] snapshot 実行失敗: {exc}")
            snapshot_code = 1
        snapshot_code = int(snapshot_code)
        self.record["snapshot_exit_code"] = snapshot_code
        if self.dry_run:
            self.log(
                "[publish] dry-run: Vercel publish (commit/push) skip。snapshot のみ生成"
            )
            self.record["publish"] = "skipped_dry_run"
            # dry-run では外部 publish 自体を行わないため publish stage は成功扱い。
            # snapshot の疎通結果は snapshot_exit_code に独立して残す。
            self.record["publish_exit_code"] = 0
            return 0
        # 2) PRIMARY worktree から data/ を publish
        primary = Path(self.args.primary_root)
        ps1 = primary / "scripts" / "publish_data_to_vercel.ps1"
        if not ps1.exists():
            self.log(f"[publish] publish script 不在: {ps1}")
            self.record["publish"] = "script_missing"
            code = snapshot_code if snapshot_code != 0 else 1
            self.record["publish_exit_code"] = code
            return code
        self.log(f"[publish] {ps1} -Date {self.date} (cwd={primary})")
        try:
            proc = subprocess.run(
                [
                    "powershell",
                    "-NoProfile",
                    "-ExecutionPolicy",
                    "Bypass",
                    "-File",
                    str(ps1),
                    "-Date",
                    self.date,
                ],
                cwd=str(primary),
                env=_child_env(),
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
            )
        except Exception as exc:  # noqa: BLE001
            self.log(f"[publish] publish 実行失敗: {exc}")
            self.record["publish"] = f"error:{exc}"
            code = snapshot_code if snapshot_code != 0 else 1
            self.record["publish_exit_code"] = code
            return code
        out = proc.stdout or ""
        err = proc.stderr or ""
        for ln in out.splitlines():
            self.log(f"  | {ln}")
        if err.strip():
            for ln in err.splitlines():
                self.log(f"  ! {ln}")
        (self.out / "publish.log").write_text(
            out + "\n---STDERR---\n" + err, encoding="utf-8"
        )
        vercel_code = int(proc.returncode)
        self.log(f"[publish] publish_data_to_vercel exit={vercel_code}")
        self.record["vercel_publish_exit_code"] = vercel_code
        # Vercel publish が失敗した場合はその code を優先。publish が成功しても
        # snapshot が失敗していれば観測段全体は失敗として main へ伝播する。
        code = vercel_code if vercel_code != 0 else snapshot_code
        self.record["publish_exit_code"] = code
        return code

    def finalize(self, aborted: bool) -> None:
        def remember_write_error(field: str, path: Path, exc: Exception) -> None:
            detail = f"{type(exc).__name__}: {exc}"
            self.record[field] = detail
            try:
                self.log(f"[finalize] {path.name} 書き込み失敗: {detail}")
            except Exception:  # noqa: BLE001 - DONE 作成後の補助ログも best-effort
                print(f"[finalize] {path.name} 書き込み失敗: {detail}", flush=True)

        # 実発注が完了した run の冪等ロックは、SUMMARY/completion の補助成果物より
        # 先に durable 化する。後続 I/O が失敗しても rc=4 等で同じ注文を再実行させない。
        done_path = self.out / "DONE.lock"
        if not aborted and not self.dry_run:
            try:
                done_path.write_text(
                    datetime.now(timezone.utc).isoformat(), encoding="utf-8"
                )
            except Exception as exc:  # noqa: BLE001 - lock failure は成功扱いにできない
                remember_write_error("done_lock_write_error", done_path, exc)
                raise RuntimeError(f"DONE.lock を作成できません: {done_path}") from exc

        lines = [
            f"# OPEN AUTO RUN {self.date} ({self.record['mode']})",
            "",
            f"- worktree: {ROOT}",
            f"- market_is_open: {self.record.get('market_is_open')}",
            f"- signal_count: {self.record.get('signal_count')} "
            f"(signals_date={self.record.get('signals_json_date')})",
            f"- account_equity: {self.record.get('account_equity')}",
        ]
        if aborted:
            lines.append(f"- **ABORTED**: {self.record.get('abort')}")
        else:
            if not self.entry_allowed:
                lines.append(
                    f"- **ENTRY SKIPPED**: {self.record.get('entry_skip_reason')} "
                    "(exit は実行済み)"
                )
            lines += [
                f"- exit_count: {self.record.get('exit_count')}",
                f"- entry: submitted={self.record.get('entry_submitted')} "
                f"skipped={self.record.get('entry_skipped')} "
                f"failed={self.record.get('entry_failed')} "
                f"status={self.record.get('entry_status')}",
                f"- sizing_equity(used): {self.record.get('sizing_equity')}",
                f"- final_positions: {self.record.get('final_positions')}",
                f"- observability: notify_rc={self.record.get('notify_exit_code')} "
                f"publish_rc={self.record.get('publish_exit_code')}",
            ]
        summary_path = self.out / "SUMMARY.md"
        try:
            summary_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
        except (
            Exception
        ) as exc:  # noqa: BLE001 - DONE 作成済み、補助成果物は best-effort
            remember_write_error("summary_write_error", summary_path, exc)

        # SUMMARY の失敗情報も completion_recon に残せるよう、最後に dump する。
        # _dump 自体は I/O 失敗を吸収するが、警告 log 側の例外も念のため封じる。
        completion_path = self.out / "completion_recon.json"
        try:
            self._dump(completion_path.name, self.record)
        except Exception as exc:  # noqa: BLE001 - DONE 作成後は絶対に再発注させない
            remember_write_error("completion_recon_write_error", completion_path, exc)

    # -- orchestration -----------------------------------------------------
    def main(self) -> int:
        self.log(
            f"=== OPEN AUTO RUN start date={self.date} mode={self.record['mode']} ==="
        )
        self.log(f"worktree={ROOT}")

        # 冪等ロック
        lock = self.out / "DONE.lock"
        if lock.exists() and not self.args.force and not self.dry_run:
            self.log("[lock] DONE.lock 存在 -> 本日は実行済み。skip (--force で上書き)")
            return 0

        if not self.gate():
            self.finalize(aborted=True)
            return 3
        if not self.signals():
            self.finalize(aborted=True)
            return 3

        eq = self.equity()
        market_ids = self.exit_stage()
        self.wait_exit_fills(market_ids)  # exit->entry 順の担保点
        if self.entry_allowed:
            self.entry_stage(eq)
        else:
            self.log(
                f"[entry] SKIP: {self.record.get('entry_skip_reason')} "
                "(exit は実行済み)"
            )
            self.record["entry_status"] = "skipped_thin_signals"
            self.record["entry_submitted"] = 0
        self.record_stage()

        # notify が recon/pipeline を最新の注文結果から再構成し、その成果物を publish が
        # dashboard data として配る。この順序が逆だと ntfy は最新でも dashboard は
        # ひとつ前の pipeline のままになる。片方が失敗しても他方は必ず実行する。
        try:
            notify_rc = int(self.notify(eq))
        except Exception as exc:  # noqa: BLE001 - publish を必ず後続させる
            self.log(f"[notify] 未処理例外: {exc}")
            notify_rc = 1
        self.record["notify_exit_code"] = notify_rc

        try:
            publish_rc = int(self.publish())
        except Exception as exc:  # noqa: BLE001 - DONE を必ず durable に残す
            self.log(f"[publish] 未処理例外: {exc}")
            publish_rc = 1
        self.record["publish_exit_code"] = publish_rc

        # 注文段は既に完了しているため、観測段の失敗時も DONE.lock を先に作る。
        # rc=4 による再試行で発注を重複させないことが最優先。
        self.finalize(aborted=False)
        if notify_rc != 0 or publish_rc != 0:
            self.log(
                "=== OPEN AUTO RUN done with observability failure "
                f"notify={notify_rc} publish={publish_rc} ==="
            )
            return OBSERVABILITY_DEGRADED_EXIT_CODE
        self.log("=== OPEN AUTO RUN done ===")
        return 0


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument(
        "--date", default=None, help="対象日 YYYY-MM-DD (default: today local)"
    )
    p.add_argument(
        "--min-signals",
        type=int,
        default=10,
        help="この件数未満なら **entry のみ** 見送り (exit は継続。default 10)",
    )
    p.add_argument(
        "--thin-aborts-run",
        action="store_true",
        default=_env_flag("OPEN_RUN_THIN_ABORTS_RUN", False),
        help=(
            "[切り戻し] 薄シグナルで run 全体を ABORT する旧挙動に戻す。"
            "exit も止まるため時間 exit が滞留する点に注意 "
            "(env OPEN_RUN_THIN_ABORTS_RUN=1 でも可)。"
        ),
    )
    p.add_argument(
        "--poll-timeout",
        type=float,
        default=300.0,
        help="exit fill ポーリングの上限秒 (default 300)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="発注しない: exit/entry は dry-run、通知も dry-run、poll skip (疎通確認)",
    )
    p.add_argument(
        "--skip-signals",
        action="store_true",
        help="signal 再生成を skip し既存 today_signals JSON を使う",
    )
    p.add_argument(
        "--allow-closed",
        action="store_true",
        help="market closed でも段を通す (off-hours テスト)",
    )
    p.add_argument(
        "--force",
        action="store_true",
        help="DONE.lock があっても実行する",
    )
    p.add_argument(
        "--flatten-all",
        action="store_true",
        help="exit stage で保護 exit ではなく全ポジションを market close (一回限りリセット用)",
    )
    p.add_argument(
        "--no-publish",
        action="store_true",
        help="publish stage を skip (snapshot 再生成 + Vercel monitor への push をしない)",
    )
    p.add_argument(
        "--primary-root",
        default=r"C:\Repos\quant_trading_system_0510to0906",
        help="publish_data_to_vercel.ps1 を持つ PRIMARY worktree (monitor-webapp checkout)",
    )
    args = p.parse_args(argv)
    try:
        return Runner(args).main()
    except KeyboardInterrupt:
        print("interrupted")
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
