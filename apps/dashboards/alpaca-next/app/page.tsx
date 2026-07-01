import { loadCoverage } from '@/lib/loadCoverage';
import type { CoverageDay, SystemStat } from '@/lib/types';

export const dynamic = 'force-static';

const SYSTEMS = ['sys1', 'sys2', 'sys3', 'sys4', 'sys5', 'sys6', 'sys7'];

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

export default function Home() {
  const payload = loadCoverage();
  const history = payload.history;
  const latest: CoverageDay | undefined = history[history.length - 1];

  return (
    <main className="max-w-md mx-auto p-4 sm:p-6">
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
              const cell: SystemStat | undefined =
                latest?.survival_by_system?.[sys];
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

      <footer className="mt-4 text-[10px] text-muted">
        source: results_csv/polygon_daily_coverage_YYYYMMDD.json (last 7 days).
        Build-time static export via Next.js.
      </footer>
    </main>
  );
}
