# 凍結 orphan の枠占有を止める — `risk.exclude_orphans_from_slots` (2026-08-28)

**Status:** 実装済み / **フラグ既定 OFF** / paper のみ / 発注コードは無変更。
OFF の間は件数・exposure・summary JSON の全てが従来と byte 一致する。

---

## 1. 問題 — 決済できない建玉が枠だけ食い続ける

2026-08-28 の実 book (`results_csv/exit_orders_20260828_execution.json`, 22:35 の
broker read) は long 29 件。うち 2 件は:

| symbol | qty | market_value | system 帰属 | broker |
|---|---:|---:|---|---|
| CDTX | 10 | $2,213.80 | なし | `tradable: false` / `untradable_no_exit_possible` |
| FOLD | 143 | $2,072.07 | なし | `tradable: false` / `untradable_no_exit_possible` |

上場廃止で **API から close できない**。こちらから決済する手段が無いので、資金は
broker の清算まで凍結される。にもかかわらず本番の cap report は:

```json
"held": {"long": 29, "short": 0, "total": 29},
"held_unmapped": {"long": 2, "short": 0, "total": 2},
"allow": {"long": 11, "short": 30, "total": 41}
```

`allow.long = 40 - 29 = 11`。**long プール 40 枠のうち 2 枠が、永久に取引できない
2 銘柄に握られている**。ダッシュボードの「orphan … ロング枠を2つ占有 (system枠には
数えられない)」はこの状態を指している。

### 既存の orphan 扱いとの関係 (重複していないこと)

| 既存 | 何をしているか | 本件との関係 |
|---|---|---|
| `scripts/paper_exit_check.py::refine_orphan_classifications` | orphan を「帰属欠落 (直せば守れる)」と「取引不能 (手動対応)」に分類 | **判定規約をそのまま踏襲**。確認できない時は断定しない |
| `common/alpaca_trading.py::probe_asset_tradable` | read-only `get_asset` で `True/False/None` | **そのまま再利用**（新しい broker 呼び出しは足していない） |
| `scripts/export_alpaca_snapshot.py::_fetch_inactive_assets` | ダッシュの `system: "delisted"` ラベル | 同じ事実を別経路で出しているだけ。本件は表示ではなく**枠計算**を変える |
| `core/final_allocation.py::count_active_positions_by_system` | per-system 枠。未帰属は元から**数えない** | **変更なし**。per-system 枠は既に orphan を無視している |
| `core/final_allocation.py::count_positions_with_unmapped` | プール枠。未帰属も held に**算入**する (P1 fix 2026-07-21) | **ここが枠を食っている当事者**。変更対象 |
| `scripts/replay_portfolio_caps.py` arm `C_no_orphan` | 「cap か held 独占か」を切り分ける診断用 counterfactual | 本件はその counterfactual を **条件付きで本物にする** |

---

## 2. 設計 — 「枠は返すが、資金は返さない」

orphan を一律に外すのは**危険**。2 種類あって、扱いを分けなければならない:

- **取引可能な未帰属保有** — 生きた建玉。exit も追撃もできる。**枠を占有し続けるのが
  正しい**。帰属を直せば守れる。
- **上場廃止で close 不能な保有** — こちらから何もできない。**件数枠だけを食う**。

よって「枠を返す」条件は **AND**:

1. system 帰属が無い (`count_positions_with_unmapped` と同じ規約)、**かつ**
2. broker で `tradable is False` と**確認できた** (`None` = 確認できず は含めない)

さらに、枠を返しても**資金は凍結されたまま**である。枠だけ空けて exposure からも
落とすと「凍結資金の上に新規を積む」= over-leverage の穴になる。そこで:

> **件数上限からは外す。市場価値は gross / net exposure 上限の側で引き続き占有として
> 数える。**

### net は「締める方向にしか」倒さない

net exposure は符号付きなので、凍結が long・新規が short 寄りの日には
`|net|` が **小さく** なる。実測 (2026-08-28 の実フレーム):

```
|net| OFF $7,857.07  ->  素朴な符号付き加算では $3,571.20   (= short 余力が増える)
```

帰属済みの保有はそもそも exposure に入っていないのだから、凍結分だけを符号付きで
入れて net 上限が緩むのは筋が通らない。実装は

```python
net_used = max(|new_long - new_short|,
               |(new_long + frozen_long) - (new_short + frozen_short)|)
```

