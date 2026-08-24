# 保留タスク: 朝の口座 read-only 再計測パス (2026-08-20 parked)

**ステータス**: 未着手 / ユーザー判断で 2026-08-20 に保留。着手前に「7. ユーザーへのオープンな問い」を読むこと。
**種別**: observability (計測経路の冗長化)。**発注ロジックには一切触れない。**
**想定ブランチ**: `claude/monitor-webapp` (このドキュメントの置かれたブランチ)。

このファイルは単体で読めるように書いてある。前提知識なしで着手してよい。

---

## 1. 一行で

`exit_ledger_*.json` / `alpaca_snapshot_*.json` を再生成する経路が **22:35 JST の
`open_auto_run` 1 系統だけ**になったので、**パイプラインを回さず publish もせずに
口座だけ read-only で測り直す**軽量パスを足したい。

---

## 2. 背景 (リポジトリで検証済み)

### 2-1. 2026-08-20: exit 台帳の鮮度判定をセッション基準へ

- コミット `8bca2f9` "fix(obs): judge exit-ledger freshness by trading session, not calendar today"
  (2026-08-20 08:41 JST, branch `claude/monitor-webapp`)
- 新規 `common/market_session.py`
  - `last_opened_session()` = すでに寄り付きを迎えた直近の NYSE 立会日 (`common/market_session.py:96`)
  - `last_opened_session_yyyymmdd()` (`common/market_session.py:108`)
  - 判定境界は **ET 09:30**。`zoneinfo` / `pandas_market_calendars` が無い環境では
    「より古いセッション」側へ倒れる = 偽の赤を作らない
- `scripts/morning_brief.py`
  - `_expected_session()` (`scripts/morning_brief.py:242`)
  - `check_exit_ledger(..., expected_session_yyyymmdd=None)` (`scripts/morning_brief.py:262`、判定は `:308`)
  - 呼び出し側 (`scripts/morning_brief.py:387-389`) が `primary_root / "results_csv"` を見る
- 詳細な durable log: `logs/exit_ledger_freshness_session_aware_20260820.md`
  - ⚠ **`logs/` は `.gitignore:8` (`logs/*`) で追跡外**。この log は host のローカルにしか無い。
    リモートしか見られない状況では、この docs/tasks/… が唯一の一次情報になる。

これは **判定を正した**だけで、計測そのものは増やしていない。

### 2-2. 副作用の出どころ: `26385b0`

- コミット `26385b0` "fix(observability): publish verified dashboard bundles" (2026-08-17 17:17 JST)
- `scripts/publish_data_to_vercel.ps1:179-186` — `-AutoLatest` 指定時に
  `$RefreshAccount = $false` を**強制**する:

  ```powershell
  # AutoLatest is a catch-up publisher for already-generated artifacts.  A
  # read-only account refresh still changes generated_at/hash and defeats the
  # promised idempotent no-op, creating a data commit on every catch-up run.
  # Explicit -Date runs remain the account refresh path.
  if ($RefreshAccount) { ...; $RefreshAccount = $false }
  ```

- これは **意図的な変更** (catch-up publish を byte-stable / 冪等にするため)。差し戻してはいけない。
- 副作用: 08:00 JST の self-heal (`scripts/morning_brief.ps1:106-108` →
  `publish_data_to_vercel.ps1 -AutoLatest`) が口座 artifact を作り直さなくなった。

### 2-3. 現在の計測経路 (grep 検証済み)

`scripts/build_exit_ledger.py` と `scripts/export_alpaca_snapshot.py` を呼ぶ箇所は、
リポジトリ全体で **`scripts/publish_data_to_vercel.ps1:288-303` の `if ($RefreshAccount)`
ブロックだけ**。

| 経路 | `$RefreshAccount` | 口座 artifact |
|---|---|---|
| 明示 `-Date` の publish (= 22:35 JST の `open_auto_run` 経路) | `$true` | **再生成される** |
| `-AutoLatest` (= 08:00 JST の morning self-heal) | `$false` (2-2 で強制) | 再生成されない |

- 夜間タスク: `schtasks` の `\QuantTrading_OpenAutoRun`、Next Run = 2026/08/20 22:35、MON-FRI。
  実行ツリーは `C:\tmp\qts-main-run` (branch `claude/open-auto-run`)。
