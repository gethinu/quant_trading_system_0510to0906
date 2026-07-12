"""定例「オープン自動発注」ランナー (paper 専用・exit->entry / equity 連動 / ntfy)。

`logs/design_open_auto_run_20260708.md` の設計を恒久実装したもの。今日の一回限り
`C:\\tmp\\open_run_20260708.py` (削除済) を汎用化し、以下を段で行う:

    1. [gate]    paper env 断言 + market-open (Alpaca clock) + データ鮮度 +
                 シグナル数 < 閾値 なら **ABORT** (薄データで自動発注しない)。
    2. [signals] apps/app_today_signals.py --headless --date <d> で当日シグナル生成。
    3. [exit]    scripts/paper_exit_check.py --confirm --yes で計画/protective exit を先に発注。
    4. [wait]    market close (order_type=market) の fill をポーリング → post-exit を確定。
    5. [entry]   scripts/paper_trading_submit.py --signals-json --confirm --yes。
                 main の equity 連動サイジング (mode=equity_linked, deploy_pct=0.5) が
                 Alpaca から equity を自動取得して効く。**exit fill 後**に発注 = 順序担保。
    6. [record]  entry fill をポーリング + 最終ポジション snapshot。
    7. [notify]  scripts/publish_execution_summary.py (非 dry-run) で ntfy 実績通知
                 (UTF-8-safe な NtfyPublisher 経由。素の str POST の latin-1 死を回避)。
    8. [durable] logs/open_run_<date>/ に全成果物を残す。

安全ガード:
    - paper 固定 (assert_paper_env)。live/実マネーは一切扱わない。
    - market-open gate + 薄シグナル ABORT + 冪等ロック (DONE.lock)。
    - exit fill 確認後にのみ entry (exit->entry 順の強制)。

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


def _child_env() -> dict[str, str]:
    """子プロセス用 env: UTF-8 を強制して cp932 デコード事故を根絶する。"""
    env = dict(os.environ)
    env["PYTHONUTF8"] = "1"
    env["PYTHONIOENCODING"] = "utf-8"
    return env


class Runner:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        self.date = args.date or datetime.now().strftime("%Y-%m-%d")
        self.compact = self.date.replace("-", "")
        self.dry_run = bool(args.dry_run)
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

        n = self._count_signals()
        self.record["signal_count"] = n
        self.log(
            f"[gate] signal_count={n} (threshold={self.args.min_signals}) "
            f"signals_date={self.record.get('signals_json_date')}"
        )
        if n < self.args.min_signals:
            self.log(
                f"[gate] 薄シグナル ({n} < {self.args.min_signals}) -> ABORT "
                "(06:00 薄データ事故の再発防止)"
            )
            self.record["abort"] = f"thin_signals:{n}<{self.args.min_signals}"
            self._ntfy_warn(
                f"OpenAutoRun ABORT {self.date}",
                f"signals={n} < 閾値{self.args.min_signals}: 薄データのため自動発注を中止 (paper)。",
            )
            return False
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

    def circuit_breaker_check(self, eq: float | None) -> bool:
        """drawdown サーキットブレーカ (config gated, default 無効)。

        entry の **前** に equity ドローダウンを判定し、config
        (risk.portfolio.drawdown_flatten_pct) で有効化されていて閾値超え & 全ガード
        通過なら全ポジションを flatten して **run を ABORT** する (ドローダウン中に
        新規建てしない = 安全弁)。config が 0 (既定) の間は完全に no-op。

        戻り値: True = 発火して flatten 済 (呼び出し側は abort すべき)。
                False = 無効 / 閾値内 / ガード抑止 / dry-run (通常継続)。
        """
        try:
            from common.drawdown_breaker import (
                assess,
                flatten_all_paper,
                load_equity_history,
                resolve_peak_equity,
            )
            from common.portfolio_guard import load_guard_config
        except Exception as exc:  # noqa: BLE001
            self.log(f"[breaker] import 失敗 (skip): {exc}")
            return False

        threshold = float(load_guard_config().get("drawdown_flatten_pct", 0.0) or 0.0)
        if threshold <= 0:
            self.log("[breaker] disabled (drawdown_flatten_pct=0) -> skip")
            self.record["breaker"] = "disabled"
            return False

        history = load_equity_history(self.results / "alpaca_equity_history.json")
        peak, n_points = resolve_peak_equity(history, eq)
        a = assess(eq, peak, threshold, n_history_points=n_points)
        self.record["breaker"] = a.to_dict()
        self.log(
            f"[breaker] armed={a.armed} breached={a.breached} "
            f"would_flatten={a.would_flatten} dd={a.drawdown_pct:.2%} "
            f"threshold={a.threshold_pct:.2%} hist={n_points} reason={a.reason}"
        )
        if not a.would_flatten:
            return False

        body = (
            f"peak drawdown {a.drawdown_pct:.2%} >= 閾値 {a.threshold_pct:.2%} "
            f"(equity ${a.equity:,.0f} / peak ${a.peak_equity:,.0f})"
        )
        if self.dry_run:
            self.log(f"[breaker] WOULD FLATTEN (dry-run のため未執行): {body}")
            self._ntfy_warn(
                f"DrawdownBreaker WOULD FIRE {self.date}",
                "オープン run: ドローダウン閾値成立 (dry-run のため未執行)。\n" + body,
            )
            return False

        # 実 run: paper 断言 → flatten → ABORT (新規建てしない)
        try:
            from common.alpaca_trading import assert_paper_env

            assert_paper_env()
            client = self._client()
        except Exception as exc:  # noqa: BLE001
            self.log(f"[breaker][SAFETY ABORT] paper 断言失敗、flatten 中止: {exc}")
            self.record["breaker_error"] = str(exc)
            return False

        self.log(f"[breaker] FLATTEN 実行 (paper) & 新規建て中止: {body}")
        result = flatten_all_paper(client)
        self.record["breaker_flatten"] = result
        self.record["abort"] = "drawdown_flatten"
        self._dump(
            "drawdown_flatten.json", {"assessment": a.to_dict(), "flatten": result}
        )
        self._ntfy_warn(
            f"DrawdownBreaker FIRED {self.date}",
            (
                "オープン run: サーキットブレーカ発火。全ポジション flatten & 新規建て中止 (paper)。\n"
                f"{body}\nclose ok={result.get('ok')} failed={result.get('failed')}"
            ),
        )
        return True

    def notify(self, eq: float | None) -> None:
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
                self.log(f"[notify] stale recon 削除失敗 (無視): {exc}")
        argv = [
            str(ROOT / "scripts" / "publish_execution_summary.py"),
            "--date",
            self.date,
        ]
        if eq is not None:
            argv += ["--account-equity", str(eq)]
        if self.dry_run:
            argv += ["--dry-run"]
        self.run_step("notify", argv)

    def publish(self) -> None:
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
            return
        # 1) post-entry snapshot 再生成 (read-only)
        self.run_step(
            "snapshot",
            [str(ROOT / "scripts" / "export_alpaca_snapshot.py"), "--date", self.date],
        )
        if self.dry_run:
            self.log(
                "[publish] dry-run: Vercel publish (commit/push) skip。snapshot のみ生成"
            )
            return
        # 2) PRIMARY worktree から data/ を publish
        primary = Path(self.args.primary_root)
        ps1 = primary / "scripts" / "publish_data_to_vercel.ps1"
        if not ps1.exists():
            self.log(f"[publish] publish script 不在 (skip): {ps1}")
            self.record["publish"] = "script_missing"
            return
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
            self.log(f"[publish] publish 実行失敗 (無視): {exc}")
            self.record["publish"] = f"error:{exc}"
            return
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
        self.log(f"[publish] publish_data_to_vercel exit={proc.returncode}")
        self.record["publish_exit_code"] = proc.returncode

    def finalize(self, aborted: bool) -> None:
        self._dump("completion_recon.json", self.record)
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
            lines += [
                f"- exit_count: {self.record.get('exit_count')}",
                f"- entry: submitted={self.record.get('entry_submitted')} "
                f"skipped={self.record.get('entry_skipped')} "
                f"failed={self.record.get('entry_failed')} "
                f"status={self.record.get('entry_status')}",
                f"- sizing_equity(used): {self.record.get('sizing_equity')}",
                f"- final_positions: {self.record.get('final_positions')}",
            ]
        (self.out / "SUMMARY.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
        if not aborted and not self.dry_run:
            (self.out / "DONE.lock").write_text(
                datetime.now(timezone.utc).isoformat(), encoding="utf-8"
            )

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
        if self.circuit_breaker_check(eq):
            # ドローダウン発火: flatten 済。新規建てせず、flat 状態を dashboard に反映して abort。
            self.record_stage()
            self.publish()
            self.notify(eq)
            self.finalize(aborted=True)
            self.log("=== OPEN AUTO RUN aborted by drawdown breaker ===")
            return 4
        market_ids = self.exit_stage()
        self.wait_exit_fills(market_ids)  # exit->entry 順の担保点
        self.entry_stage(eq)
        self.record_stage()
        self.publish()  # post-entry snapshot 再生成 + Vercel monitor へ data/ publish
        self.notify(eq)
        self.finalize(aborted=False)
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
        help="この件数未満なら薄データ ABORT (default 10)",
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
