# システム監査レポート — 2026-07-02 (opus-4-8 / docs 主軸 re-review)

**監査者**: Claude (claude-opus-4-8)
**方式**: `docs/systems/システム1〜7.txt` を主軸に、`core/systemN.py` / `common/trade_management.py` / `common/profit_protection.py` / `strategies/*` / `common/system_setup_predicates.py` を照合する doc-vs-code 一気通貫レビュー
**前回監査**: `docs/SYSTEM_AUDIT_20260701.md`（branch `claude/system-audit`）— **本レポートは置換ではなく併存**。前回の指摘を再検証し、証拠を強化した。
**制約遵守**: `core/system1-7.py` + `CacheManager` + `common/trade_management.py` は **read-only**。実発注ノータッチ。修正は patch 提案のみ（未実装）。

> ⚠️ 重要な前提: 本監査は「仕様書 (.txt)」を規範として code を照合したが、**規範の実装はプロジェクト方針上 `core/systemN.py` が最終権威**である。以下で「乖離」と記した箇所は「仕様書テキストと実装コードの差」であり、必ずしも実装バグを意味しない。各項目に **判定（仕様が正 / 実装が正 / 要人間確認）** を付す。

---

## Part 1: docs 主軸の戦略解説（7 system × 思想・論理的必然性）

### System1 — ロング・トレンド・ハイ・モメンタム（配分 25%）
**思想**: 「強いものはさらに強くなる」順張りモメンタム。SPY が 100日SMA 上（地合いフィルタ）かつ銘柄自身が 25>50日SMA の上昇トレンドにあるときだけ、200日ROC 上位＝過去1年で最も上げた銘柄を買う。
**論理的必然性**: SPYフィルタは「相場全体が上向きのときだけロングを取る」システミックリスク遮断。25/50SMA は中期トレンドの持続性、200日ROC はモメンタムの強度ランキング。損切り 5×ATR20 は「トレンドは緩い、ノイズで振り落とされない広いストップ」という順張り哲学の帰結。利益目標なし＋25%トレーリングは「勝ちトレードを最大限伸ばす」トレンドフォローの核心。
**特性（doc 記載範囲）**: 明示的な過去 backtest 数値は spec に無し。設計上は低勝率・高ペイオフ（トレンドフォロー典型）。

### System2 — ショート RSI スラスト（配分 40%）
**思想**: 短期的に買われ過ぎた銘柄の平均回帰を空売り。3日RSI>90 の過熱 + 2日連続高値引けの「最後のひと吹き」を、翌日 +4% 以上のギャップアップで売る。
**論理的必然性**: RSI3>90 は極端な短期過熱、2日連続上昇は「まだ上げている＝踏み上げ前の頂点」を捉える。+4%指値は「さらに吹き上がった瞬間だけ約定」させ悪いエントリーを排除。ATR%≥3% フィルタは「動く銘柄でないと空売りの妙味がない」。3日RSI・7日ADX ランキングで最も過熱・最も勢いのある銘柄を選ぶ。利確 4%／2日タイムアウトは「短期スラストの反落は速い」前提。
**特性**: 短保有・高回転。ショート枠 40% は大きく、暴騰リスク管理が肝。

### System3 — ロング・ミーン・リバージョン・セルオフ（配分 25%）
**思想**: 上昇トレンド中の急落を拾う押し目買い。150日SMA 上（長期上昇継続中）の銘柄が 3日で 12.5% 以上急落したところを、さらに 7% 下の指値で買う。
**論理的必然性**: 150日SMA 上限定で「構造的な上昇トレンド銘柄」に限定し、落ちるナイフの中でも回復力の高いものを選別。3日で12.5%下落は「パニック的売られ過ぎ」、-7%指値は「もう一段の投げ売りにだけ乗る」逆張りの規律。下落幅降順ランキングで最も売られた銘柄を優先。4%利確／3日タイムアウトで「反発は短命」を織り込む。

