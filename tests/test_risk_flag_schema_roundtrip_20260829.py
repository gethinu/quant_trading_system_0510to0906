"""risk.* の設定キーが YAML から本当に届くかの回帰テスト (2026-08-29)。

背景: `config/schemas.py` の `RiskModel` に宣言されていないキーは
`AppConfigModel.model_dump()` で **黙って落ちる**。設定ローダ
(`config/settings.py::_load_yaml_config_validated`) は model_dump() の結果を
そのまま `_build_risk_config` に渡すので、未宣言キーは YAML に書いても既定値へ
戻る = *YAML から設定できない* という無警告の drift になる。env override だけが
効くので「フラグは存在するのに YAML で有効化できない」状態に気付けない。

実際 `risk.fair_pool_trim` (c371c34) がこれで落ちており、`FAIR_POOL_TRIM` env
でしか有効化できなかった。schema へ宣言を足して修正 (既定値は据え置き)。

このファイルは **キー名をハードコードしない**。shipped `config/config.yaml` の
risk セクションを読んで、そこに在る全キーを対象に検査する。新しいフラグを
YAML と settings.py にだけ足して schema を忘れたら、追加のテストを書かなくても
ここが落ちる。守るのは 3 つ:

  1. **落ちない** — risk.* / risk.portfolio.* の全キーが model_dump() を生き延びる。
  2. **届く** — 各キーは YAML → get_settings() の経路で非既定値が実際に届く。
  3. **既定は変わらない** — YAML 上書きも env も無いとき、ゲートは全て OFF。
     schema への宣言追加が挙動を変えていないことの担保。
"""

from __future__ import annotations

from pathlib import Path
import sys

import pytest
import yaml

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from config.schemas import (  # noqa: E402
    PortfolioRiskModel,
    RiskModel,
    validate_config_dict,
)
from config.settings import RiskConfig, get_settings  # noqa: E402

SHIPPED_CONFIG = ROOT / "config" / "config.yaml"


def _shipped_risk() -> dict:
    with open(SHIPPED_CONFIG, encoding="utf-8") as f:
        return (yaml.safe_load(f) or {}).get("risk", {}) or {}


SHIPPED_RISK = _shipped_risk()
# portfolio はネストなので別扱い。
RISK_KEYS = sorted(k for k in SHIPPED_RISK if k != "portfolio")
PORTFOLIO_KEYS = sorted(_shipped_risk().get("portfolio") or {})
# 既定 OFF であるべき boolean ゲート (= shipped config が false にしているもの)。
BOOLEAN_GATES = sorted(k for k in RISK_KEYS if SHIPPED_RISK[k] is False)

# YAML → settings の経路を汚しうる env override。テスト中は必ず外す。
# (運用 .env が load_dotenv でプロセスへ漏れるため、明示的に落とす必要がある)
FLAG_ENV_VARS = (
    "RISK_PCT",
    "MAX_POSITIONS",
    "MAX_PCT",
    "SLOTS_FROM_CAPITAL",
    "FAIR_POOL_TRIM",
    "EXCLUDE_ORPHANS_FROM_SLOTS",
)


def _probe_value(key: str, current):
    """`current` と必ず異なる、かつ schema の bounds 内に収まる値を返す。

    bool は反転。数値は半分 / +1 にする — risk_pct(0.02) max_pct(0.10)
    gross_budget_factor(1.0) はいずれも (0, 1) 系の上限なので半分は安全側、
    件数系 int は +1 が安全側。
    """
    if isinstance(current, bool):
        return not current
    if isinstance(current, int):
        return current + 1
    if isinstance(current, float):
        return current / 2.0
    raise AssertionError(f"risk.{key}: 想定外の型 {type(current)!r} — テスト要更新")


@pytest.fixture
def clean_env(monkeypatch):
    """運用 .env 由来の override を外し、get_settings の lru_cache を落とす。"""
    for name in FLAG_ENV_VARS:
        monkeypatch.delenv(name, raising=False)
    # config.json が拾われると YAML が読まれない。存在しないパスへ固定する。
    monkeypatch.setenv("APP_CONFIG_JSON", str(ROOT / "config" / "__absent__.json"))
    get_settings.cache_clear()
    yield monkeypatch
    get_settings.cache_clear()


def _settings_risk_from_yaml(monkeypatch, tmp_path: Path, risk: dict):
    """risk セクションを差し替えた YAML を書いて settings.risk を解決する。"""
    cfg = tmp_path / "config.yaml"
    # backtest の日付は必ず quote する。unquoted だと yaml が datetime.date にして
    # AppConfigModel の検証が例外になり、settings が生 dict へフォールバックする
    # (= model_dump() を通らないので、このテストが何も検証しなくなる)。
    cfg.write_text(
        yaml.safe_dump({"risk": risk}, allow_unicode=True, sort_keys=False)
        + 'backtest:\n  start_date: "2018-01-01"\n  end_date: "2024-12-31"\n',
        encoding="utf-8",
    )
    monkeypatch.setenv("APP_CONFIG", str(cfg))
    get_settings.cache_clear()
    return get_settings(create_dirs=False).risk


def test_shipped_risk_section_is_not_empty():
    """土台の確認 — config.yaml が読めていなければ以下は全部空回りする。"""
    assert RISK_KEYS, "config/config.yaml の risk セクションが読めていない"
    assert PORTFOLIO_KEYS, "risk.portfolio セクションが読めていない"


# --- 1. 落ちない -----------------------------------------------------------


