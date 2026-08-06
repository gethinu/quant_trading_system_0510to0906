"""
スナップショット自動生成ツール

コア変更時に自動でスナップショットを生成し、差分を検出。

使い方:
    python tools/auto_snapshot.py
    python tools/auto_snapshot.py --skip-comparison  # 比較スキップ
"""

from datetime import datetime
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Optional

# --- Windows console (cp932) safety -------------------------------------
# Force UTF-8 on stdout/stderr so emoji / non-ASCII output does not raise
# UnicodeEncodeError under Windows' default cp932 code page when this hook
# runs at pre-push. reconfigure() exists on Python 3.7+; guard for streams
# that don't support it.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass
# ------------------------------------------------------------------------

SNAPSHOT_DIR = Path("snapshots")


def run_mini_test() -> bool:
    """ミニテスト実行"""
    print("🔍 Running mini test to generate snapshot...")

    result = subprocess.run(
        [
            sys.executable,
            "scripts/run_all_systems_today.py",
            "--test-mode",
            "mini",
            "--skip-external",
            "--save-csv",
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    if result.returncode != 0:
        print("❌ Test run failed:")
        print(result.stderr)
        return False

    print("✅ Test run completed")
    return True


def save_snapshot() -> Path:
    """スナップショットを保存"""
    snapshot_name = f"auto_{datetime.now():%Y%m%d_%H%M%S}"
    snapshot_path = SNAPSHOT_DIR / snapshot_name
    snapshot_path.mkdir(parents=True, exist_ok=True)

    # 結果CSVをコピー
    test_results = Path("results_csv_test")

    if not test_results.exists():
        print("⚠️  No test results found")
        return snapshot_path

    copied_files = 0
    for csv in test_results.glob("*.csv"):
        shutil.copy(csv, snapshot_path / csv.name)
        copied_files += 1

    # ベンチマークJSONもコピー
    for json_file in test_results.glob("benchmark_*.json"):
        shutil.copy(json_file, snapshot_path / json_file.name)
        copied_files += 1

    print(f"📸 Snapshot saved: {snapshot_path} ({copied_files} files)")

    # メタデータ保存
    import json

    try:
        git_commit = (
            subprocess.check_output(
                ["git", "rev-parse", "HEAD"], stderr=subprocess.DEVNULL
            )
            .decode()
            .strip()
        )
    except Exception:
        git_commit = "unknown"

    metadata = {
        "timestamp": datetime.now().isoformat(),
        "git_commit": git_commit,
        "file_count": copied_files,
    }

    with open(snapshot_path / "metadata.json", "w", encoding="utf-8") as f:
        json.dump(metadata, f, indent=2, ensure_ascii=False)

    return snapshot_path


def get_previous_snapshot() -> Optional[Path]:
    """前回のスナップショットを取得"""
    if not SNAPSHOT_DIR.exists():
        return None

    snapshots = sorted(SNAPSHOT_DIR.glob("auto_*"))

    if len(snapshots) >= 2:
        return snapshots[-2]  # 2番目に新しい（最新の一つ前）

    return None


def compare_snapshots(current: Path, previous: Path) -> bool:
    """スナップショットを比較"""
    print("\n🔍 Comparing with previous snapshot:")
    print(f"   Previous: {previous.name}")
    print(f"   Current:  {current.name}\n")

    result = subprocess.run(
        [
            sys.executable,
            "tools/compare_snapshots.py",
            "--baseline",
            str(previous),
            "--current",
            str(current),
            "--threshold",
            "0.01",  # 1%未満の差分は許容
        ],
        capture_output=True,
        text=True,
        encoding="utf-8",
    )

    print(result.stdout)

    if result.returncode != 0:
        print("\n⚠️  Snapshot differences detected!")
        print(f"📊 Review the diff report: {current}/diff_report.json")
        return False

    return True


def main():
    """メイン処理"""
    import argparse

    parser = argparse.ArgumentParser(description="スナップショット自動生成")
    parser.add_argument(
        "--skip-comparison",
        action="store_true",
        help="前回スナップショットとの比較をスキップ",
    )
    parser.add_argument(
        "--skip-interactive", action="store_true", help="対話的確認をスキップ"
    )
    args = parser.parse_args()

    # ミニテスト実行
    if not run_mini_test():
        return 1

    # スナップショット保存
    current_snapshot = save_snapshot()

    # 前回スナップショットと比較
    if not args.skip_comparison:
        previous_snapshot = get_previous_snapshot()

        if previous_snapshot:
            no_diff = compare_snapshots(current_snapshot, previous_snapshot)

            if not no_diff and not args.skip_interactive:
                print("\n差分が検出されました。")
                response = input("Continue with commit? (y/N): ")
                if response.lower() != "y":
                    print("❌ Aborted by user")
                    return 1
        else:
            print("ℹ️  No previous snapshot for comparison")
            print("   This snapshot will be used as baseline.")

    print(f"\n✅ Snapshot process completed: {current_snapshot}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
