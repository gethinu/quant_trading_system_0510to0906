# Exit ステージ「未計測」逆戻り — 根因と着地手順 (2026-07-30)

> read-only 診断済み / paper・表示のみ / 発注なし。
> 07-30 の pipeline / dashboard data は本セッションで recon から再 patch 済み（下記「即時ヒール」）。
> 恒久解は「ミニPCで流す確定コマンド列」を実行して初めて完了する。

## 結論（3行）
1. exit 漏斗の配線 (`patch_pipeline_exit` / `_wire_pipeline_exit` / fired-armed 分離) は
   **`claude/monitor-webapp` にしか無い**。`main` にも `origin/main` にも、そして
   **prod が実行するブランチ (`claude/daily-main-follow`, `claude/open-auto-run`) にも未着地**。
2. prod のパイプライン生成/publish worktree はこれらのブランチを **working-tree 直読 (pull なし)** で
   回すため、`patch_pipeline_exit` が一度も呼ばれず Exit=null → 毎日「未計測」に戻る。
3. 07-30 は **recon 有り**（exit_close=10 / protect=2、fired=0）だが **pipeline は measured=False**。
   ＝「recon はあるのに pipeline に反映されない」既存乖離の再発。

---

## 証拠 A: exit 配線は main に無い（git）

| ref | patch_pipeline_exit in publish_execution_summary / build_execution_recon / daily_polygon_monitor | da22f16 (wire) / 538c39b (fired-armed) を含む |
|---|---|---|
| `origin/main` (55a8545 #151) | 0 / 0 / 0 | **NO** |
| `main` (local d407068) | 0 / 0 / 0 | **NO** |
| `claude/daily-main-follow` (0831226) ＝ **06:00 coverage worktree** `C:\tmp\qts-daily-main` | 0 / 0 / 0 | **NO** |
| `claude/open-auto-run` (0c44623) ＝ **22:35 open runner worktree** `C:\tmp\qts-main-run` | 0 / 0 / 0 | **NO** |
| `claude/monitor-webapp` (18ab05d) ＝ dev / Vercel push 先 | あり | **YES** |

`da22f16` `538c39b` は `origin/claude/monitor-webapp` にのみ存在（push 済み）。
つまり「昨日 main に着地させた」は成立しておらず、実際には monitor-webapp 上のコミットで
止まっている。07-29 のダッシュに Exit が出たのは、`da22f16` が
`apps/.../data/pipeline_20260729.json`（当日ぶん）をコミットに同梱していたため。
自動 prod 経路には配線が無いので **翌日 07-30 は元に戻った**。

## 証拠 B: prod のパイプライン生成経路（どこで patch が呼ばれていないか）

- `pipeline_YYYYMMDD.json` を生成するのは `scripts/daily_polygon_monitor.py::build_pipeline_report`。
  monitor-webapp 版は「build 時に同日 recon があれば opportunistic に patch」する。
  だが **prod の coverage worktree (`C:\tmp\qts-daily-main` = `claude/daily-main-follow`) の
  daily_polygon_monitor.py には patch_pipeline_exit が無い** → 生成時に Exit=null 固定。
- 実発注後に patch し直すのは `scripts/publish_execution_summary.py::_wire_pipeline_exit`
  （daily_pipeline Step5d / open_auto_run notify）。だが **prod の open runner
  (`C:\tmp\qts-main-run` = `claude/open-auto-run`) の publish_execution_summary.py にも
  patch_pipeline_exit が無い** → notify でも埋まらない。
- ＝ prod の両生成経路とも patch を持たない。配線は monitor-webapp に隔離されている。

## 証拠 C: 07-30 実データ突合

- `results_csv/recon_20260730.json` : **有り**。`inputs.exit_orders=True`、
  portfolio `exit_submitted=0 / exit_close=10 / exit_protect=2`。
- `results_csv/pipeline_20260730.json` および dashboard 配信コピー
  `apps/dashboards/alpaca-next/data/pipeline_20260730.json` : 全 system の Exit が
  `count=null, measured=False, fired=null, armed=null` ＝「未計測」。
- 現行（monitor-webapp）の `patch_pipeline_exit` を 07-30 実データに当てると
  `status=ok, n_filled=7, measured=True`。コード自体は正しく機能する
  → **バグではなく配線（着地）の問題**。
- 07-30 の exit は当日寄り付き前で全て **armed（未発火の待機注文）**:
  fired=0、armed=12（system2 = close 10 armed、system1 = protect 2 armed）。
  旧 recon（prod 側 build）は armed 分離を持たず close10/protect2 と記録していた。

## 補足の複合要因: stale `.git/index.lock`
`C:\Repos\...\.git\index.lock`（0 byte, 07-30 08:07）が残っており、08:00 の
morning_brief self-heal は `git add` が lock で落ち「data/ に差分なし → commit/push skip」で
**偽成功 (exit 0)** になっていた（＝ Exit を patch しても push が届かない二次障害）。
着地前にこの lock を除去する必要がある。
（作業ツリーには hardening 版 `publish_data_to_vercel.ps1`＝Mutex 直列化 + stale-lock 除去 が
未コミットで存在。exit スコープ外なので本手順では触れない。別途 land を推奨。）

---

## 即時ヒール（本セッションで実施済み）
`results_csv/recon_20260730.json` を現行コードで再ビルド（fired/armed 分離）し、
`results_csv/pipeline_20260730.json` と dashboard コピー
`apps/dashboards/alpaca-next/data/pipeline_20260730.json` を patch 済み。
結果: 全 system `measured=True`、system1 `2 armed`、system2 `10 armed`、他 0。
（元ファイルは `*.pre_exit_heal.bak` にバックアップ）
→ あとは **origin/claude/monitor-webapp に push すればダッシュに即反映**（Vercel push 先）。

---

## ミニPCで流す確定コマンド列

> PowerShell。ブランチ運用は既存に合わせる。すべて paper / 表示のみ、発注しない。
> `<sha>` = `da22f16d`（wire）, `538c39b2`（fired-armed 分離）。

### 0) stale lock 除去（前提。これが無いと push が偽成功で凍結する）
```powershell
Get-Process git -ErrorAction SilentlyContinue      # git.exe が動いていないことを確認
Remove-Item C:\Repos\quant_trading_system_0510to0906\.git\index.lock -ErrorAction SilentlyContinue
```

### 1) 即時: ヒール済み 07-30 Exit をダッシュへ push（Vercel = origin/claude/monitor-webapp）
```powershell
cd C:\Repos\quant_trading_system_0510to0906
git add -- apps/dashboards/alpaca-next/data/pipeline_20260730.json   # ← このファイルだけ stage（他の未コミット WIP は触らない）
git commit -m "fix(dashboard): 07-30 Exit funnel を recon から再patch (measured=True, armed 12)"
git push origin claude/monitor-webapp
```
→ 数十秒でダッシュボードの Exit ステージが「未計測」→ 実数（armed 12）に切り替わる。

### 2) 恒久着地: main へ配線を cherry-pick（→ daily-main-follow に流れる本命）
```powershell
cd C:\Repos\quant_trading_system_0510to0906
git fetch origin
git switch main
git pull --ff-only origin main
git cherry-pick da22f16d 538c39b2
#   競合したら: exit 配線を残す方向で解決 → git add <file> → git cherry-pick --continue
git push origin main
git switch claude/monitor-webapp
```

### 3) 恒久着地: open-auto-run（22:35 runner）にも配線
```powershell
git switch claude/open-auto-run
git pull --ff-only origin claude/open-auto-run
git cherry-pick da22f16d 538c39b2
git push origin claude/open-auto-run
git switch claude/monitor-webapp
```
cherry-pick が競合する場合のフォールバック（exit 関連ファイルのみ差し替え。ただし
daily_polygon_monitor.py は monitor-webapp 側の周辺変更も入りうる点に留意）:
```powershell
git switch claude/open-auto-run
git checkout origin/claude/monitor-webapp -- `
  scripts/build_execution_recon.py `
  scripts/publish_execution_summary.py `
  common/publishers/execution_summary.py `
  tests/test_pipeline_exit_wiring_20260729.py
git commit -m "fix(exit): wire pipeline Exit patch onto open-auto-run (from monitor-webapp)"
git push origin claude/open-auto-run
```

### 4) prod worktree を配線後の HEAD に前進（working-tree 直読なので必須）
```powershell
git -C C:\tmp\qts-daily-main fetch origin
git -C C:\tmp\qts-daily-main merge origin/main            # daily-main-follow は main を取り込む
git -C C:\tmp\qts-main-run  fetch origin
git -C C:\tmp\qts-main-run  merge --ff-only origin/claude/open-auto-run
```

### 5) 検証（次回 auto-run を待たず 07-30 を配線後経路で再生成; dry-run=ntfy 送信なし）
```powershell
cd C:\tmp\qts-daily-main
Remove-Item results_csv\recon_20260730.json -ErrorAction SilentlyContinue   # 再ビルド強制
python scripts\publish_execution_summary.py --date 2026-07-30 --dry-run       # recon 再build→pipeline patch→書き戻し
python -c "import json;d=json.load(open(r'results_csv/pipeline_20260730.json',encoding='utf-8'));print([(k,[p for p in v['phases'] if p['name']=='Exit'][0]['measured']) for k,v in d['systems'].items()])"
powershell -File C:\Repos\quant_trading_system_0510to0906\scripts\publish_data_to_vercel.ps1 -Date 2026-07-30 -AutoLatest
```
期待: `measured=True` が全 system で出て、push 後ダッシュに Exit 実数が出る。

## 恒久性チェック（毎日戻らないための確認）
翌営業日の朝、`apps/dashboards/alpaca-next/data/pipeline_<当日>.json` の Exit が
`measured=True` で生成されていれば着地成功。もし再び未計測なら、
`git -C C:\tmp\qts-daily-main log --oneline -3` と
`git show HEAD:scripts/daily_polygon_monitor.py | Select-String patch_pipeline_exit`
で worktree が配線後 HEAD に居るかを確認する（worktree の前進漏れが最頻の再発原因）。

## 既知の隣接課題（本 exit スコープ外・別途）
- open_auto_run の段順が `publish_data_to_vercel`（copy）→ `publish_execution_summary`（patch）で
  逆。open 経路単独では patch が copy に間に合わない。daily_pipeline は Step5d→Step6 で正順。
  open runner の段順修正は別 PR 推奨。
- `publish_data_to_vercel.ps1` hardening（Mutex 直列化 + stale-lock 除去 + exit-code 検査）が
  作業ツリー未コミット。index.lock 偽成功の恒久対策として別途 land 推奨。
