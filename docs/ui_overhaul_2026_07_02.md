# UI/UX overhaul 2026-07-02 — subscriber pitch quality

## 背景

2026-07-02 の user screenshot review で dashboard が subscriber pitch 品質を満たさない不具合が 5 件同時発覚。

- **A. header "no data" 誤表示**: `latest: 2026-07-02` と表示されながら大見出しが「no data」。実 data は 49 signals / $37K / BUY:39 SELL:10。
- **B. AI Narrator card 崩壊**: narrator body 全部 (~1000 字) が改行なしで流し込まれ、per-symbol reasons の 49 銘柄が **垂直** に流れて右方向へ無限オーバーフロー。mobile 完全不可読。
- **C. Signal Pipeline カード全部 "not measured"**: sys1-7 の 6 phase 全部で "not measured" 固定表示。
- **D. ntfy title mangled**: `📈⚠️ 749BUYSELL10100%3` — 日本語 headline から ASCII だけ残った gibberish。
- **E. 情報密度過剰**: mobile viewport (380px) で操作困難、visual hierarchy 欠如。

## Root cause (code-level)

| 症状 | 直接原因 | 場所 |
| --- | --- | --- |
| A | header の hero は `universeOf(pipeline.systems['sys1']).Tgt.count` を使うが、`pipeline_20260702.json` は sys1.Tgt.count = null (monitor が未計測)。 | `apps/dashboards/alpaca-next/app/page.tsx:309` (旧) |
| B | `tailwind.config.ts` の `content` scan に `./components/**` が入っていない → NarrativeCard.tsx の `flex-wrap`, `bg-gradient-*`, `border-sky-*` 等が JIT purge され unstyled 崩壊。 | `apps/dashboards/alpaca-next/tailwind.config.ts:4` (旧) |
| C | pipeline_20260702.json では TRDlist/Entry のみ count 実測 (10 / 9)、Tgt/FILpass/STUpass/Exit は count=null。UI 側は表示ロジック正しいが 4 phase 表記が視覚的に貧弱で「全滅」の印象を与える。 | data-side issue + UI 表現改善 |
| D | `common/publishers/ntfy.py:113` (旧) の `title.encode("ascii", "ignore")` が日本語文字を全消去 → `749BUYSELL10100%3` の残骸。 | `common/publishers/ntfy.py` |
| E | Signal Pipeline が always-open、narrator が gaping section、KPI hero が universe (二次情報) 主体で subscriber value (signals count / notional) が埋もれる。 | `apps/dashboards/alpaca-next/app/page.tsx` |

## 修正内容

### Phase 2A: hero が universe だけに依存しない

`app/page.tsx` を全面書き換え:
- hero を **`portfolio.total_signals`** 主体に変更 (お金に直結する数字が第一印象)
- BUY / SELL chip + notional (`$37K`) を同じ行に配置
- universe は date と同じ行の sub-info (`universe 12,445` 表記)
- 'no data' fallback を廃止 (signals があれば必ず表示)

### Phase 2B: NarrativeCard 3 段構成 + 銘柄 chip wrap

`components/NarrativeCard.tsx` を rewrite:
1. **headline** (常時表示)
2. **TL;DR** (最初の段落 or 140 字, 常時表示)
3. **`<details>` 詳細を見る** (default closed): full summary + per-symbol reasons chip grid

per_symbol_reasons を `flex flex-wrap gap-1.5` で必ず水平配置。長い理由文は `truncate max-w-[14rem]`。**tailwind.config.ts に `./components/**` を追加** して class が JIT purge されないようにする (これが B の主因)。

### Phase 2C: Signal Pipeline metrics 改善

`components/PipelineSection.tsx` を新設 (`page.tsx` から extract):
- section 全体を `<details>` accordion 化 → default collapsed (E の情報密度削減にも寄与)
- "not measured" → 「未計測」 (`text-muted/60 italic`) に視覚的にダウングレード
- 実測 phase の value は `text-cardfg` (bright) で数字を強調
- progress bar の測定/未測定の差別化 (`bg-sky-400/70` vs `bg-white/10`)

### Phase 2D: ntfy title synth + narrator prompt 強化

