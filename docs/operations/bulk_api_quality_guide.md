# Bulk API データ品質ガイド

**最終更新**: 2025-10-12  
**対象バージョン**: branch0906

## 概要

日次更新処理では、EOD Historical Data の Bulk API を使用して全銘柄のデータを一括取得します。このガイドでは、Bulk API のデータ品質検証と設定方法について説明します。

## 問題の背景

Bulk API から取得するデータには、以下の特性があります：

1. **速報値 vs 確定値**: 市場クローズ直後は速報値が配信され、後に確定値へ更新されます
2. **Volume 差異**: 出来高データは特に確定までに 1〜5%程度の変動が発生します
3. **タイミング依存**: 取得時刻によってデータの精度が変わります

## デフォルト設定（2025-10-12 改善）

### 改善前の問題

- Volume 差異の許容範囲が**0.5%**と厳格すぎた
- 市場データの特性上、避けられない誤差で信頼性スコアが低下
- 結果として、Bulk API が使えず個別 API フォールバック（API コール大量消費）

### 改善後の設定

| 項目                 | 環境変数                    | デフォルト値 | 説明                      |
| -------------------- | --------------------------- | ------------ | ------------------------- |
| **Volume 許容範囲**  | `BULK_API_VOLUME_TOLERANCE` | `5.0` (%)    | 出来高データの許容差異    |
| **価格許容範囲**     | `BULK_API_PRICE_TOLERANCE`  | `0.5` (%)    | OHLC 価格データの許容差異 |
| **最低信頼性スコア** | `BULK_API_MIN_RELIABILITY`  | `70.0` (%)   | Bulk API 使用の基準値     |

### 効果

- **信頼性スコア**: 30% → **100%** に改善
- **Bulk API 活用率**: 向上（API コール削減）
- **価格データの厳格性**: 維持（0.5%）

## 環境変数での設定方法

### 1. `.env`ファイルで設定（推奨）

```bash
# Bulk APIデータ品質設定
BULK_API_VOLUME_TOLERANCE=5.0    # Volume差異5%まで許容
BULK_API_PRICE_TOLERANCE=0.5     # 価格差異0.5%まで許容
BULK_API_MIN_RELIABILITY=70.0    # 信頼性70%以上で使用
```

### 2. PowerShell で一時的に設定

```powershell
# デフォルト設定で検証
python scripts/verify_bulk_accuracy.py

# Volume許容範囲を3%に厳格化
$env:BULK_API_VOLUME_TOLERANCE="3.0"
python scripts/verify_bulk_accuracy.py

# 信頼性基準を80%に引き上げ
$env:BULK_API_MIN_RELIABILITY="80.0"
python scripts/verify_bulk_accuracy.py
```

### 3. コード内で参照

```python
from config.environment import get_env_config

env = get_env_config()

# Volume許容範囲を取得（5.0なら0.05で計算）
volume_tolerance = env.bulk_api_volume_tolerance / 100.0

# 価格許容範囲を取得
price_tolerance = env.bulk_api_price_tolerance / 100.0

# 最低信頼性スコアを取得
min_reliability = env.bulk_api_min_reliability / 100.0
```

## 品質検証の実行

### 基本的な検証

```powershell
# デフォルト10銘柄で検証
python scripts/verify_bulk_accuracy.py
```

**出力例（改善後）**:

```
============================================================
📋 検証結果サマリー
============================================================
  検証銘柄数: 10/10
  完全一致: 8件
  問題検出: 0件
  データ欠損: 0件

✅ 完全一致した銘柄: SPY, AAPL, MSFT, GOOGL, AMZN, TSLA, NVDA, META

============================================================
✅ 信頼性スコア: 100.0%
👍 Bulk APIは高品質です。安心して使用できます。
============================================================
```

### カスタム銘柄で検証

```powershell
# 特定銘柄のみ検証
python scripts/verify_bulk_accuracy.py --symbols AAPL,MSFT,GOOGL

# 個別APIとも比較（APIコール消費注意）
python scripts/verify_bulk_accuracy.py --use-api
```

### 取得タイミングの影響調査

```powershell
# 現在時刻が推奨範囲内か確認
python scripts/verify_bulk_accuracy.py --timing
```

**推奨実行時間**: 日本時間 6:00〜10:00（米国市場クローズ後、データ確定済み）

## 設定値の調整ガイドライン

