# reference_remote_access_stack.md

mini-PC (SERV870) 上の quant_trading_system_0510to0906 系サービスへの
リモート/公開アクセス構成を、layer 別に整理する reference memo。

**Last updated**: 2026-07-01 (Streamlit + Tailscale path 追加)

---

## Tailnet の基本情報

- tailnet: gethinu (single user tailnet)
- mini-PC hostname on tailnet: (SERV870, Windows)
- mini-PC Tailscale IP: **`100.124.50.52`**
- iPhone / 外出先 PC も tailnet member で `Tailscale` アプリ ON にすることで overlay 参加。

**tailnet に居ないデバイスからは 100.124.50.52 に到達できない (これが認証代わり)。**

---

## Path A: Vercel (公開/事業化 view)

- URL: `https://quant-trading-monitor.vercel.app`
- Repo path: `apps/dashboards/alpaca-next/` (Next.js static export)
- 対象 view: coverage + signals 概要 (静的、read-only)
- 誰がアクセス可: **誰でも (public)**
- Deploy: `main` push で auto。preview は feature branch ごとに URL 発行。
- ドキュメント: `docs/HUMAN_TASK_vercel_monitor_deploy_20260701.md`

用途: 認知獲得 / 事業化の front door。SEO ready、iPhone browser も可。

---

## Path B: RDP (mini-PC の GUI をそのまま持ち込む, admin 作業向け)

- mini-PC: Windows Pro の RDP を有効化 (auto-login active な想定)。
- 接続元 (iPhone / MacBook) から:
  - Tailscale 経由で `100.124.50.52` に RDP。
  - iOS の場合は "Microsoft Remote Desktop" アプリ。
- 用途: Task Scheduler いじる、venv 修復、Docker 起動、cache 更新の手動 kick 等。

---

## Path C: Streamlit apps (自分専用の詳細分析 view, private)  ★新規 2026-07-01

- listen: `0.0.0.0` (LAN + Tailscale overlay 両方)
- Firewall: Private profile のみ許容 (Public 露出禁止)
- 実効到達可能なのは tailnet member (自分) だけ

| app | port | Tailscale URL | 用途 |
|---|---|---|---|
| `apps/app_today_signals.py`             | 8501 | `http://100.124.50.52:8501` | 当日 signal 生成/詳細分析 |
| `apps/dashboards/app_alpaca_dashboard.py` | 8502 | `http://100.124.50.52:8502` | Alpaca broker dashboard |

**常時起動**: Windows Task Scheduler ONLOGON trigger + auto-login。
launcher scripts:

- `scripts/launch_streamlit_today.ps1`
- `scripts/launch_streamlit_alpaca.ps1`
- `scripts/stop_streamlit_all.ps1`

ドキュメント: `docs/HUMAN_TASK_streamlit_tailscale_20260701.md`

**再起動 1-liner (何かおかしくなった時):**

```powershell
powershell -File C:\Repos\quant_trading_system_0510to0906\scripts\stop_streamlit_all.ps1; `
  schtasks /Run /TN "QuantTrading_StreamlitToday"; `
  schtasks /Run /TN "QuantTrading_StreamlitAlpaca"
```

---

## 棲み分け (Vercel vs Streamlit)

|  | Vercel (Path A) | Streamlit (Path C) |
|---|---|---|
| 誰が見る | 誰でも (public) | 自分だけ (tailnet member) |
| 目的 | 認知獲得 / 事業化 landing | 実作業 / 詳細分析 |
| 内容 | 概要, coverage, 公開してよい signal 抜粋 | 全 signal, broker position, 詳細 backtest |
| 認証 | 不要 (公開 read-only) | Tailscale membership が実効 auth |
| デプロイ | main push で auto | reboot 後 auto-login → Task Scheduler ONLOGON |

**運用の思想**: 公開して収益化する view と、自分の decision support 用の view を
分離することで、機密 (broker key, 個別 position, 未成熟な signal) を露出せずに
Vercel 側で自由に marketing できる。

---

## Path D: (Phase 2, out of scope 現状) Tailscale Funnel or Cloudflared

- Streamlit を tailnet 外に配信して有料 tier のユーザに提供したくなったら検討。
- `tailscale funnel 8501` で https 公開 (ただし tailnet の subdomain)。
- 独自ドメイン + Cloudflared tunnel の方が landing とセットで見せやすい。
- 認証: streamlit-authenticator (basic auth) or Auth0 / Clerk。

---

## 診断チートシート

```powershell
# mini-PC 側で Streamlit が listen してるか
Test-NetConnection 127.0.0.1 -Port 8501
Test-NetConnection 127.0.0.1 -Port 8502

# Tailscale の状態 (mini-PC 側)
tailscale status
tailscale ip -4

# Task Scheduler の状態
schtasks /Query /TN "QuantTrading_StreamlitToday"  /V /FO LIST | Select-String "TaskName","Status","Last Result"
schtasks /Query /TN "QuantTrading_StreamlitAlpaca" /V /FO LIST | Select-String "TaskName","Status","Last Result"

# log の live tail
Get-Content -Wait -Tail 40 C:\Repos\quant_trading_system_0510to0906\logs\streamlit_today.log
Get-Content -Wait -Tail 40 C:\Repos\quant_trading_system_0510to0906\logs\streamlit_alpaca.log
```
