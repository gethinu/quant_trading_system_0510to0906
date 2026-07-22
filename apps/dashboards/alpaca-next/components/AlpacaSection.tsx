'use client';

import { useMemo, useState } from 'react';
import type {
  AlpacaPosition,
  AlpacaSnapshot,
  ClosedTrade,
  EquityCurve,
  EquityRange,
  EquityRangeKey,
  RealizedBlock,
  RealizedDay,
  SystemExposure,
} from '@/lib/types';

// --------------------------------------------------------------------------
// formatters
// --------------------------------------------------------------------------
function fmtUsd(v: number | null | undefined, digits = 0): string {
  if (v == null) return '—';
  const abs = Math.abs(v);
  if (abs >= 1_000_000) return `$${(v / 1_000_000).toFixed(2)}M`;
  if (abs >= 10_000) return `$${(v / 1_000).toFixed(1)}K`;
  return `$${v.toLocaleString('en-US', { maximumFractionDigits: digits })}`;
}

function fmtSignedUsd(v: number | null | undefined): string {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '−';
  return `${s}${fmtUsd(Math.abs(v), 2)}`;
}

function fmtPct(v: number | null | undefined, digits = 2): string {
  if (v == null) return '—';
  const s = v >= 0 ? '+' : '−';
  return `${s}${Math.abs(v).toFixed(digits)}%`;
}

function fmtPrice(v: number | null | undefined): string {
  if (v == null) return '—';
  return v < 1 ? `$${v.toFixed(4)}` : `$${v.toFixed(2)}`;
}

function fmtQty(v: number): string {
  return Number.isInteger(v) ? String(v) : v.toFixed(3);
}

const pnlText = (v: number | null | undefined) =>
  v == null ? 'text-muted' : v > 0 ? 'text-ok' : v < 0 ? 'text-fail' : 'text-muted';

// system → accent color (tag chip / allocation bar)
const SYSTEM_COLOR: Record<string, string> = {
  system1: '#38bdf8',
  system2: '#f472b6',
  system3: '#a78bfa',
  system4: '#34d399',
  system5: '#fbbf24',
  system6: '#fb7185',
  system7: '#94a3b8',
  // 上場廃止 (INACTIVE / 非tradable) で API から close 不能なポジション。
  // muted terracotta で「取引不能・要注意」を示し、system 各色とも被らない。
  delisted: '#c08457',
  unknown: '#64748b',
};
const sysColor = (s: string) => SYSTEM_COLOR[s] ?? '#64748b';
const sysShort = (s: string) => (s.startsWith('system') ? 'S' + s.slice(6) : s);

// --------------------------------------------------------------------------
// KPI hero
// --------------------------------------------------------------------------
function Kpi({
  label,
  value,
  sub,
  tone,
}: {
  label: string;
  value: string;
  sub?: string;
  tone?: string;
}) {
  return (
    <div className="rounded-lg bg-white/[0.03] border border-white/5 px-3 py-2">
      <div className="text-[10px] uppercase tracking-wide text-muted">{label}</div>
      <div className={`text-lg font-semibold tabular-nums leading-tight ${tone ?? ''}`}>
        {value}
      </div>
      {sub ? <div className="text-[10px] text-muted tabular-nums">{sub}</div> : null}
    </div>
  );
}

// --------------------------------------------------------------------------
// equity curve (SVG, drawdown band) — no external chart lib (static export)
// --------------------------------------------------------------------------
// 期間切替タブ。snapshot の equity_ranges を持たない旧データでは非表示にする。
const RANGE_ORDER: EquityRangeKey[] = ['1D', '1W', '1M', '3M', 'ALL'];

function EquityPanel({ snap }: { snap: AlpacaSnapshot }) {
  const ranges = snap.equity_ranges ?? null;
  const available = RANGE_ORDER.filter((k) => ranges?.[k] != null);
  // 既定は「1月」= 直近が読める粒度。旧データは 3M 固定 curve に fallback。
  const [sel, setSel] = useState<EquityRangeKey>('1M');

  if (!ranges || available.length === 0) {
    return <EquityChart curve={snap.equity_curve} periodLabel={snap.equity_curve.period} />;
  }

  const active = ranges[sel] ?? ranges[available[available.length - 1]]!;

  return (
    <div>
      <div className="mb-2 flex flex-wrap gap-1">
        {available.map((k) => {
          const r = ranges[k]!;
          const on = k === sel;
          const empty = r.n_points < 2;
          return (
            <button
              key={k}
              type="button"
              onClick={() => setSel(k)}
              disabled={empty}
              title={empty ? 'この期間はデータがありません' : `${r.start} → ${r.end}`}
              className={`rounded px-2 py-0.5 text-[11px] tabular-nums transition ${
                on
                  ? 'bg-white/15 text-cardfg'
                  : empty
                    ? 'text-muted/40 cursor-not-allowed'
                    : 'text-muted hover:bg-white/5'
              }`}
            >
              {r.label}
            </button>
          );
        })}
      </div>
      <EquityChart
        curve={{
          timeframe: active.timeframe,
          period: active.label,
          base_value: null,
          points: active.points,
          peak_equity: active.peak_equity,
          max_drawdown_pct: active.max_drawdown_pct,
          period_return_pct: active.period_return_pct,
          source: 'equity_ranges',
        }}
        periodLabel={active.label}
      />
      <div className="mt-1 text-[10px] text-muted/60 leading-relaxed">
        {active.basis === 'intraday'
          ? `当セッションの 5 分足 equity（live equity と同一基準）。${active.start ?? '—'} → ${active.end ?? '—'}`
          : `broker の日次エクイティ系列（${active.start ?? '—'} → ${active.end ?? '—'}）。` +
            'この系列は上場廃止で決済不能な建玉を計上しないため、上の live equity とは水準が異なります。' +
            '末尾に live equity を継ぎ足していないのは、基準の違う点を足すと最終日だけ跳ねて見えるためです。'}
      </div>
    </div>
  );
}

