# 3 点同期フル実行ガイド（JSONL × スクショ × 診断）

**目的**: UI フル実行時に JSONL・スクリーンショット・診断スナップショットの 3 点を同一ランで取得し、同期分析で整合性を確認する。

**最終更新**: 2025-10-13

---

## 概要

3 点同期では、以下を突き合わせて候補数や実行タイミングの整合性を検証します：

1. **JSONL**: `logs/progress_today.jsonl` のシステム別候補数と allocation イベント
2. **スクリーンショット**: `screenshots/progress_tracking/*.png` の代表スクショ（start/complete 近傍）
3. **診断スナップショット**: `results_csv/diagnostics_test/diagnostics_snapshot_*.json` の ranked_top_n_count

---

## 前提条件

- Streamlit UI（`apps/app_today_signals.py`）が起動可能
- Playwright 環境（`scripts/capture_ui_progress.py`）がインストール済み
- 環境変数 `ENABLE_PROGRESS_EVENTS=1` が設定済み（JSONL 出力用）

---

## ステップ 1: 環境変数の設定

### Windows PowerShell

```powershell
# 一時的に設定（現在のセッションのみ）
$env:ENABLE_PROGRESS_EVENTS = "1"
$env:EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS = "1"
```

### .env ファイル（永続化したい場合）

プロジェクトルートの `.env` に追記：

```env
ENABLE_PROGRESS_EVENTS=1
EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS=1
```

**重要**: EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS=1 により、本番実行でも `results_csv/diagnostics_test/` に診断スナップショットが出力されます。

---

## ステップ 2: フル実行（ヘッドあり・ダークモード）

### 方法 A: Playwright キャプチャ経由

```powershell
# Streamlit UIを別ターミナルで起動
streamlit run apps/app_today_signals.py

# 別のターミナルでキャプチャスクリプトを実行（ヘッドあり・ダークモード）
python scripts/capture_ui_progress.py --headful --dark
```

**動作**:

- Chromium がヘッドありで起動し、Streamlit UI に接続
- 「▶ 本日のシグナル実行」ボタンをクリック
- 進捗イベント（JSONL）を監視しながら定期スクショ撮影
- 完了検知後、final1 ～ final5 の最終スクショ撮影
- 診断スナップショットが `results_csv/diagnostics_test/diagnostics_snapshot_YYYYMMDD_HHMMSS.json` に自動出力

### 方法 B: CLI で直接実行（UI なし）

```powershell
python scripts/run_all_systems_today.py --parallel --save-csv
```

**動作**:

- JSONL: `logs/progress_today.jsonl` に進捗イベント出力
- 診断スナップショット: `results_csv/diagnostics_test/` に出力
- スクショ: なし（UI キャプチャ不要の場合）

---

## ステップ 3: 出力確認

### 必須ファイル

1. **JSONL**:

   ```
   logs/progress_today.jsonl
   ```

   - `system_start/complete` イベントと `phase5_allocation_start/complete` が含まれていること
   - 各システムの candidates 値が記録されていること

2. **診断スナップショット**:

   ```
   results_csv/diagnostics_test/diagnostics_snapshot_YYYYMMDD_HHMMSS.json
   ```

   - `export_date` が実行時刻と一致
   - `mode` が空文字または null（test モードでない）
   - 各システムの `ranked_top_n_count` が存在

3. **スクリーンショット**（UI 実行時のみ）:
   ```
   screenshots/progress_tracking/progress_*.png
   screenshots/progress_tracking/final1.png～final5.png
   ```

### 確認コマンド（PowerShell）

```powershell
# JSONL イベント数確認
Get-Content logs/progress_today.jsonl | Measure-Object -Line

# 診断スナップショット一覧
Get-ChildItem results_csv/diagnostics_test/diagnostics_snapshot_*.json | Sort-Object LastWriteTime -Descending | Select-Object -First 1

# スクショ枚数確認
(Get-ChildItem screenshots/progress_tracking/progress_*.png).Count
```

