import type { Narrative } from '@/lib/types';

/**
 * top-bar narrative card。AI narrator (common/narrator.py) が生成した当日
 * headline + summary を表示する。narrative 未設定 (null) なら描画しない
 * ので、page 側で `narrative &&` gating しても、単体でも安全に hidden になる。
 */
export function NarrativeCard({ narrative }: { narrative: Narrative | null }) {
  if (!narrative || (!narrative.headline && !narrative.summary)) return null;

  const reasons = narrative.per_symbol_reasons
    ? Object.entries(narrative.per_symbol_reasons)
    : [];

  return (
    <section className="bg-gradient-to-r from-sky-500/10 to-indigo-500/10 border border-sky-400/20 rounded-xl p-4 shadow-lg mb-4">
      <div className="flex items-baseline justify-between gap-2 mb-1">
        <h2 className="text-[10px] uppercase tracking-wider text-sky-300/80">
          AI narrator
        </h2>
        <span className="text-[10px] text-muted tabular-nums">
          {narrative.model ?? ''}
          {typeof narrative.cost_usd === 'number'
            ? ` · $${narrative.cost_usd.toFixed(3)}`
            : ''}
        </span>
      </div>

      {narrative.headline ? (
        <p className="text-base sm:text-lg font-semibold leading-snug">
          {narrative.headline}
        </p>
      ) : null}

      {narrative.summary ? (
        <p className="text-sm text-muted mt-1 leading-relaxed">
          {narrative.summary}
        </p>
      ) : null}

      {reasons.length > 0 ? (
        <ul className="mt-2 flex flex-wrap gap-x-4 gap-y-1 text-[11px] text-muted">
          {reasons.map(([sym, why]) => (
            <li key={sym}>
              <span className="font-medium text-cardfg">{sym}</span> {why}
            </li>
          ))}
        </ul>
      ) : null}
    </section>
  );
}

export default NarrativeCard;
