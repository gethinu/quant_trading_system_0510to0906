# システム仕様書

各トレーディングシステムの詳細仕様です。

## ロングシステム（買い戦略）

### [システム 1 - ロング・トレンド・ハイ・モメンタム](./システム1.txt)

- **配分**: トレード資産の 25%
- **特徴**: 高モメンタム銘柄のトレンドフォロー戦略
- **関連**: [System1 実装](../../core/system1.py) | [戦略ラッパ](../../strategies/system1_strategy.py)

### [システム 3 - ロング・ミーン・リバージョン・セルオフ](./システム3.txt)

- **配分**: トレード資産の 25%
- **特徴**: 売られ過ぎからの反発を狙う戦略
- **関連**: [System3 実装](../../core/system3.py) | [戦略ラッパ](../../strategies/system3_strategy.py)

### [システム 4 - ロング・トレンド・ロー・ボラティリティ](./システム4.txt)

- **配分**: トレード資産の 25%
- **特徴**: 低ボラティリティ銘柄のトレンド戦略
- **関連**: [System4 実装](../../core/system4.py) | [戦略ラッパ](../../strategies/system4_strategy.py)

### [システム 5 - ロング・ミーン・リバージョン・ハイ ADX・リバーサル](./システム5.txt)

- **配分**: トレード資産の 25%
- **特徴**: ADX 高値からの反転を狙う戦略
- **関連**: [System5 実装](../../core/system5.py) | [戦略ラッパ](../../strategies/system5_strategy.py)

## ショートシステム（売り戦略）

### [システム 2 - ショート RSI スラスト](./システム2.txt)

- **配分**: トレード資産の 40%
- **特徴**: RSI 過熱からの下落を狙う戦略
- **関連**: [System2 実装](../../core/system2.py) | [戦略ラッパ](../../strategies/system2_strategy.py)

### [システム 6 - ショート・ミーン・リバージョン・ハイ・シックスデイサージ](./システム6.txt)

- **配分**: トレード資産の 40%
- **特徴**: 6 日連続上昇後の反落を狙う戦略
- **関連**: [System6 実装](../../core/system6.py) | [戦略ラッパ](../../strategies/system6_strategy.py)

### [システム 7 - カタストロフィーヘッジ](./システム7.txt)

- **配分**: トレード資産の 20%
- **特徴**: SPY 固定のヘッジ戦略（**変更禁止**）
- **関連**: [System7 実装](../../core/system7.py) | [戦略ラッパ](../../strategies/system7_strategy.py)

## 共通仕様

- **Two-Phase 処理**: フィルター判定 → セットアップ判定 → ランキング → 配分
- **データ階層**: rolling → base → full_backup
- **配分管理**: [symbol_system_map.json](../../data/symbol_system_map.json)

### 関連ドキュメント

- [今日のシグナル処理](../today_signal_scan/) - 実行フロー詳細
- [必須指標](../technical/required_indicators.md) - 計算仕様
- [統合バックテスト](../../common/integrated_backtest.py) - テスト基盤
