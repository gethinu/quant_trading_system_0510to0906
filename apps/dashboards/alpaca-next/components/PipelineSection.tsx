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
  const phase = sys.phases.find((p) => p.name === 'Tgt');
  return phase?.measured === true && Number.isFinite(phase.count)
    ? phase.count
    : null;
}

function finalOf(sys: SystemPipeline): number | null {
  const phase = sys.phases.find((p) => p.name === 'Entry');
  return phase?.measured === true && Number.isFinite(phase.count)
    ? phase.count
    : null;
}

/**
 * phase 表示ロジック:
 *   - measured=true + finite count → 実数値 + progress bar
 *   - countあり/measured=false → 「未検証」(producer契約の不整合)
 *   - count=null/measured=false → 「未計測」(取得不可)
 */
function PhaseRow({ phase }: { phase: SystemPipelinePhase }) {
  const measured = phase.measured === true && Number.isFinite(phase.count);
  const hasUnverifiedValue = !measured && phase.count != null;
  const barPct = !measured
    ? 0
    : phase.ratio_of_prev != null
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
            measured
              ? 'text-cardfg'
              : hasUnverifiedValue
                ? 'text-warn italic'
                : 'text-muted/60 italic'
          }`}
          title={phase.unmeasured_reason ?? undefined}
        >
          {measured ? fmtCount(phase.count) : hasUnverifiedValue ? '未検証' : '未計測'}
        </span>
      </div>
      <div className="mt-1 h-1.5 w-full rounded bg-white/5 overflow-hidden">
        <div
          className={`h-full rounded ${
            measured
              ? 'bg-sky-400/70'
              : hasUnverifiedValue
                ? 'bg-warn/40'
                : 'bg-white/10'
          }`}
          style={{ width: `${barPct}%` }}
        />
      </div>
      <div className="mt-0.5 flex justify-between text-[10px] text-muted tabular-nums gap-2">
        <span className="truncate">
          {phase.unmeasured_reason ?? phase.condition ?? ''}
        </span>
        <span className="shrink-0">
          prev {fmtRatio(measured ? phase.ratio_of_prev : null)} · univ{' '}
          {fmtRatio(measured ? phase.ratio_of_universe : null)}
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
  signalsRunId,
}: {
  payload: PipelinePayload | null;
  signalsRunId?: string | null;
}) {
  const lineageMismatch =
    payload?.source_signals_run_id &&
    signalsRunId &&
    payload.source_signals_run_id !== signalsRunId;
  const lineageUnknown =
    payload && !payload.from_legacy && !payload.source_signals_run_id;
  return (
    <section className="bg-card rounded-xl p-4 shadow-lg">
      {/* default collapsed — 情報密度削減 (E) の柱。 */}
      <details>
        <summary className="cursor-pointer select-none list-none flex items-baseline justify-between mb-1 gap-2">
          <h2 className="text-xs uppercase tracking-widest text-muted">
            ▸ Signal Pipeline <span className="normal-case tracking-normal">(6 phase × 7 system)</span>
          </h2>
          <span className="text-[10px] text-muted tabular-nums shrink-0">
            {payload?.date ?? ''}
          </span>
        </summary>

        <p className="text-[10px] text-muted mb-3 mt-2 leading-snug">
          「なぜ Entry が 0 か」は上の<span className="text-cardfg">枠ビュー</span>が答えます。
          こちらは絞込の内訳を見たい時だけ開いてください。<br />
          Tgt → FILpass → STUpass → TRDlist → Entry → Exit の 6 phase 絞込フロー。
          数値は<span className="text-cardfg"> 絞込透明性のための参考値</span>で、
          通過率は評価軸ではありません (厳しい gate ほど TRDlist/Entry は少数になる設計)。
        </p>
        {lineageMismatch ? (
          <p className="mb-2 rounded border border-fail/30 bg-fail/10 px-2 py-1 text-[10px] text-fail">
            pipeline と signals の run_id が不一致です。値は表示対象外として確認してください。
          </p>
        ) : lineageUnknown ? (
          <p className="mb-2 rounded border border-warn/30 bg-warn/10 px-2 py-1 text-[10px] text-warn">
            pipeline の run lineage は未検証です（旧データ）。
          </p>
        ) : null}
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
