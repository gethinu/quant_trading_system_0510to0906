# Dashboard publish 恒久修正 (2026-07-30)

`scripts/publish_data_to_vercel.ps1` の publish 取りこぼし（2 日連続で served=前日、
手動 `-AutoLatest` 運用、`git commit` が `.git/index.lock: File exists` で exit=128）
を恒久修正した記録。

## 根因

- このリポの **唯一の git writer は publish_data_to_vercel.ps1**（grep 済: 他の
  scheduled `.ps1` は git add/commit/push を直接叩かない）。それが 2 系統から呼ばれる:
  06:00 `daily_pipeline.ps1` step6 と ~08:00 `morning_brief.ps1 -AutoLatest`。
- scheduler が `-RestartCount 2 / -RestartInterval 15min / -StartWhenAvailable`
  のため、遅延・ハングした 06:00 run が再起動され self-heal と重なると、共有
  `.git/index` に 2 本の `git add`/`commit` が競合し片方が `.git/index.lock` を残す。
  crash / host sleep / 2h ExecutionTimeLimit 打ち切りでも lock は残る（0-byte lock を確認）。
- 旧実装は **`git add -A` の exit code を検査していなかった**。lock で add が落ちても
  素通りし、直後の `git diff --cached --quiet` が「staged 差分なし」と判定して
  `data/ に差分なし。commit/push をスキップ` で **exit 0 の偽成功**を返していた
  （＝ntfy は新しいのに dashboard 凍結）。lock が commit まで残った日は `commit exit=128`
  として顕在化。両症状は同じ根因の裏表。

## 対策（既存挙動を維持しつつ堅牢化）

- **A. 二重起動ガード**: named Mutex `Global\qts_publish_data_vercel` で publish 同士を
  直列化。他 instance が publish 中なら最大 90s 待ち、なお継続中なら静かに exit 0。
- **B. 専用 index**: `GIT_INDEX_FILE`（`.git/index.publish`）を HEAD から seed し
  `data/` のみ stage→commit。日次 pipeline の `.git/index` とロックを共有せず、
  作業ツリーの無関係 dirty も commit に混ざらない。
- **C. stale lock 除去 + retry**: `git.exe` 不在 かつ mtime が `StaleLockSeconds`（既定
  300s）超の `.git/index.lock` / `HEAD.lock` / `<private>.lock` のみ crash 残骸として除去。
  全 git step を `Invoke-GitRetry`（retry + 除去）で包む。
- **D. exit code 検査**: add/commit/read-tree の失敗を必ず検出し非ゼロ終了。偽の
  「差分なし skip」を出さない。
- **E. publish 後 verify**: served（HEAD にコミット済 `data/` の最新 `today_signals`）
  == generated（`results_csv` の最新）かつ origin 反映済を検証。ズレたら非ゼロ終了 +
  ntfy WARN。**成功時は静か**（通知しない）。
- `-AutoLatest` の既存挙動・push rebase self-heal・`git rm`/`git add -A`/KeepDays=7 の
  purge 契約は維持（`tests/system/test_publish_data_ps1_contract.py` 通過）。

## Alpaca キー（human task #9）— 1 行メモ

`build_exit_ledger.py` / `export_alpaca_snapshot.py` は Alpaca キー未設定だと exit=1 に
なり得るが、これは `RefreshAccount` ブロックで **WARN 継続**であり publish の成否判定と
切り離されている。後段 verify は `today_signals` のみを見るので `alpaca_snapshot` 欠落で
publish が失敗扱いになることはない。キー設定自体は human task #9 側の課題。

## ミニPC での確認（実走はユーザー手動）

Windows PowerShell (user stair) で:

```powershell
cd C:\Repos\quant_trading_system_0510to0906
# 1) dry-run（push しない・commit まで）で verify が通るか
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_data_to_vercel.ps1 -AutoLatest -NoPush
# 2) 本番（push まで）。served==generated を自己検証し、失敗時のみ ntfy
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_data_to_vercel.ps1 -AutoLatest
# 3) 冪等確認: もう一度叩いて「差分なし」→ exit 0（静か）になること
powershell -NoProfile -ExecutionPolicy Bypass -File scripts\publish_data_to_vercel.ps1 -AutoLatest
# 4) 突合（served == generated）を独立に確認
python scripts\check_dashboard_freshness.py --json
```