とし、**フラグは exposure を締める方向にしか効かない**ことを契約にした
(`tests/test_orphan_slot_exclusion_20260828.py::test_flag_only_ever_tightens_the_net_cap`)。

### fail-closed 3 点

| 状況 | 挙動 |
|---|---|
| `tradable` を確認できなかった (`None`) | **枠は返さない** (従来どおり占有)。WARNING を出す |
| 未帰属だが取引可能 (`True`) | **枠は返さない**。生きた建玉 |
| `market_value` が読めない | **枠は返さない** (`unpriced_kept_in_slots` に計上)。exposure へ付け替えられない建玉の枠を返すと穴になる |
| 帰属済みの建玉が取引不能 | **枠は返さない**。per-system 枠が既に数えているので二重に緩む |
| settings を読めない | OFF へ退避 (保守側) + WARNING |

---

## 3. 実装 — どこに何を足したか

| ファイル:行 | 内容 |
|---|---|
| `config/config.yaml:34` | `risk.exclude_orphans_from_slots: false` (既定 OFF) |
| `config/settings.py:73` / `:514` | `RiskConfig.exclude_orphans_from_slots` + env `EXCLUDE_ORPHANS_FROM_SLOTS` |
| `config/schemas.py:43` | `RiskModel` へ宣言。**宣言しないと `model_dump()` が YAML のキーを落として設定が黙って無効化される** (下の注記) |
| `core/final_allocation.py:_load_exclude_orphans_from_slots` | フラグ読み出し (失敗は OFF + WARNING) |
| `core/final_allocation.py:count_frozen_orphans` | 「未帰属 AND 取引不能」の件数と市場価値を数える純関数 |
| `core/final_allocation.py:_apply_portfolio_caps` | additive kwargs `exclude_frozen_slots` / `frozen_symbols`。件数から外し、exposure へ付け替える |
| `core/final_allocation.py:finalize_allocation` | additive kwarg `frozen_symbols` (既定 None) |
| `scripts/run_all_systems_today.py:_resolve_frozen_orphan_symbols` | OFF なら **broker を一切叩かず** `[]`。ON のときだけ未帰属銘柄の `tradable` を read-only GET で確認 |

`report["frozen_orphans"]` は **ON のときだけ**付く (OFF の report を byte 一致させる契約):

```json
"frozen_orphans": {
  "enabled": true,
  "count": {"long": 2, "short": 0, "total": 2},
  "exposure_usd": {"long": 4285.87, "short": 0.0, "gross": 4285.87},
  "unpriced_kept_in_slots": 0,
  "symbols": ["CDTX", "FOLD"]
}
```

### 注記: YAML のフラグが黙って落ちる既存の穴

`config/settings.py::_load_yaml_config_validated` は `validate_config_dict(data)` の
**`model_dump()`** を返す。pydantic は宣言されていないキーを落とすので、
`config/schemas.py::RiskModel` に無いフラグは **YAML に書いても届かない** (env のみ有効)。
宣言を忘れた状態では、こうなる:

```
>>> validate_config_dict({'risk': {'<未宣言のフラグ>': True}}).model_dump()['risk']
{'risk_pct': 0.02, ..., 'slots_from_capital_min_slots': 1, 'portfolio': {...}}   # キーが消えている
```

