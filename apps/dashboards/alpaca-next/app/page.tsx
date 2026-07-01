import { loadCoverage } from '@/lib/loadCoverage';
import { loadSignals } from '@/lib/loadSignals';
import type {
  CoverageDay,
  SystemStat,
  SignalsPayload,
  Signal,
  SystemSignals,
  Narrative,
} from '@/lib/types';

export const dynamic = 'force-static';

const SYSTEMS = ['sys1', 'sys2', 'sys3', 'sys4', 'sys5', 'sys6', 'sys7'];
const WARN_SURVIVAL = 0.05;

function statusClass(s: string): string {
  if (s === 'warn') return 'text-warn';
  if (s === 'fail') return 'text-fail';
  return 'text-ok';
}

function Sparkline({ values }: { values: number[] }) {
  if (values.length === 0) return null;
  const W = 120;
  const H = 24;
  const maxV = Math.max(...values, 0.001);
  const points = values
    .map((v, i) => {
      const x = (i / Math.max(values.length - 1, 1)) * W;
      const y = H - (v / maxV) * H;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');
  return (
    <svg
      viewBox={`0 0 ${W} ${H}`}
      width={W}
      height={H}
      preserveAspectRatio="none"
      className="inline-block align-middle"
    >
      <polyline
        fill="none"
        stroke="#60a5fa"
        strokeWidth="1.5"
        points={points}
      />
    </svg>
  );
}

function fmtPrice(v: number | null): string {
  return v == null ? '—' : `$${v.toFixed(2)}`;
}

function fmtWeight(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`;
}

function survivalClass(ratio: number): string {
  if (ratio < WARN_SURVIVAL) return 'text-fail';
  if (ratio < 0.1) return 'text-warn';
  return 'text-ok';
}

// --- AI narrative top card (Pack 2 narrator 出力を表示) ---------------------
function NarrativeCard({ narrative }: { narrative?: Narrative }) {
  // narrative が無い / 空 (headline も summary も無い) ときは既存 layout を保つため非表示。
  if (!narrative || (!narrative.headline && !narrative.summary)) return null;
  const reasons = Object.entries(narrative.per_symbol_reasons || {});
  return (
    <section className="bg-card rounded-xl p-4 shadow-lg md:sticky md:top-4 z-10 border border-indigo-400/20">
      <details open>
        <summary className="cursor-pointer select-none list-none">
          <span className="flex items-baseline justify-between gap-2">
            <span className="flex items-center gap-2">
              <span className="text-[10px] uppercase tracking-wider text-muted">
                AI narrative
              </span>
              {narrative.fallback ? (
                <span className="inline-block px-1.5 py-0.5 rounded text-[9px] uppercase bg-warn/20 text-warn">
                  fallback
                </span>
              ) : null}
            </span>
            {typeof narrative.cost_usd === 'number' ? (
              <span className="text-[10px] text-muted tabular-nums">
                ${narrative.cost_usd.toFixed(4)}
              </span>
            ) : null}
          </span>
          <div className="text-lg sm:text-xl font-semibold mt-1 leading-snug">
            {narrative.headline || 'Today’s narrative'}
          </div>
        </summary>
        {narrative.summary ? (
          <p className="text-sm text-cardfg/90 mt-2 whitespace-pre-line leading-relaxed">
            {narrative.summary}
          </p>
        ) : null}
        {reasons.length > 0 ? (
          <ul className="mt-3 space-y-1 text-[12px] text-muted">
            {reasons.map(([sym, why]) => (
              <li key={sym}>
                <span className="font-medium text-cardfg">{sym}</span> — {why}
              </li>
            ))}
          </ul>
        ) : null}
      </details>
    </section>
  );
}

function SignalRow({
  s,
  narrativeReason,
}: {
  s: Signal;
  narrativeReason?: string;
}) {
  const buy = s.side === 'BUY';
  const reasonText = narrativeReason || s.reason || '';
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
      <td
        className="py-1.5 text-right text-[11px] text-muted max-w-[9rem] truncate"
        title={reasonText}
      >
        {reasonText}
      </td>
    </tr>
  );
}

function SystemAccordion({
  sys,
  data,
  reasons,
}: {
  sys: string;
  data: SystemSignals;
  reasons: Record<string, string>;
}) {
  const ratioPct = (data.gate_survival_ratio * 100).toFixed(1);
  const hasSignals = data.signals.length > 0;
  const warn = data.gate_survival_ratio < WARN_SURVIVAL;
  return (
    <details
      className={`rounded-lg bg-white/[0.03] border ${
        warn ? 'border-fail/30' : 'border-white/5'
      }`}
      open={hasSignals}
    >
      <summary className="cursor-pointer select-none list-none px-3 py-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2">
          <span className="font-medium">{sys}</span>
          <span className="inline-block px-2 py-0.5 rounded-full bg-white/10 text-[10px] tabular-nums">
            {data.n_signals_output} signal{data.n_signals_output === 1 ? '' : 's'}
          </span>
          {warn ? (
            <span className="inline-block px-1.5 py-0.5 rounded text-[9px] uppercase bg-fail/20 text-fail">
              warn
            </span>
          ) : null}
        </span>
        <span
          className={`text-[11px] tabular-nums ${survivalClass(
            data.gate_survival_ratio,
          )}`}
        >
          survival {ratioPct}%
          <span className="text-muted"> ({data.n_signals_output}/{data.n_candidates_input})</span>
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
                <SignalRow
                  key={`${s.symbol}-${i}`}
                  s={s}
                  narrativeReason={s.symbol ? reasons[s.symbol.toUpperCase()] : undefined}
                />
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
  const reasons = payload.meta?.narrative?.per_symbol_reasons || {};

  // card sort: (1) WARN badge 付き system を優先、(2) PICKS 多い順、(3) sys 番号昇順。
  // Mobile 単列では上から目に入る順序 = 重要度順になる。
  const orderedSystems = SYSTEMS.filter((s) => payload.systems[s]).sort((a, b) => {
    const A = payload.systems[a];
    const B = payload.systems[b];
    const warnA = A.gate_survival_ratio < WARN_SURVIVAL ? 1 : 0;
    const warnB = B.gate_survival_ratio < WARN_SURVIVAL ? 1 : 0;
    if (warnA !== warnB) return warnB - warnA;
    if (B.n_signals_output !== A.n_signals_output)
      return B.n_signals_output - A.n_signals_output;
    return a.localeCompare(b);
  });

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
          <SystemAccordion
            key={sys}
            sys={sys}
            data={payload.systems[sys]}
            reasons={reasons}
          />
        ))}
      </div>
    </section>
  );
}

function CoverageSection({
  history,
  latest,
}: {
  history: CoverageDay[];
  latest: CoverageDay | undefined;
}) {
  return (
    <section className="bg-card rounded-xl p-4 shadow-lg">
      <h2 className="text-xs uppercase tracking-wider text-muted mb-2">
        Gate survival rate (last day)
      </h2>
      <table className="w-full text-sm tabular-nums">
        <thead>
          <tr className="text-muted text-xs">
            <th className="text-left font-normal py-1">system</th>
            <th className="text-right font-normal py-1">ratio</th>
            <th className="text-right font-normal py-1">status</th>
            <th className="text-right font-normal py-1">7d trend</th>
          </tr>
        </thead>
        <tbody>
          {SYSTEMS.map((sys) => {
            const cell: SystemStat | undefined = latest?.survival_by_system?.[sys];
            const trend = history.map(
              (d) => d.survival_by_system?.[sys]?.ratio ?? 0,
            );
            return (
              <tr key={sys} className="border-t border-white/5">
                <td className="py-1.5">{sys}</td>
                <td className={`py-1.5 text-right ${statusClass(cell?.status || 'ok')}`}>
                  {cell ? (cell.ratio * 100).toFixed(1) + '%' : '—'}
                </td>
                <td className="py-1.5 text-right">
                  {cell ? (
                    <span
                      className={`inline-block px-2 py-0.5 rounded text-[10px] uppercase ${
                        cell.status === 'warn'
                          ? 'bg-warn/20 text-warn'
                          : cell.status === 'fail'
                          ? 'bg-fail/20 text-fail'
                          : 'bg-ok/20 text-ok'
                      }`}
                    >
                      {cell.status}
                    </span>
                  ) : (
                    <span className="text-muted">—</span>
                  )}
                </td>
                <td className="py-1.5 text-right">
                  <Sparkline values={trend} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </section>
  );
}

export default function Home() {
  const payload = loadCoverage();
  const history = payload.history;
  const latest: CoverageDay | undefined = history[history.length - 1];
  const signals: SignalsPayload | null = loadSignals();
  const narrative = signals?.meta?.narrative;

  return (
    <main className="max-w-md lg:max-w-5xl mx-auto p-4 sm:p-6">
      <header className="mb-4">
        <h1 className="text-sm tracking-wider text-muted uppercase">
          QUANT_TRADING · POLYGON COVERAGE
        </h1>
        <div className="text-3xl font-semibold mt-1">
          {latest
            ? `${(latest.n_candidates_total || 0).toLocaleString()} tickers`
            : 'no data'}
        </div>
        <div className="text-xs text-muted">
          {latest ? `latest: ${latest.date}` : ''}
        </div>
      </header>

      {/* top bar: AI narrative (mobile-first、md 以上で sticky)。無ければ非表示。 */}
      <div className="mb-4">
        <NarrativeCard narrative={narrative} />
      </div>

      {/* Desktop (lg+): 2 列 grid — 左: coverage / gate survival、右: today's signals。
          Mobile/Tablet (<lg): 単列 (coverage が先、signals が下)。 */}
      <div className="grid grid-cols-1 lg:grid-cols-2 gap-4 items-start">
        <div className="space-y-4">
          <CoverageSection history={history} latest={latest} />
        </div>
        <div className="space-y-4">
          <SignalsSection payload={signals} />
        </div>
      </div>

      <footer className="mt-4 text-[10px] text-muted">
        coverage: results_csv/polygon_daily_coverage_YYYYMMDD.json (last 7 days).
        signals: results_csv/today_signals_YYYYMMDD.json (latest, incl.
        meta.narrative). Build-time static export via Next.js.
      </footer>
    </main>
  );
}
