"""Unit + regression tests for scripts/morning_brief.py.

Covers:
  - 天気 (2026-07-22 地点観測ベース化): 気象庁(JMA)公式=府県予報(天気/降水/最高)+
           AMeDAS 観測(当日最低)。telop code→短文、当日値の抽出、内部矛盾ガード
           (乾いた空 + pop>=70% は数値を伏せる)、観測欠損時は range 捏造せず最高のみ。
  - tribe: swimmy-fx-tribe の forward_status.txt (LIVE_READY/RUNNING/total) を
           read-only で1行ステータスへ。deploy 無し=live 0 を明示。
  - 家族ボード: 実体が無いうちは行ごと沈黙 (毎朝の「ソース無し」ノイズを止める)。
"""

from __future__ import annotations

from datetime import date
import importlib.util
import os
from pathlib import Path
import sys

_MB_PATH = Path(__file__).resolve().parents[1] / "scripts" / "morning_brief.py"
_spec = importlib.util.spec_from_file_location("morning_brief", _MB_PATH)
mb = importlib.util.module_from_spec(_spec)
sys.modules["morning_brief"] = mb
assert _spec.loader is not None
_spec.loader.exec_module(mb)


# --------------------------------------------------------------------------
# 天気: telop code -> 短文 / 乾き判定
# --------------------------------------------------------------------------
def test_jma_weather_text_known_and_coarse_fallback():
    assert mb._jma_weather_text("110") == "晴れのち時々くもり"  # 7/21 東京
    assert mb._jma_weather_text("101") == "晴れ時々くもり"  # 7/21 千葉
    assert mb._jma_weather_text("300") == "雨"
    # 未収載コードは先頭桁で粗くフォールバック(生 code は出さない)。
    assert mb._jma_weather_text("299") == "くもり"
    assert mb._jma_weather_text("999") is None  # 分類不能は None (呼び側で扱う)
    assert mb._jma_weather_text(None) is None


def test_text_is_dry():
    assert mb._text_is_dry("晴れ") is True
    assert mb._text_is_dry("晴れのち時々くもり") is True
    assert mb._text_is_dry("雨") is False
    assert mb._text_is_dry("晴れ時々雨で雷") is False


# --------------------------------------------------------------------------
# 天気: 内部整合ガード
# --------------------------------------------------------------------------
def test_weather_dry_sky_with_high_pop_is_inconsistent():
    assert mb._weather_consistent("晴れ", 100) is False
    assert mb._weather_consistent("くもり", 90) is False


def test_weather_coherent_cases_pass():
    assert mb._weather_consistent("晴れのち時々くもり", 20) is True  # 乾き + 低 pop
    assert mb._weather_consistent("雨", 90) is True  # 雨 + 高 pop は整合
    assert mb._weather_consistent(None, 100) is True  # 天気不明なら判定しない


def test_format_weather_flags_contradiction_instead_of_lying():
    # 乾いた空(晴れ) + pop 100% は数値を伏せて「整合性エラー」を返す。
    out = mb._format_weather("有明", "100", 36, 27, 100)
    assert "整合性エラー" in out
    assert "36" not in out and "27" not in out  # 矛盾時は気温を出さない


def test_format_weather_normal_range():
    # 7/21 東京の実データ相当: 最高=予報36 / 最低=観測27 / 降水=予報max20。
    out = mb._format_weather("有明", "110", 36, 27, 20)
    assert out == "有明: 晴れのち時々くもり 27〜36℃ 降水20%"


def test_format_weather_rain_shows_pct():
    out = mb._format_weather("有明", "300", 24, 20, 90)
    assert out == "有明: 雨 20〜24℃ 降水90%"


def test_format_weather_missing_amedas_min_shows_max_only():
    # 観測(最低)欠損時は range を捏造せず「最高X℃」だけ。
    out = mb._format_weather("有明", "110", 36, None, 20)
    assert "最高36℃" in out and "〜" not in out


def test_format_weather_nothing_returns_none():
    assert mb._format_weather("有明", None, None, None, None) is None


# --------------------------------------------------------------------------
# 天気: 府県予報 JSON パーサ (純関数・当日値抽出)
# --------------------------------------------------------------------------
def test_parse_jma_forecast_extracts_today_values():
    # 実 JMA 構造の最小再現 (東京 2026-07-21, 05時発表)。temps は当日 min スロットが
    # max と同値の placeholder → 当日 max=36 を最高として採る。
    blocks = [
        {
            "reportDatetime": "2026-07-21T05:00:00+09:00",
            "timeSeries": [
                {
                    "timeDefines": [
                        "2026-07-21T05:00:00+09:00",
                        "2026-07-22T00:00:00+09:00",
                    ],
                    "areas": [
                        {"area": {"code": "130010"}, "weatherCodes": ["110", "101"]}
                    ],
                },
                {
                    "timeDefines": [
                        "2026-07-21T06:00:00+09:00",
                        "2026-07-21T12:00:00+09:00",
                        "2026-07-21T18:00:00+09:00",
                        "2026-07-22T00:00:00+09:00",
                    ],
                    "areas": [
                        {"area": {"code": "130010"}, "pops": ["10", "20", "20", "0"]}
                    ],
                },
                {
                    "timeDefines": [
                        "2026-07-21T09:00:00+09:00",
                        "2026-07-21T00:00:00+09:00",
                        "2026-07-22T00:00:00+09:00",
                        "2026-07-22T09:00:00+09:00",
                    ],
                    "areas": [
                        {"area": {"code": "44132"}, "temps": ["36", "36", "27", "36"]}
                    ],
                },
            ],
        }
    ]
    fc = mb._parse_jma_forecast(blocks, "130010", "44132", "2026-07-21")
    assert fc["code"] == "110"
    assert fc["text"] == "晴れのち時々くもり"
    assert fc["pop"] == 20  # 当日 06/12/18 = 10/20/20 の max、明日 00:00 の 0 は除外
    assert fc["tmax"] == 36  # 当日 temps の max (min placeholder に汚染されない)
    assert fc["report"] == "2026-07-21T05:00:00+09:00"


