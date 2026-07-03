import type { Signal, SignalsPayload, SystemSignals } from '@/lib/types';

const SYSTEMS = ['sys1', 'sys2', 'sys3', 'sys4', 'sys5', 'sys6', 'sys7'];

const SYSTEM_LABELS: Record<string, string> = {
  sys1: 'ROC200 momentum',
  sys2: '過熱 short',
  sys3: '3日下落 reversal',
  sys4: 'SPY 押し目',
  sys5: 'ADX 反発',
  sys6: '6日 short',
  sys7: 'SPY 52週安値 hedge',
};

function fmtPrice(v: number | null): string {
  return v == null ? '—' : `$${v.toFixed(2)}`;
}

function fmtWeight(v: number | null): string {
  return v == null ? '—' : `${(v * 100).toFixed(1)}%`;
}

/** 上位 3 銘柄を chip 形式で 1 行に (mobile 折返し可)。 */
function SignalChips({ signals }: { signals: Signal[] }) {
  const top = signals.slice(0, 3);
  if (top.length === 0) return null;
  return (
    <div className="flex flex-wrap gap-1.5 px-3 pb-2">
      {top.map((s, i) => {
        const buy = s.side === 'BUY';
        return (
          <span
            key={`${s.symbol}-${i}`}
            className={[
              'inline-flex items-baseline gap-1 rounded-md px-1.5 py-0.5',
              'text-[11px] tabular-nums border',
              buy
                ? 'bg-ok/10 border-ok/30 text-ok'
                : 'bg-fail/10 border-fail/30 text-fail',
            ].join(' ')}
            title={s.reason ?? ''}
          >
            <span className="font-semibold">{s.symbol}</span>
            <span className="opacity-80">{s.side}</span>
            <span className="text-cardfg/80">{fmtPrice(s.entry_price)}</span>
          </span>
        );
      })}
      {signals.length > top.length ? (
        <span className="text-[11px] text-muted self-center">
          +{signals.length - top.length} more
        </span>
      ) : null}
    </div>
  );
}

