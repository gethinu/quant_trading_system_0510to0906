import type { Narrative } from '@/lib/types';

/**
 * AI narrator の当日ナラティブを 3 段構成で表示するカード:
 *   1. headline (常時表示, 1-2 行)
 *   2. summary の TL;DR (2-3 行, 先頭 ~140 字 or 最初の段落)
 *   3. <details> で "詳細を見る" (full summary + per_symbol_reasons)
 *
 * 銘柄 chip は flex-wrap で必ず折り返す (2026-07-02 の垂直オーバーフロー
 * incident を回避)。mobile viewport 380px でも読める font/spacing。
 */

interface Props {
  narrative: Narrative | null;
}

/**
 * summary の TL;DR を最初の段落 or 最初の 140 字で作る。
 * markdown 太字 (`**xxx**`) は plain text 化。
 */
function truncateSummary(summary: string, limit = 140): string {
  const plain = summary.replace(/\*\*(.+?)\*\*/g, '$1').trim();
  if (plain.length <= limit) return plain;
  // 最初の句点 (「。」) までを preferred cut。無ければ limit + "…"。
  const firstStop = plain.indexOf('。');
  if (firstStop > 0 && firstStop <= limit + 20) {
    return plain.slice(0, firstStop + 1);
  }
  return plain.slice(0, limit) + '…';
}

/** markdown 太字を bold 化した React nodes に変換 (段落単位で split)。 */
function renderRich(summary: string): React.ReactNode[] {
  // 「**買いシグナル（39件）の構成：**」等が段落 marker になる。
  // 見た目上は段落間で改行を入れて可読性を上げる。
  const paragraphs = summary
    .split(/(?<=[。」])\s*(?=\*\*)/g)
    .map((p) => p.trim())
    .filter(Boolean);
  return paragraphs.map((p, idx) => {
    const nodes: React.ReactNode[] = [];
    let rest = p;
    let key = 0;
    while (rest.length > 0) {
      const m = rest.match(/^\*\*(.+?)\*\*/);
      if (m) {
        nodes.push(
          <strong key={`b${idx}-${key++}`} className="text-cardfg font-semibold">
            {m[1]}
          </strong>,
        );
        rest = rest.slice(m[0].length);
      } else {
        const next = rest.indexOf('**');
        const chunk = next === -1 ? rest : rest.slice(0, next);
        nodes.push(<span key={`t${idx}-${key++}`}>{chunk}</span>);
        rest = next === -1 ? '' : rest.slice(next);
      }
    }
    return (
      <p key={`p${idx}`} className="text-sm text-muted leading-relaxed">
        {nodes}
      </p>
    );
  });
}

export function NarrativeCard({ narrative }: Props) {
  if (!narrative || (!narrative.headline && !narrative.summary)) return null;

  const reasons = narrative.per_symbol_reasons
    ? Object.entries(narrative.per_symbol_reasons)
    : [];

  const tldr = truncateSummary(narrative.summary ?? '', 140);
  const hasMore =
    (narrative.summary ?? '').length > tldr.length || reasons.length > 0;

  return (
    <section
      className={[
        'mb-4 rounded-xl p-4 shadow-lg',
        'bg-gradient-to-r from-sky-500/10 to-indigo-500/10',
        'border border-sky-400/20',
      ].join(' ')}
      aria-label="AI narrator card"
    >
      {/* row 1: meta */}
      <div className="mb-1.5 flex items-baseline justify-between gap-2">
        <h2 className="text-[10px] uppercase tracking-widest text-sky-300/80">
          AI narrator
        </h2>
        <span className="text-[10px] text-muted tabular-nums truncate max-w-[60%] text-right">
          {narrative.model ?? ''}
          {typeof narrative.cost_usd === 'number'
            ? ` · $${narrative.cost_usd.toFixed(3)}`
            : ''}
        </span>
      </div>

      {/* row 2: headline (1-2 行) */}
      {narrative.headline ? (
        <p className="text-base sm:text-lg font-semibold leading-snug text-cardfg">
          {narrative.headline}
        </p>
      ) : null}

      {/* row 3: TL;DR (2-3 行) */}
      {tldr ? (
        <p className="mt-1.5 text-sm text-muted leading-relaxed">{tldr}</p>
      ) : null}

      {/* row 4: 詳細 accordion (default closed) */}
      {hasMore ? (
        <details className="group mt-2.5">
          <summary
            className={[
              'cursor-pointer select-none list-none inline-flex items-center gap-1',
              'text-[11px] uppercase tracking-wider text-sky-300/80',
              'hover:text-sky-200 transition-colors',
            ].join(' ')}
          >
            <span className="group-open:hidden">▸ 詳細を見る</span>
            <span className="hidden group-open:inline">▾ 詳細を閉じる</span>
          </summary>

          <div className="mt-3 space-y-2">
            {/* full summary (段落分け + bold) */}
            {narrative.summary ? renderRich(narrative.summary) : null}

            {/* per-symbol reason chips (flex-wrap で必ず折り返す) */}
            {reasons.length > 0 ? (
              <div className="mt-3">
                <h3 className="text-[10px] uppercase tracking-wider text-muted mb-1.5">
                  Per-symbol reasons ({reasons.length})
                </h3>
                <ul className="flex flex-wrap gap-1.5">
                  {reasons.map(([sym, why]) => (
                    <li
                      key={sym}
                      className={[
                        'inline-flex items-baseline gap-1',
                        'rounded-md bg-white/[0.06] border border-white/10',
                        'px-1.5 py-1 text-[11px] leading-tight',
                        'max-w-full',
                      ].join(' ')}
                      title={why}
                    >
                      <span className="font-semibold text-cardfg tabular-nums">
                        {sym}
                      </span>
                      <span className="text-muted truncate max-w-[14rem]">
                        {why}
                      </span>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        </details>
      ) : null}
    </section>
  );
}

export default NarrativeCard;
