# プール上限が効いたときの「切り捨て順」の構造的偏り — de Prado 視点の精査と修正方針

Status: 2026-08-28。フラグ `risk.fair_pool_trim` は **既定 OFF**（OFF は現行挙動と byte 一致）。
paper のみ。発注コード・サイジング・リスク計算・40/30/70 の上限**値**は一切変更しない。
変えるのは「上限が効いたときにどの system の枠を落とすか」という**順序だけ**。

---

## 1. 現状 — 何が起きているか（実測で確認済み）

### 1.1 コード経路

| 場所 | 何をしているか |
|---|---|
| `core/final_allocation.py:1893` `_sort_final_frame` | `sort_cols = ["side", "_system_no"]` で安定ソート。→ **long ブロック（S1,S3,S4,S5）→ short ブロック（S2,S6,S7）** の並びが確定。各 system 内は `score` 順（system4 のみ昇順）。 |
| `core/final_allocation.py:1996` `_apply_portfolio_caps` | その並びを**先頭から**なめて `n_total >= allow_total` / `n_long >= allow_long` / `n_short >= allow_short` になった時点以降を捨てる。= **末尾切り捨て**。 |
| `core/final_allocation.py:166` `_cap_slots_by_side` | largest-remainder の端数決定を `key=(-frac, item)` = **名前のアルファベット順**で決める（`slots_from_capital` ON の経路のみ）。 |

`_sort_final_frame` の並び順（実測）:

```
system
system1     1
system3    16
system4    31
system5    46
system2    61
system6    76
system7    91
```

### 1.2 結果 — S5 と S7 が構造的に最初の犠牲

各 system が 15 本要求、上限 L40/S30/T70 を素で当てた実測:

```
=== kept per system (demand 15 each, caps L40/S30/T70) ===
system1    15
system3    15
system4    10
system5     0     <-- long プールが system4 で尽きる
system2    15
system6    15
system7     0     <-- total プールが system6 で尽きる

trimmed: {'long_count': 20, 'total': 15}
```

上流の `max_positions=10` を通した現実的なシナリオ（既保有 long18 / short9 → allow_long=22, allow_short=21, allow_total=43）:

```
system1 10, system3 10, system4  2, system5  0
system2 10, system6 10, system7  1
```

`_cap_slots_by_side` の端数（raw_slots 完全同値 = 純粋な tie）:

```
side_cap=38: {system1:10, system3:10, system4:9,  system5:9 }
side_cap=39: {system1:10, system3:10, system4:10, system5:9 }
side_cap=41: {system1:11, system3:10, system4:10, system5:10}
side_cap=28: {system2:10, system6:9,  system7:9 }
side_cap=31: {system2:11, system6:10, system7:10}
```

→ **端数は必ず S1 / S2 が得て、S5 / S7 が失う。**

### 1.3 これは「最適化の余地」ではなく「仕様の破れ」

上限値そのものは config.yaml で

```yaml
max_long_positions: 40       # long 側 建玉数上限 (sys1/3/4/5 × 10)
max_short_positions: 30      # short 側 建玉数上限 (sys2/6/7 × 10)
```

と、**system あたり 10 枠の等分**として定義されている。にもかかわらず実装は
「ソート順で先に来た system が総取り」なので、S5 は 0、S7 は 0〜1 になる。
つまり **live で回っている portfolio は、上限値が想定していた portfolio ではない**。
これは推定の問題ではなく仕様の取りこぼしであり、直すべきは「並びの偏り」だけ。

de Prado 的に重要なのは、ここで**賢くしようとしないこと**。仕様（等分）へ戻すのが
目的で、配分層に新しい最適化を差し込むのは別の話（後述 D）。

---

## 2. 候補 A〜D の評価

前提として置く de Prado の原則:

- **P1 多重性と過学習** (Ch.11 / DSR): 配分層に自由パラメータを増やすと、それ自体が
  新しい試行次元になり選択バイアスを膨らませる。本 repo の DSR は修正後の再測定で
  **7 系統・統合すべて 0.95 未達 (FAIL)**（`CLAUDE.md`, `docs/VALIDATION_REMEASURE_LIMIT_FILL_20260821.md`）。
  この状態で「チューニング済みの配分層」を足すと、数字の信頼度は上がるどころか下がる。
