# 技術文書

システムの内部実装と技術仕様に関するドキュメント集です。

## 📈 指標・計算ロジック

### [必須指標仕様](./required_indicators.md)

- ROC, ADX, RSI 等の計算式
- System 別指標要件
- データ品質チェック項目

## 🔄 処理フロー

### [今日のシグナル処理](../today_signal_scan/)

シグナル抽出の 8 フェーズ詳細：

1. [実行全体の流れ](../today_signal_scan/1.%20実行全体の流れ.md)
2. [基礎データ読込](../today_signal_scan/2.%20基礎データ読込フェーズ.md)
3. [フィルター実行](../today_signal_scan/3.%20フィルター実行フェーズ.md)
4. [セットアップ評価](../today_signal_scan/4.%20セットアップ評価フェーズ.md)
5. [シグナル抽出](../today_signal_scan/5.%20シグナル抽出フェーズ（トレード候補選定）.md)
6. [配分・最終リスト生成](../today_signal_scan/6.%20配分・最終リスト生成フェーズ.md)
7. [保存・通知](../today_signal_scan/7.%20保存・通知フェーズ.md)

### [Two-Phase 処理](../today_signal_scan/two-phaze_and_rank_rule.md)

- フィルター → セットアップ → ランキング → 配分
- システム別ランキングルール

## 🤖 AI 連携

### [MCP 統合計画](./mcp_integration_plan.md)

- Model Context Protocol サーバー
- VS Code 連携機能
- 自動化ツール群

## 🗃️ データ管理

### [キャッシュインデックス要件](./cache_index_requirements.md)

- Feather 形式の制約と DatetimeIndex 変換
- Fast-path 最適化の落とし穴
- Silent Exception 診断手法
- インデックス型保証のベストプラクティス

### キャッシュ階層

- **rolling**: 直近 300 日（今日用）
- **base**: 指標付与長期データ
- **full_backup**: 原本データ

### データ取得順

- **today**: rolling → base → full_backup
- **backtest**: base → full_backup

## 🔧 開発・テスト

### [候補数ゼロガイド](./zero_candidates_guide.md)

- 各システムで候補が出ない理由(正常動作)
- System6 の厳しい閾値(return_6d > 20%)の説明
- エラーとの区別方法
- テストモードでの期待値

### [テスト文書](../testing.md)

- 決定性テスト
- ミニモード検証
- パフォーマンステスト

### 必須実行コマンド

```powershell
# 高速検証（2秒）
python scripts/run_all_systems_today.py --test-mode mini --skip-external --benchmark

# 決定性テスト
pytest -q

# コード品質チェック
pre-commit run --files <changed_files>
```

## 🔗 関連リンク

- [システム仕様](../systems/) - System1-7 詳細
- [運用ガイド](../operations/) - 自動実行・通知設定
- [GitHub 指示書](../../.github/copilot-instructions.md) - AI 開発ルール
