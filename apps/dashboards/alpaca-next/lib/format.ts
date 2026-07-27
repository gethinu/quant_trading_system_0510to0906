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
