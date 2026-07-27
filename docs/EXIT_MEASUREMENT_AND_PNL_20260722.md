# exit の計測と「今日の損益」の定義 (2026-07-22)

paper 限定・read-only。発注も決済もしない。

## 1. 何が壊れていたか

### A. exit が計測されていなかった

`scripts/paper_exit_check.py` は exit の **意図** (`results_csv/exit_orders_YYYYMMDD.json`)
を書くだけで、その後 *実際に約定したか / いくらだったか* を記録する場所が
どこにも無かった。結果:

- 実現損益 (realized P&L) が系のどこにも存在しない
- 「exit するつもりだったのに約定していない」を検出できない
- しかも **その欠落自体に気づけない** (0 件なのか未計測なのか区別が無い)

### B. 「今日 +$2,850.35」は幻だった

旧 exporter は当日損益を `equity - last_equity` で出していた。この 2 つは
**会計基準が違う**:

| 系列 | 上場廃止 (INACTIVE) 建玉の時価 |
|---|---|
| live `equity` / intraday portfolio-history | 最終気配で計上する |
| `last_equity` / daily(1D) portfolio-history | **計上しない** |

2026-07-22 実測で CDTX + FOLD の **$4,285.87** がこの差。基準の違う 2 つの数を
引いていたので、当日損益が丸ごと水増しされていた。

- 2026-07-20 の published snapshot: `103,515.47 - 100,665.12 = +$2,850.35 (+2.83%)`
- 同じ 07-20 の **実現損益は −$2,333.06** (exit 台帳で再構成した実績)

つまり実際には大きく負けた日を「+2.83%」と表示していた。
**注釈やバナーで補正しない。基準を揃えるか、出さないかの二択にした。**

## 2. 今の作り

```
Alpaca /v2/account/activities/FILL  (約定 = ground truth)
        │
        ├── scripts/build_exit_ledger.py   → results_csv/exit_ledger_YYYYMMDD.json
        │     FIFO で round-trip 再構成 / broker position と突合 / 意図と突合
        │
        └── scripts/export_alpaca_snapshot.py → alpaca_snapshot_YYYYMMDD.json
              realized ブロック + pnl_today ブロックを載せる
                    │
                    └── dashboard (Alpaca タブ)
```

`scripts/publish_data_to_vercel.ps1` が毎日 `-RefreshAccount`(既定 on) で
**台帳 → snapshot** の順に read-only で作り直してから publish する。
以前はこの 2 つを呼ぶ pipeline が無く、手で叩いた日 (最後は 07-20) しか
生成されていなかった。

### exit code (silent success を作らない)

`scripts/build_exit_ledger.py`:

| code | 意味 |
|---|---|
| 0 | 計測できた |
| 1 | 取得エラー (broker 不通) |
| 2 | safety abort (paper でない) |
| 3 | **未計測を検知** (`--fail-on-unmeasured` 指定時) |

## 3. 「今日の損益」の定義 — これ 1 つだけ

```
総額 = 現在の equity − 前セッション終値の equity
       (どちらも intraday 系列 = 同一基準)
総額 = 実現 (決済で確定) + 含みの当日変動
```

- 基準は `prev_session_intraday` **のみ**。`last_equity` と daily(1D) 系列は
  当日損益に**使わない** (参考値として並べるだけ)。
- セッション日は **broker clock の ET 営業日**。ローカル(JST)日付でも UTC でもない。
  JST 早朝は ET だと前日なので、間違えると 1 セッションずれる。
- 実現損益は「当日損益と同じ立会日」で台帳の `realized.by_day` から引く。
  台帳の `today` ブロックは pipeline のローカル日付なので流用しない。
- **同一基準の前セッション終値が取れなければ数字を出さない** (`measured=false`)。
  `common/exit_ledger.resolve_session_pnl` がこの判断を持つ唯一の場所。

`tests/test_export_alpaca_snapshot.py::test_last_equity_is_never_used_for_today_pnl`
が source guard として `equity - last_equity` の復活を落とす
(コメント/docstring は tokenize で除外するので、禁止理由の説明文は誤検知しない)。

## 4. 0 と「不明」を混同しない

| 状態 | 出す値 |
|---|---|
| 決済が 1 件も無かった | `0.0` (事実) |
| 約定履歴が取れていない | `null` + `measured=false` + 理由 |
| 台帳が対象セッションに届いていない | `null` (0 で埋めない) |

dashboard も同じ規律で、未計測は数字ではなく「未計測」と理由を出す。

## 5. まだ計測できていないもの (正直に残す)

- **建玉不一致 8 銘柄** (`AGNT CHRN EKSO EXPI FI FISV MF UBXG`)。
  多くは ticker rename (例: `FISV` → `FI`)。約定履歴からの再構成と broker の
  実 position が食い違うので、この銘柄の実現損益は信用しない。
  `measurement.unmeasured_symbols` に列挙して表に出す。
- **system 不明の決済 273 本**。client_order_id に system tag が入る前の
  古い約定。捨てずに `unknown` にまとめる。
- **exit 理由が記録なしの決済 85 本**。`exit_orders_*.json` が残っていない
  期間の分。推測せず「記録なし」と表示する。

## 6. 監視 (silent-fail を毎朝表に出す)

`scripts/morning_brief.py` の `check_exit_ledger()` が 08:00 JST の ntfy で見る:

- 赤にする: 台帳が無い / 当日ぶん未更新 / `measured=false` /
  立会終了後も未約定の exit (`intended_not_filled`)
- 赤にしない: 執行待ち (`intended_pending`) と建玉不一致 —
  原因が既知で毎日出るため、件数だけサマリに残す (オオカミ少年にしない)

計測が黙って止まったら「実現損益が無いことにすら気づけない」ので、
ここを通さない限り台帳は信用しない。