- **P2 共分散の推定誤差** (Ch.16, HRP の動機 / "Markowitz's curse"): 相関の高い資産が
  増えるほど共分散行列の条件数が悪化し、最適解は不安定になる。
- **P3 look-ahead 禁止** (Ch.7 purge/embargo): 判定時点で入手可能な情報しか使えない。
- **P4 turnover / 取引コスト**: backtest に映らないコストを生む規則は不可。
- **P5 決定論・再現性**: 監査と replay（`scripts/replay_portfolio_caps.py`）が成立すること。

### (A) largest-remainder + 走行ごとの ROTATING / seeded tie-break

- **単体では今回の偏りを直せない。** 今の偏りは端数の問題ではなく「ブロック総取り」の
  問題（S5 は 9 対 10 で負けているのではなく、22 枠のうち **0** しか得ていない）。
  largest-remainder は「まず割当基準（quota basis）」が要り、rotation はその上の
  tie-break にすぎない。→ **完全な規則ではなく部品**。
- path-dependence の評価:
  - **look-ahead は無い**（P3 OK）。offset は当日の signal_date だけから決まり、
    未来の情報を含まない。
  - **再現性はある**（P5 OK）。`offset = signal_date.toordinal() mod n` は純関数で、
    永続 state を持たない。replay が同じ答えを出す。
  - **churn の上限**: offset が 1 動くと、束縛しているプール **1 つあたり最大 1 枠**が
    別 system へ移る（証明は §4 G3）。
  - **決定的に重要な点**: この層は **新規エントリーのゲートだけ**で、既存建玉を
    決済しない（決済は各 system の exit 規則と protect エンジンの担当）。
    したがって「昨日 S5 が枠を得て今日は得ない」は **round trip を生まない** ——
    de Prado が問題にする往復取引コストはここでは発生しない。P4 は当たらない。
- **判定: 端数の tie-break としてのみ採用。** ±1 の上限を明示して使う。

### (B) round-robin（system 横断で 1 枠ずつ配る）

- これは progressive fill = **max-min fair**。保証: 同じ side で未消化候補が残っている
  2 system 間の枠数差は **必ず 1 以下**。片方が 10、もう片方が 0 は原理的に起きない。
- **推定入力ゼロ・時間 state ゼロ・単調・決定論**。P1/P2/P3/P5 すべてクリア。
- **上限値の定義そのもの（system あたり 10 の等分）と一致する。** 新しい思想を持ち込むのでは
  なく、config.yaml が既に書いている仕様へ戻すだけ。これが最大の論拠。
- 弱点: 資金ウェイトを見ない。short 側のウェイトは 0.40/0.40/0.20 なので、**件数**を
  等分すると S7 はドル換算で相対的に厚遇される。
  - 反論: 件数上限はドル上限ではない。ドルウェイトは既に
    `long_allocations`/`short_allocations`（サイジング/予算層）と、ON のときは
    `derive_capital_weighted_slots`（上流の枠導出）で効いている。trim 層でもう一度
    ウェイトを掛けるのは**二重計上**。
- **判定: quota basis として採用。**

### (C) 資金ウェイト比例で落とす

- 安定・時間 state なし・推定なし。**筋は良い**。真面目に検討した。
- しかし却下:
  1. **二重計上**（上記）。特に `slots_from_capital` ON のときは上流の枠が既に
     capital-derived なので、trim 層でもウェイトを掛けると**ウェイトが二乗**になる。
  2. **恒久的な順位を焼き付ける。** S7 は 0.20 なので、プールが束縛するたびに常に
     最後尾寄りになる。これは「ソート順で常に最後」を「ウェイトで常に最後」へ
     置き換えただけで、ユーザーが消したい構造的偏りの弱い版が残る。
  3. 件数上限の意味論（等分）と config のコメントに矛盾する。
- **判定: 主基準としては却下。** ただし `slots_from_capital` ON のとき、上流の枠が
  capital-derived になっている上で残余の希少性を round-robin で中立に配る、という
  **合成**が正しい形になる（本実装はそうなっている）。

### (D) リスク / 分散寄与を見て落とす（相関考慮）

一見もっとも "de Prado らしい"。しかし**中身はこの層で de Prado が警告している当のもの**。

1. **推定誤差** (P2): 7 系統から日々 200〜300 の候補が出る。T≈250 営業日に対し
   N≈70 建玉の標本共分散はほぼ特異で、marginal risk contribution のランキングは
   ノイズで反転する。「最も分散に寄与しない枠」は日替わりで入れ替わる。
