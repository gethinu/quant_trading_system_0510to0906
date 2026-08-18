'use client';

import { useState } from 'react';
import type { AlpacaSnapshot } from '@/lib/types';
import { computeStatus, type StatusAlert } from '@/lib/status';
import {
  executionLabel,
  fmtPct,
  fmtSignedUsd,
  pnlText,
  sysColor,
  sysShort,
} from '@/lib/format';
import { fmtGenerated, useFreshness } from '@/lib/freshness';
import { FreshnessBanner } from '@/components/FreshnessBanner';

/**
 * 最上部の「本物のサマリー」。
 *
 * これまでのダッシュは事実を全部並べていたので、状態を掴むのに縦 10 画面を scroll する
 * 必要があった。ここは **これだけ見れば今の状態が分かる** 粒度に絞った 1 ブロック:
 *
 *   1. 今日の損益   … 同一 intraday 基準でしか出さない (出せない時は数字を出さない)
 *   2. 保有ポジション … 件数 + **期限超過が何件か** (= exit が詰まっていることの可視化)
 *   3. 鮮度         … 表示中データの日付 / run_id / 何営業日遅れているか
 *   4. アラート     … 赤 (今すぐ見る) の件数。中身も 1 行ずつその場に出す
 *
 * 詳細 (system 別内訳・決済履歴・エクイティ・銘柄別) はこの下で畳んである。
 * 情報は減らしていない。「サマリーで状態把握 → 必要なら開く」の 2 層構造。
 */

interface Props {
  snapshot: AlpacaSnapshot | null;
  /** 配信データ (today_signals) 側の日付・run_id。ntfy が出すのと同じ run_id。 */
  signalsDate: string | null;
  runId?: string | null;
  generatedAt?: string | null;
}

function Tile({
  label,
  value,
  valueCls,
  children,
}: {
  label: string;
  value: string;
  valueCls?: string;
  children?: React.ReactNode;
}) {
  return (
    <div className="rounded-lg bg-white/[0.03] border border-white/5 px-2.5 py-2 min-w-0">
      <div className="text-[10px] uppercase tracking-wide text-muted truncate">{label}</div>
      <div
        className={`text-xl sm:text-2xl font-semibold tabular-nums leading-tight ${
          valueCls ?? ''
        }`}
      >
        {value}
      </div>
      {children}
    </div>
  );
}

const SUB = 'text-[10px] leading-snug text-muted tabular-nums';

/** 当日損益タイル。measured=false なら数字を出さず「未計測」と言い切る。 */
function TodayTile({ snap }: { snap: AlpacaSnapshot | null }) {
  const p = snap?.pnl_today ?? null;
  const ledgerToday = snap?.realized?.today ?? null;

  if (!p || !p.measured || p.total_pl == null) {
    return (
      <Tile label="今日の損益" value="未計測" valueCls="text-muted">
        <div className={SUB}>同一基準の前セッション終値が無いため出しません</div>
      </Tile>
    );
  }

  // 寄り前は「実現 $0」が「今日はまだ何も決済していない」の意味。0 を成績と誤読
  // させないよう、セッションの状態を添える。
  const state = ledgerToday?.session_state;
  const stateNote =
    state === 'before_open' ? '寄り前' : state === 'open' ? '取引時間中' : null;

  return (
    <Tile
      label="今日の損益"
      value={fmtSignedUsd(p.total_pl)}
      valueCls={pnlText(p.total_pl)}
    >
      <div className={SUB}>
        {fmtPct(p.total_pl_pct)} · vs {p.baseline_session} 終値
      </div>
      <div className={`${SUB} mt-0.5`}>
        {p.realized_pl != null ? (
          <>
            実現{' '}
            <span className={pnlText(p.realized_pl)}>{fmtSignedUsd(p.realized_pl)}</span>
            {' · 含み '}
            <span className={pnlText(p.unrealized_delta)}>
              {fmtSignedUsd(p.unrealized_delta)}
            </span>
            {stateNote ? <span className="text-muted/60"> ({stateNote})</span> : null}
          </>
        ) : (
          <span className="text-muted/70">実現／含みの内訳は未計測</span>
        )}
      </div>
    </Tile>
  );
}

