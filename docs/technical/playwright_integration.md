# Playwright E2E テスト統合ガイド

## 📚 概要

このプロジェクトでは、Streamlit UI の自動テストに **Playwright** を採用しています。手動確認作業を自動化し、UI の正確性を継続的に検証します。

## 🎯 テスト対象

- **Streamlit 統合アプリ** (`apps/app_integrated.py`)
- 当日シグナル表示
- 配分結果表示
- システム全体の動作確認

## 🚀 クイックスタート

### 1. テスト実行（自動で Streamlit 起動）

```powershell
npm test
```

### 2. ブラウザ表示付きで実行

```powershell
npm run test:headed
```

### 3. インタラクティブモード（デバッグ用）

```powershell
npm run test:ui
```

## 📁 プロジェクト構成

```
quant_trading_system/
├── e2e/                              # E2E テストディレクトリ
│   ├── streamlit/                    # Streamlit UI テスト
│   │   ├── app-basic.spec.ts        # 基本動作テスト
│   │   ├── signals-display.spec.ts  # シグナル表示テスト
│   │   └── allocation-display.spec.ts # 配分結果テスト
│   ├── README.md                     # E2E テスト詳細ガイド
│   └── example.spec.ts               # Playwright サンプル
├── playwright.config.ts              # Playwright 設定
├── package.json                      # npm スクリプト定義
└── .github/workflows/playwright.yml # CI/CD 統合
```

## 🔧 VS Code タスク統合

**ターミナル > タスクの実行** から以下が利用可能:

- **E2E: Run All Tests** - 全テスト実行（ヘッドレス）
- **E2E: Run Tests (Headed)** - ブラウザ表示付き実行
- **E2E: Interactive UI** - デバッグ用インタラクティブモード
- **E2E: Show Report** - テスト結果レポート表示
- **E2E: Quick Test** - Chromium のみ高速実行

## 📊 テスト戦略

### 自動起動モード（デフォルト）

`playwright.config.ts` で Streamlit が自動起動するよう設定済み:

```typescript
webServer: {
  command: 'python -m streamlit run apps/app_integrated.py',
  url: 'http://localhost:8501',
  reuseExistingServer: !process.env.CI,
  timeout: 120 * 1000,
},
```

### テストデータ準備

実践的なテストには、事前にシグナルデータを生成:

```powershell
python scripts/run_all_systems_today.py --parallel --save-csv
```

## 🧪 テスト例

### 基本動作テスト

```typescript
test("アプリが正常に起動する", async ({ page }) => {
  await page.goto("/");
  await page.waitForLoadState("networkidle");
  await expect(page).toHaveTitle(/.*/, { timeout: 10000 });
});
```

### シグナル表示テスト

```typescript
test("シグナルデータが表示される", async ({ page }) => {
  await page.goto("/");
  await page.waitForLoadState("networkidle");

  const hasDataFrame = await page.locator('[data-testid="stDataFrame"]').count();
  expect(hasDataFrame).toBeGreaterThan(0);
});
```

## 🔄 CI/CD 統合

GitHub Actions でのテスト自動実行（`.github/workflows/playwright.yml`）:

- **トリガー**: `apps/`, `e2e/`, Playwright 設定の変更時
- **ブラウザ**: Chromium（高速化のため）
- **成果物**: テストレポート、失敗時のスクリーンショット

## 📈 テスト追加ガイドライン

### 新規テストファイル作成

```typescript
// e2e/streamlit/new-feature.spec.ts
import { test, expect } from "@playwright/test";

test.describe("新機能のテスト", () => {
  test.beforeEach(async ({ page }) => {
    await page.goto("/");
    await page.waitForLoadState("networkidle");
  });

  test("機能が正しく動作する", async ({ page }) => {
    // テストロジック
  });
});
```

### Streamlit 要素の取得

```typescript
// データフレーム
await page.locator('[data-testid="stDataFrame"]');

// ボタン
await page.locator('button:has-text("実行")');

// テキスト検索
await page.locator("text=System1");
```

## 🔍 トラブルシューティング

### テストがタイムアウトする

`playwright.config.ts` でタイムアウトを調整:

```typescript
use: {
  actionTimeout: 30 * 1000, // 30秒
},
```

### Streamlit が起動しない

```powershell
# Python 環境確認
python --version

# Streamlit インストール確認
pip show streamlit

# 手動起動テスト
streamlit run apps/app_integrated.py
```

### スクリーンショットでデバッグ

失敗したテストのスクリーンショットは `test-results/` に自動保存されます。

## 🎨 ベストプラクティス

1. **テスト分離**: 各テストは独立して実行可能にする
2. **明確な名前**: テストケース名は動作を明示的に記述
3. **待機処理**: `waitForLoadState('networkidle')` で UI の完全読み込みを待つ
4. **スクリーンショット**: デバッグ用に自動保存を活用
5. **CI 最適化**: Chromium のみで高速化

## 📚 参考リンク

- [Playwright 公式ドキュメント](https://playwright.dev/)
- [Streamlit テスト戦略](https://docs.streamlit.io/knowledge-base/using-streamlit/how-to-test-your-streamlit-app)
- [プロジェクト内 E2E ガイド](../../e2e/README.md)

## 🔄 既存テストとの連携

### Python pytest との共存

- **pytest**: ロジック・関数レベルのテスト
- **Playwright**: UI・統合レベルのテスト

```powershell
# Python ロジックテスト
pytest -q

# Streamlit UI テスト
npm test

# 両方実行
pytest -q && npm test
```

### pre-commit 統合

`.pre-commit-config.yaml` に Playwright テストを追加可能（オプション）:

```yaml
- repo: local
  hooks:
    - id: playwright-test
      name: Playwright E2E Tests
      entry: npm test
      language: system
      pass_filenames: false
      files: ^(apps|e2e)/.*\.(py|ts)$
```

## 📝 次のステップ

1. **カスタムテスト追加**: プロジェクト固有の UI 操作を自動化
2. **テストカバレッジ拡大**: 全タブ・全機能をカバー
3. **パフォーマンステスト**: ページ読み込み時間の監視
4. **ビジュアルリグレッション**: スクリーンショット比較テスト

---

**自動化重視・AI 駆動開発** の一環として、Playwright テストを積極的に活用してください 🚀
