import { loadPipeline } from '@/lib/loadPipeline';
import { loadSignals } from '@/lib/loadSignals';
import { loadNarrative } from '@/lib/loadNarrative';
import { NarrativeCard } from '@/components/NarrativeCard';
import type {
  PipelinePayload,
  SystemPipeline,
  SystemPipelinePhase,
  SignalsPayload,
  Signal,
  SystemSignals,
  Narrative,
} from '@/lib/types';

export const dynamic = 'force-static';

const SYSTEMS = ['sys1', 'sys2', 'sys3', 'sys4', 'sys5', 'sys6', 'sys7'];

function fmtPrice(v: number | null): string {
  return v == null ? '—' : `$${v.toFixed(2)}`;
}

function fmtWeight(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`;
}

function fmtRatio(v: number | null): string {
  if (v == null) return '—';
  if (v >= 0.1) return `${(v * 100).toFixed(1)}%`;
  if (v >= 0.001) return `${(v * 100).toFixed(2)}%`;
  return `${(v * 100).toFixed(3)}%`;
}

function fmtCount(v: number | null): string {
  return v == null ? '—' : v.toLocaleString();
}

// ---------------- Signal Pipeline (絞込フロー) ----------------

function universeOf(sys: SystemPipeline | undefined): number | null {
  if (!sys) return null;
  return sys.phases.find((p) => p.name === 'universe')?.count ?? null;
}

function finalOf(sys: SystemPipeline): number | null {
  if (sys.final_signals != null) return sys.final_signals;
  return sys.phases.find((p) => p.name === 'final')?.count ?? null;
}

/** 各 phase を 1 行で。bar は「直前 phase に対する残存率」(絞込の勢い) を表す。 */
function PhaseRow({ phase }: { phase: SystemPipelinePhase }) {
  const measured = phase.count != null;
  // bar 幅: ratio_of_prev があればそれ、無ければ universe phase は満幅。
  const barPct =
    phase.ratio_of_prev != null
      ? Math.max(2, Math.min(100, phase.ratio_of_prev * 100))
      : phase.name === 'universe'
      ? 100
      : 0;
  return (
    <div className="py-1.5 border-t border-white/5">
      <div className="flex items-baseline justify-between gap-2">
        <span className="text-[13px] font-medium truncate">{phase.label}</span>
        <span
          className={`text-[13px] tabular-nums ${
            measured ? '' : 'text-muted italic'
          }`}
        >
          {measured ? fmtCount(phase.count) : 'not measured'}
        </span>
      </div>
      {/* narrowing bar (ratio of previous phase) */}
      <div className="mt-1 h-1.5 w-full rounded bg-white/5 overflow-hidden">
        <div
          className={`h-full rounded ${
            measured ? 'bg-sky-400/70' : 'bg-white/10'
          }`}
          style={{ width: `${barPct}%` }}
        />
      </div>
      <div className="mt-0.5 flex justify-between text-[10px] text-muted tabular-nums">
        <span className="truncate mr-2">{phase.condition ?? ''}</span>
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
        <span className="flex items-center gap-2">
          <span className="font-medium">{sys.system_id}</span>
          <span className="text-[10px] text-muted tabular-nums">
            {fmtCount(universe)} → {fmtCount(final)}
          </span>
        </span>
        <span className="inline-block px-2 py-0.5 rounded-full bg-white/10 text-[10px] tabular-nums">
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

function PipelineSection({ payload }: { payload: PipelinePayload | null }) {
  return (
    <section className="bg-card rounded-xl p-4 shadow-lg">
      <div className="flex items-baseline justify-between mb-1">
        <h2 className="text-xs uppercase tracking-wider text-muted">
          Signal Pipeline
        </h2>
        {payload?.date ? (
          <span className="text-[10px] text-muted">{payload.date}</span>
        ) : null}
      </div>
      <p className="text-[10px] text-muted mb-3 leading-snug">
        universe → setup → filter → … → final の絞込フロー。数値は
        <span className="text-cardfg"> 絞込透明性のための参考値</span>で、
        通過率は評価軸ではありません (厳しい gate ほど final は少数になる設計)。
      </p>
      {!payload ? (
        <div className="text-sm text-muted">
          No pipeline data yet. Run{' '}
          <code className="text-cardfg">scripts/daily_polygon_monitor.py</code>.
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
          ※ 旧 coverage schema から fallback 表示中 (universe → gate のみ)。
        </p>
      ) : null}
    </section>
  );
}

// ---------------- Today's Signals (unchanged) ----------------

function SignalRow({ s }: { s: Signal }) {
  const buy = s.side === 'BUY';
  return (
    <tr className="border-t border-white/5">
      <td className="py-1.5 text-muted">{s.rank ?? '—'}</td>
      <td className="py-1.5 font-medium">{s.symbol}</td>
      <td className="py-1.5">
        <span
          className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold ${
            buy ? 'bg-ok/20 text-ok' : 'bg-fail/20 text-fail'
          }`}
        >
          {s.side}
        </span>
      </td>
      <td className="py-1.5 text-right tabular-nums">{fmtPrice(s.entry_price)}</td>
      <td className="py-1.5 text-right tabular-nums">{fmtWeight(s.weight)}</td>
      <td className="py-1.5 text-right text-[11px] text-muted max-w-[8rem] truncate">
        {s.reason ?? ''}
      </td>
    </tr>
  );
}

