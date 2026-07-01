# HUMAN_TASK: Streamlit 常時起動化 + Tailscale 経由 private アクセス (2026-07-01)

対象 branch: `claude/monitor-webapp`
対象マシン: mini-PC (SERV870, Windows), Tailscale IP = `100.124.50.52`
対象アプリ:
- `apps/app_today_signals.py` (当日 signal 生成, 詳細分析) → **port 8501**
- `apps/dashboards/app_alpaca_dashboard.py` (Alpaca dashboard) → **port 8502**

**目的**: Vercel 側 (`https://quant-trading-monitor.vercel.app`) を「公開/事業化 view」、
このドキュメントで扱う Streamlit 2 本を「自分専用の詳細分析 view (private)」として分離運用する。
Tailscale overlay 経由なので LAN の外 (iPhone LTE、外出先 PC) からも tailnet member としてのみアクセスできる。

---

## 全体像 (何を作ったか)

| ファイル | 役割 |
|---|---|
| `scripts/launch_streamlit_today.ps1`  | app_today_signals を port 8501 で bind 0.0.0.0 起動 |
| `scripts/launch_streamlit_alpaca.ps1` | app_alpaca_dashboard を port 8502 で bind 0.0.0.0 起動 |
| `scripts/stop_streamlit_all.ps1`      | 全 streamlit プロセスを cleanup |
| `docs/HUMAN_TASK_streamlit_tailscale_20260701.md` | 本ドキュメント |

Task Scheduler は **user 側 (admin PowerShell) 手動登録**。
`At log on` trigger で reboot 後 auto-login 済のセッションで自動起動する。

---

## §1. Task Scheduler 登録 (admin PowerShell, 1 回だけ実行)

**admin PowerShell を開いて、下記 2 本を貼り付けて実行。**

```powershell
# --- 1) today signals (port 8501) ---
schtasks /Create /F /TN "QuantTrading_StreamlitToday" `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Repos\quant_trading_system_0510to0906\scripts\launch_streamlit_today.ps1" `
  /SC ONLOGON /RL HIGHEST

# --- 2) alpaca dashboard (port 8502) ---
schtasks /Create /F /TN "QuantTrading_StreamlitAlpaca" `
  /TR "powershell.exe -NoProfile -ExecutionPolicy Bypass -File C:\Repos\quant_trading_system_0510to0906\scripts\launch_streamlit_alpaca.ps1" `
  /SC ONLOGON /RL HIGHEST
```

登録確認:

```powershell
schtasks /Query /TN "QuantTrading_StreamlitToday"  /V /FO LIST | Select-String "TaskName","Status","Next Run Time","Last Run Time"
schtasks /Query /TN "QuantTrading_StreamlitAlpaca" /V /FO LIST | Select-String "TaskName","Status","Next Run Time","Last Run Time"
```

**削除する場合:**

```powershell
schtasks /Delete /F /TN "QuantTrading_StreamlitToday"
schtasks /Delete /F /TN "QuantTrading_StreamlitAlpaca"
```

---

## §2. 動作確認 (即座に起動して verify)

Task Scheduler は次回 log on まで待つので、初回は手動で叩いて確認する。

```powershell
# admin か user どちらでも OK
schtasks /Run /TN "QuantTrading_StreamlitToday"
schtasks /Run /TN "QuantTrading_StreamlitAlpaca"

# 30 秒待って TCP listen 確認 (LISTEN 出れば OK)
Start-Sleep -Seconds 30
Test-NetConnection 127.0.0.1 -Port 8501
Test-NetConnection 127.0.0.1 -Port 8502
```

log を追う:

```powershell
Get-Content -Wait -Tail 40 C:\Repos\quant_trading_system_0510to0906\logs\streamlit_today.log
Get-Content -Wait -Tail 40 C:\Repos\quant_trading_system_0510to0906\logs\streamlit_alpaca.log
```

---

## §3. アクセス URL

**mini-PC ローカル (デスクトップ):**
- `http://localhost:8501` (today signals)
- `http://localhost:8502` (alpaca dashboard)

**Tailscale 経由 (推奨, iPhone / 外出先 PC):**
- `http://100.124.50.52:8501` (today signals)
- `http://100.124.50.52:8502` (alpaca dashboard)

**iPhone Safari:**
1. Tailscale アプリを開き、右上のスイッチが ON (緑) であることを確認。
2. Safari で `http://100.124.50.52:8501` を開く。
3. Streamlit の画面が出れば OK。ホーム画面追加でアプリライクに常駐化推奨。