`common/narrator.py`:
- `_HEADLINE_MAX_LEN = 50` を宣言
- `_headline_char_ok`, `_is_valid_headline`, `_synth_headline` の 3 helper を追加
- `narrate()` が LLM headline を **post-validation** し、日本語混入 or 長すぎ の場合は `_synth_headline` で決定論的に差し替え (`headline_synth: True` flag 立て)
- `_SYSTEM_PROMPT` を強化: ASCII+emoji only 指示、書式例 3 つ (正)、誤例 3 つ (負)、gibberish 「'7系統49シグナル…' が '749BUYSELL10100%3' に潰れる」の明示

`common/publishers/ntfy.py`:
- 旧 `safe_title = title.encode("ascii", "ignore").decode("ascii").strip() or "Today's Signals"` を廃止
- `_to_safe_ascii_title(title, message)` に置換: ASCII 保持率 60% 以上ならそのまま採用、そうでなければ portfolio 統計から `YYYY-MM-DD | 49 signals | BUY 39 / SELL 10 | $37K` を synth (最終防波堤)
- `_TITLE_LIMIT = 120` (X-Title 実用上限)

### Phase 2E: 情報密度削減 + mobile 最適化

- `SignalsSection` component 化 (`components/SignalsSection.tsx`)
- 各 sys accordion の summary 行に **上位 3 銘柄 chip** を配置 (閉じたままでも trader value が見える)
- system_label mapping (`sys1 → 'ROC200 momentum'` etc) を chip タイトル横に表示
- 空 systems (`n_signals_output = 0`) はまとめて "信号なし ({n} systems)" の折りたたみに集約
- KPI hero を `flex-wrap` で mobile 380px でも 2 行に収まる layout に
- publisher status badge を hero の隣に小さく (「発信済」の証跡)

## 変更 files

新規:
- `apps/dashboards/alpaca-next/components/PipelineSection.tsx`
- `apps/dashboards/alpaca-next/components/SignalsSection.tsx`
- `tests/test_ntfy_title_synth.py`
- `tests/test_narrator_headline_format.py` (以前の test を全 rewrite)
- `tests/system/test_dashboard_ui_contract.py`
- `docs/ui_overhaul_2026_07_02.md` (本ドキュメント)

修正:
- `apps/dashboards/alpaca-next/tailwind.config.ts` (`./components/**` 追加)
- `apps/dashboards/alpaca-next/app/page.tsx` (全面 rewrite: hero + component 分離)
- `apps/dashboards/alpaca-next/components/NarrativeCard.tsx` (全面 rewrite: 3 段構成)
- `common/publishers/ntfy.py` (`_to_safe_ascii_title` 導入)
- `common/narrator.py` (headline validation + synth + prompt 強化)

## Windows 側で走らせるコマンド (Phase 4 + 5)

### Phase 4: build 検証

```powershell
cd C:\Repos\quant_trading_system_0510to0906\apps\dashboards\alpaca-next
npm run build
# → success の場合 `out/` に static export が生成される
# 確認: `out/index.html` が存在すること、404.html も自動生成されること
Get-ChildItem out -Recurse | Where-Object { $_.Extension -in ".html", ".css", ".js" } | Select-Object -First 15
```

### Phase 4-b: python test

```powershell
cd C:\Repos\quant_trading_system_0510to0906
python -m pytest tests/test_ntfy_title_synth.py tests/test_narrator_headline_format.py tests/system/test_dashboard_ui_contract.py tests/system/test_loadsignals_ts_contract.py tests/test_narrator.py -v
```

### Phase 5: commit + push (direct origin claude/monitor-webapp)