function AlertRow({
  alert,
  expandable,
  expanded,
  onToggle,
  children,
}: {
  alert: StatusAlert;
  expandable?: boolean;
  expanded?: boolean;
  onToggle?: () => void;
  children?: React.ReactNode;
}) {
  const red = alert.level === 'red';
  const body = (
    <>
      <span
        aria-hidden="true"
        className={`mt-[5px] inline-block h-1.5 w-1.5 shrink-0 rounded-full ${
          red ? 'bg-fail' : 'bg-warn'
        }`}
      />
      <span className="min-w-0 flex-1">
        <span className={red ? 'text-fail font-medium' : 'text-warn'}>{alert.text}</span>
        {alert.detail ? (
          <span className="text-muted/70"> — {alert.detail}</span>
        ) : null}
      </span>
      {expandable ? (
        <span className="shrink-0 text-muted/60 text-[10px]">
          {expanded ? '閉じる' : '明細'}
        </span>
      ) : null}
    </>
  );

  return (
    <li className="text-[11px] leading-snug">
      {expandable ? (
        <button
          type="button"
          onClick={onToggle}
          aria-expanded={expanded}
          className="flex w-full items-start gap-1.5 text-left hover:bg-white/[0.03] rounded px-1 -mx-1 py-0.5"
        >
          {body}
        </button>
      ) : (
        <div className="flex items-start gap-1.5 px-1 -mx-1 py-0.5">{body}</div>
      )}
      {expanded ? children : null}
    </li>
  );
}