- そのツリーの `results_csv` と `logs` は **PRIMARY への Junction** (実測確認済み) なので、
  夜間 run が書いた artifact はそのまま
  `C:\Repos\quant_trading_system_0510to0906\results_csv\` に現れ、
  モーニングブリーフが読む場所と一致する。
- 実ファイルの mtime も 1 系統化を裏づける:
  `exit_ledger_20260817/18/19.json` = 22:54 / 22:51 / 22:51 (`alpaca_snapshot_*` も同時刻帯)。
  8/15・8/16 は 08:00 = 朝の crutch が生きていた頃の痕跡。

### 2-4. 残存リスク (本タスクの動機)

**22:35 の run が落ちた日・skip された日は、口座の数字 (equity / positions / 実現損益) が
まる 1 日凍る。** 次の夜間 run が成功するまで誰も測り直さない。朝の catch-up はもう無い。

morning brief の exit 台帳チェックは 2-1 で「台帳日 < 直近立会日なら赤」に正されたので、
**本物の stale (セッションは走ったのに台帳が前日のまま) は依然として赤で出る**。
検知はできる。**足りないのは、赤を正当に消すための「安全な測り直し手段」のほう。**

---

## 3. やること

**朝 (あるいは任意のタイミング) に、パイプラインを回さず publish もせずに、
Alpaca から口座を read-only で測り直すだけの軽量パスを足す。**

### 3-1. 機能要件

1. `scripts/build_exit_ledger.py --date <YYYY-MM-DD>` と
   `scripts/export_alpaca_snapshot.py --date <YYYY-MM-DD>` を呼び、
   `results_csv/exit_ledger_<YYYYMMDD>.json` /
   `results_csv/alpaca_snapshot_<YYYYMMDD>.json` を更新する。
   - 両スクリプトとも既に read-only (fills 履歴 / positions / portfolio-history の GET のみ)。
     **新たに発注 API を叩くコードを書かないこと。**
2. **冪等**であること。同じ日に 2 回走らせても、broker 側に新しい約定が無ければ
   実質同一の中身になる (生成時刻フィールドの差は許容 — publish しないので data commit を汚さない)。
3. **publish しない。** `publish_data_to_vercel.ps1` を呼ばない。`data/` に触らない。
   git commit / push を一切しない。
4. **再エントリを誘発しない。** `open_auto_run` / `daily_pipeline` / signal 生成を呼ばない。
5. **`DONE.lock` を書かない。**
   `logs/open_run_<date>/DONE.lock` は `scripts/open_auto_run.py:752` が書き、
   `:764-766` と `scripts/self_monitor_check.py:258` が読む冪等ロック。
   ここに触ると **その日の 22:35 定例 run が「実行済み」と誤認して skip する**。
   ※ `open_auto_run.py` / `self_monitor_check.py` は `main` と `claude/open-auto-run` には
   在るが `claude/monitor-webapp` には**無い**。branch を跨ぐときは必ず現物を確認すること。
6. 対象日は `common.market_session.last_opened_session_yyyymmdd()` から取る
   (`--date` 明示も可)。カレンダー当日を素で使わない — それが 2-1 で潰したバグそのもの。
7. 実行の痕跡を `logs/` に durable 保存する (既存の運用慣習に合わせる)。

### 3-2. paper ガード (必須)

`common/alpaca_trading.py:131-148` の `assert_paper_env()` を**必ず**通す。中身は 2 段:

- `ALPACA_PAPER` が truthy (`ALPACA_PAPER_STRICT=1` の場合は明示設定を必須化)
- `ALPACA_API_BASE_URL` が `paper-api.alpaca.markets`
  (`_PAPER_HOST`, `common/alpaca_trading.py:55`) を指す

3 段目は client 側の `ba.get_client(paper=True)`
(`scripts/build_exit_ledger.py:385-392`, `scripts/export_alpaca_snapshot.py:1286`)。

> **paraphrase 訂正 (重要)**: 「`PA` で始まる `account_number` の照合」は
> **現行の共有パスには入っていない**。grep した限り、この照合は臨時スクリプト
> `logs/close_orphans_20260712/close_orphans_fold_cdtx.py:45-47` にしか無い:
>
> ```python
> acct_no = str(getattr(acct, "account_number", ""))
> if not acct_no.startswith("PA"):
>     raise SystemExit(f"ABORT: account_number not paper (PA*): {acct_no!r}")
> ```
>
> **本タスクでは、これを 3 段目のガードとして新パスに入れることを推奨する** (安価で、
> 環境変数より確実に live 誤接続を弾ける)。共有ヘルパへ昇格させるかは実装者判断でよいが、
> 既存の `assert_paper_env()` のシグネチャを壊す変更は avoid (呼び出し元が 10 箇所以上ある)。

---

## 4. 受け入れ条件 (done の定義)

- [ ] 新パスを 1 回叩くと `results_csv/exit_ledger_<最新立会日>.json` と
      `results_csv/alpaca_snapshot_<最新立会日>.json` が **実データで**更新される
      (Alpaca からの実 read。既存ファイルの日付だけ書き換える偽装は不可)。
- [ ] 直後に `scripts/morning_brief.py` を走らせると exit 台帳チェックが 🟢 になる。
      **`check_exit_ledger` の閾値・比較ロジックは 1 行も緩めない**こと —
      緑になる理由は「本当に測り直したから」でなければならない。
- [ ] 同じ日に 2 回走らせても副作用が無い (2 回目が発注・ロック・commit・publish を誘発しない)。
- [ ] `ALPACA_PAPER` を落とす / live URL を差す / 非 PA 口座、のいずれでも
      **書き込み前に abort** する (テストで固定)。
- [ ] `git status` が `data/` に対してクリーンなまま (= publish 経路を一切踏んでいない)。
- [ ] `logs/open_run_<date>/DONE.lock` が**作られない**ことをテストで固定。
- [ ] 22:35 の `open_auto_run` の挙動が変わらない (既存テスト green)。
- [ ] 既存テストが green (`tests/test_market_session.py` 10 件・`tests/test_morning_brief.py` を含む
      2026-08-20 時点の 125 件)。
- [ ] durable log を `logs/<slug>_<YYYYMMDD>.md` に残す (既存慣習)。

---

## 5. ガードレール (逸脱したら止める)

- **paper 限定。** live 口座・実マネーは絶対禁止。
- **発注ゼロ。** entry も exit も protection も出さない。read-only の GET のみ。
- **publish しない。** `data/` を触らない、git commit/push しない、Vercel を叩かない。
- **再エントリを誘発しない。** signal 生成・`open_auto_run`・`daily_pipeline` を呼ばない。
- **`DONE.lock` を書かない / 消さない。**
- **`26385b0` の `-AutoLatest` → `$RefreshAccount=$false` を差し戻さない。**
  あれは冪等性のための意図的な設計。新パスは publish とは別系統として足すこと。
- **鮮度判定を緩めて緑にしない。** `8bca2f9` で正した判定はそのまま。
- additive・後方互換。既存呼び出しを壊す破壊的変更をしない。

---

## 6. 参考: 検証に使えるコマンド (すべて read-only)

scratch へ出せば本番 artifact を汚さずに差分比較できる:

```bash
python scripts/build_exit_ledger.py --date 2026-08-19 --results-dir /tmp/scratch_ledger
```

```bash
python scripts/morning_brief.py --dry-run
```

2026-08-20 の検証はこの手で
`fills=1735 / closed_trades=1211 / realized=-1753.56` の差分ゼロを確認している
(= その時点で未計測の約定はゼロだった)。

---

## 7. ユーザーへのオープンな問い (2026-08-20 に parked)

このタスクは**まだ着手承認が出ていない**。以下がユーザー判断待ちの論点:

1. **そもそも足すか。** 22:35 が落ちる頻度は現状ほぼゼロで、落ちても翌日の run で
   自然回復する。冗長化のコストに見合うか。
2. **いつ走らせるか。** 08:00 の `morning_brief.ps1` に常時組み込むか、
   「台帳が stale と判定された時だけ」の条件実行にするか、手動 opt-in の一発スクリプトに留めるか。
   常時実行は副作用ゼロとはいえ Alpaca API の呼び出しが毎朝増える。
3. **どのツリー / どの branch に置くか。** morning brief は PRIMARY
   (`C:\Repos\quant_trading_system_0510to0906`) で動き、夜間 run は
   `C:\tmp\qts-main-run` (`claude/open-auto-run`) で動く。`results_csv` は Junction で
   共有されるので書き先は一致するが、実装の置き場は決めが要る。
4. **PA-prefix ガードを共有ヘルパへ昇格させるか** (3-2 の訂正メモ参照)。

**着手する前にこの 4 点をユーザーに確認すること。** 保留の理由は技術的な難しさではなく、
「入れる価値があるか」の判断が未了だから。