def test_parse_jma_forecast_empty_is_safe():
    fc = mb._parse_jma_forecast([], "130010", "44132", "2026-07-21")
    assert fc["code"] is None and fc["tmax"] is None and fc["pop"] is None


# --------------------------------------------------------------------------
# tribe: forward_status.txt パーサ + アダプタ
# --------------------------------------------------------------------------
_FWD_SAMPLE = (
    "Forward Go/No-Go: LIVE_READY=0 RUNNING=7 FAIL=0 BLOCKED_OOS=48 total=55 "
    "| forward evidence: min_days=30 min_trades=300 min_sharpe=0.70 min_pf=1.50 "
    "| probe telemetry: probe_count=0 probe_last_seen=N/A\n"
    "updated: 07/21 08:27 JST / 23:27 UTC reason: report\n"
)


def test_parse_forward_status_extracts_fields():
    p = mb._parse_forward_status(_FWD_SAMPLE)
    assert p is not None
    assert p["live_ready"] == 0
    assert p["running"] == 7
    assert p["blocked_oos"] == 48
    assert p["total"] == 55
    assert p["updated"].startswith("07/21 08:27")


def test_parse_forward_status_rejects_wrong_format():
    assert mb._parse_forward_status("nothing here") is None


def _write_forward(tmp_path: Path, text: str) -> Path:
    d = tmp_path / "data" / "reports"
    d.mkdir(parents=True, exist_ok=True)
    (d / "forward_status.txt").write_text(text, encoding="utf-8")
    return tmp_path


def test_adapter_tribe_reads_status_and_marks_deploy_none(tmp_path):
    root = _write_forward(tmp_path, _FWD_SAMPLE)
    st = mb.adapter_tribe(root, date(2026, 7, 21))
    assert st.available is True
    assert st.red is False
    assert "live 0(deploy無)" in st.status_line
    assert "total55" in st.status_line
    assert st.alerts == []  # LIVE_READY=0 は静か


def test_adapter_tribe_warns_when_live_ready_positive(tmp_path):
    text = _FWD_SAMPLE.replace("LIVE_READY=0", "LIVE_READY=2")
    root = _write_forward(tmp_path, text)
    st = mb.adapter_tribe(root, date(2026, 7, 21))
    assert st.available is True
    assert any("LIVE_READY=2" in a for a in st.alerts)
    assert "live 2" in st.status_line


def test_adapter_tribe_flags_stale_file(tmp_path):
    root = _write_forward(tmp_path, _FWD_SAMPLE)
    p = root / "data" / "reports" / "forward_status.txt"
    # ファイル mtime を 07/16 に(UTC epoch)。今日=07/21 で 5d 古 → stale を検証。
    epoch = (date(2026, 7, 16) - date(1970, 1, 1)).days * 86400 + 12 * 3600
    os.utime(p, (epoch, epoch))
    st = mb.adapter_tribe(root, date(2026, 7, 21))
    # 07/21 - 07/16 = 5d 古 → stale 注記
    assert "古" in st.status_line
    assert st.note and "未更新" in st.note


def test_adapter_tribe_missing_file_is_not_available(tmp_path):
    st = mb.adapter_tribe(tmp_path, date(2026, 7, 21))
    assert st.available is False
    assert "未検出" in st.note


# --------------------------------------------------------------------------
# 家族ボード: 沈黙
# --------------------------------------------------------------------------
def test_adapter_family_is_silent():
    st = mb.adapter_family()
    assert st.available is False
    assert st.note == ""  # 実体が無いうちは gaps にも出さない


def test_family_produces_no_line_in_brief():
    """build_brief で家族ボードが一切の行を生まない (PJ 行にも未取得注記にも)。"""
    fam = mb.adapter_family()
    tribe = mb.ProjectStatus(key="tribe", label="tribe")
    tribe.available = True
    tribe.status_line = "live 0(deploy無) · forward RUN7/blkOOS48/total55"
    # 天気は成功文字列を渡して天気 gap を切り離し、家族由来の行だけを検証。
    _, body, _, _, _ = mb.build_brief(
        [tribe, fam], "有明: 晴れ", date(2026, 7, 21), None
    )
    assert "家族ボード" not in body
    assert "未取得" not in body  # 家族の gap 注記が消えている