### System4 — ロング・トレンド・ロー・ボラティリティ（配分 25%）
**思想**: S&P500 が 200日SMA 上の強気相場で、低ボラの安定トレンド銘柄を買う。HV 10〜40% の「動きすぎない」銘柄を、4日RSI 昇順（短期押し目）で拾う。
**論理的必然性**: 200日SMA 二重条件（指数＋銘柄）で長期トレンドを二重確認。HV バンドは「暴れ馬でも死に体でもない」中庸ボラ。4日RSI 昇順は「トレンド内の一時的な弱さ」を買う押し目タイミング。損切り 1.5×ATR40（タイトめ）＋20%トレーリング＋利食いなしは「低ボラゆえ狭いストップでも耐えられ、伸びる限り持つ」設計。

### System5 — ロング・ミーン・リバージョン・ハイADX・リバーサル（配分 25%）
**思想**: 強いトレンド（高ADX）の中の短期的な売られ過ぎ反転を狙う。100日SMA+ATR 上・7日ADX>55・3日RSI<50 の三条件で「勢いはあるが一時的に押した」局面を、3% 下の指値で買う。
**論理的必然性**: 高ADX は「トレンドが生きている」証拠、RSI3<50 は「その中で一時的に緩んだ」タイミング。ATR ベースの利確（1×ATR10）と 6日タイムアウトは「反発は測れる幅・短期」という平均回帰の想定。
**⚠️ 注**: 実装は仕様と大きく乖離（Part 2 参照）。

### System6 — ショート・ミーン・リバージョン・ハイ・シックスデイサージ（配分 40%）
**思想**: 6日で 20% 以上の暴騰後の反落を空売り。+5% 指値で売り、5% 利確 or 3日タイムアウト。
**論理的必然性**: 6日+20% は「短期に行き過ぎた急騰」で反落確率が高い。2日連続高値引けで「まだ天井前」を捉え、+5%指値でさらなる吹き上げにだけ乗る。spec 自身が「return_6d>0.20 は非常に厳しく候補0も正常」と明記——**低頻度・高選別**が設計思想。

### System7 — カタストロフィーヘッジ（配分 20%、**変更禁止**）
**思想**: 収益ではなく損失軽減が目的の SPY 専用ヘッジ。SPY が 50日安値を割ったら翌日寄りで空売りし、70日高値回復まで保有。
**論理的必然性**: 50日安値割れ＝下落相場入りのシグナル。ロング6システムが最も痛む局面で SPY ショートが利益を出し、ポートフォリオ全体のドローダウンを相殺する「保険」。ランキング不要（対象は SPY のみ）、利食い目標なし（保険はトレンド転換まで持つ）。3×ATR50 の広いストップは「保険を早期に切らない」ため。

**資本配分の根拠**: `docs/systems/INDEX.md` — ロング枠（S1/3/4/5）各 25%、ショート枠（S2/6）各 40%＋S7 20%。各バケット内で 100% になる構成。配分の一元管理は `core/final_allocation.py` / `data/symbol_system_map.json`。spec の各 .txt は「リスク2%・最大10%・最大10ポジション」というポジションサイジング規律を共通に持つ。

---

## Part 2: entry / exit / filter 整合性 3-layer table

凡例: ✅一致 / ⚠️乖離（実装が仕様と異なる） / ❓要確認 / 💀デッドコード

### Layer 1 — Entry rules（仕様 vs 実装）

| Sys | 仕様（.txt） | 実装 | 判定 | 引用 |
|-----|------------|------|------|------|
| 1 | 翌日寄付・成行 | live候補 `entry_price=Close`（同足終値, proxy）／backtest は翌日Open | ⚠️→Part3 | `core/system1.py:1178`, `strategies/base_strategy.py:478` |
| 2 | 前日終値+4%以上で売り指値 | `entry_price_offset_pct=4.0`, LIMIT short | ✅ | `common/trade_management.py:211` |
| 3 | 前日終値-7%の買い指値 | `entry_price_offset_pct=-7.0`, LIMIT long | ✅ | `common/trade_management.py:225` |
| 4 | 寄付・成行（スリッページ無視） | `entry_reference="open"`, MARKET／live候補 `Close if>0 else Open` | ⚠️→Part3 | `common/trade_management.py:239`, `core/system4.py:349` |
| 5 | 前日終値-3%の買い指値 | `entry_price_offset_pct=-3.0`, LIMIT long | ✅ | `common/trade_management.py:251` |
| 6 | 前日終値+5%の売り指値 | `entry_price_offset_pct=5.0`, LIMIT short | ✅ | `common/trade_management.py:266` |
| 7 | 翌日寄付・成行 | backtest `entry_price=Open[entry_idx]`（翌営業日） | ✅ | `strategies/system7_strategy.py:235` |

