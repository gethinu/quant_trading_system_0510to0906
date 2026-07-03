import type {
  PipelinePayload,
  SystemPipeline,
  SystemPipelinePhase,
} from '@/lib/types';

const SYSTEMS = ['sys1', 'sys2', 'sys3', 'sys4', 'sys5', 'sys6', 'sys7'];

function fmtCount(v: number | null): string {
  return v == null ? '—' : v.toLocaleString();
}

function fmtRatio(v: number | null): string {
  if (v == null) return '—';
  if (v >= 0.1) return `${(v * 100).toFixed(1)}%`;
  if (v >= 0.001) return `${(v * 100).toFixed(2)}%`;
  return `${(v * 100).toFixed(3)}%`;
}

function universeOf(sys: SystemPipeline): number | null {
  return sys.phases.find((p) => p.name === 'Tgt')?.count ?? null;
}

function finalOf(sys: SystemPipeline): number | null {
  if (sys.final_signals != null) return sys.final_signals;
  return sys.phases.find((p) => p.name === 'Entry')?.count ?? null;
}

/**
 * phase 表示ロジック:
 *   - count が number → 実数値 + progress bar (絞込透明性)
 *   - count が null → 「未計測」を淡く表示 (誤解を招かない、hard "not measured" 表現ではなく)
 */
function PhaseRow({ phase }: { phase: SystemPipelinePhase }) {
  const measured = phase.count != null;
  const barPct =
    phase.ratio_of_prev != null
      ? Math.max(2, Math.min(100, phase.ratio_of_prev * 100))
      : phase.name === 'Tgt' && measured
      ? 100
      : 0;
  return (
    <div className="py-1.5 border-t border-white/5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium truncate">{phase.label}</span>
        <span
          className={`text-[13px] tabular-nums ${
            measured ? 'text-cardfg' : 'text-muted/60 italic'
          }`}
        >
          {measured ? fmtCount(phase.count) : '未計測'}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded bg-white/5 overflow-hidden">
        <div
          className={`h-full rounded ${
            measured ? 'bg-sky-400/70' : 'bg-white/10'
          }`}
          style={{ width: `${barPct}%` }}
        />
      </div>
      <div className="mt-0.5 flex justify-between text-[10px] text-muted tabular-nums gap-2">
        <span className="truncate">{phase.condition ?? ''}</span>
        <span className="shrink-0">
          prev {fmtRatio(phase.ratio_of_prev)} · univ{' '}
          {fmtRatio(phase.ratio_of_universe)}
        </span>
      </div>
    </div>
  );
}

function SystemPipelineAccordion({ sys }: { sys: SystemPipeline }) {
  const universe = universeOf(sys);
  const final = finalOf(sys);
  return (
    <details className="rounded-lg bg-white/[0.03] border border-white/5">
      <summary className="cursor-pointer select-none list-none px-3 py-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 min-w-0">
          <span className="font-medium">{sys.system_id}</span>
          <span className="text-[10px] text-muted tabular-nums truncate">
            {fmtCount(universe)} → {fmtCount(final)}
          </span>
        </span>
        <span className="inline-block px-2 py-0.5 rounded-full bg-white/10 text-[10px] tabular-nums shrink-0">
          {final == null ? '—' : final} final
        </span>
      </summary>
      <div className="px-3 pb-2">
        {sys.phases.map((p) => (
          <PhaseRow key={p.name} phase={p} />
        ))}
      </div>
    </details>
  );
}

export function PipelineSection({
  payload,
}: {
  payload: PipelinePayload | null;
}) {
  return (
    <section className="bg-card rounded-xl p-4 shadow-lg">
      {/* default collapsed — 情報密度削減 (E) の柱。 */}
      <details>
        <summary className="cursor-pointer select-none list-none flex items-baseline justify-between mb-1 gap-2">
          <h2 className="text-xs uppercase tracking-widest text-muted">
            ▸ Signal Pipeline
          </h2>
          <span className="text-[10px] text-muted tabular-nums shrink-0">
            {payload?.date ?? ''}
          </span>
        </summary>

        <p className="text-[10px] text-muted mb-3 mt-2 leading-snug">
          Tgt → FILpass → STUpass → TRDlist → Entry → Exit の 6 phase 絞込フロー。
          数値は<span className="text-cardfg"> 絞込透明性のための参考値</span>で、
          通過率は評価軸ではありません (厳しい gate ほど TRDlist/Entry は少数になる設計)。
        </p>
        {!payload ? (
          <div className="text-sm text-muted">
            No pipeline data yet. Run{' '}
            <code className="text-cardfg">
              scripts/daily_polygon_monitor.py
            </code>
            .
          </div>
        ) : (
          <div className="space-y-2">
            {SYSTEMS.filter((s) => payload.systems[s]).map((s) => (
              <SystemPipelineAccordion key={s} sys={payload.systems[s]} />
            ))}
          </div>
        )}
        {payload?.from_legacy ? (
          <p className="mt-2 text-[10px] text-warn">
            ※ 旧 coverage schema から fallback 表示中 (Tgt → FILpass のみ)。
          </p>
        ) : null}
      </details>
    </section>
  );
}

export default PipelineSection;
