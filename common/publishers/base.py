"""Publisher 抽象基底 (Phase 2/3 配信基盤)。

配信先 (Discord / LINE / Email / 汎用 Webhook) を差し替え可能にする ABC。
Phase 1 では ``DiscordPublisher`` のみ実装し、他は skeleton。Phase 2 で
subscriber 制 (per-subscriber routing) を肉付けする際、この interface の
下で実装を差し替えるだけで済むよう設計している。

domain object ``SignalMessage`` は今日のシグナル JSON (schema v1.0) を
そのまま保持し、各 publisher が自分の payload 形式へ render する。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

# system 別の絵文字 (summary 見栄え用)
_SYSTEM_EMOJI = {
    "sys1": "📈",
    "sys2": "📉",
    "sys3": "🔄",
    "sys4": "🧭",
    "sys5": "⚡",
    "sys6": "🌊",
    "sys7": "🛡️",
}

# gate 生存率がこれ未満なら WARN badge を付ける
WARN_SURVIVAL_THRESHOLD = 0.05


@dataclass
class PublishResult:
    """1 回の配信結果。"""

    publisher: str
    ok: bool
    status_code: int | None = None
    detail: str = ""
    target: str = ""  # 送信先識別 (masked webhook / subscriber id 等)

    def as_dict(self) -> dict[str, Any]:
        return {
            "publisher": self.publisher,
            "ok": self.ok,
            "status_code": self.status_code,
            "detail": self.detail,
            "target": self.target,
        }


@dataclass
class SignalMessage:
    """配信対象の当日シグナル (schema v1.0 payload をラップ)。"""

    payload: dict[str, Any] = field(default_factory=dict)

    # --- convenience accessors -----------------------------------------
    @property
    def date(self) -> str:
        return str(self.payload.get("date", ""))

    @property
    def run_id(self) -> str:
        return str(self.payload.get("meta", {}).get("run_id", ""))

    @property
    def provider(self) -> str:
        return str(self.payload.get("provider", ""))

    @property
    def systems(self) -> dict[str, Any]:
        return self.payload.get("systems", {}) or {}

    @property
    def total_signals(self) -> int:
        return int(self.payload.get("portfolio", {}).get("total_signals", 0) or 0)

    @property
    def hedge(self) -> dict[str, Any] | None:
        return self.payload.get("portfolio", {}).get("hedge")

    def title(self) -> str:
        return f"📊 Today's Signals — {self.date}"

    def has_warnings(self) -> bool:
        for cfg in self.systems.values():
            if float(cfg.get("gate_survival_ratio", 1.0)) < WARN_SURVIVAL_THRESHOLD:
                return True
        return False

    def system_summary_lines(self, *, top_n: int = 3) -> list[str]:
        """system 別サマリ行 (絵文字 + 生存率 + 上位 signals) を生成する。"""
        lines: list[str] = []
        for sys_key in sorted(
            self.systems.keys(),
            key=lambda x: int(x[3:]) if x[3:].isdigit() else 99,
        ):
            cfg = self.systems[sys_key]
            sigs = cfg.get("signals", []) or []
            ratio = float(cfg.get("gate_survival_ratio", 0.0))
            emoji = _SYSTEM_EMOJI.get(sys_key, "•")
            warn = " ⚠️ WARN" if ratio < WARN_SURVIVAL_THRESHOLD else ""
            head = f"{sys_key} {emoji} {ratio * 100:.0f}% survival{warn}:"
            if not sigs:
                lines.append(f"{head} (no signals)")
                continue
            parts = []
            for s in sigs[:top_n]:
                price = s.get("entry_price")
                price_s = f"${price:.2f}" if isinstance(price, (int, float)) else "—"
                parts.append(
                    f"{s.get('symbol')} {s.get('side')} {price_s} (rank {s.get('rank')})"
                )
            more = "" if len(sigs) <= top_n else f", +{len(sigs) - top_n} more"
            lines.append(f"{head} " + ", ".join(parts) + more)
        return lines

    def footer(self) -> str:
        return f"run_id: {self.run_id} · provider: {self.provider}"


class Publisher(ABC):
    """配信先 1 種を表す抽象基底。実装は publish() のみ必須。"""

    #: 短い識別名 (log / PublishResult に載る)
    name: str = "base"

    @abstractmethod
    def publish(
        self, message: SignalMessage, *, dry_run: bool = False
    ) -> PublishResult:
        """``message`` を配信する。dry_run=True では送信せず payload だけ検証。"""
        raise NotImplementedError

    def is_configured(self) -> bool:
        """送信可能 (webhook/token 等が揃っている) かを返す。"""
        return True