function SystemAccordion({ sys, data }: { sys: string; data: SystemSignals }) {
  const hasSignals = data.signals.length > 0;
  return (
    <details
      className="rounded-lg bg-white/[0.03] border border-white/5"
      open={hasSignals}
    >
      <summary className="cursor-pointer select-none list-none px-3 py-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <span className="font-medium">{sys}</span>
          <span className="inline-block px-2 py-0.5 rounded-full bg-white/10 text-[10px] tabular-nums">
            {data.n_signals_output} signal{data.n_signals_output === 1 ? '' : 's'}
          </span>
        </span>
        <span className="text-[11px] tabular-nums text-muted">
          {data.n_signals_output}/{data.n_candidates_input} candidates
        </span>
      </summary>
      {hasSignals ? (
        <div className="px-3 pb-3">
          <table className="w-full text-sm">
            <thead>
              <tr className="text-muted text-[10px] uppercase">
                <th className="text-left font-normal py-1">#</th>
                <th className="text-left font-normal py-1">sym</th>
                <th className="text-left font-normal py-1">side</th>
                <th className="text-right font-normal py-1">entry</th>
                <th className="text-right font-normal py-1">wt</th>
                <th className="text-right font-normal py-1">reason</th>
              </tr>
            </thead>
            <tbody>
              {data.signals.map((s, i) => (
                <SignalRow key={`${s.symbol}-${i}`} s={s} />
              ))}
            </tbody>
          </table>
        </div>
      ) : (
        <div className="px-3 pb-3 text-xs text-muted">no signals today</div>
      )}
    </details>
  );
}

function SignalsSection({ payload }: { payload: SignalsPayload | null }) {
  if (!payload) {
    return (
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h2 className="text-xs uppercase tracking-wider text-muted mb-2">
          Today&apos;s Signals
        </h2>
        <div className="text-sm text-muted">
          No signals file yet. Run{' '}
          <code className="text-cardfg">app_today_signals.py --headless</code>.
        </div>
      </section>
    );
  }

  const { portfolio } = payload;
  const orderedSystems = SYSTEMS.filter((s) => payload.systems[s]);

  return (
    <section className="bg-card rounded-xl p-4 shadow-lg">
      <div className="flex items-baseline justify-between mb-2">
        <h2 className="text-xs uppercase tracking-wider text-muted">
          Today&apos;s Signals
        </h2>
        <span className="flex items-center gap-2">
          {payload.meta?.publish_status ? (
            <span
              className={`inline-block px-1.5 py-0.5 rounded text-[9px] uppercase ${
                payload.meta.publish_status === 'failed'
                  ? 'bg-fail/20 text-fail'
                  : payload.meta.publish_status === 'partial'
                  ? 'bg-warn/20 text-warn'
                  : 'bg-ok/20 text-ok'
              }`}
              title="publish_status (ntfy/email 配信結果)"
            >
              publish: {payload.meta.publish_status}
            </span>
          ) : null}
          <span className="text-[10px] text-muted">{payload.date}</span>
        </span>
      </div>

      <div className="flex items-center gap-4 mb-3 text-sm tabular-nums">
        <div>
          <span className="text-2xl font-semibold">{portfolio.total_signals}</span>
          <span className="text-muted text-xs ml-1">signals</span>
        </div>
        <div className="text-muted text-xs">
          notional ${Math.round(portfolio.total_notional_usd).toLocaleString()}
        </div>
        {portfolio.hedge?.symbol ? (
          <div className="ml-auto text-[11px]">
            <span className="text-muted">hedge </span>
            <span className="text-fail font-medium">
              {portfolio.hedge.side} {portfolio.hedge.symbol}
            </span>
          </div>
        ) : null}
      </div>

      <div className="space-y-2">
        {orderedSystems.map((sys) => (
          <SystemAccordion key={sys} sys={sys} data={payload.systems[sys]} />
        ))}
      </div>
    </section>
  );
}

export default function Home() {
  const pipeline: PipelinePayload | null = loadPipeline();
  const signals: SignalsPayload | null = loadSignals();
  const narrative: Narrative | null = loadNarrative();

  const universe = pipeline ? universeOf(pipeline.systems['sys1']) : null;

  return (
    <main className="max-w-5xl mx-auto p-4 sm:p-6">
      <header className="mb-4">
        <h1 className="text-sm tracking-wider text-muted uppercase">
          QUANT_TRADING · SIGNAL PIPELINE
        </h1>
        <div className="text-3xl font-semibold mt-1">
          {universe != null ? `${universe.toLocaleString()} tickers` : 'no data'}
        </div>
        <div className="text-xs text-muted">
          {pipeline?.date ? `latest: ${pipeline.date}` : ''}
        </div>
      </header>

      <NarrativeCard narrative={narrative} />

      <div className="grid grid-cols-1 sm:grid-cols-2 gap-4 items-start">
        <PipelineSection payload={pipeline} />
        <SignalsSection payload={signals} />
      </div>

      <footer className="mt-4 text-[10px] text-muted">
        pipeline: results_csv/pipeline_YYYYMMDD.json (新 schema, 旧
        polygon_daily_coverage_*.json に fallback). signals:
        results_csv/today_signals_YYYYMMDD.json (latest). Build-time static
        export via Next.js.
      </footer>
    </main>
  );
}