```powershell
cd C:\Repos\quant_trading_system_0510to0906
git checkout claude/monitor-webapp
git status
git add apps/dashboards/alpaca-next/tailwind.config.ts `
        apps/dashboards/alpaca-next/app/page.tsx `
        apps/dashboards/alpaca-next/components/NarrativeCard.tsx `
        apps/dashboards/alpaca-next/components/PipelineSection.tsx `
        apps/dashboards/alpaca-next/components/SignalsSection.tsx `
        common/publishers/ntfy.py `
        common/narrator.py `
        tests/test_ntfy_title_synth.py `
        tests/test_narrator_headline_format.py `
        tests/system/test_dashboard_ui_contract.py `
        docs/ui_overhaul_2026_07_02.md

git commit -m "ui: subscriber-pitch overhaul — hero, narrator card, ntfy title

Fixes 5 issues surfaced in the 2026-07-02 subscriber-pitch review:

A. 'no data' false-negative in hero — sys1.Tgt.count was null on
   2026-07-02 so the hero showed 'no data' despite 49 signals existing.
   Hero now displays portfolio.total_signals with BUY/SELL split and
   notional; universe becomes a small sub-info.

B. NarrativeCard collapse — tailwind.config.ts was not scanning
   components/, so NarrativeCard's flex-wrap / bg-gradient / border-sky
   classes were JIT-purged. This caused the 49 per_symbol_reasons to
   overflow vertically. Fix: add ./components/**, and rewrite
   NarrativeCard as headline / TL;DR / <details> detail with wrapping
   chip grid.

C. Signal Pipeline 'not measured' visual noise — extract to
   PipelineSection with default-collapsed accordion. Rename to '未計測'
   with muted italic; measured phases now render bright with progress
   bar.

D. ntfy X-Title mangled — 'title.encode(ascii, ignore)' collapsed the
   Japanese headline '7系統49シグナル、BUY主流…' into '749BUYSELL10100%3'.
   narrator.py now validates the headline (ASCII+emoji, <=50 chars) and
   deterministically re-synthesises via _synth_headline when the LLM
   returns Japanese. ntfy.py adds _to_safe_ascii_title as final
   defensive layer that synthesises 'YYYY-MM-DD | N signals |
   BUY x / SELL y | \$Zk' from portfolio when the input is mostly
   non-ASCII.

E. Density / mobile — page.tsx factored into SignalsSection and
   PipelineSection; sys accordions surface top-3 signal chips even
   when collapsed; empty systems collapse into one summary row; hero
   flex-wraps on <380px viewports.

New tests:
- tests/test_ntfy_title_synth.py (2026-07-02 mangled-title regression)
- tests/test_narrator_headline_format.py (post-validation + synth)
- tests/system/test_dashboard_ui_contract.py (tailwind scan, hero
  contract, NarrativeCard flex-wrap contract)"

git push origin claude/monitor-webapp
```

### Vercel 側

- Framework Preset: **Other** (変更禁止 constraint)
- `next.config.js` の `output: 'export'` は変更なし
- `.vercel/project.json` の設定はそのまま
- 通常 GitHub push で自動 deploy がトリガーされる

## 予想 UX 改善 (before / after)

### before (screenshot から)

- 大見出し: **"no data"** — 実際は 49 signals あるのに死んでる
- 直下に narrative body 1000 字が改行なしで垂れ流し
- その右下に 49 銘柄が**縦文字で** SDOT WOLF BNY NUAI... と水平無限流し
- Signal Pipeline は sys1-7 の 6 phase 全部「not measured」で「動いてない monitor」印象
- ntfy: `📈⚠️ 749BUYSELL10100%3` (読めない)

### after (実装)

- hero: **49 signals** (36px 太字) + `BUY 39` `SELL 10` chip + `$37K notional`
- header 右隅に `2026-07-02 · universe 12,445` (universe は sub-info)
- narrator card:
  - 太字 headline (「7系統 49 シグナル…」の日本語は narrator 側で validation され synth 済 headline に置換)
  - TL;DR 140 字 (「買いシグナル 39 件は sys1/sys3/sys4/sys5 に分布…」)
  - `▸ 詳細を見る` (default closed) → 開くと段落分けした full summary + 49 銘柄 chip (flex-wrap で必ず折返し)
- Signal Pipeline: `▸ Signal Pipeline · 2026-07-02` (default closed)
- 各 sys accordion に上位 3 銘柄 chip が summary 行に露出 → 閉じたままでも trader value visible
- 空 systems (sys7 のみ) は「▸ 信号なし (1 systems: sys7)」に集約
- ntfy: `📈 07-02 49 signals / BUY:39 SELL:10 / $37K` (narrator の synth headline を採用)

## 制約遵守 verify

- `core/system1-7` + `common/trade_management.py`: **未変更** (signal logic は触っていない)
- narrator は Claude Haiku 4.5 継続 (`DEFAULT_MODEL = "claude-haiku-4-5-20251001"` 変更なし)
- `next.config.js` の `output: 'export'`: **未変更**
- Vercel Framework Preset (Other): **未変更**
- `-AutoSubmitPaper` default dryrun: 触っていない (publish path 未変更)
