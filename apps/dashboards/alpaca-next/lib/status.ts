/**
 * 「今どういう状態か」を snapshot から 1 回で導出する。
 *
 * なぜ必要か: これまでダッシュは事実を全部並べていたが、状態を掴むには縦に長い表を
 * 端まで読む必要があった。特に **期限超過の建玉**（max_hold を過ぎたのに残っている =
 * exit が詰まっている）は、保有ポジション表の一番下まで scroll して badge を数えないと
 * 分からなかった。ここで件数として数え、最上部のサマリーに出す。
 *
 * 方針:
 *   - 数字は snapshot にある事実からしか作らない (推測で埋めない)。
 *   - 「出せない」は 0 ではなく amber の alert として出す (0 と不明は別物)。
 *   - red = 今すぐ人間が見るべき / amber = 把握しておくべき、で分ける。
 */

import type { AlpacaPosition, AlpacaSnapshot, ExitExecutionState } from './types';

export type AlertLevel = 'red' | 'amber';

export interface StatusAlert {
  id: string;
  level: AlertLevel;
  /** 1 行で状態が分かる本文。 */
  text: string;
  /** 根拠・内訳 (件数の出どころ)。 */
  detail?: string;
}

/** 期限超過 1 件。サマリーから畳んで開ける最小の明細。 */
export interface OverdueRow {
  symbol: string;
  system: string;
  /** 何日超過しているか (正の数)。 */
  daysOver: number;
  unrealizedPl: number;
  /** 当日 exit artifact と突合した broker 送信状態。旧 snapshot は unmeasured。 */
  executionState: ExitExecutionState;
}

export interface DashboardStatus {
  overdue: OverdueRow[];
  maxDaysOver: number | null;
  /** 期限超過建玉の含み損益合計。 */
  overduePl: number;
  /** 期限超過のうち broker へ送信済みの件数。 */
  overdueSubmitted: number;
  /** 本日満期 (days_remaining === 0)。まだ超過ではない。 */
  dueToday: number;
  /** 上場廃止などで API から決済できない建玉。 */
  delisted: number;
  alerts: StatusAlert[];
  nRed: number;
  nAmber: number;
}

const EMPTY: DashboardStatus = {
  overdue: [],
  maxDaysOver: null,
  overduePl: 0,
  overdueSubmitted: 0,
  dueToday: 0,
  delisted: 0,
  alerts: [],
  nRed: 0,
  nAmber: 0,
};

const isOverdue = (p: AlpacaPosition) => p.days_remaining != null && p.days_remaining < 0;