### Layer 2 — Exit / Stop rules（仕様 vs 実装）

| Sys | 仕様 stop | 仕様 exit | 実装 stop | 実装 exit | 判定 | 引用 |
|-----|-----------|-----------|-----------|-----------|------|------|
| 1 | 20日5ATR / 25%トレーリング / 利食い無 | — | `stop_atr_period=20, ×5.0`, trailing 25% | — | ✅ | `trade_management.py:200-204` |
| 2 | 10日3ATR | 4%利確→翌大引け / 2日で未達→翌大引け | 10日×3.0, %target=4, max_hold=2 | 含み益4%/2日→大引け | ✅ | `trade_management.py:214-218`, `profit_protection.py:238-246` |
| 3 | 10日2.5ATR | 4%利確→翌大引け / 3日で未達→翌大引け | 10日×2.5, %target=4, max_hold=3 | 4%/3・6日→大引け | ✅ | `trade_management.py:227-232` |
| 4 | 40日1.5ATR / 20%トレーリング / 利食い無 | — | 40日×1.5, trailing 20% | — | ✅ | `trade_management.py:240-243` |
| 5 | 10日3ATR | 1×ATR10利確→翌寄り / 6日で未達→翌寄り | 10日×3.0, atr target=1.0, max_hold=6 | 6日→翌寄り | ✅（stop/exit） | `trade_management.py:254-259`, `profit_protection.py:247-253` |
| 6 | 10日3ATR | 5%利確→翌大引け / 3日→大引け | 10日×3.0, %target=5, max_hold=3 | 5%/3日→大引け | ✅ | `trade_management.py:269-273`, `profit_protection.py:238-241` |
| 7 | **50日3ATR** / 70日高値まで保有→翌寄り | 利食い無 | **実路: ATR50×`STOP_ATR_MULTIPLE_DEFAULT`(=3.0)** / 70日高値(max_70)→翌寄り | ✅（実路） | ⚠️ **stub矛盾**→Part4 | `strategies/system7_strategy.py:56,253,275-278`, `constants.py:19` |
| 7 | 同上 | 同上 | 💀 **`SYSTEM_TRADE_RULES["system7"]`= 20日×5.0**（未使用stub, comment「詳細は要確認」） | 💀 | 💀 デッドコード | `trade_management.py:276-286` |

### Layer 3 — Filter / Setup chain（仕様 vs 実装）