/** table 版 (sticky header + tabular-nums + reason は max-width で truncate)。 */
function SignalTable({ signals }: { signals: Signal[] }) {
  return (
    <div className="px-3 pb-3 overflow-x-auto">
      <table className="w-full text-sm min-w-[380px]">
        <thead className="text-muted text-[10px] uppercase sticky top-0 bg-card">
          <tr>
            <th className="text-left font-normal py-1 pr-2">#</th>
            <th className="text-left font-normal py-1 pr-2">sym</th>
            <th className="text-left font-normal py-1 pr-2">side</th>
            <th className="text-right font-normal py-1 pr-2">entry</th>
            <th className="text-right font-normal py-1 pr-2">wt</th>
            <th className="text-right font-normal py-1">reason</th>
          </tr>
        </thead>
        <tbody>
          {signals.map((s, i) => {
            const buy = s.side === 'BUY';
            return (
              <tr key={`${s.symbol}-${i}`} className="border-t border-white/5">
                <td className="py-1.5 text-muted tabular-nums">
                  {s.rank ?? '—'}
                </td>
                <td className="py-1.5 font-medium">{s.symbol}</td>
                <td className="py-1.5">
                  <span
                    className={[
                      'inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold',
                      buy ? 'bg-ok/20 text-ok' : 'bg-fail/20 text-fail',
                    ].join(' ')}
                  >
                    {s.side}
                  </span>
                </td>
                <td className="py-1.5 text-right tabular-nums">
                  {fmtPrice(s.entry_price)}
                </td>
                <td className="py-1.5 text-right tabular-nums">
                  {fmtWeight(s.weight)}
                </td>
                <td className="py-1.5 text-right text-[11px] text-muted max-w-[10rem] truncate">
                  {s.reason ?? ''}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </div>
  );
}

function SystemAccordion({ sys, data }: { sys: string; data: SystemSignals }) {
  const hasSignals = data.signals.length > 0;
  const label = SYSTEM_LABELS[sys] ?? '';
  return (
    <details
      className="rounded-lg bg-white/[0.03] border border-white/5"
      // default collapsed で密度削減。summary 行に chip を出すので閉じたままでも
      // 上位 3 銘柄は見える。
    >
      <summary className="cursor-pointer select-none list-none px-3 py-2 flex items-center justify-between gap-2">
        <span className="flex items-center gap-2 min-w-0">
          <span className="font-medium">{sys}</span>
          <span className="text-[10px] text-muted truncate">{label}</span>
        </span>
        <span className="flex items-center gap-2 shrink-0">
          <span className="inline-block px-2 py-0.5 rounded-full bg-white/10 text-[10px] tabular-nums">
            {data.n_signals_output} signal
            {data.n_signals_output === 1 ? '' : 's'}
          </span>
        </span>
      </summary>
      {hasSignals ? (
        <>
          <SignalChips signals={data.signals} />
          <SignalTable signals={data.signals} />
        </>
      ) : (
        <div className="px-3 pb-3 text-xs text-muted">no signals today</div>
      )}
    </details>
  );
}

export function SignalsSection({
  payload,
}: {
  payload: SignalsPayload | null;
}) {
  if (!payload) {
    return (
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h2 className="text-xs uppercase tracking-widest text-muted mb-2">
          Today&apos;s Signals
        </h2>
        <div className="text-sm text-muted">
          No signals file yet. Run{' '}
          <code className="text-cardfg">
            app_today_signals.py --headless
          </code>
          .
        </div>
      </section>
    );
  }

  const orderedSystems = SYSTEMS.filter(
    (s) => payload.systems[s] && payload.systems[s].signals.length > 0,
  );
  const emptySystems = SYSTEMS.filter(
    (s) => payload.systems[s] && payload.systems[s].signals.length === 0,
  );

  return (
    <section className="bg-card rounded-xl p-4 shadow-lg">
      <div className="flex items-baseline justify-between mb-3">
        <h2 className="text-xs uppercase tracking-widest text-muted">
          Today&apos;s Signals
        </h2>
        <span className="flex items-center gap-2">
          {payload.meta?.publish_status ? (
            <span
              className={[
                'inline-block px-1.5 py-0.5 rounded text-[9px] uppercase',
                payload.meta.publish_status === 'failed'
                  ? 'bg-fail/20 text-fail'
                  : payload.meta.publish_status === 'partial'
                  ? 'bg-warn/20 text-warn'
                  : 'bg-ok/20 text-ok',
              ].join(' ')}
              title="publish_status (ntfy/email 配信結果)"
            >
              publish: {payload.meta.publish_status}
            </span>
          ) : null}
          <span className="text-[10px] text-muted">{payload.date}</span>
        </span>
      </div>

      {payload.portfolio.hedge?.symbol ? (
        <div className="mb-3 flex items-center gap-2 text-[11px]">
          <span className="text-muted">hedge</span>
          <span className="text-fail font-medium">
            {payload.portfolio.hedge.side} {payload.portfolio.hedge.symbol}
          </span>
        </div>
      ) : null}

      <div className="space-y-2">
        {orderedSystems.map((sys) => (
          <SystemAccordion key={sys} sys={sys} data={payload.systems[sys]} />
        ))}
        {emptySystems.length > 0 ? (
          <details className="rounded-lg bg-white/[0.02] border border-white/5">
            <summary className="cursor-pointer select-none list-none px-3 py-2 text-[11px] text-muted">
              ▸ 信号なし ({emptySystems.length} systems: {emptySystems.join(', ')})
            </summary>
            <div className="px-3 pb-2 text-[11px] text-muted">
              gate 通過なし。データ側は正常。
            </div>
          </details>
        ) : null}
      </div>
    </section>
  );
}

export default SignalsSection;