def test_every_shipped_risk_key_is_declared_in_schema():
    """config.yaml の risk.* 全キーが RiskModel に宣言されている。

    未宣言キーは model_dump() で消える = YAML から設定できない。新しいキーを
    schema に足し忘れたら、このテストがそのキー名を挙げて落ちる。
    """
    missing = sorted(k for k in RISK_KEYS if k not in RiskModel.model_fields)
    assert missing == [], (
        f"risk.* のキーが config/schemas.py::RiskModel に未宣言: {missing}. "
        "model_dump() で落ちるため YAML から設定できません "
        "(env override でしか効かない状態)。"
    )


def test_every_shipped_portfolio_key_is_declared_in_schema():
    missing = sorted(
        k for k in PORTFOLIO_KEYS if k not in PortfolioRiskModel.model_fields
    )
    assert (
        missing == []
    ), f"risk.portfolio.* のキーが PortfolioRiskModel に未宣言: {missing}"


def test_shipped_config_survives_model_dump_without_losing_keys():
    """shipped config を検証 → model_dump しても risk 配下のキーが減らない。"""
    dumped = validate_config_dict({"risk": SHIPPED_RISK}).model_dump()["risk"]
    lost = sorted(k for k in SHIPPED_RISK if k not in dumped)
    assert lost == [], f"model_dump() が risk.* を落とした: {lost}"

    pf_out = dumped.get("portfolio") or {}
    lost_pf = sorted(k for k in PORTFOLIO_KEYS if k not in pf_out)
    assert lost_pf == [], f"model_dump() が risk.portfolio.* を落とした: {lost_pf}"


@pytest.mark.parametrize("key", RISK_KEYS)
def test_key_roundtrips_through_model_dump(key):
    """各キーが非既定値のまま model_dump() を往復する。"""
    value = _probe_value(key, SHIPPED_RISK[key])
    dumped = validate_config_dict({"risk": {key: value}}).model_dump()["risk"]
    assert key in dumped, f"risk.{key} が model_dump() で落ちた (schema 未宣言)"
    assert dumped[key] == value


# --- 2. 届く ---------------------------------------------------------------


@pytest.mark.parametrize("key", RISK_KEYS)
def test_yaml_value_reaches_settings(clean_env, tmp_path, key):
    """YAML の非既定値が get_settings().risk まで届く (env override 無し)。"""
    shipped = SHIPPED_RISK[key]
    value = _probe_value(key, shipped)
    risk = _settings_risk_from_yaml(clean_env, tmp_path, {key: value})
    got = getattr(risk, key)
    assert got == value, (
        f"YAML の risk.{key}={value!r} が settings に届かず {got!r} のまま "
        f"(shipped 既定 {shipped!r})。config/schemas.py::RiskModel の宣言漏れの疑い。"
    )


@pytest.mark.parametrize(
    "key,env_name",
    [
        ("fair_pool_trim", "FAIR_POOL_TRIM"),
        ("exclude_orphans_from_slots", "EXCLUDE_ORPHANS_FROM_SLOTS"),
        ("slots_from_capital", "SLOTS_FROM_CAPITAL"),
    ],
)
def test_env_override_still_wins_over_yaml(clean_env, tmp_path, key, env_name):
    """env は YAML より強い、という運用の非常口が壊れていない (両方向)。"""
    if key not in RISK_KEYS:
        pytest.skip(f"risk.{key} はこの branch の config.yaml に無い")

    clean_env.setenv(env_name, "1")
    risk = _settings_risk_from_yaml(clean_env, tmp_path, {key: False})
    assert getattr(risk, key) is True, f"{env_name}=1 が YAML false を上書きできない"

    # 逆向き (YAML ON を env で殺す) も効かないと非常口として機能しない。
    clean_env.setenv(env_name, "0")
    risk = _settings_risk_from_yaml(clean_env, tmp_path, {key: True})
    assert getattr(risk, key) is False, f"{env_name}=0 が YAML true を殺せない"


# --- 3. 既定は変わらない ----------------------------------------------------


@pytest.mark.parametrize("key", BOOLEAN_GATES)
def test_boolean_gates_default_off_in_schema(key):
    """schema の既定が OFF (config.yaml の false と一致)。"""
    assert RiskModel().model_dump()[key] is False


def test_shipped_settings_leave_all_gates_off(clean_env):
    """YAML 上書きも env も無いとき、実際の settings でゲートは全て OFF。

    schema へ宣言を足したことが何も有効化していない、という担保。
    """
    clean_env.delenv("APP_CONFIG", raising=False)
    get_settings.cache_clear()
    risk = get_settings(create_dirs=False).risk
    for key in BOOLEAN_GATES:
        assert getattr(risk, key) is False, f"risk.{key} が既定で ON になっている"


@pytest.mark.parametrize("key", RISK_KEYS)
def test_shipped_config_value_is_what_settings_resolves(clean_env, key):
    """env 無しでの settings の値 == config.yaml に書かれている値。

    宣言追加で既定値がずれていないことを、キーごとに突き合わせる。
    """
    clean_env.delenv("APP_CONFIG", raising=False)
    get_settings.cache_clear()
    risk = get_settings(create_dirs=False).risk
    assert getattr(risk, key) == SHIPPED_RISK[key], (
        f"risk.{key}: settings={getattr(risk, key)!r} が "
        f"config.yaml={SHIPPED_RISK[key]!r} と食い違う"
    )


@pytest.mark.parametrize("key", RISK_KEYS)
def test_schema_default_matches_settings_dataclass_default(key):
    """RiskModel と RiskConfig の既定値が一致 (片方だけ動くのを防ぐ)。"""
    schema_default = RiskModel().model_dump()[key]
    dataclass_default = getattr(RiskConfig(), key)
    assert schema_default == dataclass_default, (
        f"risk.{key}: RiskModel 既定={schema_default!r} と "
        f"RiskConfig 既定={dataclass_default!r} がずれている"
    )