| Sys | 仕様 filter+setup | 実装（`core/systemN.py` 権威） | 判定 | 引用 |
|-----|-------------------|-------------------------------|------|------|
| 1 | DV20>5000万$, 株価≥5 / SPY>100SMA, 25SMA>50SMA, 200ROC降順 | Close≥5, DV20≥5000万 / sma25>sma50, roc200>0 | ✅ | `system_setup_predicates.py:100,115` |
| 2 | 株価≥5, DV20>2500万$, ATR10≥3% / RSI3>90, 2日連続上昇 / 7日ADX降順 | Close≥5, DV20>2500万, atr_ratio>0.03, rsi3>90, twodayup | ✅ | `system_setup_predicates.py:236-242` |
| 3 | 株価≥**1**, **50日平均出来高≥100万株**, ATR10≥5% / Close>150SMA, 3日で12.5%下落 | Close>**5**, **DV20>2500万$**, atr_ratio≥0.05, drop3d≥0.125 | ⚠️ **filter乖離** | `core/system3.py:26,295-300` |
| 4 | 50日平均売買代金>1億$, HV 10-40% / S&P500>200SMA, 銘柄>200SMA / 4日RSI昇順 | DV50>1億, 10≤hv50≤40, Close>sma200 | ✅（SPY条件は別途）| `system_setup_predicates.py:259` |
| 5 | 50日出来高>50万株, 50日売買代金>250万$, **ATR>4%** / **Close>100SMA+ATR10**, **7日ADX>55**, **3日RSI<50** | Close≥5, **adx7>35**, **atr_pct>2.5%** のみ（filter==setup） | ⚠️ **重大乖離** | `core/system5.py:56,59,63,96,123` |
| 6 | 株価≥5, 50日売買代金>1000万$ / 6日で20%上昇, 2日連続高値引け / 上昇率降順 | return_6d>0.20, uptwodays | ✅（setup）| `system_setup_predicates.py:302-308` |
| 7 | フィルタ無 / SPY 50日安値 | Low≤min_50 | ✅ | `core/system7.py:130`, `system_setup_predicates.py:289-295` |

---

## Part 3: entry_price = 同足 Close の検証結論

**判定: 意図的な approximation（look-ahead ではない）。ただしコメントとの不整合＝要修正。**

**根拠**:
1. **live 候補生成路** (`core/system1.py:1178` 他): `entry_price = close_val`（=セットアップ日の終値）。直上コメントは「翌日寄り付きで買い」(`:1177`)。使うのは **その日の確定終値**＝シグナル生成時点で既知の過去/現在値。**未来データ（翌日Open）は一切参照していない → look-ahead 情報リークではない**。
2. **backtest 路** (`strategies/base_strategy.py:478`): `entry_price = df.iloc[entry_idx]["Open"]`（entry_idx=翌営業日）、ATR は `entry_idx-1`（前日）から取得 (`:488`)。**約定は正しく翌日Open、指標は前日まで → look-ahead 無し**。
3. `docs/ENTRY_EXIT_TEST_RESULTS.md:120-126` の想定 fill も System1/4/7=Open, 指値系=prev_close×係数 で一貫。
4. System7 も同型: 候補 payload `entry_price=Close.iloc[-1]`（`core/system7.py:269`）は表示/サイジング用 proxy、実約定は `Open[entry_idx]`（`system7_strategy.py:235`）。

**残存リスク（bug ではないが監査対象）**: live のポジションサイジング（`shares = risk / (entry_price - stop_price)`）が **セットアップ日終値を entry proxy** に使う一方、実発注は翌日寄り。翌日 Open が終値から乖離するほど、リスク%（2%）・最大10%配分が実際とズレる **fill/slippage 近似誤差**。方向性の情報リークは無く、影響は寄り付きギャップ幅に有界。

**patch 提案（未実装）**:
- `core/system1.py:1177` 等のコメントを「翌日寄付で発注。ここでの entry_price はサイジング用に当日終値を代理値として使用（実約定は翌営業日 Open）」へ修正し、proxy であることを明示。
- もしくは live サイジングでも直近 Open を使う一貫化を検討（要 backtest 影響評価）。

---

## Part 4: System7 exit 不整合の判定

**判定: 実装（ATR50×3, 70日高値→翌寄り）が正。仕様書と一致している。矛盾の正体は `SYSTEM_TRADE_RULES["system7"]` の未使用 stub。**

