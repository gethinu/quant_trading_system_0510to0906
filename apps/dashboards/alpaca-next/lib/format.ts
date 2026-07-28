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
  // System8 は血統が別 (独自開発の event-driven)。lime で 1-7 のどの色とも被らせない。
  system8: '#a3e635',
  // 上場廃止 (INACTIVE / 非tradable) で API から close 不能なポジション。
  // muted terracotta で「取引不能・要注意」を示し、system 各色とも被らない。
  delisted: '#c08457',
  unknown: '#64748b',
};

/**
 * system の血統 (lineage)。
 *
 * - `bensdorp`: Laurens Bensdorp の自動売買システム本に準拠した定型システム群 (System1-7)。
 *   広いユニバースに指標フィルター + セットアップを当て上位 N を取る、
 *   モメンタム / 平均回帰 × ロング / ショートの組み合わせ。
 * - `original`: 当リポジトリ独自開発。Bensdorp 準拠ではない。現状 System8 のみ
 *   (FOMC 声明日のオーバーナイト・ドリフト = イベントドリブン)。
 *
 * 正準定義は Python 側 `common/system_constants.py` の `SYSTEM_LINEAGE`。
 * ここはその写しなので、増える時は両方を更新すること (docs/SYSTEM_LINEAGE.md)。
 */
export type Lineage = 'bensdorp' | 'original';

export const SYSTEM_LINEAGE: Record<string, Lineage> = {
  system1: 'bensdorp',
  system2: 'bensdorp',
  system3: 'bensdorp',
  system4: 'bensdorp',
  system5: 'bensdorp',
  system6: 'bensdorp',
  system7: 'bensdorp',
  system8: 'original',
};

/** 独自開発 system に付ける控えめな印。Python 側 `LINEAGE_MARKER` と同じ字。 */
export const LINEAGE_MARKER = '◆';

export const LINEAGE_LABEL: Record<Lineage, string> = {
  bensdorp: 'Bensdorp 準拠',
  original: '独自開発 (event-driven)',
};

/** 凡例 1 行。マーカーを出す画面には必ずこれも出して意味不明にしない。 */
export const LINEAGE_LEGEND = `${LINEAGE_MARKER} = ${LINEAGE_LABEL.original} / 無印 = ${LINEAGE_LABEL.bensdorp}`;

export const sysLineage = (s: string): Lineage | null => SYSTEM_LINEAGE[s] ?? null;

/** 独自開発なら marker、それ以外 (未知含む) は空文字。 */
export const sysMarker = (s: string) =>
  SYSTEM_LINEAGE[s] === 'original' ? LINEAGE_MARKER : '';

/** チップ等の tooltip 文言。血統が分かる system のみ返す。 */
export const sysLineageTitle = (s: string): string | undefined => {
  const lin = sysLineage(s);
  return lin ? `${s} — ${LINEAGE_LABEL[lin]}` : undefined;
};

export const sysColor = (s: string) => SYSTEM_COLOR[s] ?? '#64748b';

/**
 * チップ表示用の短縮名。独自開発 system には血統マーカーを付ける
 * (例: `system8` → `S8◆`)。突合や JSON key には使わないこと。
 */
export const sysShort = (s: string) =>
  s.startsWith('system') ? 'S' + s.slice(6) + sysMarker(s) : s;