---

## §4. Windows Firewall (デフォルトでは追加不要)

Tailscale overlay の traffic は `Tailscale` ネットワーク profile (通常 Private) 経由で入ってくる。
既存の Private profile が inbound を許容していれば追加設定は不要。
もし iPhone から `http://100.124.50.52:8501` が繋がらない場合のみ、下記を admin PowerShell で追加:

```powershell
New-NetFirewallRule -DisplayName "Streamlit Quant Trading" `
  -Direction Inbound -Protocol TCP -LocalPort 8501,8502 `
  -Action Allow -Profile Private
```

削除:

```powershell
Remove-NetFirewallRule -DisplayName "Streamlit Quant Trading"
```

**Public profile を許容してはならない** (`-Profile Any` は避ける)。Tailscale 以外の
LAN 越しに 8501/8502 を露出させると、Streamlit は auth 無しなので誰でも見える。

---

## §5. 認証について

Streamlit デフォルトは **auth 無し**。ただし本構成では

- listen: `0.0.0.0` (LAN + Tailscale overlay の両方)
- Firewall Private only → 実効的に **Tailscale overlay の member (自分のみ)** からしか到達不可

なので追加認証は不要。

心配なら Phase 2 で `streamlit-authenticator` を導入:

```powershell
pip install streamlit-authenticator
```

- `.streamlit/secrets.toml` に basic auth credential を書き、各 app 冒頭で `stauth.Authenticate(...)` を wrap する。
- 実装は Phase 2 で別 branch。今は out of scope。

---

## §6. 手動 start / stop / restart

```powershell
# 手動 start
schtasks /Run /TN "QuantTrading_StreamlitToday"
schtasks /Run /TN "QuantTrading_StreamlitAlpaca"

# 全 streamlit プロセスを止める (port 8501/8502 の python.exe も含めて kill)
powershell -File C:\Repos\quant_trading_system_0510to0906\scripts\stop_streamlit_all.ps1

# restart は stop → schtasks /Run の順
```

---

## §7. トラブルシュート

**`Test-NetConnection 127.0.0.1 -Port 8501` が失敗する:**
- `Get-Content C:\Repos\quant_trading_system_0510to0906\logs\streamlit_today.log -Tail 100` で例外を確認。
- venv が壊れていれば `python -m venv .venv` で作り直し、必要な package を再 install。
- streamlit 未インストールなら: `pip install streamlit`。

**iPhone から `http://100.124.50.52:8501` が繋がらない:**
1. Tailscale アプリで tailnet が ON であることを確認。
2. mini-PC 側の Tailscale が上がってるか: `tailscale status` (100.124.50.52 が self に出る)。
3. `Test-NetConnection 100.124.50.52 -Port 8501` を **別マシン (iPhone 以外の tailnet member)** から実行。
4. 通らなければ §4 の Firewall rule を Private profile で追加。

**reboot 後に起動しない:**
- auto-login が効いてるか確認 (`netplwiz`)。ログイン画面で止まっていると ONLOGON が発火しない。
- `schtasks /Query /TN "QuantTrading_StreamlitToday" /V /FO LIST` の `Last Result` が 0 か確認。

---

## §8. Vercel との棲み分け

| Layer | URL | 対象 view | 誰がアクセス可 |
|---|---|---|---|
| 公開/事業化 | `https://quant-trading-monitor.vercel.app` | coverage + signals 概要 (静的) | 誰でも (public) |
| 詳細分析 (private) | `http://100.124.50.52:8501` | app_today_signals (当日 signal 詳細) | Tailscale member (自分) |
| 詳細分析 (private) | `http://100.124.50.52:8502` | app_alpaca_dashboard (broker view) | Tailscale member (自分) |

Vercel は「見せる」view、Streamlit は「自分が使う」view。目的が分離されているので、
Streamlit に個人情報や broker key を含めても OK。ただし `.env` は git 管理しない (既存 `.gitignore` で除外済のはず)。

---

## §9. 事業化への繋がり

- Vercel 側 (公開) で認知獲得 → landing → 有料 tier (Streamlit private view の access 提供 or Slack notification / API subscription) への CV を狙う。
- 詳細な signal / broker view は課金の materialised value になる。private Streamlit → SaaS 化する場合は cloudflared / Tailscale Funnel でエンドユーザ配信も可 (Phase 2)。

---

**Owner**: げすいぬ / **Created**: 2026-07-01 / **Branch**: `claude/monitor-webapp`