export function StatusSummary({ snapshot, signalsDate, runId, generatedAt }: Props) {
  const [showAmber, setShowAmber] = useState(false);
  const [showOverdue, setShowOverdue] = useState(false);

  const status = computeStatus(snapshot);
  const { checked, behind } = useFreshness(signalsDate);

  // 鮮度は build 時に判定できない (静的 export) ので、ブラウザで突き合わせた結果を
  // 赤アラートに算入する。「まだ確認していない」を「最新」と言わない。
  // 中身は下の alert 列ではなく既存の警告バナー (FreshnessBanner) が出すので、
  // 件数にだけ足して二重に言わない。
  const reds = status.alerts.filter((a) => a.level === 'red');
  const ambers = status.alerts.filter((a) => a.level === 'amber');
  const nRed = reds.length + (behind ? 1 : 0);

  const s = snapshot?.summary ?? null;
  const gen = fmtGenerated(generatedAt);
  const nOverdue = status.overdue.length;

  const freshValue = !checked ? '確認中' : behind ? `${behind.days} 営業日遅れ` : '最新';
  const freshCls = !checked ? 'text-muted' : behind ? 'text-fail' : 'text-ok';

  return (
    <section className="bg-card rounded-xl shadow-lg p-3 sm:p-4 mb-4">
      <div className="mb-2 flex flex-wrap items-baseline justify-between gap-x-2 gap-y-0.5">
        <h2 className="text-xs uppercase tracking-widest text-muted">状態</h2>
        <span className="text-[10px] text-muted tabular-nums">
          as of <span className="text-cardfg">{signalsDate ?? '—'}</span>
          {runId ? (
            <>
              {' · '}run <span className="text-cardfg">{runId}</span>
            </>
          ) : null}
          {gen ? <> · gen {gen} JST</> : null}
        </span>
      </div>

      <FreshnessBanner date={signalsDate} behind={behind} />

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <TodayTile snap={snapshot} />

        <Tile
          label="保有ポジション"
          value={s ? String(s.n_positions) : '—'}
          valueCls={nOverdue > 0 ? 'text-warn' : undefined}
        >
          {nOverdue > 0 ? (
            <div className="mt-0.5">
              <span className="inline-block rounded bg-fail/20 px-1.5 py-0.5 text-[10px] font-medium text-fail tabular-nums">
                期限超過 {nOverdue}
              </span>
            </div>
          ) : (
            <div className={`${SUB} mt-0.5 text-ok/70`}>期限超過 なし</div>
          )}
          <div className={`${SUB} mt-0.5`}>
            {s ? `L${s.n_long} / S${s.n_short}` : '—'}
            {status.dueToday > 0 ? ` · 本日満期 ${status.dueToday}` : ''}
          </div>
        </Tile>

        <Tile label="データ鮮度" value={freshValue} valueCls={freshCls}>
          <div className={SUB}>{signalsDate ?? '—'} のデータを表示中</div>
          {snapshot?.date && signalsDate && snapshot.date !== signalsDate ? (
            <div className={`${SUB} text-warn/80`}>口座 snapshot は {snapshot.date}</div>
          ) : null}
        </Tile>

        <Tile
          label="赤アラート"
          value={String(nRed)}
          valueCls={nRed > 0 ? 'text-fail' : 'text-ok'}
        >
          <div className={SUB}>
            {ambers.length > 0 ? `注意 ${ambers.length} 件` : '注意なし'}
          </div>
          {nRed === 0 && ambers.length === 0 ? (
            <div className={`${SUB} text-ok/70`}>異常なし</div>
          ) : null}
        </Tile>
      </div>

      {reds.length > 0 ? (
        <ul className="mt-2.5 space-y-1">
          {reds.map((a) =>
            a.id === 'overdue' ? (
              <AlertRow
                key={a.id}
                alert={a}
                expandable
                expanded={showOverdue}
                onToggle={() => setShowOverdue((v) => !v)}
              >
                <ul className="mt-1 mb-1 ml-3 grid grid-cols-1 sm:grid-cols-2 gap-x-4 gap-y-0.5">
                  {status.overdue.map((r) => (
                    <li
                      key={r.symbol}
                      className="flex items-center gap-1.5 text-[10px] tabular-nums"
                    >
                      <span
                        className="rounded px-1 text-[9px] font-semibold shrink-0"
                        style={{ color: sysColor(r.system), background: `${sysColor(r.system)}22` }}
                      >
                        {sysShort(r.system)}
                      </span>
                      <span className="text-cardfg font-medium">{r.symbol}</span>
                      <span className="text-fail">超過 {r.daysOver}d</span>
                      <span className={`ml-auto ${pnlText(r.unrealizedPl)}`}>
                        {fmtSignedUsd(r.unrealizedPl)}
                      </span>
                      <span
                        className={`shrink-0 text-[9px] ${executionLabel(r.executionState).cls}`}
                        title={executionLabel(r.executionState).title}
                      >
                        {executionLabel(r.executionState).short}
                      </span>
                    </li>
                  ))}
                </ul>
                <div className="ml-3 mb-1 text-[10px] text-muted/60 tabular-nums">
                  期限超過分の含み損益 合計{' '}
                  <span className={pnlText(status.overduePl)}>
                    {fmtSignedUsd(status.overduePl)}
                  </span>
                </div>
              </AlertRow>
            ) : (
              <AlertRow key={a.id} alert={a} />
            ),
          )}
        </ul>
      ) : null}

      {ambers.length > 0 ? (
        <div className="mt-2">
          <button
            type="button"
            onClick={() => setShowAmber((v) => !v)}
            aria-expanded={showAmber}
            className="text-[10px] text-muted hover:text-cardfg transition-colors"
          >
            {showAmber ? '▾' : '▸'} 注意 {ambers.length} 件
          </button>
          {showAmber ? (
            <ul className="mt-1 space-y-1">
              {ambers.map((a) => (
                <AlertRow key={a.id} alert={a} />
              ))}
            </ul>
          ) : null}
        </div>
      ) : null}
    </section>
  );
}

export default StatusSummary;
