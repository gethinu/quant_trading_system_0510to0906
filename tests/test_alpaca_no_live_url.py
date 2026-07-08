"""live Alpaca endpoint の直参照が code 側に紛れ込まないことの regression test.

`api.alpaca.markets` (live) は絶対に paper 経路の code に現れてはならない。
`paper-api.alpaca.markets` (paper) のみ許可。

allowlist:
    - このテスト自体 (guard の負例で live URL 文字列を含む)
    - `tests/test_alpaca_trading_mock.py` (assert_paper_env が live URL で raise することの検証)
    - `docs/**` (説明・checklist)
    - Vercel dashboard JSON snapshots (`apps/dashboards/*/data/*.json`) — 過去
      pipeline 出力の literal で code 経路ではない

fail した場合: 該当ファイルを paper-api.* に置き換えるか、意図的な負例なら
`ALLOWED_HITS` に絶対パスを追加する (レビュー要)。
"""

from __future__ import annotations

from pathlib import Path
import re

REPO_ROOT = Path(__file__).resolve().parents[1]

# live 直参照が許容されるファイル (guard のテスト等)
ALLOWED_HITS: set[str] = {
    # このテストファイル自身
    "tests/test_alpaca_no_live_url.py",
    # assert_paper_env が live URL で例外化することの negative test
    "tests/test_alpaca_trading_mock.py",
    # paper 強制の new regression test (guard の match 対象文字列を含む)
    "tests/test_alpaca_paper_only_enforce.py",
}

# 除外ディレクトリ
EXCLUDED_DIRS = {
    ".git",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    "dist",
    "build",
    "logs",
    "data_cache",
    "data_cache_recent",
    ".ipynb_checkpoints",
    "memory",
}

# scan 対象拡張子
CODE_EXTS = {".py", ".ps1", ".psm1", ".sh", ".bat"}

# `api.alpaca.markets` を含みつつ `paper-api.alpaca.markets` "ではない" 参照
LIVE_URL_PATTERN = re.compile(r"(?<!paper-)api\.alpaca\.markets")


def _iter_code_files():
    for path in REPO_ROOT.rglob("*"):
        if not path.is_file():
            continue
        # 除外 dir
        rel_parts = path.relative_to(REPO_ROOT).parts
        if any(p in EXCLUDED_DIRS for p in rel_parts):
            continue
        if path.suffix.lower() not in CODE_EXTS:
            continue
        yield path


def test_no_live_alpaca_url_in_code():
    """コード (py/ps1) 中の api.alpaca.markets (live) 直参照は 0 でなければならない。"""
    hits: list[tuple[str, int, str]] = []
    for path in _iter_code_files():
        rel = str(path.relative_to(REPO_ROOT)).replace("\\", "/")
        if rel in ALLOWED_HITS:
            continue
        try:
            text = path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for i, line in enumerate(text.splitlines(), 1):
            if LIVE_URL_PATTERN.search(line):
                hits.append((rel, i, line.strip()))

    assert not hits, (
        "live Alpaca URL (api.alpaca.markets) がコードに紛れ込んでいます:\n"
        + "\n".join(f"  {r}:{ln}: {txt}" for r, ln, txt in hits)
        + "\n→ paper-api.alpaca.markets に置換するか、意図的な負例なら "
        "tests/test_alpaca_no_live_url.py の ALLOWED_HITS に追加してください。"
    )
