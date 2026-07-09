# docs-check の markdown-link-check base-branch 修正 (2026-07-09)

## 背景

`.github/workflows/docs-check.yml` の markdown-link-check ステップは
`check-modified-files-only: "yes"` を使うが `base-branch` を指定して
いなかった。この action の既定 `base-branch` は `master` で、本リポジトリ
の既定ブランチは `main` のため、変更ファイルの算出時に
`fatal: couldn't find remote ref master` で失敗していた。

docs 系ファイル (`docs/**/*.md`, `README.md`, `CHANGELOG.md`,
`CONTRIBUTING.md`) を触る全 PR / push で再現するワークフロー設定バグ。

## 修正

markdown-link-check の `with` に `base-branch: "main"` を明示追加。
他の docs-check 設定 (config-file, folder-path, file-path,
check-modified-files-only, continue-on-error など) は不変。

## 検証

この doc 追加自体が docs-check ワークフローを trigger し、
`base-branch: "main"` で変更ファイル算出が成功することを確認するための
リンクを含まない最小ドキュメント。
