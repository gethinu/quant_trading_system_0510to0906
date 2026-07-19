# サービスイン向け E2E 実測 — 起点・記録・残件 (2026-07-19)

## サービスイン基準
**Alpaca paper で entry + exit が E2E で完結して連続 1 週間 (= 5 取引日) 実測。**
entry だけ回っている状態を「サービスイン」とは呼ばない。

## 現到達度 (正直な評価)
E2E の **配線 (wiring) は完成**しているが、**連続運用は未達**。

| 要素 | 状態 | 根拠 |
|---|---|---|
| entry 発注→paper fill | ✅ 動作 | ledger_reconciliation n_desync=0 (214/220 一致)。07-08 の 37 entry 全 fill 実測。|
| exit 発注→paper fill | ⚠ 動作するが不完全 | time-based close は動くが、resting protective 注文が qty を握ると 40310000 で失敗 (下記ブロッカー)。|
| reconcile (fill 台帳突合) | ✅ 動作 | alpaca_snapshot.ledger_reconciliation, n_desync=0。|
| **連続 5 取引日 E2E-clean** | ❌ **未達** | 最長 2 日 (07-13, 07-14)。07-15 でブロッカー発火、07-16/17 は端末ダウンで**ランナー未実行**、07-19 は dry-run。overdue exit が 38 件滞留。|

ledger (`logs/e2e_measurement/ledger.md`) 実測: **9 取引日中 E2E-clean は 2 日、最長連続 2 日**。

## E2E を塞いでいる残件

### 1. [コード] cancel-before-close 未実装 → time-exit が held_for_orders で失敗 (本 PR で修正)
日次 exit フローは、time-based の成行 close を出す前に、その銘柄の resting
protective 注文 (stop/limit/trailing) を cancel していなかった。protective が
position qty を全量握る (held_for_orders = qty) と、成行 close が Alpaca
`code 40310000 (insufficient qty available)` で reject され、**position が exit
できず期限超過として滞留**する。
- 実測: 07-15 に time-based close が **9 件失敗** (全て system2 short)。live open orders
  で overdue の 9 short 全てが今も protective STOP を保持 → 次回もこのままでは失敗。
- **修正 (本 PR)**: `paper_exit_check.py` が close 対象銘柄 (time/breakout) の resting
  注文だけを先に cancel → qty 解放 → 成行 close。`--no-cancel-before-close` で無効化可。
  保有継続銘柄の保護は不変。unit test 5 件。**live 検証は次の市場オープン (07-20) 待ち**。

### 2. [運用] 実行継続性 — ランナーが取引日に必ず発火する保証がない
order 実行ランナー `QuantTrading_OpenAutoRun` (月〜金 22:35 JST) は端末が
起動している必要がある。07-16/17 は端末ダウンで**未実行** → その日の exit/entry が
丸ごと欠落し、overdue が積み上がった。**catch-up は市場が閉じた後では効かない**
(signal pipeline の cache catch-up とは別問題)。
- durable fix 候補: (a) 22:35 JST に端末が起きている運用保証 (wake timer / 常時起動),
  (b) 起床時に「未実行の取引日ぶんの overdue exit を market open で flush」する catch-up。
  → 運用判断 (HUMAN_TASK 相当)。本セッションでは特定・記録のみ。

## 1 週間実測の「起点」と「記録の仕組み」
- **記録の仕組み**: `scripts/build_e2e_ledger.py` (read-only)。ランナーが既に書く
  recon/exit_orders/paper_orders/alpaca_snapshot を 1 行/取引日に集約し
  `logs/e2e_measurement/ledger.{jsonl,md}` に upsert。日次で再実行すれば積み上がる。
- **clean な取引日の定義**: `ran(submitted) & time_exit_failed==0 & n_desync==0 &
  overdue_exits==0`。protect_* の重複拒否は無害として除外。
- **起点 (measurement start condition)**: 次の全てを満たす最初の取引日を Day 1 とする。
  1. cancel-before-close 修正が runner tree に反映済 (arming)。
  2. overdue backlog (現 38 件) が flush されて overdue_exits==0。
  3. その日が `e2e_clean == true`。
  以後 **連続 5 取引日 clean** で「1 週間 E2E 実測」達成。ledger の「最長連続」で判定。
- **運用**: 毎日ランナー完了後に `python scripts/build_e2e_ledger.py` を叩く
  (open_auto_run への配線は後追いでも可。まずは手動 or SelfMonitor から)。

## 次アクション
1. 本 PR (cancel-before-close + ledger) を main へ。
2. **arming**: runner tree (`C:/tmp/qts-main-run`, `claude/open-auto-run`) に
   cancel-before-close を反映するか判断 (無人 paper 発注の挙動変更のため要判断)。
3. 07-20 (月) の実ランで overdue 38 件が flush され time_exit_failed==0 になることを
   ledger で確認 → Day 1 判定。
