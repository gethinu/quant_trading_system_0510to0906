from __future__ import annotations

"""
TRDlist バリデーションユーティリティ。

最終候補やシステム別候補（signals_*_*.csv 相当）の DataFrame について、
最低限の整合性チェックを実施し、エラー/警告の一覧を返します。

想定スキーマ（柔軟に対応）:
    - 必須: symbol(str), system(str), entry_date(Timestamp),
                     entry_price(float), stop_price(float)
  - 任意: score, score_key, reason, shares(int/float) など

注意:
  - 本モジュールは I/O を行いません。結果 JSON の保存は呼び出し側（scripts/run_all_systems_today.py）で行います。
"""

from dataclasses import dataclass
from typing import Any

import pandas as pd


@dataclass
class ValidationResult:
    errors: list[str]
    warnings: list[str]

    def to_dict(self) -> dict[str, Any]:  # JSON シリアライズ用
        return {"errors": list(self.errors), "warnings": list(self.warnings)}


def _is_nan(val: Any) -> bool:
    try:
        return bool(pd.isna(val))
    except Exception:
        return False


def validate_trd_frame(
    df: pd.DataFrame, *, name: str | None = None
) -> ValidationResult:
    errs: list[str] = []
    warns: list[str] = []
    label = (name or "").strip() or "unknown"

    if df is None:
        errs.append(f"[{label}] frame is None")
        return ValidationResult(errs, warns)
    if getattr(df, "empty", True):
        allow_empty = label.lower() in {"system6", "system7"}
        if not allow_empty:
            warns.append(f"[{label}] frame is empty")
        return ValidationResult(errs, warns)

    # 必須列チェック
    required = ["symbol", "system", "entry_date", "entry_price", "stop_price"]
    missing = [c for c in required if c not in df.columns]
    if missing:
        errs.append(f"[{label}] missing required columns: {','.join(missing)}")
        # 続行して分かる範囲の検証も行う

    # 重複（symbol, system）組の検出
    try:
        dup_mask = df.duplicated(
            subset=[c for c in ("symbol", "system") if c in df.columns]
        )
        if dup_mask.any():
            dups = df.loc[
                dup_mask,
                [c for c in ("symbol", "system") if c in df.columns],
            ].head(10)
            errs.append(
                (
                    f"[{label}] duplicate symbol/system pairs detected (first 10 shown): {dups.to_dict('records')}"
                )
            )
    except Exception:
        pass

    # 同一 symbol が複数行に現れるケース（複数システムや両建ての可能性）
    # まずは警告レベルで通知（将来的に方針が固まればエラーへ格上げ）。
    try:
        if "symbol" in df.columns and len(df) > 1:
            dups_sym = df.duplicated(subset=["symbol"], keep=False)
            if bool(dups_sym.any()):
                cols = [c for c in ("symbol", "system", "side") if c in df.columns]
                sample = df.loc[dups_sym, cols].dropna(how="all").head(10)
                warns.append(
                    (
                        f"[{label}] duplicate symbols detected (first 10 shown): {sample.to_dict('records')}"
                    )
                )
    except Exception:
        pass

    # 行ごとの基本検証
    for idx, row in df.iterrows():
        sym = str(row.get("symbol", ""))
        sys_name = str(row.get("system", ""))
        # entry_date
        if "entry_date" in df.columns:
            if _is_nan(row.get("entry_date")):
                errs.append(
                    f"[{label}] {sym}/{sys_name} has NaT entry_date at index {idx}"
                )
        # prices

        def _to_float_safe(x: Any) -> float | None:
            if x is None:
                return None
            try:
                val = float(x)
                return val
            except Exception:
                return None

        ep = (
            _to_float_safe(row.get("entry_price"))
            if "entry_price" in df.columns
            else None
        )
        sp = (
            _to_float_safe(row.get("stop_price"))
            if "stop_price" in df.columns
            else None
        )
        if ep is None or _is_nan(ep) or (isinstance(ep, (int, float)) and ep <= 0):
            errs.append(
                (
                    f"[{label}] {sym}/{sys_name} invalid entry_price at index {idx}: {row.get('entry_price')}"
                )
            )
        if sp is None or _is_nan(sp) or (isinstance(sp, (int, float)) and sp <= 0):
            errs.append(
                (
                    f"[{label}] {sym}/{sys_name} invalid stop_price at index {idx}: {row.get('stop_price')}"
                )
            )

        # side（存在する場合）
        if "side" in df.columns:
            try:
                sd = str(row.get("side", "")).strip().lower()
                if sd and sd not in {"long", "short"}:
                    warns.append(
                        (
                            f"[{label}] {sym}/{sys_name} unknown side at index {idx}: {row.get('side')}"
                        )
                    )
            except Exception:
                warns.append(
                    (
                        f"[{label}] {sym}/{sys_name} invalid side value at index {idx}: {row.get('side')}"
                    )
                )

        # shares（存在する場合）
        if "shares" in df.columns:
            try:
                sh = _to_float_safe(row.get("shares"))
                if sh is None or _is_nan(sh) or sh <= 0:
                    errs.append(
                        (
                            f"[{label}] {sym}/{sys_name} invalid shares at index {idx}: {row.get('shares')}"
                        )
                    )
            except Exception:
                warns.append(
                    (
                        f"[{label}] {sym}/{sys_name} non-numeric shares at index {idx}: {row.get('shares')}"
                    )
                )

        # position_value 整合性（存在する場合）
        if "position_value" in df.columns:
            try:
                pv = _to_float_safe(row.get("position_value"))
                if pv is None or _is_nan(pv) or pv <= 0:
                    warns.append(
                        (
                            f"[{label}] {sym}/{sys_name} invalid position_value "
                            f"at index {idx}: "
                            f"{row.get('position_value')}"
                        )
                    )
                else:
                    if "shares" in df.columns and ep is not None:
                        try:
                            sh2 = _to_float_safe(row.get("shares")) or 0.0
                            expected = abs(float(ep)) * float(sh2)
                            # 許容誤差: 1 セント or 0.1% のいずれか大きい方
                            tol = max(0.01, expected * 0.001)
                            if not (abs(float(pv) - expected) <= tol):
                                warns.append(
                                    (
                                        f"[{label}] {sym}/{sys_name} position_value "
                                        f"mismatch at index {idx}: "
                                        f"expected≈{expected:.2f}, "
                                        f"actual={pv}"
                                    )
                                )
                        except Exception:
                            # shares が非数など
                            pass
                    else:
                        # 片側欠落
                        warns.append(
                            (
                                f"[{label}] {sym}/{sys_name} position_value given but "
                                f"shares/entry_price missing at index {idx}"
                            )
                        )
            except Exception:
                # position_value 検証での例外は警告として握り、パイプラインを止めない
                warns.append(
                    (
                        f"[{label}] {sym}/{sys_name} position_value validation error "
                        f"at index {idx}: {row.get('position_value')}"
                    )
                )

    # --- グループレベル検証（capital モード系の軽量整合チェック） ---
    try:
        has_sys = "system" in df.columns
        has_pv = "position_value" in df.columns
        has_sb = "system_budget" in df.columns
        has_ra = "remaining_after" in df.columns

        # per-row: remaining_after ≈ system_budget - position_value
        if has_pv and has_sb and has_ra:
            try:
                for idx, row in df.iterrows():
                    sys_name = str(row.get("system", ""))
                    sb_val = row.get("system_budget")
                    pv_val = row.get("position_value")
                    ra_val = row.get("remaining_after")

                    # 数値化
                    sb = None if sb_val is None else float(sb_val)
                    pv = None if pv_val is None else float(pv_val)
                    ra = None if ra_val is None else float(ra_val)

                    if sb is None or pv is None or ra is None:
                        continue
                    tol = max(0.01, abs(sb) * 0.001)
                    expected = sb - pv
                    if not (abs(ra - expected) <= tol):
                        warns.append(
                            (
                                f"[{label}] {sys_name} row {idx}: "
                                f"remaining_after mismatch (expected≈{expected:.2f}, "
                                f"actual={ra:.2f})"
                            )
                        )
                    # 負の残高が大きすぎないか
                    if ra < -tol:
                        warns.append(
                            (
                                f"[{label}] {sys_name} row {idx}: remaining_after negative ({ra:.2f}) beyond tolerance"
                            )
                        )
            except Exception:
                pass

        # per-system: sum(position_value) <= inferred initial budget
        if has_sys and has_pv and has_sb:
            try:
                # 正規化
                cols = [
                    c
                    for c in ("system", "position_value", "system_budget")
                    if c in df.columns
                ]
                work = df[cols].copy()
                work["system"] = work["system"].astype(str).str.lower().str.strip()
                # 数値化（非数は無視）

                def _to_num(x: Any) -> float | None:
                    try:
                        return float(x)
                    except Exception:
                        return None

                work["_pv"] = work["position_value"].apply(_to_num)
                work["_sb"] = work["system_budget"].apply(_to_num)

                for sys_key, grp in work.groupby("system"):
                    pv_sum = float(grp["_pv"].dropna().sum())
                    if pv_sum <= 0:
                        continue
                    sb_max = grp["_sb"].dropna().max() if "_sb" in grp else None
                    if sb_max is None or pd.isna(sb_max):
                        # 予算情報が無い場合はスキップ
                        continue
                    budget = float(sb_max)
                    tol_budget = max(1.0, budget * 0.01)  # 1% or $1 を許容
                    if pv_sum > budget + tol_budget:
                        warns.append(
                            (
                                f"[{label}] {sys_key}: total position_value "
                                f"{pv_sum:.2f} exceeds inferred budget {budget:.2f} "
                                f"(tol {tol_budget:.2f})"
                            )
                        )
            except Exception:
                pass
    except Exception:
        pass

    return ValidationResult(errs, warns)