### Volume 許容範囲（BULK_API_VOLUME_TOLERANCE）

| 設定値    | 用途               | トレードオフ               |
| --------- | ------------------ | -------------------------- |
| **3.0%**  | 厳格な検証         | API コール増加の可能性     |
| **5.0%**  | 推奨（デフォルト） | 速報値誤差を許容、実用的   |
| **10.0%** | 緩和               | 低品質データを見逃すリスク |

### 価格許容範囲（BULK_API_PRICE_TOLERANCE）

| 設定値       | 用途       | 推奨                            |
| ------------ | ---------- | ------------------------------- |
| **0.5%**     | デフォルト | ✅ 戦略への影響を最小化         |
| **1.0%**     | やや緩和   | ⚠️ エントリー価格に影響の可能性 |
| **2.0%以上** | 非推奨     | ❌ 戦略の正確性を損なう         |

### 最低信頼性スコア（BULK_API_MIN_RELIABILITY）

| 設定値    | 動作               | 影響                        |
| --------- | ------------------ | --------------------------- |
| **60.0%** | 低基準             | Bulk API を積極的に使用     |
| **70.0%** | 推奨（デフォルト） | バランスが良い              |
| **80.0%** | 高基準             | 個別 API フォールバック増加 |

## トラブルシューティング

### 信頼性スコアが低い（70%未満）

**原因**:

1. 市場クローズ直後の速報値タイミング
2. 価格許容範囲が厳格すぎる
3. 特定銘柄のデータ品質問題

**対策**:

```powershell
# 1. タイミング確認
python scripts/verify_bulk_accuracy.py --timing

# 2. Volume許容範囲を緩和
$env:BULK_API_VOLUME_TOLERANCE="7.0"
python scripts/verify_bulk_accuracy.py

# 3. 問題銘柄を特定
python scripts/verify_bulk_accuracy.py --symbols 問題の銘柄
```

### 個別 API にフォールバックされる

**ログ例**:

```
[ERROR] 品質チェック不合格: 信頼性が低いです（65.0%）
[WARNING] ⚠️ Bulk APIの品質が低いため、個別APIを使用します
```

**対策**:

1. 環境変数の確認: `python -c "from config.environment import get_env_config; env = get_env_config(); print(f'Volume: {env.bulk_api_volume_tolerance}%, Min: {env.bulk_api_min_reliability}%')"`
2. `.env`ファイルに設定を追加
3. 信頼性基準を調整（例: `BULK_API_MIN_RELIABILITY=65.0`）

### 価格データに差異が検出される

**重大度**: 高（戦略への影響大）

**対策**:

1. 価格許容範囲は緩和**しない**（0.5%維持）
2. 問題銘柄を個別確認: `python scripts/verify_bulk_accuracy.py --symbols 問題の銘柄 --use-api`
3. EOD Historical Data のサポートに連絡

## ベストプラクティス

### 本番環境

```bash
# .env
BULK_API_VOLUME_TOLERANCE=5.0      # 実用的な許容範囲
BULK_API_PRICE_TOLERANCE=0.5       # 厳格に維持
BULK_API_MIN_RELIABILITY=70.0      # 標準基準
```

### 開発/テスト環境

```bash
# .env
BULK_API_VOLUME_TOLERANCE=10.0     # 緩和OK
BULK_API_PRICE_TOLERANCE=1.0       # やや緩和
BULK_API_MIN_RELIABILITY=60.0      # 低基準
```

### 高頻度取引

```bash
# .env
BULK_API_VOLUME_TOLERANCE=3.0      # 厳格
BULK_API_PRICE_TOLERANCE=0.3       # 極めて厳格
BULK_API_MIN_RELIABILITY=90.0      # 高基準
```

## 関連ドキュメント

- [環境変数一覧](../technical/environment_variables.md#7-bulk-apiデータ品質検証)
- 日次更新処理
- キャッシュシステム

## 変更履歴

### 2025-10-12: Volume 許容範囲の緩和

- **変更内容**: `BULK_API_VOLUME_TOLERANCE` のデフォルトを 0.5% → 5.0% に変更
- **理由**: 市場データの速報値 vs 確定値の差異を許容し、Bulk API の活用率向上
- **影響**: 信頼性スコア 30% → 100% に改善、API コール削減
- **価格データへの影響**: なし（価格許容範囲は 0.5% を維持）