---

## ステップ 4: 同期分析の実行

```powershell
python tools/sync_analysis.py
```

**出力ファイル**:

- `screenshots/progress_tracking/sync_analysis.json` - 全スクショとイベントの対応
- `screenshots/progress_tracking/ANALYSIS_REPORT.md` - Markdown 形式の分析レポート
- `screenshots/progress_tracking/sync_summary.json` - 所要時間とシステム別サマリー
- `screenshots/progress_tracking/tri_sync_report.json` - **3 点同期の詳細結果**

---

## ステップ 5: 結果の確認

### tri_sync_report.json の読み方

```json
{
  "selected_diagnostics": "results_csv\\diagnostics_test\\diagnostics_snapshot_20251013_104230.json",
  "diagnostics_mode": null, // null または空文字 = 本番実行
  "allocation_total_candidates": 40,
  "sum_jsonl_candidates": 40,
  "sum_jsonl_vs_alloc_match": true, // 合計一致（OK）
  "systems": [
    {
      "system": "system1",
      "jsonl_candidates": 10,
      "diagnostics_ranked_top_n": 10, // 一致（OK）
      "status": "match"
    },
    {
      "system": "system2",
      "jsonl_candidates": 10,
      "diagnostics_ranked_top_n": 5, // 不一致
      "status": "mismatch"
    }
  ]
}
```

### 期待される結果

- ✅ `sum_jsonl_vs_alloc_match: true` - パイプライン整合 OK
- ✅ 各システムの `status: "match"` が多数 - システム別整合 OK
- ⚠️ `status: "mismatch"` が残る場合:
  - 診断スナップショットの `diagnostics_mode` が "mini" / "quick" などテストモードでないか確認
  - `export_date` と overall_end の時刻差が ±45 分以内か確認
  - 必要に応じて `tools/sync_analysis.py` の許容窓を ±60 分に調整

---

## トラブルシューティング

### Q1: 診断スナップショットが出力されない

**確認**:

```powershell
$env:EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS
```

→ "1" が表示されるか？

**解決**:

- 環境変数が設定されていない場合は再設定
- `.env` ファイルに記載した場合は、プロセスを再起動

### Q2: 診断スナップショットが "mini" モードになる

**原因**: `--test-mode mini` で CLI 実行した

**解決**:

- `--test-mode` フラグなしで実行
- または UI 経由でフル実行（テストモード指定なし）

### Q3: スクショと JSONL の時刻が大きくずれる

**原因**: Streamlit UI の起動が遅れた、または実行中に UI 応答が停滞した

**解決**:

- UI を事前に起動しておく
- `--headful` でブラウザの動作を目視確認

### Q4: tri_sync_report.json で古い診断が選ばれる

**原因**: 同日に複数回実行し、最新ではないスナップショットが近接している

**解決**:

- 古いスナップショットを削除または別フォルダへ移動
- 分析ツールの許容窓を狭める（±30 分など、`tools/sync_analysis.py` の `tolerance` を調整）

---

## まとめ

1. 環境変数 `EXPORT_DIAGNOSTICS_SNAPSHOT_ALWAYS=1` を設定
2. フル実行（UI or CLI）
3. `tools/sync_analysis.py` で 3 点同期分析
4. `tri_sync_report.json` でシステム別整合性を確認

**期待効果**:

- JSONL candidates と diagnostics.ranked_top_n_count が一致
- 代表スクショで実行タイミングの視覚的確認が可能
- パイプライン全体の透明性・デバッグ効率の向上

---

## 参照

- 環境変数一覧: [docs/technical/environment_variables.md](../technical/environment_variables.md)
- 進捗イベント仕様: `docs/technical/progress_events.md`
- 診断 API: [docs/technical/diagnostics.md](../technical/diagnostics.md)
- UI キャプチャスクリプト: [scripts/capture_ui_progress.py](../../scripts/capture_ui_progress.py)
