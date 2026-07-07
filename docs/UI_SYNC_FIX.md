# UI 進捗バーとログの同期問題修正

## 問題の概要

**報告された問題**:

- UI 進捗バーの表示がコンソールログと同期していない
- 進捗が戻る現象が発生する
- メトリクス表示 (`Tgt`, `FILpass`, `STUpass`, `TRDlist`, `Entry`) が JSONL 進捗イベントと一致しない

## 根本原因

1. **進捗後退の原因**:

   - `GLOBAL_STAGE_METRICS.get_snapshot()` から取得した値が古い場合、並列実行で複数のコールバックが競合すると進捗が戻る
   - `max(prev, value)` でも、古い値で上書きされるタイミングがあった

2. **ログ不一致の原因**:
   - UI 表示は `metrics_store` (メモリ内の状態) から取得
   - JSONL 進捗イベント (`logs/progress_today.jsonl`) とは別系統で管理されていた
   - コールバック経由の更新タイミングと JSONL 書き込みタイミングがズレていた

## 実装した解決策

### 1. **進捗後退防止の強化** (`update_progress()`)

**変更前**:

```python
# 通常時は進捗後退を防ぐが、開始時（phase="start"）はリセットを許可
if phase != "start":
    prev = int(self.states.get(key, 0))
    value = max(prev, value)
self.states[key] = value
```

**変更後**:

```python
# 進捗後退防止: 常に前回値との最大値を採用
if phase == "start":
    # 開始時は強制リセット
    self.states[key] = 0
    value = 0
else:
    # 通常時は進捗後退を防ぐ: 前回値より大きい場合のみ更新
    prev = int(self.states.get(key, 0))
    if value > prev:
        self.states[key] = value
    else:
        # 後退する場合は前回値を維持
        value = prev
```

**改善点**:

- 値が後退する場合、状態を更新せずに前回値を維持
- `phase="start"` 時のみ強制的に 0 にリセット
- `phase="done"` 時は強制的に 100 に設定

### 2. **JSONL 進捗イベントとの同期** (`_sync_from_jsonl_if_needed()`)

**新規追加メソッド**:

```python
def _sync_from_jsonl_if_needed(self) -> None:
    """JSONL進捗イベントから最新の候補数を取得してメトリクスを更新する"""
    # logs/progress_today.jsonl から system_complete イベントを読み込み
    # 各システムの candidates をメトリクスに反映
```

**タイミング**:

- `update_progress()` 実行時に毎回呼び出される
- 最新 100 件の JSONL イベントをチェック
- `system_complete` イベントから候補数を抽出して `TRDlist` と `Entry` を更新

### 3. **最終候補数の同期** (`_sync_final_counts_from_jsonl()`)

**新規追加メソッド**:

```python
def _sync_final_counts_from_jsonl(self) -> None:
    """JSONL進捗イベントから最終候補数を取得してメトリクスを更新する"""
    # pipeline_complete イベントから final_rows を取得
    # system_complete イベントから各システムの候補数を取得
```

**タイミング**:

- `finalize_counts()` 実行時に最初に呼び出される
- `refresh_all()` 実行時にも呼び出される

### 4. **定期同期の統合** (`refresh_all()`)

**変更前**:

```python
def refresh_all(self) -> None:
    for name in self.metrics_store.systems():
        self._render_metrics(name)
```

**変更後**:

```python
def refresh_all(self) -> None:
    # まずJSONLから最新データを同期
    try:
        self._sync_final_counts_from_jsonl()
    except Exception:
        pass
    # 全システムのメトリクスを再描画
    for name in self.metrics_store.systems():
        self._render_metrics(name)
```

## 同期フロー

```
UI実行開始
    ↓
┌───────────────────────────────────────────┐
│ Phase 0-4: システム処理                   │
│                                           │
│ update_progress("system1", "start")       │
│   → 進捗バー: 0%                          │
│   → _sync_from_jsonl_if_needed()          │
│      └─ JSONL読込 (system_complete)       │
│         └─ TRDlist/Entry更新              │
│                                           │
│ update_progress("system1", phase)         │
│   → 進捗バー: 進捗値更新（後退防止）      │
│   → _sync_from_jsonl_if_needed()          │
│                                           │
│ update_progress("system1", "done")        │
│   → 進捗バー: 100%                        │
│   → _sync_from_jsonl_if_needed()          │
└───────────────────────────────────────────┘
    ↓
┌───────────────────────────────────────────┐
│ Phase 5-6: 配分・保存                     │
│                                           │
│ finalize_counts()                         │
│   → _sync_final_counts_from_jsonl()       │
│      └─ JSONL読込 (system_complete全件)   │
│         └─ 全システムのメトリクス更新      │
└───────────────────────────────────────────┘
    ↓
完了: UI進捗バー・メトリクス・JSONL・ログが完全同期
```

## 検証方法

### 1. **進捗後退の検証**

```powershell
# Streamlit UI起動
streamlit run apps/app_today_signals.py

# 実行中に各システムの進捗バーを観察
# → 進捗が戻らないことを確認
```

### 2. **メトリクス同期の検証**

```powershell
# 実行完了後、JSONL と UI メトリクスを比較
Get-Content logs\progress_today.jsonl | ConvertFrom-Json | Where-Object { $_.event_type -eq 'system_complete' } | Select-Object @{L='system';E={$_.data.system}},@{L='candidates';E={$_.data.candidates}}

# UI表示の TRDlist と Entry が上記の candidates と一致することを確認
```

### 3. **三点同期の検証**

```powershell
# JSONL final_rows
(Get-Content logs\progress_today.jsonl | ConvertFrom-Json | Where-Object { $_.event_type -eq 'pipeline_complete' }).data.final_rows

# CSV 行数
(Import-Csv data_cache\signals\signals_final_*.csv).Count

# UI 表示の各システム Entry 合計
# → 3つが一致することを確認
```

## 期待される効果

- ✅ **進捗後退の完全防止**: 値が前回より小さい場合は更新されない
- ✅ **リアルタイム同期**: JSONL 進捗イベントと UI メトリクスが常に同期
- ✅ **三点同期**: JSONL・コンソールログ・CSV・UI 進捗バーが完全一致
- ✅ **デバッグ容易性**: 検証パネルとメトリクス表示の両方で進捗を確認可能

## 今後の改善案（オプション）

1. **並列実行の最適化**: コールバック競合を減らすためのロック機構
2. **JSONL 自動リロード**: WebSocket/polling で UI が自動的に JSONL を追跡
3. **詳細ログモード**: 進捗更新の詳細履歴をログに記録（デバッグ用）

## 参考

- JSONL 進捗イベント仕様: `docs/technical/progress_events.md` (存在する場合)
- 検証パネル使用方法: UI 上の「🔍 検証: 進捗イベント (JSONL)」展開パネル