**証拠固め（推測でなく doc/comment/code）**:
1. 仕様書 `システム7.txt`: 「過去50日間の3ATR の位置に損切り」「SPY が直近70日間の高値を付けるまで保有→翌寄りで手仕舞い」。
2. 実装 `strategies/system7_strategy.py:56` docstring: 「ストップ: エントリー + 3×ATR50」「利確: 直近70日高値(max_70)を更新した翌営業日寄り」。
3. 実 backtest 路 `system7_strategy.py:253` `stop_price = entry_price + stop_mult * atr`、`stop_mult = STOP_ATR_MULTIPLE_DEFAULT = 3.0`（`constants.py:19`）、`atr` は ATR50（`core/system7.py:74,105`）。→ **50日3ATR、仕様一致**。
4. exit `system7_strategy.py:275-278`: `High >= max_70 → 翌 idx の Open で決済`。`max_70` = 70日高値（`core/system7.py:119`, `profit_protection.py:22-49`）。→ **70日高値→翌寄り、仕様一致**。
5. 一方 `common/trade_management.py:276-286` の `SYSTEM_TRADE_RULES["system7"]` は `stop_atr_period=20, stop_atr_multiplier=5.0`、コメント「SPY固定、詳細は要確認」。この dict は System7 の backtest/exit 経路で **参照されていない**（System7 は strategy 側カスタム run_backtest を使い、`core/system7.py:27` にも「run_backtest は strategy 側にカスタム実装が残る」と明記）。

**結論**: 前回監査 B9 が指摘した「仕様50日3ATR vs code 20日5ATR」の乖離は、**実運用ロジックは 50日3ATR で仕様準拠**であり、乖離の実体は `trade_management.py` 内の **未使用プレースホルダ stub**（他6システムの dict を機械的にコピーし System7 だけ埋め忘れた痕跡）。事故リスクは低いが、将来 `SYSTEM_TRADE_RULES` を System7 に配線した瞬間に **stop が 20日5ATR に化ける時限バグ**。

**patch 提案（未実装, trade_management.py は read-only ゆえ提案のみ）**:
```python
# common/trade_management.py:276  System7 stub を実路と一致させる
"system7": SystemTradeRules(
    system_name="system7", side="short",
    entry_type=OrderType.MARKET, entry_reference="open",
    stop_atr_period=50, stop_atr_multiplier=3.0,   # 20/5.0 → 50/3.0（仕様準拠）
    use_trailing_stop=False, profit_target_type="none",
    max_holding_days=0,  # 70日高値までの保有はコードで別管理
),
```
または、参照されない stub なら **明示的に「未使用・System7 は strategy 側で完結」コメントを付す**。

---

## Part 5: 事業リスク再列挙（前回 top5 + opus-4-8 追加、優先順再付け）

### 🔴 P0（運用開始前に必須）

1. **System5 の setup が仕様から重大乖離**（新規強調）
   - 仕様: Close>100SMA+ATR10, ADX7>**55**, RSI3<**50**, ATR>**4%**, 出来高/売買代金フィルタ。
   - 実装 (`core/system5.py:96,123`): Close≥5 & adx7>**35** & atr_pct>**2.5%** のみ（filter==setup）。**100SMA+ATR バンド・RSI3<50・ADX>55・4%ATR が enforcement されていない**。
   - 影響: System5 は「高ADXの押し目リバーサル」ではなく、はるかに**緩い条件で無差別ロング**している可能性。過去 backtest 特性（勝率/DD）が spec 前提と別物になる。
   - 対応: 実装が意図か spec 誤りか **Core Team 判定必須**。意図なら spec 更新、そうでなければ setup 条件追加（backtest 再検証込み）。

2. **entry_price look-ahead 監査 → 決着**（Part3）
   - look-ahead では**ない**。ただし live サイジングの終値 proxy とコメント不整合を修正推奨。P0→**P2 へ降格可**（情報リーク無しのため）。

3. **System7 exit stub 時限バグ**（Part4）
   - 実路は仕様準拠。`SYSTEM_TRADE_RULES["system7"]` の 20日5ATR stub が将来配線されると事故。stub 修正 or 明示コメントで無害化。P1 相当。

### 🟠 P1（早期対応）

4. **日次シグナルの時系列保存**（前回踏襲）: 現状の当日上書き運用では、後日の再現・regime 分析・fill 検証ができない。日次スナップショット永続化を推奨（メモリ `[[ps1-utf8-bom-and-stderr]]` の Task Scheduler 経路に組込み）。

