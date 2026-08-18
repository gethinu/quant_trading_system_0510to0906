"""exit artifact の *役割* (提案 / 実発注) を分離して解決するための共有ヘルパ。

``results_csv/exit_orders_YYYYMMDD.json`` は 1 営業日に **2 回、別々の意味で**
書かれる:

    06:00 JST  daily_pipeline.ps1 [exit_check]  -> dry-run の **提案** (proposal)
    22:35 JST  open_auto_run.py   exit_stage    -> --confirm 付きの **実発注** (execution)

同じ path なので後から書いた方が勝つ。その結果、朝から夜までの間は「当日の
exit_orders」が提案でしかないのに、読み手は実発注記録として扱ってしまう
(exit_verify は 07:20 に走るので **毎日必ず提案を検証していた**)。

ここでは 2 つを混ぜないために:

  - artifact に ``role`` と ``written_at`` を刻む
  - role ごとの sidecar (``exit_orders_YYYYMMDD_execution.json`` /
    ``..._proposal.json``) を併記する。**提案は実発注 sidecar を上書きしない**
  - 実発注記録が欲しい読み手のために ``latest_execution`` を提供する

sidecar が無い過去日のために、``mode == "submitted"`` の legacy artifact も
実発注として認識する (新 writer が回る前から遡って検証できるようにする)。
"""

from __future__ import annotations

from datetime import datetime, timezone
import json
from pathlib import Path
from typing import Any

ROLE_PROPOSAL = "proposal"
ROLE_EXECUTION = "execution"
ROLES = (ROLE_PROPOSAL, ROLE_EXECUTION)

# legacy artifact (role 未記載) を実発注とみなす mode 値。
_LEGACY_EXECUTION_MODES = {"submitted"}


def role_for(dry_run: bool) -> str:
    """dry-run かどうかから role を決める (submit したものだけが execution)。"""
    return ROLE_PROPOSAL if dry_run else ROLE_EXECUTION


def artifact_role(payload: dict[str, Any] | None) -> str | None:
    """artifact の role を返す。legacy は ``mode`` から推定する。"""
    if not isinstance(payload, dict):
        return None
    role = payload.get("role")
    if isinstance(role, str) and role in ROLES:
        return role
    mode = str(payload.get("mode") or "").lower()
    if not mode:
        return None
    return ROLE_EXECUTION if mode in _LEGACY_EXECUTION_MODES else ROLE_PROPOSAL


def sidecar_path(output_path: Path, role: str) -> Path:
    """``exit_orders_20260818.json`` -> ``exit_orders_20260818_execution.json``。"""
    return output_path.with_name(f"{output_path.stem}_{role}{output_path.suffix}")


def stamp_role(payload: dict[str, Any], role: str) -> dict[str, Any]:
    """payload に role / written_at を刻む (既存 key は壊さない)。"""
    payload["role"] = role
    payload["written_at"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    return payload


def write_with_sidecar(output_path: Path, payload: dict[str, Any], role: str) -> Path:
    """canonical path と role sidecar の両方へ書き、sidecar path を返す。

    canonical path (``exit_orders_<date>.json``) は既存の読み手のために従来どおり
    「最後に走った run」を指す。sidecar は同じ role の run しか触らないので、
    朝の提案が夜の実発注記録を消すことがない。
    """
    stamp_role(payload, role)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    output_path.write_text(text, encoding="utf-8")
    side = sidecar_path(output_path, role)
    side.write_text(text, encoding="utf-8")
    return side


def load_artifact(path: Path) -> dict[str, Any] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def _compact(date_str: str) -> str:
    return date_str.replace("-", "")


def latest_execution(
    results_dir: Path,
    on_or_before: str | None = None,
    max_scanned: int = 15,
) -> tuple[Path, dict[str, Any]] | None:
    """直近の **実発注** artifact を (path, payload) で返す。無ければ None。

    朝 07:20 に当日を指定して呼ぶと、前営業日夜の実発注記録が返る。当日の提案は
    role で弾かれるので、提案を実発注として検証してしまう事故が起きない。

    ``on_or_before`` (YYYY-MM-DD) 以前の artifact だけを見る。sidecar を優先し、
    無い日は legacy の unsuffixed artifact を ``mode`` で判定する。
    """
    if not results_dir.exists():
        return None
    limit = _compact(on_or_before) if on_or_before else None

    def _date_key(p: Path) -> str:
        # exit_orders_20260818[_execution].json -> 20260818
        return p.stem.replace("exit_orders_", "").split("_")[0]

    candidates = [
        p
        for p in results_dir.glob("exit_orders_*.json")
        if _date_key(p).isdigit()
        and not p.stem.endswith(f"_{ROLE_PROPOSAL}")
        and (limit is None or _date_key(p) <= limit)
    ]
    # 日付降順 → 同日は sidecar を legacy より先に見る。
    candidates.sort(
        key=lambda p: (_date_key(p), p.stem.endswith(f"_{ROLE_EXECUTION}")),
        reverse=True,
    )
    for path in candidates[:max_scanned]:
        payload = load_artifact(path)
        if payload is None:
            continue
        if artifact_role(payload) == ROLE_EXECUTION:
            return path, payload
    return None
