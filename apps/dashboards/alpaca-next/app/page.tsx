import { loadPipeline } from '@/lib/loadPipeline';
import { loadSignals } from '@/lib/loadSignals';
import { loadNarrative } from '@/lib/loadNarrative';
import { NarrativeCard } from '@/components/NarrativeCard';
import { PipelineSection } from '@/components/PipelineSection';
import { SignalsSection } from '@/components/SignalsSection';
import type {
  PipelinePayload,
  SignalsPayload,
  Narrative,
} from '@/lib/types';

export const dynamic = 'force-static';

function universeOf(payload: PipelinePayload | null): number | null {
  if (!payload) return null;
  // sys1..7 の Tgt phase から最大値を採る (measured/unmeasured 両対応)。
  // 全 sys の Tgt が同じ universe を指すはずだが、null 混在があっても
  // 実測値が 1 つでもあれば拾えるよう max を採る。
  let best: number | null = null;
  for (const sys of Object.values(payload.systems)) {
    const tgt = sys.phases.find((p) => p.name === 'Tgt')?.count;
    if (typeof tgt === 'number' && (best == null || tgt > best)) best = tgt;
  }
  return best;
}

function fmtUsd(v: number): string {
  if (v >= 1_000_000) return `$${(v / 1_000_000).toFixed(1)}M`;
  if (v >= 1_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${Math.round(v)}`;
}

function countSides(payload: SignalsPayload | null): {
  buy: number;
  sell: number;
} {
  let buy = 0;
  let sell = 0;
  if (!payload) return { buy, sell };
  for (const sys of Object.values(payload.systems)) {
    for (const s of sys.signals) {
      if (s.side === 'BUY') buy += 1;
      else if (s.side === 'SELL') sell += 1;
    }
  }
  return { buy, sell };
}

export default function Home() {
  const pipeline: PipelinePayload | null = loadPipeline();
  const signals: SignalsPayload | null = loadSignals();
  const narrative: Narrative | null = loadNarrative();

  const universe = universeOf(pipeline);
  const total = signals?.portfolio.total_signals ?? 0;
  const notional = signals?.portfolio.total_notional_usd ?? 0;
  const { buy, sell } = countSides(signals);
  const displayDate =
    signals?.date ?? narrative?.date ?? pipeline?.date ?? '';

  // 「今日は何が起きたか」を第一印象で伝える 3 KPI:
  //   1. total signals (お金に直結する count)
  //   2. BUY / SELL split (方向感)
  //   3. notional (規模)
  // universe は sub-info (小さい text で date 隣)。
  const hasSignals = total > 0;

  return (
    <main className="mx-auto max-w-5xl p-4 sm:p-6 pb-16">
      <header className="mb-4">
        <div className="flex items-baseline justify-between gap-2">
          <h1 className="text-[11px] sm:text-xs tracking-widest text-muted uppercase">
            QUANT · daily signals
          </h1>
          <span className="text-[11px] text-muted tabular-nums">
            {displayDate}
            {universe != null ? (
              <span className="ml-2">
                · universe {universe.toLocaleString()}
              </span>
            ) : null}
          </span>
        </div>

        {/* KPI hero (mobile-first: single row wraps to 2 rows on <380px) */}
        <div className="mt-2 flex flex-wrap items-baseline gap-x-5 gap-y-1">
          <div className="flex items-baseline gap-1.5">
            <span className="text-4xl sm:text-5xl font-semibold tabular-nums leading-none">
              {total}
            </span>
            <span className="text-sm text-muted">
              {hasSignals ? 'signals' : 'no signals'}
            </span>
          </div>
          {hasSignals ? (
            <div className="flex items-center gap-2 text-sm tabular-nums">
              <span className="px-1.5 py-0.5 rounded bg-ok/15 text-ok font-medium">
                BUY {buy}
              </span>
              <span className="px-1.5 py-0.5 rounded bg-fail/15 text-fail font-medium">
                SELL {sell}
              </span>
              <span className="text-muted">· {fmtUsd(notional)} notional</span>
            </div>
          ) : null}
        </div>
      </header>

      <NarrativeCard narrative={narrative} />

      <div className="grid grid-cols-1 gap-4 items-start">
        {/* signals first (subscriber pitch: 実データが見出しの直下) */}
        <SignalsSection payload={signals} />
        {/* pipeline は詳細 (default collapsed via <details>) */}
        <PipelineSection payload={pipeline} />
      </div>

      <footer className="mt-6 text-[10px] text-muted leading-relaxed">
        <div>
          <span className="text-cardfg">signals</span>:
          {' '}today_signals_YYYYMMDD.json (schema v1.0) ·
          {' '}<span className="text-cardfg">pipeline</span>:
          {' '}pipeline_YYYYMMDD.json (v1) ·
          {' '}<span className="text-cardfg">narrator</span>:
          {' '}Claude Haiku 4.5 (fail-safe, cost 記録)
        </div>
        <div className="mt-1">Static export via Next.js. Data 更新は日次バッチ。</div>
      </footer>
    </main>
  );
}
