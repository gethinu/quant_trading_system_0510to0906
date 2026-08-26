import { loadDashboardBundle } from '@/lib/loadDashboardBundle';
import { loadSlotFlowArtifacts } from '@/lib/loadSlotFlow';
import { buildSlotFlowModel, type SlotFlowModel } from '@/lib/slotModel';
import { NarrativeCard } from '@/components/NarrativeCard';
import { SlotGridSection } from '@/components/SlotGridSection';
import { FlowSection } from '@/components/FlowSection';
import { PipelineSection } from '@/components/PipelineSection';
import { SignalsSection } from '@/components/SignalsSection';
import { AlpacaSection } from '@/components/AlpacaSection';
import { Tabs } from '@/components/Tabs';
import { StatusSummary } from '@/components/StatusSummary';
import type {
  PipelinePayload,
  SignalsPayload,
  Narrative,
  AlpacaSnapshot,
  DashboardBundleManifest,
  NotifyDelivery,
} from '@/lib/types';

export const dynamic = 'force-static';

function universeOf(payload: PipelinePayload | null): number | null {
  if (!payload) return null;
  // sys1..7 の Tgt phase から最大値を採る (measured/unmeasured 両対応)。
  // 全 sys の Tgt が同じ universe を指すはずだが、null 混在があっても
  // 実測値が 1 つでもあれば拾えるよう max を採る。
  let best: number | null = null;
  for (const sys of Object.values(payload.systems)) {
    const phase = sys.phases.find((p) => p.name === 'Tgt');
    const tgt = phase?.measured === true ? phase.count : null;
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

/** Signals tab content — unchanged from the original single-page layout. */
function SignalsView({
  signals,
  pipeline,
  narrative,
  notifyDelivery,
  slotFlow,
  slotFlowMissing,
}: {
  signals: SignalsPayload | null;
  pipeline: PipelinePayload | null;
  narrative: Narrative | null;
  notifyDelivery: NotifyDelivery | null;
  slotFlow: SlotFlowModel | null;
  slotFlowMissing: string[];
}) {
  const universe = universeOf(pipeline);
  const total = signals?.portfolio.total_signals ?? 0;
  const notional = signals?.portfolio.total_notional_usd ?? 0;
  const { buy, sell } = countSides(signals);
  const hasSignals = total > 0;

  return (
    <div>
      <header className="mb-4">
        <div className="flex items-baseline justify-between gap-2">
          <h1 className="text-[11px] sm:text-xs tracking-widest text-muted uppercase">
            QUANT · daily signals
          </h1>
          <span className="text-[11px] text-muted tabular-nums">
            {signals?.date ?? ''}
            {universe != null ? (
              <span className="ml-2">· universe {universe.toLocaleString()}</span>
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
        {/* 枠 → フローが主役。「なぜ Entry が 0 か」の答えを最上部に置く。 */}
        {slotFlow ? <SlotGridSection model={slotFlow} /> : null}
        {slotFlow ? <FlowSection model={slotFlow} /> : null}
        {slotFlow && slotFlowMissing.length > 0 ? (
          <p className="rounded-lg border border-white/10 bg-card px-3 py-2 text-[10px] leading-relaxed text-muted">
            未 publish の補助 artifact: {slotFlowMissing.join(' / ')}
            。これが届くと、エントリーの skip 理由と寄り前ポジションが実測になります
            （届くまでは today_signals / alpaca_snapshot から読める範囲で表示）。
          </p>
        ) : null}
        {/* signals は一覧 (銘柄・価格) */}
        <SignalsSection payload={signals} notifyDelivery={notifyDelivery} />
        {/* ファネル 6 phase × 7 system は情報密度が高いので既定で畳む (details) */}
        <PipelineSection
          payload={pipeline}
          signalsRunId={signals?.meta.run_id}
        />
      </div>
    </div>
  );
}

function BundleHealth({
  manifest,
  issues,
}: {
  manifest: DashboardBundleManifest | null;
  issues: string[];
}) {
  if (issues.length === 0 && manifest) {
    return (
      <div className="mb-3 rounded-lg border border-ok/25 bg-ok/10 px-3 py-2 text-[10px] text-ok">
        bundle verified · run {manifest.source_run_id} · funnel{' '}
        {manifest.measurement.funnel_measured}/{manifest.measurement.funnel_total} · Exit{' '}
        {manifest.measurement.exit_measured}/7
      </div>
    );
  }
  if (issues.length === 0) return null;
  return (
    <details
      open
      className="mb-3 rounded-lg border border-warn/30 bg-warn/10 px-3 py-2 text-[10px] text-warn"
    >
      <summary className="cursor-pointer font-medium">
        bundle check: {issues.length} issue{issues.length === 1 ? '' : 's'}
      </summary>
      <ul className="mt-1 list-disc pl-4">
        {issues.map((issue) => (
          <li key={issue}>{issue}</li>
        ))}
      </ul>
    </details>
  );
}

export default function Home() {
  const bundle = loadDashboardBundle();
  const pipeline: PipelinePayload | null = bundle.pipeline;
  const signals: SignalsPayload | null = bundle.signals;
  let narrative: Narrative | null = bundle.narrative;
  const alpaca: AlpacaSnapshot | null = bundle.alpaca;
  const bundleIssues = [...bundle.issues];
  if (signals && narrative && narrative.date !== signals.date) {
    bundleIssues.push(
      `narrative=${narrative.date} / signals=${signals.date} の日付が不一致です。`,
    );
    narrative = null;
  }
  if (
    bundle.manifest?.files?.alpaca_snapshot &&
    alpaca &&
    alpaca.date !== bundle.manifest.date
  ) {
    bundleIssues.push(
      `alpaca=${alpaca.date} / bundle=${bundle.manifest.date} の日付が不一致です。`,
    );
  }
  const uniqueBundleIssues = [...new Set(bundleIssues)];

  // 枠 / フロービュー。bundle の fail-closed 判定には一切影響させない
  // (補助 artifact が欠けても signals / pipeline の表示は止めない)。
  const slotArtifacts = loadSlotFlowArtifacts(signals?.date ?? null, bundle.manifest);
  const slotFlow: SlotFlowModel | null = signals
    ? buildSlotFlowModel({
        signals,
        snapshot: alpaca,
        paperOrders: slotArtifacts.paperOrders,
        exitProposal: slotArtifacts.exitProposal,
        exitExecution: slotArtifacts.exitExecution,
        sidecarVerified: slotArtifacts.verified,
      })
    : null;

  const total = signals?.portfolio.total_signals ?? 0;
  // 当日損益は「同一基準で計測できた時だけ」出す。measured=false の snapshot で
  // バッジにだけ数字が残ると、本文が「未計測」と言っているのに見出しは断言する、
  // という一番たちの悪い矛盾になるので、ここで明示的に落とす。
  const pnlMeasured = alpaca?.pnl_today?.measured ?? true;
  const pnlPct = pnlMeasured ? (alpaca?.account.pnl_today_pct ?? null) : null;
  const alpacaBadge =
    alpaca != null
      ? `${alpaca.summary.n_positions}${
          pnlPct != null ? ` · ${pnlPct >= 0 ? '+' : '−'}${Math.abs(pnlPct).toFixed(1)}%` : ''
        }`
      : undefined;
  const alpacaTone: 'up' | 'down' | 'flat' =
    pnlPct == null ? 'flat' : pnlPct >= 0 ? 'up' : 'down';

  return (
    <main className="mx-auto max-w-5xl p-4 sm:p-6 pb-16">
      <BundleHealth manifest={bundle.manifest} issues={uniqueBundleIssues} />
      {/* 最上部の「状態」サマリー。今日の損益 / 保有と期限超過 / 鮮度と run_id /
          赤アラート件数だけを出す。ここから下 (両タブ) は詳細で、既定で畳んである。 */}
      <StatusSummary
        snapshot={alpaca}
        signalsDate={signals?.date ?? null}
        runId={signals?.meta.run_id ?? null}
        generatedAt={signals?.generated_at ?? null}
      />
      <Tabs
        signalsView={
          <SignalsView
            signals={signals}
            pipeline={pipeline}
            narrative={narrative}
            notifyDelivery={bundle.notifyDelivery}
            slotFlow={slotFlow}
            slotFlowMissing={slotArtifacts.missing}
          />
        }
        alpacaView={<AlpacaSection payload={alpaca} />}
        signalsBadge={total > 0 ? String(total) : undefined}
        alpacaBadge={alpacaBadge}
        alpacaBadgeTone={alpacaTone}
      />

      <footer className="mt-6 text-[10px] text-muted leading-relaxed">
        <div>
          <span className="text-cardfg">signals</span>:{' '}
          today_signals_YYYYMMDD.json (schema v1.0) ·{' '}
          <span className="text-cardfg">pipeline</span>: pipeline_YYYYMMDD.json (v1) ·{' '}
          <span className="text-cardfg">alpaca</span>: alpaca_snapshot_YYYYMMDD.json
          (read-only/paper) ·{' '}
          <span className="text-cardfg">枠/フロー</span>: paper_orders_YYYYMMDD.json ·
          exit_orders_YYYYMMDD_proposal.json ·{' '}
          <span className="text-cardfg">narrator</span>: Claude Haiku 4.5
        </div>
        <div className="mt-1">Static export via Next.js. Data 更新は日次バッチ。</div>
      </footer>
    </main>
  );
}