function EquityChart({
  curve,
  periodLabel,
}: {
  curve: EquityCurve;
  periodLabel?: string;
}) {
  const pts = curve.points ?? [];
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  if (pts.length < 2) {
    return (
      <div className="text-xs text-muted py-6 text-center">
        この期間の equity データがありません（{pts.length} point）。
      </div>
    );
  }
  const W = 640;
  const H = 150;
  const padT = 10;
  const padB = 12;
  const equities = pts.map((p) => p.equity);
  const peaks = pts.map((p) => p.peak ?? p.equity);
  const yMin = Math.min(...equities);
  const yMax = Math.max(...peaks);
  const span = yMax - yMin || 1;
  const x = (i: number) => (i / (pts.length - 1)) * W;
  const y = (v: number) => padT + (1 - (v - yMin) / span) * (H - padT - padB);

  const eqLine = pts
    .map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.equity).toFixed(1)}`)
    .join(' ');
  const peakLine = pts
    .map((p, i) => `${i ? 'L' : 'M'}${x(i).toFixed(1)} ${y(p.peak ?? p.equity).toFixed(1)}`)
    .join(' ');
  const eqArea = `${eqLine} L ${W} ${H - padB} L 0 ${H - padB} Z`;
  // drawdown band = peak line forward, equity line backward, closed.
  const ddBand =
    peakLine +
    ' ' +
    pts
      .map((_, ri) => {
        const i = pts.length - 1 - ri;
        return `L ${x(i).toFixed(1)} ${y(pts[i].equity).toFixed(1)}`;
      })
      .join(' ') +
    ' Z';

  const up = (curve.period_return_pct ?? 0) >= 0;
  const lineColor = up ? '#34d399' : '#f87171';
  const last = pts[pts.length - 1];
  const minIdx = equities.indexOf(yMin);

  const nLabels = 4;
  const xLabels = Array.from({ length: nLabels }, (_, k) => {
    const i = Math.round((k / (nLabels - 1)) * (pts.length - 1));
    return { t: pts[i].t.slice(5), left: (i / (pts.length - 1)) * 100 };
  });

  // hover/touch scrub → nearest point index (pointer events cover mouse + touch)
  const pickIndex = (e: React.PointerEvent<HTMLDivElement>) => {
    const rect = e.currentTarget.getBoundingClientRect();
    if (rect.width <= 0) return;
    const frac = Math.min(1, Math.max(0, (e.clientX - rect.left) / rect.width));
    setHoverIdx(Math.round(frac * (pts.length - 1)));
  };

  const hp = hoverIdx != null ? pts[hoverIdx] : null;
  const hoverLeft = hoverIdx != null ? (hoverIdx / (pts.length - 1)) * 100 : 0;
  const hoverTop = hp ? (y(hp.equity) / H) * 100 : 0;
  // clamp tooltip horizontally so it never overflows the card edges
  const tipLeft = Math.min(88, Math.max(12, hoverLeft));

  return (
    <div>
      <div className="flex items-baseline justify-between mb-1">
        <span className="text-[11px] text-muted">
          equity · {periodLabel ?? curve.period}
        </span>
        <span className="flex items-center gap-3 text-[11px] tabular-nums">
          <span className={up ? 'text-ok' : 'text-fail'}>
            期間 {fmtPct(curve.period_return_pct)}
          </span>
          <span className="text-fail">最大DD {fmtPct(curve.max_drawdown_pct)}</span>
        </span>
      </div>
      <div className="relative">
        <svg
          viewBox={`0 0 ${W} ${H}`}
          preserveAspectRatio="none"
          className="w-full h-[150px]"
          role="img"
          aria-label="equity curve"
        >
          <defs>
            <linearGradient id="eqfill" x1="0" y1="0" x2="0" y2="1">
              <stop offset="0%" stopColor={lineColor} stopOpacity="0.28" />
              <stop offset="100%" stopColor={lineColor} stopOpacity="0" />
            </linearGradient>
          </defs>
          {/* baseline grid at start equity */}
          <line
            x1="0"
            x2={W}
            y1={y(pts[0].equity)}
            y2={y(pts[0].equity)}
            stroke="#ffffff"
            strokeOpacity="0.08"
            strokeDasharray="3 4"
            vectorEffect="non-scaling-stroke"
          />
          {/* drawdown band (peak → equity) */}
          <path d={ddBand} fill="#f87171" fillOpacity="0.10" stroke="none" />
          {/* equity area + line */}
          <path d={eqArea} fill="url(#eqfill)" stroke="none" />
          <path
            d={eqLine}
            fill="none"
            stroke={lineColor}
            strokeWidth="2"
            vectorEffect="non-scaling-stroke"
          />
          {/* markers */}
          <circle cx={x(minIdx)} cy={y(yMin)} r="2.5" fill="#f87171" />
          <circle cx={x(pts.length - 1)} cy={y(last.equity)} r="3" fill={lineColor} />
        </svg>

        {/* hover crosshair + point marker */}
        {hp ? (
          <>
            <div
              className="pointer-events-none absolute top-0 bottom-3 w-px bg-white/30"
              style={{ left: `${hoverLeft}%` }}
            />
            <div
              className="pointer-events-none absolute z-10 h-2.5 w-2.5 -translate-x-1/2 -translate-y-1/2 rounded-full border-2 border-white bg-card shadow"
              style={{ left: `${hoverLeft}%`, top: `${hoverTop}%` }}
            />
          </>
        ) : null}

        {/* pointer/touch capture layer (touch-action pan-y keeps vertical page scroll) */}
        <div
          className="absolute inset-0 cursor-crosshair"
          style={{ touchAction: 'pan-y' }}
          onPointerMove={pickIndex}
          onPointerDown={pickIndex}
          onPointerLeave={() => setHoverIdx(null)}
          onPointerUp={() => setHoverIdx(null)}
          onPointerCancel={() => setHoverIdx(null)}
          role="presentation"
        />

        {/* tooltip: 日付 + equity (+ 日次 P&L / DD) */}
        {hp ? (
          <div
            className="pointer-events-none absolute top-0 z-20 -translate-x-1/2 rounded-md border border-white/10 bg-black/80 px-2 py-1 text-[10px] leading-tight tabular-nums shadow-lg backdrop-blur-sm"
            style={{ left: `${tipLeft}%` }}
          >
            <div className="text-muted">{hp.t}</div>
            <div className="text-sm font-semibold text-cardfg">
              {fmtUsd(hp.equity, 0)}
            </div>
            <div className="flex gap-2">
              <span className={pnlText(hp.pl_pct)}>{fmtPct(hp.pl_pct)}</span>
              {hp.dd_pct != null && hp.dd_pct < 0 ? (
                <span className="text-fail">DD {fmtPct(hp.dd_pct)}</span>
              ) : null}
            </div>
          </div>
        ) : null}

        <div className="pointer-events-none absolute inset-x-0 bottom-0 flex justify-between text-[9px] text-muted px-0.5">
          {xLabels.map((l, i) => (
            <span key={i} className="tabular-nums">
              {l.t}
            </span>
          ))}
        </div>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// exposure: net/gross vs caps + long/short split + per-system allocation
// --------------------------------------------------------------------------
function CapGauge({
  label,
  pct,
  cap,
}: {
  label: string;
  pct: number | null;
  cap: number;
}) {
  const val = Math.abs(pct ?? 0);
  const fill = cap > 0 ? Math.min(100, (val / cap) * 100) : 0;
  const hot = val > cap * 0.9;
  return (
    <div>
      <div className="flex justify-between text-[10px] text-muted mb-0.5">
        <span>{label}</span>
        <span className="tabular-nums">
          {val.toFixed(1)}% <span className="text-muted/60">/ cap {cap.toFixed(0)}%</span>
        </span>
      </div>
      <div className="h-2 rounded bg-white/5 overflow-hidden">
        <div
          className={`h-full rounded ${hot ? 'bg-fail/80' : 'bg-sky-400/70'}`}
          style={{ width: `${fill}%` }}
        />
      </div>
    </div>
  );
}

function ExposureBlock({ snap }: { snap: AlpacaSnapshot }) {
  const ex = snap.exposure;
  const long = ex.long_usd;
  const short = ex.short_usd;
  const gross = ex.gross_usd || 1;
  const longW = (long / gross) * 100;

  // delisted は上場廃止・close 不能の死荷重（gross の大半を占め得る）。
  // 「配分」の対象として不適切なのでこのチャートからは除外し、
  // active(非delisted) gross を基準に % を再計算してスケールを是正する。
  // 数値の実態（gross に delisted を含む）は上のエクスポージャ側で保持する。
  const bucketGross = (s: SystemExposure) => s.long_usd + s.short_usd;
  const delistedBucket = ex.by_system.delisted;
  const activeEntries = Object.entries(ex.by_system).filter(
    ([sys]) => sys !== 'delisted',
  );
  const activeGross =
    activeEntries.reduce((sum, [, s]) => sum + bucketGross(s), 0) || 1;
  const systems = activeEntries
    .map(
      ([sys, s]) =>
        [sys, s, (bucketGross(s) / activeGross) * 100] as [
          string,
          SystemExposure,
          number,
        ],
    )
    .sort((a, b) => b[2] - a[2]);
  const maxPct = Math.max(1, ...systems.map(([, , pct]) => pct));

  return (
    <div className="space-y-3">
      <div className="grid grid-cols-2 gap-3">
        <CapGauge label="net exposure" pct={ex.net_pct} cap={ex.net_cap_pct} />
        <CapGauge label="gross exposure" pct={ex.gross_pct} cap={ex.gross_cap_pct} />
      </div>

      <div>
        <div className="flex justify-between text-[10px] text-muted mb-0.5">
          <span>long / short</span>
          <span className="tabular-nums">
            <span className="text-ok">{fmtUsd(long)}</span>
            {' · '}
            <span className="text-fail">{fmtUsd(short)}</span>
          </span>
        </div>
        <div className="h-2.5 rounded bg-fail/40 overflow-hidden flex">
          <div className="h-full bg-ok/70" style={{ width: `${longW}%` }} />
        </div>
      </div>

      <div>
        <div className="text-[10px] text-muted mb-1">
          system 別配分（% of active gross・delisted 除外）
        </div>
        <div className="space-y-1">
          {systems.map(([sys, s, pct]) => (
            <div key={sys} className="flex items-center gap-2">
              <span
                className="text-[10px] tabular-nums min-w-9 shrink-0 font-medium whitespace-nowrap pr-1"
                style={{ color: sysColor(sys) }}
              >
                {sysShort(sys)}
              </span>
              <div className="flex-1 h-2 rounded bg-white/5 overflow-hidden">
                <div
                  className="h-full rounded"
                  style={{
                    width: `${(pct / maxPct) * 100}%`,
                    backgroundColor: sysColor(sys),
                    opacity: 0.75,
                  }}
                />
              </div>
              <span className="text-[10px] text-muted tabular-nums w-24 text-right shrink-0">
                {pct.toFixed(1)}% · {s.count}
                <span className={`ml-1 ${pnlText(s.unrealized_pl)}`}>
                  {fmtSignedUsd(s.unrealized_pl)}
                </span>
              </span>
            </div>
          ))}
        </div>
        {delistedBucket ? (
          <div className="mt-1.5 flex items-start gap-1.5 text-[9px] leading-snug text-muted/70">
            <span
              className="mt-[3px] inline-block w-2 h-2 rounded-sm shrink-0"
              style={{ backgroundColor: sysColor('delisted'), opacity: 0.55 }}
            />
            <span>
              delisted（close 不能）: {delistedBucket.count} pos ·{' '}
              {fmtUsd(bucketGross(delistedBucket), 2)} · gross の{' '}
              {delistedBucket.pct_of_gross.toFixed(1)}%
              <span className="text-muted/50"> — 配分対象外（死荷重）</span>
            </span>
          </div>
        ) : null}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// reconciliation: signals → orders → held
// --------------------------------------------------------------------------
function ReconStrip({ snap }: { snap: AlpacaSnapshot }) {
  const r = snap.reconciliation;
  const step = (top: string, main: string, sub?: string) => (
    <div className="flex-1 min-w-0">
      <div className="text-[9px] uppercase tracking-wide text-muted truncate">{top}</div>
      <div className="text-sm font-semibold tabular-nums">{main}</div>
      {sub ? <div className="text-[9px] text-muted truncate">{sub}</div> : null}
    </div>
  );
  const arrow = <span className="text-muted/50 px-1 self-center">→</span>;
  return (
    <div>
      <div className="flex items-stretch gap-1">
        {step(
          `signals ${r.signals_date ?? ''}`,
          r.signals_total != null ? String(r.signals_total) : '—',
          r.signals_buy != null ? `B${r.signals_buy} / S${r.signals_sell}` : undefined,
        )}
        {arrow}
        {step(
          `orders ${r.orders_date ?? ''}`,
          r.orders_submitted != null ? String(r.orders_submitted) : '—',
          'submitted',
        )}
        {arrow}
        {step('held now', String(r.held_now), 'positions')}
        {arrow}
        {step(
          'signals→held',
          r.held_from_signals != null ? String(r.held_from_signals) : '—',
          '最新シグナル銘柄',
        )}
      </div>
      {r.note ? (
        <p className="mt-2 text-[9px] text-muted leading-snug">{r.note}</p>
      ) : null}
    </div>
  );
}

// --------------------------------------------------------------------------
// positions table (sortable / filterable, P&L heatmap, exit badge)
// --------------------------------------------------------------------------
function exitBadge(p: AlpacaPosition): { text: string; cls: string; sub?: string } {
  if (p.exit_expected === 'time_based') {
    // days_remaining = max_hold - holding_days: 0 = 本日満期, <0 = 期限超過。
    // exit_expected='time_based' は days_remaining<=0 で必ず立つため、以前は
    // 超過分を区別できず全部「本日手仕舞い」に潰れていた (超過 Nd が dead code)。
    const d = p.days_remaining;
    if (d != null && d < 0) {
      return { text: `超過 ${-d}d`, cls: 'bg-fail/30 text-fail', sub: p.exit_date ?? undefined };
    }
    return { text: '本日手仕舞い', cls: 'bg-fail/20 text-fail', sub: p.exit_date ?? undefined };
  }
  if (p.exit_type === 'time' && p.days_remaining != null) {
    const d = p.days_remaining;
    const cls =
      d <= 0 ? 'bg-fail/20 text-fail' : d <= 2 ? 'bg-warn/20 text-warn' : 'bg-ok/15 text-ok';
    return {
      text: d <= 0 ? `超過 ${-d}d` : `あと${d}日`,
      cls,
      sub: p.exit_date ?? undefined,
    };
  }
  if ((p.exit_type === 'trailing' || p.exit_type === 'stop') && p.stop_price_est != null) {
    return {
      text: `stop ${fmtPrice(p.stop_price_est)}`,
      cls: 'bg-white/10 text-muted',
      sub: p.distance_to_stop_pct != null ? `${fmtPct(p.distance_to_stop_pct, 1)}` : undefined,
    };
  }
  if (p.exit_type === 'spy_hedge') {
    return { text: 'SPYヘッジ', cls: 'bg-sky-400/15 text-sky-300' };
  }
  if (p.exit_type === 'delisted' || p.system === 'delisted') {
    return { text: '上場廃止', cls: 'bg-white/10 text-muted', sub: 'API close 不能' };
  }
  return { text: '—', cls: 'text-muted' };
}

// P&L heatmap cell background: green/red intensity ∝ |pl_pct| (cap 10%).
function heatStyle(pct: number | null): React.CSSProperties {
  if (pct == null) return {};
  const intensity = Math.min(1, Math.abs(pct) / 10);
  const rgb = pct >= 0 ? '52, 211, 153' : '248, 113, 113';
  return { backgroundColor: `rgba(${rgb}, ${(intensity * 0.22).toFixed(3)})` };
}

type SortKey =
  | 'pl'
  | 'pl_pct'
  | 'value'
  | 'holding'
  | 'remaining'
  | 'symbol'
  | 'system';

function PositionsTable({ positions }: { positions: AlpacaPosition[] }) {
  const [sortKey, setSortKey] = useState<SortKey>('value');
  const [asc, setAsc] = useState(false);
  const [sysFilter, setSysFilter] = useState<string>('all');
  const [sideFilter, setSideFilter] = useState<string>('all');
  const [q, setQ] = useState('');
  const [exitOnly, setExitOnly] = useState(false);

  const systemsList = useMemo(
    () => Array.from(new Set(positions.map((p) => p.system))).sort(),
    [positions],
  );

  const rows = useMemo(() => {
    let out = positions.slice();
    if (sysFilter !== 'all') out = out.filter((p) => p.system === sysFilter);
    if (sideFilter !== 'all') out = out.filter((p) => p.side === sideFilter);
    if (exitOnly)
      out = out.filter(
        (p) => p.exit_expected != null || (p.days_remaining != null && p.days_remaining <= 2),
      );
    if (q.trim()) {
      const needle = q.trim().toUpperCase();
      out = out.filter((p) => p.symbol.includes(needle));
    }
    const val = (p: AlpacaPosition): number | string => {
      switch (sortKey) {
        case 'pl':
          return p.unrealized_pl ?? 0;
        case 'pl_pct':
          return p.unrealized_pl_pct ?? 0;
        case 'value':
          return Math.abs(p.market_value ?? 0);
        case 'holding':
          return p.holding_days ?? -1;
        case 'remaining':
          return p.days_remaining ?? 9999;
        case 'symbol':
          return p.symbol;
        case 'system':
          return p.system;
      }
    };
    out.sort((a, b) => {
      const va = val(a);
      const vb = val(b);
      const cmp =
        typeof va === 'string'
          ? va.localeCompare(vb as string)
          : (va as number) - (vb as number);
      return asc ? cmp : -cmp;
    });
    return out;
  }, [positions, sysFilter, sideFilter, exitOnly, q, sortKey, asc]);

  const toggleSort = (k: SortKey) => {
    if (k === sortKey) setAsc((v) => !v);
    else {
      setSortKey(k);
      setAsc(k === 'symbol' || k === 'system' || k === 'remaining');
    }
  };
  const arrow = (k: SortKey) => (k === sortKey ? (asc ? ' ▲' : ' ▼') : '');

  const Th = ({
    k,
    children,
    align = 'right',
  }: {
    k: SortKey;
    children: React.ReactNode;
    align?: 'left' | 'right';
  }) => (
    <th
      className={`font-normal py-1 px-1.5 cursor-pointer select-none whitespace-nowrap hover:text-cardfg ${
        align === 'left' ? 'text-left' : 'text-right'
      }`}
      onClick={() => toggleSort(k)}
    >
      {children}
      {arrow(k)}
    </th>
  );

  return (
    <div>
      {/* controls */}
      <div className="flex flex-wrap items-center gap-2 mb-2">
        <input
          value={q}
          onChange={(e) => setQ(e.target.value)}
          placeholder="銘柄検索"
          className="bg-white/5 border border-white/10 rounded px-2 py-1 text-xs w-24 focus:outline-none focus:border-sky-400/50"
        />
        <select
          value={sysFilter}
          onChange={(e) => setSysFilter(e.target.value)}
          className="bg-white/5 border border-white/10 rounded px-1.5 py-1 text-xs"
        >
          <option value="all">全system</option>
          {systemsList.map((s) => (
            <option key={s} value={s}>
              {s}
            </option>
          ))}
        </select>
        <select
          value={sideFilter}
          onChange={(e) => setSideFilter(e.target.value)}
          className="bg-white/5 border border-white/10 rounded px-1.5 py-1 text-xs"
        >
          <option value="all">L+S</option>
          <option value="long">Long</option>
          <option value="short">Short</option>
        </select>
        <button
          onClick={() => setExitOnly((v) => !v)}
          className={`px-2 py-1 rounded text-xs border ${
            exitOnly
              ? 'bg-warn/20 text-warn border-warn/30'
              : 'bg-white/5 text-muted border-white/10'
          }`}
        >
          exit間近
        </button>
        <span className="text-[10px] text-muted ml-auto tabular-nums">
          {rows.length} / {positions.length}
        </span>
      </div>

      {/* table */}
      <div className="overflow-x-auto -mx-1">
        <table className="w-full text-[12px] min-w-[560px]">
          <thead className="text-muted text-[10px] uppercase sticky top-0 bg-card z-10">
            <tr className="border-b border-white/10">
              <Th k="symbol" align="left">
                sym
              </Th>
              <Th k="system" align="left">
                sys
              </Th>
              <th className="font-normal py-1 px-1.5 text-right">qty</th>
              <th className="font-normal py-1 px-1.5 text-right whitespace-nowrap">
                avg→now
              </th>
              <Th k="pl">P&amp;L</Th>
              <Th k="value">値洗い</Th>
              <Th k="holding">保有</Th>
              <Th k="remaining">エグジット</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((p) => {
              const badge = exitBadge(p);
              const isLong = p.side === 'long';
              return (
                <tr key={p.symbol} className="border-b border-white/5">
                  <td className="py-1.5 px-1.5">
                    <div className="flex items-center gap-1.5">
                      <span
                        className="inline-flex items-center justify-center w-4 h-4 rounded text-[9px] font-bold leading-none"
                        style={
                          isLong
                            ? { color: '#38bdf8', backgroundColor: '#38bdf822' }
                            : { color: '#fbbf24', backgroundColor: '#fbbf2422' }
                        }
                        title={
                          isLong
                            ? 'Long — 買い持ち（価格上昇で利益）'
                            : 'Short — 売り持ち（価格上昇で損失。AVG→NOW 上昇＝赤字は正常）'
                        }
                      >
                        {isLong ? 'L' : 'S'}
                      </span>
                      <span className="font-medium">{p.symbol}</span>
                    </div>
                  </td>
                  <td className="py-1.5 px-1.5">
                    <span
                      className="inline-block px-1.5 py-0.5 rounded text-[9px] font-semibold"
                      style={{
                        color: sysColor(p.system),
                        backgroundColor: `${sysColor(p.system)}22`,
                      }}
                    >
                      {sysShort(p.system)}
                    </span>
                  </td>
                  <td className="py-1.5 px-1.5 text-right tabular-nums text-muted">
                    {fmtQty(p.qty)}
                  </td>
                  <td className="py-1.5 px-1.5 text-right tabular-nums whitespace-nowrap">
                    <span className="text-muted">{fmtPrice(p.avg_entry_price)}</span>
                    <span className="text-muted/40">→</span>
                    <span>{fmtPrice(p.current_price)}</span>
                  </td>
                  <td
                    className="py-1.5 px-1.5 text-right tabular-nums whitespace-nowrap"
                    style={heatStyle(p.unrealized_pl_pct)}
                  >
                    <div className={pnlText(p.unrealized_pl)}>
                      {fmtSignedUsd(p.unrealized_pl)}
                    </div>
                    <div className={`text-[10px] ${pnlText(p.unrealized_pl_pct)}`}>
                      {fmtPct(p.unrealized_pl_pct, 1)}
                    </div>
                  </td>
                  <td className="py-1.5 px-1.5 text-right tabular-nums text-muted">
                    {fmtUsd(Math.abs(p.market_value), 0)}
                  </td>
                  <td className="py-1.5 px-1.5 text-right tabular-nums">
                    {p.holding_days != null ? (
                      <span>
                        {p.holding_days}
                        <span className="text-muted/50">d</span>
                      </span>
                    ) : (
                      <span className="text-muted/40">—</span>
                    )}
                  </td>
                  <td className="py-1.5 px-1.5 text-right whitespace-nowrap">
                    <span
                      className={`inline-block px-1.5 py-0.5 rounded text-[10px] font-medium ${badge.cls}`}
                      title={badge.sub ?? ''}
                    >
                      {badge.text}
                    </span>
                  </td>
                </tr>
              );
            })}
            {rows.length === 0 ? (
              <tr>
                <td colSpan={8} className="py-6 text-center text-muted text-xs">
                  条件に一致するポジションがありません。
                </td>
              </tr>
            ) : null}
          </tbody>
        </table>
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// 当日損益 — 定義は 1 つだけ:
//   総額 = 現在 equity − 前セッション終値 equity (どちらも intraday 系列 = 同一基準)
//   総額 = 実現 (決済で確定) + 含みの当日変動
// 基準が取れない時は「未計測」と出す。数字は出さない。注釈で誤魔化さない。
// --------------------------------------------------------------------------
function TodayPnl({ snap }: { snap: AlpacaSnapshot }) {
  const p = snap.pnl_today ?? null;

  if (!p || !p.measured || p.total_pl == null) {
    const reason =
      p?.reason ??
      'この snapshot は当日損益の基準情報を持ちません（旧 exporter が生成）。';
    return (
      <div className="flex flex-col gap-1">
        <div className="flex items-center gap-2 text-sm">
          <span className="px-1.5 py-0.5 rounded bg-white/[0.06] text-muted font-medium">
            当日損益 — 未計測
          </span>
          <span className="text-muted text-[11px]">
            同一基準の前セッション終値が取れないため数字を出しません
          </span>
        </div>
        <div className="text-[11px] text-muted/80 leading-relaxed">{reason}</div>
      </div>
    );
  }

  const hasSplit = p.realized_pl != null && p.unrealized_delta != null;
  return (
    <div className="flex flex-col gap-1">
      <div className="flex flex-wrap items-center gap-2 text-sm tabular-nums">
        <span
          className={`px-1.5 py-0.5 rounded font-medium ${
            p.total_pl >= 0 ? 'bg-ok/15 text-ok' : 'bg-fail/15 text-fail'
          }`}
          title={`${p.session_date} セッション = 現在 equity ${fmtUsd(
            p.equity_now,
            0,
          )} − 前セッション(${p.baseline_session}) 終値 ${fmtUsd(
            p.baseline_equity,
            0,
          )}。どちらも intraday 系列 (同一基準)。`}
        >
          {fmtSignedUsd(p.total_pl)} ({fmtPct(p.total_pl_pct)})
        </span>
        <span className="text-muted">
          今日{' '}
          <span className="text-muted/60 text-[11px]">
            {p.session_date} · vs {p.baseline_session} 終値 {fmtUsd(p.baseline_equity, 0)}
          </span>
        </span>
      </div>
      {hasSplit ? (
        <div className="flex flex-wrap gap-x-3 gap-y-0.5 text-[11px] tabular-nums text-muted">
          <span>
            実現{' '}
            <span className={pnlText(p.realized_pl)}>{fmtSignedUsd(p.realized_pl)}</span>
          </span>
          <span className="text-muted/40">+</span>
          <span>
            含みの当日変動{' '}
            <span className={pnlText(p.unrealized_delta)}>
              {fmtSignedUsd(p.unrealized_delta)}
            </span>
          </span>
        </div>
      ) : (
        <div className="text-[11px] text-muted/80">
          実現／含みの内訳は未計測（exit 台帳がこのセッションに届いていません）。
        </div>
      )}
    </div>
  );
}

// live equity と broker 日次系列の水準差を事実で説明する 1 行。
function EquityBasisNote({ snap }: { snap: AlpacaSnapshot }) {
  const b = snap.equity_basis ?? null;
  if (!b || b.n_frozen === 0 || !b.frozen_market_value) return null;
  return (
    <div className="mt-2 text-[11px] leading-relaxed text-muted">
      equity {fmtUsd(snap.account.equity, 0)} のうち{' '}
      <span className="text-cardfg tabular-nums">{fmtUsd(b.frozen_market_value, 2)}</span>{' '}
      は上場廃止（API から決済不能）の {b.frozen_symbols.join(' / ')}。broker の日次
      エクイティ系列と <code className="text-cardfg">last_equity</code>（
      {fmtUsd(b.last_daily_equity, 0)} @ {b.last_daily_session ?? '—'}）はこの分を計上
      しないため、live equity とは水準が {fmtUsd(b.daily_series_gap, 0)} ずれます（うち
      説明できない残差 {fmtUsd(b.residual_usd, 0)}）。当日損益はこのずれを避けるため
      intraday 系列同士でのみ計算しています。
    </div>
  );
}

// --------------------------------------------------------------------------
// 実現損益 (決済済み) — 含み損益とは別セクションに分ける
// --------------------------------------------------------------------------
const EXIT_REASON_LABEL: Record<string, string> = {
  time_based: '期間満了',
  protect_stop: 'ストップ',
  protect_target: '利確',
  protect_trailing: 'トレイリング',
  flatten_all: '全手仕舞い',
};
const exitReasonLabel = (r: string | null) =>
  r == null ? '記録なし' : (EXIT_REASON_LABEL[r] ?? r);

function MeasurementBanner({ realized }: { realized: RealizedBlock }) {
  const m = realized.measurement;
  const recon = realized.exit_intent_reconciliation ?? null;
  const pending = recon?.intended_pending ?? [];
  const missed = recon?.intended_not_filled ?? [];
  const complete = realized.complete === true;

  if (complete && pending.length === 0) {
    return (
      <div className="mb-3 rounded-lg border border-ok/20 bg-ok/[0.05] px-3 py-2 text-[11px] text-muted">
        <span className="text-ok font-medium">計測済み</span> · 約定{' '}
        {m?.fills_seen ?? '—'} 件から {realized.n_closed_trades_total ?? 0} 本の決済を
        復元し、broker のポジションと一致。取りこぼしゼロ。
      </div>
    );
  }

  return (
    <div className="mb-3 rounded-lg border border-warn/25 bg-warn/[0.06] px-3 py-2 text-[11px] leading-relaxed text-muted space-y-1">
      <div className="text-warn font-medium">
        ⚠ 一部が未計測です（下の実現損益はこの分を含みません）
      </div>
      {(m?.reasons ?? []).map((r) => (
        <div key={r}>· {r}</div>
      ))}
      {missed.length > 0 ? (
        <div>
          · exit する予定だったのに約定していない:{' '}
          <span className="text-cardfg">
            {missed.map((x) => `${x.symbol}(${exitReasonLabel(x.reason)})`).join(', ')}
          </span>
        </div>
      ) : null}
      {pending.length > 0 ? (
        <div className="text-muted/80">
          · 執行待ち（次の立会でこれから）: {pending.length} 件 —{' '}
          {pending.map((x) => x.symbol).join(', ')}
        </div>
      ) : null}
      {(m?.discrepancies ?? []).length > 0 ? (
        <div className="pt-1">
          <div className="text-muted/80">建玉が broker と一致しない銘柄:</div>
          <div className="overflow-x-auto">
            <table className="mt-1 text-[10px] tabular-nums">
              <thead className="text-muted/60">
                <tr>
                  <th className="pr-3 text-left">銘柄</th>
                  <th className="pr-3 text-right">約定から復元</th>
                  <th className="pr-3 text-right">broker</th>
                  <th className="text-left">推定原因</th>
                </tr>
              </thead>
              <tbody>
                {(m?.discrepancies ?? []).map((d) => (
                  <tr key={d.symbol}>
                    <td className="pr-3 text-cardfg">{d.symbol}</td>
                    <td className="pr-3 text-right">{fmtQty(d.reconstructed_qty)}</td>
                    <td className="pr-3 text-right">{fmtQty(d.broker_qty)}</td>
                    <td className="text-muted/70">{d.reason.split(':')[0]}</td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </div>
      ) : null}
    </div>
  );
}

// 日次実現損益 (バー) + 累計 (ライン)。データが無い期間は描かない。
function RealizedByDayChart({ rows }: { rows: RealizedDay[] }) {
  if (rows.length < 1) {
    return (
      <div className="text-xs text-muted py-6 text-center">
        決済のあった日がまだありません（データ無し）。
      </div>
    );
  }
  const W = 720;
  const H = 150;
  const padL = 46;
  const padR = 46;
  const padY = 12;
  const iw = W - padL - padR;
  const ih = H - padY * 2;

  const bars = rows.map((r) => r.realized_pl);
  const maxAbs = Math.max(...bars.map((v) => Math.abs(v)), 1);
  const cums = rows.map((r) => r.realized_pl_cum);
  const cMin = Math.min(...cums, 0);
  const cMax = Math.max(...cums, 0);
  const cSpan = cMax - cMin || 1;

  const n = rows.length;
  const slot = iw / Math.max(n, 1);
  const barW = Math.max(1.5, Math.min(14, slot * 0.6));
  const zeroY = padY + ih / 2;
  const barY = (v: number) => (v >= 0 ? zeroY - (v / maxAbs) * (ih / 2) : zeroY);
  const barH = (v: number) => (Math.abs(v) / maxAbs) * (ih / 2);
  const cx = (i: number) => padL + slot * (i + 0.5);
  const cy = (v: number) => padY + ih - ((v - cMin) / cSpan) * ih;
  const cumPath = rows.map((r, i) => `${i === 0 ? 'M' : 'L'}${cx(i)},${cy(r.realized_pl_cum)}`).join(' ');

  return (
    <div className="overflow-x-auto">
      <svg viewBox={`0 0 ${W} ${H}`} className="w-full min-w-[520px]" role="img">
        <line x1={padL} y1={zeroY} x2={W - padR} y2={zeroY} stroke="#ffffff20" strokeWidth={1} />
        {rows.map((r, i) => (
          <rect
            key={r.t}
            x={cx(i) - barW / 2}
            y={barY(r.realized_pl)}
            width={barW}
            height={Math.max(barH(r.realized_pl), 0.8)}
            fill={r.realized_pl >= 0 ? '#34d399' : '#f87171'}
            opacity={0.75}
          >
            <title>{`${r.t}  実現 ${r.realized_pl >= 0 ? '+' : '−'}$${Math.abs(r.realized_pl).toLocaleString('en-US', { maximumFractionDigits: 2 })}  / 累計 $${r.realized_pl_cum.toLocaleString('en-US', { maximumFractionDigits: 2 })}`}</title>
          </rect>
        ))}
        <path d={cumPath} fill="none" stroke="#38bdf8" strokeWidth={1.6} />
        <text x={2} y={padY + 8} className="fill-muted" fontSize={9}>
          日次 ±{fmtUsd(maxAbs, 0)}
        </text>
        <text x={W - padR + 4} y={padY + 8} className="fill-muted" fontSize={9}>
          累計 {fmtUsd(cMax, 0)}
        </text>
        <text x={W - padR + 4} y={padY + ih} className="fill-muted" fontSize={9}>
          {fmtUsd(cMin, 0)}
        </text>
        <text x={padL} y={H - 1} className="fill-muted" fontSize={9}>
          {rows[0].t}
        </text>
        <text x={W - padR} y={H - 1} textAnchor="end" className="fill-muted" fontSize={9}>
          {rows[rows.length - 1].t}
        </text>
      </svg>
      <div className="flex gap-3 text-[10px] text-muted mt-1">
        <span>
          <span className="inline-block w-2 h-2 rounded-sm align-middle" style={{ background: '#34d399' }} />{' '}
          日次実現（バー）
        </span>
        <span>
          <span className="inline-block w-3 h-[2px] align-middle" style={{ background: '#38bdf8' }} />{' '}
          累計実現（ライン）
        </span>
      </div>
    </div>
  );
}

function ClosedTradesTable({ trades }: { trades: ClosedTrade[] }) {
  const [limit, setLimit] = useState(40);
  const [reasonFilter, setReasonFilter] = useState<string>('all');

  const reasons = useMemo(() => {
    const set = new Set<string>();
    trades.forEach((t) => set.add(t.exit_reason ?? '__none__'));
    return Array.from(set).sort();
  }, [trades]);

  const filtered = useMemo(() => {
    const rows =
      reasonFilter === 'all'
        ? trades
        : trades.filter((t) => (t.exit_reason ?? '__none__') === reasonFilter);
    // 新しい決済が上
    return [...rows].sort((a, b) => (a.exit_time < b.exit_time ? 1 : -1));
  }, [trades, reasonFilter]);

  if (trades.length === 0) {
    return (
      <div className="text-xs text-muted py-6 text-center">
        決済済みトレードがまだありません（データ無し）。
      </div>
    );
  }

  const shown = filtered.slice(0, limit);
  return (
    <div>
      <div className="mb-2 flex flex-wrap items-center gap-1 text-[11px]">
        <button
          type="button"
          onClick={() => setReasonFilter('all')}
          className={`rounded px-2 py-0.5 transition ${
            reasonFilter === 'all' ? 'bg-white/15 text-cardfg' : 'text-muted hover:bg-white/5'
          }`}
        >
          すべて {trades.length}
        </button>
        {reasons.map((r) => {
          const key = r === '__none__' ? null : r;
          const count = trades.filter((t) => (t.exit_reason ?? '__none__') === r).length;
          return (
            <button
              key={r}
              type="button"
              onClick={() => setReasonFilter(r)}
              className={`rounded px-2 py-0.5 transition ${
                reasonFilter === r ? 'bg-white/15 text-cardfg' : 'text-muted hover:bg-white/5'
              }`}
            >
              {exitReasonLabel(key)} {count}
            </button>
          );
        })}
      </div>

      <div className="overflow-x-auto">
        <table className="w-full text-[11px] tabular-nums">
          <thead className="text-muted text-[10px] uppercase tracking-wide">
            <tr className="border-b border-white/10">
              <th className="py-1 pr-2 text-left">銘柄</th>
              <th className="py-1 pr-2 text-left">system</th>
              <th className="py-1 pr-2 text-left">売買</th>
              <th className="py-1 pr-2 text-right">株数</th>
              <th className="py-1 pr-2 text-left">エントリー</th>
              <th className="py-1 pr-2 text-left">エグジット</th>
              <th className="py-1 pr-2 text-right">保有</th>
              <th className="py-1 pr-2 text-right">実現損益</th>
              <th className="py-1 text-left">理由</th>
            </tr>
          </thead>
          <tbody>
            {shown.map((t, i) => (
              <tr
                key={`${t.symbol}-${t.exit_time}-${i}`}
                className="border-b border-white/5 hover:bg-white/[0.03]"
              >
                <td className="py-1 pr-2 font-medium text-cardfg">{t.symbol}</td>
                <td className="py-1 pr-2">
                  <span
                    className="px-1 rounded text-[10px]"
                    style={{
                      color: sysColor(t.system ?? 'unknown'),
                      background: `${sysColor(t.system ?? 'unknown')}22`,
                    }}
                  >
                    {t.system ? sysShort(t.system) : '—'}
                  </span>
                </td>
                <td className={`py-1 pr-2 ${t.side === 'long' ? 'text-ok/80' : 'text-fail/80'}`}>
                  {t.side === 'long' ? 'L' : 'S'}
                </td>
                <td className="py-1 pr-2 text-right">{fmtQty(t.qty)}</td>
                <td className="py-1 pr-2 text-muted">
                  {(t.entry_session ?? t.entry_time).slice(0, 10)}{' '}
                  <span className="text-cardfg">{fmtPrice(t.entry_price)}</span>
                </td>
                <td className="py-1 pr-2 text-muted">
                  {(t.exit_session ?? t.exit_time).slice(0, 10)}{' '}
                  <span className="text-cardfg">{fmtPrice(t.exit_price)}</span>
                </td>
                <td className="py-1 pr-2 text-right text-muted">{t.holding_days}d</td>
                <td className={`py-1 pr-2 text-right font-medium ${pnlText(t.realized_pl)}`}>
                  {fmtSignedUsd(t.realized_pl)}
                  {t.realized_pl_pct != null ? (
                    <span className="text-[10px] text-muted ml-1">
                      ({fmtPct(t.realized_pl_pct, 1)})
                    </span>
                  ) : null}
                </td>
                <td
                  className={`py-1 ${t.exit_reason ? 'text-muted' : 'text-muted/40 italic'}`}
                >
                  {exitReasonLabel(t.exit_reason)}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>

      {filtered.length > shown.length ? (
        <button
          type="button"
          onClick={() => setLimit((n) => n + 60)}
          className="mt-2 text-[11px] text-muted hover:text-cardfg transition"
        >
          さらに表示（{shown.length} / {filtered.length}）
        </button>
      ) : (
        <div className="mt-2 text-[10px] text-muted/60">
          {filtered.length} 件すべて表示
        </div>
      )}
    </div>
  );
}

function RealizedSection({ snap }: { snap: AlpacaSnapshot }) {
  const realized = snap.realized ?? null;

  if (!realized || !realized.available) {
    return (
      <div className="text-xs text-muted leading-relaxed">
        <span className="px-1.5 py-0.5 rounded bg-white/[0.06] text-muted font-medium">
          未計測
        </span>{' '}
        決済の実績台帳がまだ生成されていません。
        <div className="mt-1 text-[11px] text-muted/80">
          {realized?.reason ??
            'この snapshot は exit 台帳を持ちません（旧 exporter が生成）。'}{' '}
          実現損益を 0 とは表示しません（0 と不明は別物のため）。
        </div>
      </div>
    );
  }

  const all = realized.all_time;
  const byDay = realized.by_day ?? [];
  const bySystem = Object.entries(realized.by_system ?? {});

  return (
    <div className="space-y-4">
      <MeasurementBanner realized={realized} />

      {realized.stale ? (
        <div className="rounded-lg border border-warn/25 bg-warn/[0.06] px-3 py-2 text-[11px] text-muted">
          <span className="text-warn font-medium">⚠ 台帳が古い</span> — {realized.reason}
        </div>
      ) : null}

      <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
        <Kpi
          label="累計実現損益"
          value={fmtSignedUsd(all?.total_realized_pl ?? null)}
          tone={pnlText(all?.total_realized_pl ?? null)}
          sub={`${all?.n_trades ?? 0} 本の決済`}
        />
        <Kpi
          label="勝率 (実現)"
          value={all?.win_rate_pct != null ? `${all.win_rate_pct}%` : '—'}
          sub={all ? `${all.n_wins}勝 / ${all.n_losses}敗` : undefined}
        />
        <Kpi
          label="平均勝ち"
          value={fmtSignedUsd(all?.avg_win ?? null)}
          tone="text-ok"
          sub={all?.best ? `best ${all.best.symbol} ${fmtSignedUsd(all.best.realized_pl)}` : undefined}
        />
        <Kpi
          label="平均負け"
          value={fmtSignedUsd(all?.avg_loss ?? null)}
          tone="text-fail"
          sub={
            all?.worst ? `worst ${all.worst.symbol} ${fmtSignedUsd(all.worst.realized_pl)}` : undefined
          }
        />
      </div>

      <div>
        <h4 className="text-[11px] text-muted mb-1">日次・累計の実現損益</h4>
        <RealizedByDayChart rows={byDay} />
      </div>

      {bySystem.length > 0 ? (
        <div>
          <h4 className="text-[11px] text-muted mb-1">system 別（実現のみ）</h4>
          <div className="overflow-x-auto">
            <table className="w-full text-[11px] tabular-nums">
              <thead className="text-muted text-[10px] uppercase tracking-wide">
                <tr className="border-b border-white/10">
                  <th className="py-1 pr-2 text-left">system</th>
                  <th className="py-1 pr-2 text-right">決済数</th>
                  <th className="py-1 pr-2 text-right">実現損益</th>
                  <th className="py-1 pr-2 text-right">勝率</th>
                  <th className="py-1 text-right">平均勝ち / 負け</th>
                </tr>
              </thead>
              <tbody>
                {bySystem.map(([key, s]) => (
                  <tr key={key} className="border-b border-white/5">
                    <td className="py-1 pr-2">
                      <span
                        className="px-1 rounded text-[10px]"
                        style={{ color: sysColor(key), background: `${sysColor(key)}22` }}
                      >
                        {sysShort(key)}
                      </span>
                    </td>
                    <td className="py-1 pr-2 text-right text-muted">{s.n_trades}</td>
                    <td className={`py-1 pr-2 text-right font-medium ${pnlText(s.total_realized_pl)}`}>
                      {fmtSignedUsd(s.total_realized_pl)}
                    </td>
                    <td className="py-1 pr-2 text-right text-muted">
                      {s.win_rate_pct != null ? `${s.win_rate_pct}%` : '—'}
                    </td>
                    <td className="py-1 text-right text-muted">
                      <span className="text-ok">{fmtSignedUsd(s.avg_win)}</span>
                      {' / '}
                      <span className="text-fail">{fmtSignedUsd(s.avg_loss)}</span>
                    </td>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
          <div className="mt-1 text-[10px] text-muted/60">
            system が特定できない決済は unknown にまとめています（捨てていません）。
          </div>
        </div>
      ) : null}

      <div>
        <h4 className="text-[11px] text-muted mb-1">
          決済済みトレード
          {realized.n_closed_trades_total &&
          realized.n_closed_trades_total > realized.closed_trades.length ? (
            <span className="text-muted/60">
              {' '}
              — 直近 {realized.closed_trades.length} 件（全 {realized.n_closed_trades_total} 本は
              results_csv/exit_ledger_*.json に保存）
            </span>
          ) : null}
        </h4>
        <ClosedTradesTable trades={realized.closed_trades} />
      </div>

      <div className="text-[10px] text-muted/60 leading-relaxed">
        出典: Alpaca の約定履歴（/v2/account/activities/FILL）を FIFO で round-trip 化。
        台帳 {realized.ledger_date} · run {realized.ledger_run_id} · 生成{' '}
        {realized.ledger_generated_at ?? '—'} ·{' '}
        {realized.measurement?.coverage_start?.slice(0, 10) ?? '—'} 〜{' '}
        {realized.measurement?.coverage_end?.slice(0, 10) ?? '—'}
      </div>
    </div>
  );
}

// --------------------------------------------------------------------------
// section root
// --------------------------------------------------------------------------
export function AlpacaSection({ payload }: { payload: AlpacaSnapshot | null }) {
  if (!payload) {
    return (
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h2 className="text-xs uppercase tracking-widest text-muted mb-2">
          Alpaca account
        </h2>
        <div className="text-sm text-muted">
          スナップショットがまだありません。{' '}
          <code className="text-cardfg">python scripts/export_alpaca_snapshot.py</code>{' '}
          を実行してください。
        </div>
      </section>
    );
  }

  const a = payload.account;
  const s = payload.summary;

  return (
    <div className="space-y-4">
      {/* hero: equity + today's P&L */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <div className="flex items-baseline justify-between mb-1">
          <h2 className="text-xs uppercase tracking-widest text-muted">
            Alpaca · paper account
          </h2>
          <span className="flex items-center gap-2 text-[10px] text-muted">
            {a.trading_blocked ? (
              <span className="px-1.5 py-0.5 rounded bg-fail/20 text-fail">取引停止</span>
            ) : (
              <span className="px-1.5 py-0.5 rounded bg-ok/15 text-ok">{a.status || 'ACTIVE'}</span>
            )}
            {a.pattern_day_trader ? (
              <span className="px-1.5 py-0.5 rounded bg-warn/20 text-warn">PDT</span>
            ) : null}
            <span className="tabular-nums">{payload.date}</span>
          </span>
        </div>

        <div className="flex flex-wrap items-baseline gap-x-5 gap-y-1 mb-3">
          <div className="flex items-baseline gap-2">
            <span className="text-4xl sm:text-5xl font-semibold tabular-nums leading-none">
              {fmtUsd(a.equity, 0)}
            </span>
            <span className="text-sm text-muted">equity</span>
          </div>
          <TodayPnl snap={payload} />
        </div>

        <EquityBasisNote snap={payload} />

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2 mt-3">
          <Kpi label="現金" value={fmtUsd(a.cash, 0)} />
          <Kpi label="買付余力" value={fmtUsd(a.buying_power, 0)} />
          <Kpi
            label="ポジション"
            value={String(s.n_positions)}
            sub={`L${s.n_long} / S${s.n_short}`}
          />
          <Kpi
            label="含み損益 (未実現)"
            value={fmtSignedUsd(s.unrealized_pl_total)}
            tone={pnlText(s.unrealized_pl_total)}
            sub={
              s.win_rate_pct != null
                ? `保有 ${s.n_winning}/${s.n_positions} が含み益`
                : undefined
            }
          />
        </div>
        <div className="mt-1 text-[10px] text-muted/60">
          「含み損益」は保有中のポジションの評価損益（未確定）。決済済みの確定損益は
          下の「実現損益」セクションを参照（両者は別物なので合算していません）。
        </div>

        {s.biggest_winner || s.biggest_loser ? (
          <div className="flex flex-wrap gap-x-4 gap-y-1 mt-2 text-[11px] tabular-nums">
            {s.biggest_winner ? (
              <span className="text-muted">
                best{' '}
                <span className="text-ok font-medium">{s.biggest_winner.symbol}</span>{' '}
                {fmtSignedUsd(s.biggest_winner.pl)} ({fmtPct(s.biggest_winner.pl_pct, 1)})
              </span>
            ) : null}
            {s.biggest_loser ? (
              <span className="text-muted">
                worst{' '}
                <span className="text-fail font-medium">{s.biggest_loser.symbol}</span>{' '}
                {fmtSignedUsd(s.biggest_loser.pl)} ({fmtPct(s.biggest_loser.pl_pct, 1)})
              </span>
            ) : null}
            {s.exit_soon_count > 0 ? (
              <span className="text-warn">exit間近 {s.exit_soon_count}件</span>
            ) : null}
          </div>
        ) : null}
      </section>

      {/* equity curve (期間切替) */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h3 className="text-xs uppercase tracking-widest text-muted mb-2">エクイティ</h3>
        <EquityPanel snap={payload} />
      </section>

      {/* 実現損益 (決済済み) */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h3 className="text-xs uppercase tracking-widest text-muted mb-3">
          実現損益 · exit 履歴
        </h3>
        <RealizedSection snap={payload} />
      </section>

      {/* exposure */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h3 className="text-xs uppercase tracking-widest text-muted mb-3">
          エクスポージャ
        </h3>
        <ExposureBlock snap={payload} />
      </section>

      {/* signals → held bridge */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h3 className="text-xs uppercase tracking-widest text-muted mb-3">
          配信 → 口座 (recon)
        </h3>
        <ReconStrip snap={payload} />
      </section>

      {/* positions */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <h3 className="text-xs uppercase tracking-widest text-muted mb-3">
          保有ポジション
        </h3>
        <PositionsTable positions={payload.positions} />
      </section>

      <footer className="text-[10px] text-muted leading-relaxed">
        alpaca_snapshot_YYYYMMDD.json ({payload.schema}) · {payload.provider} ·
        read-only / paper · generated {payload.generated_at}
      </footer>
    </div>
  );
}

export default AlpacaSection;