2. **自由パラメータの追加** (P1): lookback / shrinkage / 相関の閾値 = 新しい試行次元。
   DSR が全系統 FAIL の現状に、チューニング可能な配分層を足せば、
   **その数字はさらに信じられなくなる**。改善したかを測る手段がこの repo に今無い。
3. **レジーム依存の致命的な失敗モード**: 「最も分散に寄与しない = 現在ブックと最も
   相関が高い」系統は、ドローダウン局面ではまさに **S7（カタストロフィーヘッジ）**に
   なりうる。リスク考慮 trim は**ヘッジが必要な時にヘッジを優先的に捨てる**。
   これは仮説ではなく、相関ベース規則の素直な帰結。
4. **leakage 危険** (P3): 判定日を含む窓で相関を取れば即 look-ahead。point-in-time を
   正しく組むのは実際に手間で、しかも効果を検証する土台（信頼できる DSR）が無い。
- **判定: 却下。** 「配分層に根拠の薄い最適化とノイズを持ち込むな」という de Prado の
  警告がそのまま当てはまるケース。分散考慮サイジングが欲しいなら、CPCV/DSR の証拠を
  付けた**独立の実験**としてやるべきで、tie-break 修正に紛れ込ませるものではない。

---

## 3. 採用する規則 — fair pool trim

> **プールが束縛したら、system 横断の round-robin（max-min fair）で 1 枠ずつ配る。
> 端数（1 ラウンド未満の残り）だけは signal_date から決まる offset で回す。
> system 内の優先順位は従来どおり score 順を維持する。**

- quota basis = **(B) round-robin**（推定入力ゼロ）
- 端数 tie-break = **(A) rotation**（±1 に上限、churn 証明あり）
- **(C) は却下**（二重計上 + 恒久順位）／**(D) は却下**（推定誤差・過学習・ヘッジを先に捨てる）

system 内の score 順を残すのは、score が**実在する情報**（その system 内での候補の優劣）
だからで、system **間**の順序だけが情報ゼロの sort 副産物だった。直すのは後者だけ。

---

## 4. 保証（実装が満たすべき性質）

| | 内容 |
|---|---|
| **G1 構造的最後尾なし** | 同一 side・未消化候補あり・exposure で弾かれていない 2 system 間の採用数差は ≤ 1。 |
| **G2 look-ahead なし** | 入力は当日フレームと当日 signal_date のみ。 |
| **G3 churn 有界** | rotation offset が 1 変わると、束縛プール 1 つあたり移動は最大 1 枠。 |
| **G4 強制決済なし** | この層は新規エントリーのゲート。既存建玉を閉じないので G3 の枠移動は往復コストを生まない。 |
| **G5 再現性** | offset = `signal_date.toordinal()`。永続 state なし。replay が同値を返す。 |
| **G6 7 系統は必ず維持** | round-robin は system を deal 順から外さない。候補が 1 本以上ある system は、他の system が 2 本目を得る前に 1 本目を得る。プール ≥ 候補ありの system 数なら全系統が ≥1 枠。 |
| **G7 OFF は byte 一致** | フラグ OFF のとき deal 順は `range(len(df))` = 現行、tie-break rotation = 0 = 現行、report に追加キーなし。 |

---

## 5. フラグ

| flag | 既定 | 効果 |
|---|---|---|
| `risk.fair_pool_trim` (env `FAIR_POOL_TRIM`) | **false** | ON のときだけ `_apply_portfolio_caps` の deal 順を round-robin に、`_cap_slots_by_side` の端数 tie-break を rotation にする。OFF は現行と byte 一致。 |

観測性: ON のとき `system_diagnostics.portfolio_caps.fair_trim` に
`{epoch, rotation, deal_order, demand, kept, dropped}` が載り、
`[FAIR_TRIM]` の INFO ログに「どの system が何枠失ったか」が出る。

---

## 6. 変更しないもの

- 40 / 30 / 70 および gross/net exposure の**上限値**
- サイジング・リスク計算（`risk_pct` / `max_pct` / deploy budget）
- 発注コード、MT5、runner、deploy
- Bensdorp 7 系統の構成（**system を削る話ではない**。上限が効いたときに
  どの system が 1 枠譲るかの話）
