# 保護エンジン硬化 — 2026-08-19

sys5 診断 (2026-08-18 の paper run) で出た「保有保護の穴」と「計測 bug」5 件の
修正記録。**paper 限定・ライブ発注ゼロ**。order 構築を変える箇所は全て flag で
可逆にしてある。

> アロケータの順序切り (`_sort_final_frame` / `_apply_portfolio_caps` の
> sys4/sys5 兵糧攻め) は **本件のスコープ外**。デプラド・アロケータ設計として
> 別途棚上げ。本ドキュメントでは触れていない。

---

## 0. 根拠にした実測 (2026-08-18 22:50 paper / `logs/open_run_20260818/exit_orders.json`)

| reason | 結果 | 件数 |
|---|---|---|
| `protect_stop` | submitted | 10 |
| `protect_target` | **ERROR** (`code 40310000`) | 10 |
| `protect_stop` | **ERROR** (`code 40310000`) | 2 |
| `time_based` | submitted | 1 |

全 12 件の error は `held_for_orders == existing_qty`、すなわち
**「その建玉の株数は既存の未約定注文で全量予約済み」**。recon はこの 12 件を
`armed` (= 保護が張れた) として表示していた。

---

## 1. Alpaca の qty 予約制約 (最重要・仕様)

**Alpaca では 1 つの open order が建玉の qty を全量予約する** (`held_for_orders`)。
したがって **同じ建玉に stop と limit(target) を同時に常駐させられない**。
後から出した注文は必ず `code 40310000 (insufficient qty available)` で拒否される。

観測された競合は 2 種類:

- **S2/S3/S5/S6**: `stop` が qty を握る → `target` が拒否 (当日 10 件)
- **S1/S4**: 前日からの `trailing` が qty を握る → 新規 `stop` が拒否 (当日 2 件)

### 採った対応

| モード | 挙動 | 既定 |
|---|---|---|
| 優先度モード | `trailing > stop > target` で **1 本だけ** 発注。残りは送らず `skip_reason="qty_reserved:*"` を付けて artifact に残す | **ON** |
| OCO モード | `stop` と `target` を **1 本の OCO 注文** に束ねて同時常駐 (qty 予約は 1 回)。`PROTECT_USE_OCO=1` | OFF |

優先度 `trailing > stop > target` は **当日 broker 上で実際に成立していた状態と同一**
(従来も 1 本しか通っていなかった)。したがって既定モードは **broker 上の最終状態を
変えない**。消えるのは「確実に拒否される API 呼び出し」と、それが `armed` に化ける
観測ノイズだけ。

### target が常駐しない間の建て付け

優先度モードでは S2/S3/S5/S6 の利確 (`profit_target`) が **ブローカーに常駐しない**。
ただしこれは今回の変更で失われたものではなく、**従来から 100% 拒否されていて
常駐していなかった**。真の解は OCO (`PROTECT_USE_OCO=1`) で、paper 検証後に
有効化する想定。

---

## 2. 端株 (fractional) は常駐注文を張れない — ブローカー制約

Alpaca は端株に native な `stop` / `limit` / `trailing` を受け付けない
(成行 DAY のみ)。したがって端株建玉は **ザラ場中ブローカー常駐注文ゼロ** で、
`stop`/`target` の評価は **日次 run のその時点でしか行われない**
(synthetic 判定 = 現値が stop/target を突破していれば成行 DAY 全数クローズ)。

2026-08-18 時点で **40 建玉中 25 が端株**。これは制約であって不具合ではないが、
従来 `logger.debug` で黙っていたため「保護されている」と誤読され得た。

### 可視化 (今回追加)

- 突破せず何も出さなかった端株は **WARNING** を出す
  (`常駐保護なし (端株): ... 日次 synthetic 判定に振替中`)。
- `exit_orders_*.json` に `protection_coverage` (建玉ごと) と
  `protection_summary` を書く:
  - `no_resident_order` … 常駐注文が無い建玉数
  - `no_resident_symbols` … その symbol 一覧
  - `daily_evaluation_date` … 日次判定を行った日 (連続監視ではないことの明示)

