import type { ExitExecutionState } from './types';

/**
 * 表示用フォーマッタと system の色。
 *
 * AlpacaSection と StatusSummary は同じ数字を別の場所に出す (サマリー / 詳細)。
 * 片方だけ丸め方や符号の出し方が違うと「上と下で数字が食い違う」ように見えるので、
 * 定義はここ 1 箇所に置いて両方から使う。
 */

export function fmtUsd(v: number | null | undefined, digits = 0): string {
  if (v == null) return '—';
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 10_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toLocaleString('en-US', { maximumFractionDigits: digits })}`;
}

export function fmtSignedUsd(v: number | null | undefined): string {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '−';
  return `${s}${fmtUsd(Math.abs(v), 2)}`;
}

export function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '−';
  return `${s}${Math.abs(v).toFixed(digits)}%`;
}

export function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '—';
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

export function fmtQty(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(3);
}

export const pnlText = (v: number | null | undefined) =>
  v == null ? 'text-muted' : v > 0 ? 'text-ok' : v < 0 ? 'text-fail' : 'text-muted';

// system → accent color (tag chip / allocation bar)
export const SYSTEM_COLOR: Record<string, string> = {
  system1: '#38bdf8',
  system2: '#f472b6',
  system3: '#a78bfa',
  system4: '#34d399',
  system5: '#fbbf24',
  system6: '#fb7185',
  system7: '#94a3b8',
  // 上場廃止 (INACTIVE / 非tradable) で API から close 不能なポジション。
  // muted terracotta で「取引不能・要注意」を示し、system 各色とも被らない。
  delisted: '#c08457',
  unknown: '#64748b',
};

export const sysColor = (s: string) => SYSTEM_COLOR[s] ?? '#64748b';
export const sysShort = (s: string) => (s.startsWith('system') ? 'S' + s.slice(6) : s);

/**
 * exit の broker 送信状態のラベル。
 *
 * 「期限が来た」と「broker へ送った」は別の事実で、混ぜると嘘になる。特に
 * ``pending_execution`` (= 当日 artifact が提案どまり) は **失敗ではない**:
 * exit の実発注は夜の open_auto_run なので、朝の publish 時点で提案しか無いのが
 * 正常。ここを未送信の赤で出すと毎朝全件赤になり、警報として死ぬ。
 */
export function executionLabel(state: ExitExecutionState): {
  state: ExitExecutionState;
  short: string;
  cls: string;
  title: string;
} {
  switch (state) {
    case 'submitted':
      return {
        state,
        short: '送信済',
        cls: 'text-warn/90',
        title: 'broker へ exit order を送信済み',
      };
    case 'pending_execution':
      return {
        state,
        short: '発注待ち',
        cls: 'text-muted/70',
        title: '本日の exit 計画に入っています (夜の実発注 run が未実行)',
      };
    case 'failed':
      return {
        state,
        short: '送信失敗',
        cls: 'text-fail',
        title: 'broker への exit 送信が失敗',
      };
    case 'not_submitted':
      return {
        state,
        short: '未送信',
        cls: 'text-fail',
        title: '実発注 run だが order_id が無い',
      };
    case 'not_planned':
      return {
        state,
        short: '未計画',
        cls: 'text-fail',
        title: '期限到来だが当日の exit 計画に入っていない',
      };
    default:
      return {
        state,
        short: '未計測',
        cls: 'text-fail/80',
        title: '当日の exit artifact が無く送信状態が不明',
      };
  }
}