export function computeStatus(snap: AlpacaSnapshot | null): DashboardStatus {
  if (!snap) return EMPTY;

  const recon = snap.realized?.exit_intent_reconciliation ?? null;

  const overdue: OverdueRow[] = snap.positions
    .filter(isOverdue)
    .map((p) => ({
      symbol: p.symbol,
      system: p.system,
      daysOver: -(p.days_remaining as number),
      unrealizedPl: p.unrealized_pl,
      executionState: p.exit_execution_state ?? 'unmeasured',
    }))
    .sort((a, b) => b.daysOver - a.daysOver || a.unrealizedPl - b.unrealizedPl);

  const maxDaysOver = overdue.length > 0 ? overdue[0].daysOver : null;
  const overduePl = overdue.reduce((n, r) => n + r.unrealizedPl, 0);
  const overdueSubmitted = overdue.filter((r) => r.executionState === 'submitted').length;
  const dueToday = snap.positions.filter((p) => p.days_remaining === 0).length;
  const delisted = snap.positions.filter(
    (p) => p.system === 'delisted' || p.exit_type === 'delisted',
  ).length;

  const alerts: StatusAlert[] = [];
  const red = (id: string, text: string, detail?: string) =>
    alerts.push({ id, level: 'red', text, detail });
  const amber = (id: string, text: string, detail?: string) =>
    alerts.push({ id, level: 'amber', text, detail });

  if (snap.account.trading_blocked) {
    red('trading_blocked', '口座が取引停止 (broker 側)', 'broker が発注を受け付けません。');
  }

  if (overdue.length > 0) {
    // intent に載っただけでは「発注済」ではない。exact-date artifact の
    // order_id/dry_run/error から broker 送信状態で出す。ただし exit の実発注は
    // 夜なので、朝の時点で提案どまりなのは正常 = pending_execution を失敗と混ぜない。
    const n = (st: ExitExecutionState) =>
      overdue.filter((r) => r.executionState === st).length;
    const executionNote =
      n('pending_execution') === overdue.length
        ? '全件が本日の exit 計画に入っています (夜の発注待ち)'
        : [
            `送信済 ${overdueSubmitted}`,
            n('pending_execution') > 0 ? `発注待ち ${n('pending_execution')}` : null,
            n('failed') > 0 ? `送信失敗 ${n('failed')}` : null,
            n('not_submitted') > 0 ? `未送信 ${n('not_submitted')}` : null,
            n('not_planned') > 0 ? `未計画 ${n('not_planned')}` : null,
            n('unmeasured') > 0 ? `未計測 ${n('unmeasured')}` : null,
          ]
            .filter(Boolean)
            .join(' · ');
    red(
      'overdue',
      `期限超過の建玉 ${overdue.length} 件`,
      `最長 ${maxDaysOver}d 超過 · ${executionNote}`,
    );
  }

  const notFilled = recon?.intended_not_filled ?? [];
  if (notFilled.length > 0) {
    red(
      'exit_not_filled',
      `exit する予定が約定していない ${notFilled.length} 件`,
      notFilled.map((x) => x.symbol).join(', '),
    );
  }

  const ex = snap.exposure;
  if (ex.gross_pct != null && ex.gross_cap_pct > 0 && ex.gross_pct > ex.gross_cap_pct) {
    red(
      'gross_cap',
      `gross exposure が cap 超過 (${ex.gross_pct.toFixed(1)}% / ${ex.gross_cap_pct.toFixed(0)}%)`,
    );
  }
  if (
    ex.net_pct != null &&
    ex.net_cap_pct > 0 &&
    Math.abs(ex.net_pct) > ex.net_cap_pct
  ) {
    red(
      'net_cap',
      `net exposure が cap 超過 (${ex.net_pct.toFixed(1)}% / ${ex.net_cap_pct.toFixed(0)}%)`,
    );
  }

  const realized = snap.realized ?? null;
  if (realized?.stale) {
    red('ledger_stale', '実現損益の台帳が古い', realized.reason ?? undefined);
  }

  // --- amber: 把握しておくべきだが今すぐ手を動かす類ではないもの ---------------
  if (dueToday > 0) {
    amber('due_today', `本日満期の建玉 ${dueToday} 件`, '今日の exit 対象。まだ超過ではありません。');
  }
  if (delisted > 0) {
    amber(
      'delisted',
      `決済できない建玉 ${delisted} 件 (上場廃止)`,
      'API から close 不能。equity には載り続けます。',
    );
  }
  if (snap.pnl_today && !snap.pnl_today.measured) {
    amber(
      'pnl_unmeasured',
      '当日損益が未計測',
      snap.pnl_today.reason ?? '同一基準の前セッション終値が取れません。',
    );
  }
  if (!realized || !realized.available) {
    amber(
      'ledger_missing',
      '実現損益の台帳が未生成',
      realized?.reason ?? '決済の実績が計測されていません (0 ではなく不明)。',
    );
  } else if (realized.complete === false) {
    const n = realized.measurement?.unmeasured_symbols?.length ?? 0;
    amber(
      'ledger_incomplete',
      `決済の一部が未計測${n > 0 ? ` (${n} 銘柄)` : ''}`,
      realized.measurement?.unmeasured_symbols?.join(', ') || undefined,
    );
  }
  if (snap.account.pattern_day_trader) {
    amber('pdt', 'PDT フラグが立っています');
  }

  return {
    overdue,
    maxDaysOver,
    overduePl,
    overdueSubmitted,
    dueToday,
    delisted,
    alerts,
    nRed: alerts.filter((a) => a.level === 'red').length,
    nAmber: alerts.filter((a) => a.level === 'amber').length,
  };
}