**整数株部分だけ常駐 stop を張る緩和案は実装していない。** 端株建玉の整数部分だけに
stop を置くと、残りの端数が無保護のまま残り、かつ「保護済み」と数えられて
実態より安全に見える。過剰実装を避け、まず可視化を確実にする方針とした。

---

## 3. orphan (system 帰属不能) の既定保護

`system` がどのソース (position_tracker / entry-coid / symbol_system_map) にも
無い建玉は、従来 time も protection も一切生成されず **完全に無保護** だった
(2026-08-18 時点で FOLD / CDTX の 2 玉 ≈ $4,286)。

- **下方保護の `stop` だけ** を既定値で張る。**close はしない**
  (どちらに手仕舞うかは方向判断なので自動化しない)。
- パラメータは **S1 の値を流用**: `stop_atr_period=20` / `stop_atr_multiplier=5.0`。
  S1 を選ぶ根拠 = 既存 6 system 中で最も緩い (5 ATR) stop であり、素性の分からない
  建玉に tight な stop を当てて不要な手仕舞いを誘発するリスクが最も小さい。
- ATR が取れない銘柄 (delisted 等で rolling 欠損) は entry price からの
  **% ストップ** (`PROTECT_STOP_FLOOR_PCT`, 既定 50%) にフォールバック。
- **time exit / 利確は作らない** (既定 `max_hold` を当てるのは数字の捏造)。
- coid は `protect-orphan-{SYM}-{entry|noentry}-protect-stop` で **日跨ぎ安定**
  (日付を混ぜると毎日 stop を積み上げてしまう)。
- 端株 orphan は native stop 不可のため張れない → `unprotected` として可視化。

判断材料は `unassigned_positions[].default_protection` に載る:
`default_stop` / `none:fractional_native_unsupported` / `none:disabled_by_flag` /
`none:already_open_or_no_price`。

### 3.1 stale ATR ガード (価格が凍った銘柄でハエ叩き stop を張らない)

orphan の母集団は「上場廃止 / 取引停止で価格が凍っている」銘柄に偏る。凍った気配
では ATR がほぼ 0 に潰れ、`5*ATR` stop が entry のすぐ下に張り付く。

実測 (2026-08-18, runner tree の rolling cache):

| symbol | entry | ATR20 | ATR/price | 素の 5*ATR stop | 判定 |
|---|---|---|---|---|---|
| FOLD | $14.26 | 0.03 | **0.21%** | $14.11 (entry の 1% 下) | **stale → フロアへ退避** |
| CDTX | $221.17 | 2.50 | 1.13% | $208.67 (5.7% 下) | 健全 → ATR stop を使用 |

FOLD の $14.11 は「保護」ではなく **ハエ叩き** で、最初の実約定で不本意な成行
手仕舞いを誘発する。これはユーザーが明示的に避けたい「方向判断の自動化」に等しい。
そこで `ATR / entry_price < ORPHAN_MIN_ATR_PCT` (既定 **0.5%**) の場合は ATR を
**使用不能** とみなし、`PROTECT_STOP_FLOOR_PCT` (既定 50%) のフロアに退避して
**WARNING** を出す。

0.5% の根拠: 通常の株式の日次 ATR は概ね 1〜3%。0.5% 未満は halt / 上場廃止の
気配とみなすのが妥当で、健全な低ボラ銘柄を巻き込みにくい。

このガードは **orphan 専用**。system タグ付きの建玉は universe の鮮度管理下にあり、
ATR stop は strategy の意図なので従来どおり素の値を使う。

---

## 4. protective stop の `$0.01` クランプ

旧実装は long stop を `max(0.01, entry - mult*ATR)` で潰していた。ATR が entry
price に対して過大な銘柄では `entry - mult*ATR` が 0 以下になり、stop が
**$0.01 = 実質「保護なし」** に silent に化けていた (発動するのは 100% 損失の手前だけ)。
「注文は通ったが保護されていない」典型的な silent success。

修正後は `entry * (1 - PROTECT_STOP_FLOOR_PCT)` の % ストップにフォールバックし、
**必ず WARNING** を出す。既定 50% の根拠 = ATR stop が使えないほど volatile な銘柄に
S1 の 25% 級 tight stop を当てると通常ノイズで即発動して strategy を壊すため、
「通常変動では触れないが 100% 損失は防ぐ」disaster stop として保守側に振った。