本件のフラグは `RiskModel` に宣言したので YAML でも env でも効く。
**`risk.fair_pool_trim` (c371c34) も 2f8430c (PR #171) で `RiskModel` へ宣言済みなので、
現在は YAML / env の両方から設定できる**。`RiskModel` 未宣言の risk フラグは
`tests/test_risk_flag_schema_roundtrip_20260829.py` が shipped `config.yaml` の
risk セクションを走査して検出する (同じ宣言漏れの再発防止)。

---

## 4. 実測 (2026-08-28 の実 book / 完全オフライン再生, 発注ゼロ)

### 4.1 OFF byte-parity

同一の決定論的入力で `_apply_portfolio_caps` × 2 と `finalize_allocation` を回し、
final CSV + report JSON を base commit (`c371c34`) と本変更で比較:

```
$ diff -r base off        # 6 ファイル
IDENTICAL (no diff)

545c43dc... base/caps_legacy_poolbound.csv   == off/caps_legacy_poolbound.csv
6e78d9ac... base/caps_legacy_real0828.json   == off/caps_legacy_real0828.json
9a29b562... base/caps_legacy_poolbound.json  == off/caps_legacy_poolbound.json
db3544a7... base/caps_legacy_real0828.csv    == off/caps_legacy_real0828.csv
f115244d... base/finalize_slotmode.csv       == off/finalize_slotmode.csv
f1d1329b... base/finalize_slotmode.json      == off/finalize_slotmode.json
```

### 4.2 ON — 2 枠が返る (需要が枠を上回る日)

実 book (long 29, 凍結 2) + long 候補 20 本 × $2,000:

| | held.long | allow.long | 採用 long | gross cap | net cap | 凍結の exposure 計上 |
|---|---:|---:|---:|---:|---:|---:|
| OFF | 29 | **11** | 11 | 100,259.95 | 50,129.97 | $0 |
| ON | 27 | **13** | 13 | 100,259.95 | 50,129.97 | **$4,285.87** |

**上限「値」は不変**。動くのは占有の数え方だけ。

### 4.3 ON — 資金が束縛する日は枠が空いても新規は増えない

gross だけを束縛させた盤面 (long 候補 20 本 × $8,000, net 無効化):

```
OFF  allow.long=11  kept.long=11  new_gross=$88,000.00
ON   allow.long=13  kept.long=11  new_gross=$88,000.00  trimmed={'gross_exposure': 9}
     新規 $88,000.00 + 凍結 $4,285.87 = $92,285.87 <= cap $100,259.95
```

枠は 11→13 に増えたが、**12 本目は件数ではなく `gross_exposure` で止まる**。
凍結分を計上しなければ 96,000 + 4,285.87 = $100,285.87 で **cap を超える** —
これがミューテーションテストで実際に検出される穴 (§5)。

net が束縛する盤面 (long 候補 20 本 × $4,200, net cap 50%) では:

```
OFF  allow.long=11  kept.long=11  new_long=$46,200.00  trimmed={'long_count': 9}
ON   allow.long=13  kept.long=10  new_long=$42,000.00  trimmed={'net_exposure': 10}
```

**ON の方が採用が少ない** (凍結資金を計上するぶん締まる)。

### 4.4 当日 (2026-08-28) の実シグナルに当てた場合

記録された本番 report をそのまま再生 (`today_signals_20260828.json` の
post-cap 18 本 + broker の保有):

```
[OFF] held={'long': 29, ...} allow={'long': 11, ...} kept={'long': 8, 'short': 10} trimmed={}
[ON ] held={'long': 27, ...} allow={'long': 13, ...} kept={'long': 8, 'short': 10} trimmed={}
最終フレーム同一か: True
```

**当日は long 需要 8 < 枠 11 で件数上限が束縛していなかったので、ON にしても
その日の発注内容は 1 件も変わらない**。効くのは long 需要が 11 本を超える日。
再生の忠実度: 記録された `held.long=29` / `allow.long=11` / `kept` を完全再現
(short 側の 1 件差は snapshot と run の観測時刻差による既知のずれ)。

---

## 5. テスト

`tests/test_orphan_slot_exclusion_20260828.py` — **20 tests, all passing**。
実 book (`exit_orders_20260828_execution.json` の 29 建玉) をフィクスチャに使用。

ミューテーションで**検査が空振りでないこと**を確認済み:

| 変異 | 落ちるテスト |
|---|---|
| net の締める方向ガードを外す (素朴な符号付き加算) | `test_flag_only_ever_tightens_the_net_cap` (`assert 9 <= 8`) |
| 枠は返すが凍結資金を exposure に計上しない | `test_frozen_capital_is_charged_to_the_exposure_caps`, `test_freed_slots_cannot_breach_the_gross_cap` (`96000.0 + 4285.87 <= 100259.95` が偽) |

---

## 6. 運用

```bash
# 有効化 (YAML でも env でも可)。既定は OFF。
EXCLUDE_ORPHANS_FROM_SLOTS=1 python -m scripts.run_all_systems_today ...
```

- **本フラグは立てていない**。デプロイもランナー前進もしていない。
- ロールバックは env を外す / YAML を `false` に戻すだけ (コード revert 不要)。
- 発注・flatten・口座リセットは一切していない。broker 照会は read-only `get_asset` のみ。