5. **SPY 欠損の無警告**（前回踏襲, 部分改善）: `core/system7.py:74` は atr50 欠損で `IMMEDIATE_STOP` を送出するが、`profit_protection.is_new_70day_high` は例外時 `None` を静かに返す（`profit_protection.py:48`）。SPY データ欠損時、ヘッジ判定が「判定失敗」表示のまま **ロング6本が無防備**になる沈黙故障。欠損を上位に alert する導線が必要。

6. **System3 filter 乖離**（Part2 Layer3）: 株価≥1→≥5、50日平均出来高100万株→DV20>2500万$ に置換。銘柄ユニバースが仕様より**大幅に絞られる**（低位株除外）。意図的リファクタ（code コメントで宣言）だが spec と不一致。spec 更新 or 復元を判定。

### 🟡 P2（opus-4-8 追加の独自視点）

7. **position sizing の robustness（小口 vs 大口）**: `shares_by_cap = max_position_value // entry_price`（`system7_strategy.py:264`）等、整数株の切り捨てが**小資本で顕著**。低位株除外（System3 の Close≥5 実装）と相まって、資本が小さいと最大10ポジションを埋められず配分が崩れる。資本レンジ別の配分達成率テストを推奨。

8. **hedge の timing lag（System7）**: セットアップ（50日安値）→翌営業日寄りエントリーの1日ラグ。ギャップダウン相場では**最も守ってほしい初日に無ヘッジ**。さらに live 候補 `entry_price=Close`（`core/system7.py:269`）と実約定 Open のズレ。急落時のヘッジ有効性を歴史的ギャップ日で定量化すべき。

9. **signal 生成の determinism**: `system_setup_predicates.py:32` が `random` を import し、`validate_predicate_equivalence` がサンプリングに `random.shuffle`（`:417`）を使用。検証専用で本番シグナルには非関与だが、`random.seed` 未固定のため**検証結果が非再現**。監査可能性のため seed 固定を推奨。

10. **過去 backtest の regime dependency**: spec に歴史的 DD/勝率の記載が無く（全 .txt）、System6 のように「候補0が正常」な超低頻度戦略を含む。強気相場サンプルに過適合した配分（ショート枠40%×2）でないか、**レジーム別（2008/2020/2022）の寄与分解**を運用前に用意すべき。

11. **filter/setup の二重実装リスク**: `common/system_setup_predicates.py` は「まだ利用箇所を差し替えていない」(docstring:9) 未配線ヘルパで、`core/systemN.py` の実 mask と**別物**（System5 は特に乖離）。二重定義は将来「どちらが真か」の事故源。single-source-of-truth 化を推奨。

### リスク優先順サマリ（前回比）
| # | 項目 | 前回 | 今回 | 変化 |
|---|------|------|------|------|
| CI/cron 全停止 | 解消済 | — | 除外 |
| System5 setup 乖離 | （未検出）| **P0** | ⬆ 新規最重要 |
| entry_price look-ahead | P0疑い | P2 | ⬇ 情報リーク無しと確定 |
| System7 exit stub | B9 乖離 | P1 | 実路は仕様準拠と確定 |
| 日次時系列保存 | top5 | P1 | 継続 |
| SPY 欠損無警告 | top5 | P1 | 継続 |
| System3 filter | （未検出）| P1 | ⬆ 新規 |
| sizing/hedge lag/determinism/regime | — | P2 | ⬆ opus-4-8 追加 |

---

## 付録: 参照 file:line 一覧（主要）
- 仕様: `docs/systems/システム1〜7.txt`, `docs/systems/INDEX.md`
- entry/exit rules: `common/trade_management.py:194-287`, `strategies/base_strategy.py:456-504`
- System7: `core/system7.py:74,105,119,130,267-278`, `strategies/system7_strategy.py:56,235,253,275-278`
- filter/setup: `core/system3.py:295-300`, `core/system5.py:56-63,96,123`, `common/system_setup_predicates.py`
- exit 判定: `common/profit_protection.py:22-49,232-253`
- 定数: `strategies/constants.py:19`
- テスト根拠: `docs/ENTRY_EXIT_TEST_RESULTS.md`（22/22 pass, 2025-11-03）
</content>
</invoke>