`entry` 自体が非正のときは **stop を出さない** (誤った stop より安全)。

---

## 5. recon: `armed` が失敗を成功表示していた

旧実装は「submitted でない」全件を `armed` に計上していた。`armed` は
「保護注文が張られた」と読まれるため、**ブローカー拒否が成功として表示** されていた。

新しい分類:

| バケット | 条件 | 意味 |
|---|---|---|
| `submitted` (fired) | `order_id` あり & `error` なし | 送信できた |
| `rejected` | `error` あり | **ブローカーが拒否した** |
| `suppressed` | `skip_reason` あり | 送れば確実に拒否されるので送らなかった |
| `armed` | いずれでもない | 純粋に未送信 (dry_run 等) |

当日の実データを新 recon に流し直した結果:

```
exit_submitted 11 / exit_close 1 / exit_protect 10
exit_armed      0   (旧: 12)
exit_rejected  12   (新バケット)
```

ダッシュボード / ntfy の Exit phase にも `rejected` / `suppressed` が別枠で載る。

---

## 6. Feature flags (全て可逆)

| Flag | 既定 | 効果 |
|---|---|---|
| `ORPHAN_DEFAULT_PROTECTION` | **ON** | orphan に既定 protective stop を張る。`=0` で従来どおり完全 skip |
| `PROTECT_STOP_FLOOR_ENABLED` | **ON** | long stop が 0 以下になるとき % フロアを使う。`=0` で旧 `$0.01` クランプ |
| `PROTECT_STOP_FLOOR_PCT` | `0.50` | フロア割合。`(0,1)` 範囲外 / 不正値は既定へフォールバック (WARN) |
| `ORPHAN_MIN_ATR_PCT` | `0.005` | orphan で ATR を信用する下限 (ATR/price)。下回ると stale とみなしフロアへ退避 |
| `PROTECT_USE_OCO` | **OFF** | `stop`+`target` を 1 本の OCO で同時常駐。有効化前に paper 検証が必要 |

既定 ON の 2 つはいずれも「無保護を保護に変える」方向であり、回帰テストを付けてある
(`tests/test_protection_hardening_20260819.py`)。

---

## 7. 付随修正

`common/broker_alpaca.py` の OCO 分岐だけ `client_order_id` を渡し忘れており、
再送時に冪等 dedup (422 duplicate) が効かず **二重発注し得た**。他の order_type と
同じく冪等キーを付けた。OCO は既定 OFF なので現時点で実害はないが、有効化前提の穴。

---

## 8. テスト

- `tests/test_protection_hardening_20260819.py` … 42 tests (新規)
- 契約が変わった既存テストを更新 (orphan が stop を得るようになったため):
  `test_alpaca_exit_orders.py` / `test_unmanaged_positions_surface.py` /
  `test_exit_tag_resolution_local.py` / `test_pipeline_exit_wiring_20260729.py`
  — いずれも「time/close exit は捏造しない」契約は維持し、flag OFF で従来挙動に
  戻ることも固定した。
- 全体スイープ: base commit `64ae9b1` と失敗 ID 集合を `comm` で突合し
  **新規失敗ゼロ** を確認 (既存の 251 件は本件と無関係な legacy failure)。

---

## 9. 次サイクルで arm される見込み (FOLD / CDTX)

実 position データ (`logs/open_run_20260818/final_positions.json`) を planner に
流した結果、runner tree を更新すれば次の exit サイクルで以下が **native GTC stop**
として発注される:

| symbol | qty | side | entry | ATR20 | stop | coid |
|---|---|---|---|---|---|---|
| FOLD | 143 | long | $14.26 | 0.03 (stale) | **$7.13** (50% フロア) | `protect-orphan-FOLD-noentry-protect-stop` |
| CDTX | 10 | long | $221.17 | 2.50 | **$208.67** (5*ATR) | `protect-orphan-CDTX-noentry-protect-stop` |

両建玉とも **整数株** なので native stop を張れる (端株なら張れない)。両者とも
`orphan_no_system_origin` 分類で `untradable_no_exit_possible` ではないため
broker 側の受理が見込める。**close はしない** ので方向判断はユーザーに残る。
coid は日跨ぎ安定なので翌日以降に積み上がらない。