def summarize_trd_frame(df: pd.DataFrame | None) -> dict[str, Any]:
    """TRD フレームの軽量サマリを返す（集計のみ、I/O 無し）。

    - rows: 行数
    - unique_symbols: 一意シンボル数
    - side_counts: side ごとの件数（あれば）
    - system_counts: system ごとの件数（あれば）
    """
    out: dict[str, Any] = {
        "rows": 0,
        "unique_symbols": 0,
        "side_counts": {},
        "system_counts": {},
    }
    try:
        if df is None or getattr(df, "empty", True):
            return out
        out["rows"] = int(len(df))
        if "symbol" in df.columns:
            try:
                out["unique_symbols"] = int(
                    df["symbol"].astype(str).str.upper().nunique()
                )
            except Exception:
                out["unique_symbols"] = int(df["symbol"].nunique())
        if "side" in df.columns:
            try:
                counts = df["side"].astype(str).str.lower().value_counts().to_dict()
                out["side_counts"] = {str(k): int(v) for k, v in counts.items()}
            except Exception:
                pass
        if "system" in df.columns:
            try:
                counts = df["system"].astype(str).str.lower().value_counts().to_dict()
                out["system_counts"] = {str(k): int(v) for k, v in counts.items()}
            except Exception:
                pass
        return out
    except Exception:
        return out


