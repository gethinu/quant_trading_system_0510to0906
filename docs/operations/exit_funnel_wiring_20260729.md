# Exit ステージ「未計測」→ 実数配線 (2026-07-29)

## 症状
ダッシュボード漏斗 `Tgt→FILpass→STUpass→TRDlist→Entry→Exit` の最終段 **Exit
(本日手仕舞い発火数)** が常に「未計測 / prev — · univ —」のまま。計測バグではなく
**表示への配線漏れ**。証拠: ntfy には exit が数値で出ている
(`exit 1 (close 0 / protect 1)` 等) ため、close/protect 別の集計ソースは既に存在した。

## 根本原因 (配線漏れの所在)
- 漏斗 JSON (`pipeline_YYYYMMDD.json`, schema `signal_pipeline/v1`) は
  `scripts/daily_polygon_monitor.py::build_pipeline_report` が生成。Exit の count は
  `today_signals_*.json` の per-system `funnel.exit_count` から引くが、この値は
  **常に null**（signal engine は exit を測らない。exit は後段で執行される）。
- そのため Exit phase は `count: null` → frontend `PipelineSection.tsx` が
  `未計測` を表示していた（`apps/dashboards/alpaca-next/components/PipelineSection.tsx`）。
- 一方、exit 実績は Step5c/5d で **recon** 化されている:
  `scripts/build_execution_recon.py::build_recon` が `exit_orders_*.json` から
  per-system `exit={submitted, close, protect}` と portfolio `exit_submitted/
  exit_close/exit_protect` を集計。ntfy 本文
  (`common/publishers/execution_summary.py::build_body`) はこの recon を single source に
  `exit {submitted} (close C / protect P)` を出している。

## ntfy が使う exit 集計ソース = recon
`recon_YYYYMMDD.json`。ntfy 見出しの exit 数 = `portfolio.exit_submitted`、内訳 =
`exit_close`/`exit_protect`、per-system は `systems[systemN].exit.{submitted,close,protect}`。

## 配線 (この変更)
Exit を **ntfy と同一の recon** から埋める。ソースを 1 本化したので原理的に乖離しない。

1. `scripts/build_execution_recon.py`
   - `exit_counts_from_recon(recon)` : recon → `{"sysN": {submitted, close, protect}}`
     ("systemN"→"sysN" 正規化)。
   - `patch_pipeline_exit(pipeline, recon)` : 各 system の Exit phase を
     `count = exit.submitted`（= ntfy 見出しと同じ定義）で埋め、condition 末尾に
     `(close C / protect P)` を併記。`measured=True`、`ratio_of_prev = exit/Entry`、
     `ratio_of_universe = exit/Tgt` を再計算。**idempotent**。
   - 正直さ: recon が無い → 何もしない（未計測を維持）。`inputs.exit_orders` が無い
     部分 recon → 未計測を維持（0 で誤魔化さない）。recon はあるが該当 system に
     exit 無し → 0（「発火しなかった」事実）。
2. `scripts/publish_execution_summary.py` (Step5d, ntfy と同一 instant)
   - recon 確定後に `results_csv/pipeline_YYYYMMDD.json` を上記で patch して書き戻す。
     Step6 `publish_data_to_vercel.ps1` が `data/` へ copy → ダッシュに反映。
     dry-run でも書く（実送信有無と独立にダッシュへ反映）。
3. `scripts/daily_polygon_monitor.py::build_pipeline_report`
   - build 時に同日 recon が既にあれば opportunistic に同じ helper で Exit を埋める
     （再実行時の即時反映）。first-run は Step5d が上書きするので、どちらの経路でも
     同一 recon に揃う。

## 実データ検証 (2026-07-29)
### 3者一致 (ntfy / 漏斗 / recon) — 当日 recon
- ntfy 本文: `exit 0 (close 0 / protect 25)`（morning run。protect 意図 25 件だが
  submit 0 = 後述の別課題）。
- 漏斗 (patch 後): Exit 合計 = **0** = `portfolio.exit_submitted` (一致)。
  per-system 内訳も recon と一致: sys1 `(close0/protect4)`, sys2 `(close0/protect20)`,
  sys4 `(close0/protect1)`。`measured=True` で「未計測」表示は消えた（0 を表示）。
- **今夜の実測値の反映確認**: recon = protect 1 件 submit のケースを流すと
  ntfy `exit 1 (close 0 / protect 1)` ↔ 漏斗 Exit 合計 **1** で一致（同一 recon 由来）。

### exit_ledger との関係（乖離ではなく測定点の違い）
`exit_ledger_YYYYMMDD.json` は broker **fill** から再構成した **実現 (realized) 決済**の
台帳。recon/ntfy/漏斗 の Exit は **発注 (submitted) 数**。両者は測定する pipeline 段階が
異なる:
- 07-29 の ledger: `today={realized_pl:0.0, n_closed:0, session_state:before_open,
  pending_exit_intents:13}`、`coverage_end=2026-07-28`。当該立会がまだ始まっていない
  ため realized=0（正しい。fill 待ち）。
- したがって「submit 1 → まだ fill 前なら ledger は 0」は乖離ではなく整合。fill 確定後の
  次 coverage で ledger にも計上される。台帳が届かない/未計測の日は ledger 側も
  `measured=False`/`stale` を正直に返す設計。

**結論**: 漏斗 Exit = ntfy exit_submitted（同一 recon、必ず一致）。ledger は realized を
測る別軸で、fill 確定に応じて後追いで一致する（黙って 0 埋めしない）。

## テスト
`tests/test_pipeline_exit_wiring_20260729.py`（6 ケース, 全 pass）: sysN 正規化 /
今夜値 `exit 1 (close0/protect1)` の ntfy↔漏斗一致 / 0 件日は 0 表示 / recon 無しは
未計測維持 / 部分 recon(exit 入力欠損) は未計測維持 / idempotent。
既存 `test_build_execution_recon` `test_execution_summary` は回帰なし。

## 引き継ぎ (今回スコープ外・別課題)
今夜の約定ゼロ = SLS が `insufficient qty (requested 47, available 0)`。古い resting order
が cancel-before-close で 0 件しか cancel できず qty を食っていた。これは exit **配線**とは
別問題（配線は「submit 0」を正しく 0 と表示するので、この bug はむしろ漏斗上で
`protect 25 だが submit 0` として可視化される）。exit 執行側の cancel-before-close を
別途修正すること。