def build_validation_report(
    final_df: pd.DataFrame | None,
    per_system: dict[str, pd.DataFrame] | None,
) -> dict[str, Any]:
    """最終出力およびシステム別出力の検証レポートを生成。"""
    report: dict[str, Any] = {
        "final": None,
        "systems": {},
        "summary": {"errors": 0, "warnings": 0},
        "final_stats": {},
        "system_stats": {},
    }

    total_err = 0
    total_warn = 0

    try:
        if final_df is not None and not getattr(final_df, "empty", True):
            res = validate_trd_frame(final_df, name="final")
            report["final"] = res.to_dict()
            total_err += len(res.errors)
            total_warn += len(res.warnings)
            # 追加: 軽量サマリ
            report["final_stats"] = summarize_trd_frame(final_df)
        else:
            report["final"] = {"errors": [], "warnings": ["final frame empty or None"]}
            total_warn += 1
    except Exception as e:
        report["final"] = {"errors": [f"validation failed: {e}"], "warnings": []}
        total_err += 1

    try:
        if per_system:
            for name, df in per_system.items():
                try:
                    res = validate_trd_frame(df, name=name)
                    report["systems"][str(name)] = res.to_dict()
                    total_err += len(res.errors)
                    total_warn += len(res.warnings)
                    report["system_stats"][str(name)] = summarize_trd_frame(df)
                except Exception as e:
                    report["systems"][str(name)] = {
                        "errors": [f"validation failed: {e}"],
                        "warnings": [],
                    }
                    total_err += 1
    except Exception:
        pass

    report["summary"] = {"errors": int(total_err), "warnings": int(total_warn)}
    return report


__all__ = [
    "ValidationResult",
    "validate_trd_frame",
    "summarize_trd_frame",
    "build_validation_report",
]
