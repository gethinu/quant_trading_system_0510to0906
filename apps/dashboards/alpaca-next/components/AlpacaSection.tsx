'use client';

import { useMemo, useState } from 'react';
import type {
  AlpacaPosition,
  AlpacaSnapshot,
  EquityCurve,
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
function EquityChart({ curve }: { curve: EquityCurve }) {
  const pts = curve.points ?? [];
  const [hoverIdx, setHoverIdx] = useState<number | null>(null);
  if (pts.length < 2) {
    return (
      <div className="text-xs text-muted py-6 text-center">
        equity 履歴が不足しています（{pts.length} point）。
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

  // 末尾 live 点が直近日次終値から大きく段差する場合の検出（日次終値ラグ / repricing）。
  // 段差が通常の日次変動の中央値の 4 倍かつ $500 超なら「当日急騰」ではなく基準ずれと注記。
  const lastReal = pts.length >= 2 ? pts[pts.length - 2] : null;
  const tailStep = last.live && lastReal ? last.equity - lastReal.equity : 0;
  const dailyMoves = pts
    .filter((p) => p.pl != null)
    .map((p) => Math.abs(p.pl as number))
    .sort((m, n) => m - n);
  const medMove = dailyMoves.length
    ? dailyMoves[Math.floor(dailyMoves.length / 2)]
    : 0;
  const tailOutlier = Math.abs(tailStep) > Math.max(500, medMove * 4);

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
          equity · {curve.period}
        </span>
        <span className="flex items-center gap-3 text-[11px] tabular-nums">
          <span className={up ? 'text-ok' : 'text-fail'}>
            期間 {fmtPct(curve.period_return_pct)}
          </span>
          <span className="text-fail">最大DD {fmtPct(curve.max_drawdown_pct)}</span>
        </span>
      </div>
      {tailOutlier ? (
        <div className="mb-1 text-[10px] leading-snug text-warn/90">
          ⚠ 末尾はライブ equity。直近日次終値との段差 {fmtSignedUsd(tailStep)} は
          日次終値ラグ由来（当日の急騰ではありません）。
        </div>
      ) : null}
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
  const up = (a.pnl_today_abs ?? 0) >= 0;

  // freeze-aware baseline の provenance (新 exporter)。pnl_today_abs/pct は補正後の値。
  const basis = a.pnl_today_basis ?? null;
  const isFreezeAdjusted = basis === 'freeze_adjusted';
  const hasProvenance = basis != null;

  // legacy: provenance を持たない旧 snapshot 用の凍結ラグ検出（後方互換の保険）。
  // 保有ポジションの当日値洗い合計 (intraday_pl) とヘッドラインが大きく乖離すれば疑い。
  const heldTodayMove = payload.positions.reduce(
    (acc, p) => acc + (p.intraday_pl ?? 0),
    0,
  );
  const todayAbs = a.pnl_today_abs ?? 0;
  const legacySuspect =
    !hasProvenance &&
    Math.abs(a.pnl_today_pct ?? 0) >= 1.5 &&
    Math.abs(heldTodayMove) < Math.abs(todayAbs) * 0.5 &&
    Math.abs(todayAbs - heldTodayMove) > 500;

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
          <div className="flex items-center gap-2 text-sm tabular-nums">
            <span
              className={`px-1.5 py-0.5 rounded font-medium ${
                up ? 'bg-ok/15 text-ok' : 'bg-fail/15 text-fail'
              }`}
              title={
                isFreezeAdjusted
                  ? `今日 = 現在の equity − 前営業日 intraday equity (${fmtUsd(
                      a.pnl_today_baseline,
                      0,
                    )})。日次終値ラグを補正した基準。`
                  : `今日 = 現在の equity − 前営業日クローズ equity (last_equity ${fmtUsd(
                      a.last_equity,
                      0,
                    )})`
              }
            >
              {fmtSignedUsd(a.pnl_today_abs)} ({fmtPct(a.pnl_today_pct)})
            </span>
            <span className="text-muted">
              今日{' '}
              <span className="text-muted/60 text-[11px]">
                {isFreezeAdjusted
                  ? 'vs 前営業日 equity · freeze-adjusted'
                  : 'vs 前日クローズ'}
              </span>
            </span>
            {isFreezeAdjusted ? (
              <span
                className="text-xs cursor-help"
                style={{ color: '#38bdf8' }}
                title="日次終値ラグを検出し、前営業日の intraday 整合 equity を基準に補正済み。生の基準値は下の注記。"
              >
                ℹ
              </span>
            ) : legacySuspect ? (
              <span
                className="text-warn text-xs cursor-help"
                title="前日クローズ(last_equity)が実質 equity より低く据え置かれた日次終値ラグの疑い。下の注記を参照。"
              >
                ⚠
              </span>
            ) : null}
          </div>
        </div>

        {isFreezeAdjusted ? (
          <div
            className="mb-3 rounded-lg border px-3 py-2 text-[11px] leading-relaxed text-muted"
            style={{ borderColor: '#38bdf840', backgroundColor: '#38bdf80f' }}
          >
            <span className="font-medium" style={{ color: '#7dd3fc' }}>
              ℹ 日次終値ラグを補正しました（freeze-adjusted）。
            </span>{' '}
            生の daily-close 基準（last_equity {fmtUsd(a.last_equity, 0)}）だと{' '}
            <span className="text-cardfg">
              {fmtSignedUsd(a.pnl_today_abs_raw)} ({fmtPct(a.pnl_today_pct_raw)})
            </span>{' '}
            の phantom になりますが、前営業日クローズが実勢より{' '}
            <span className="text-cardfg">{fmtSignedUsd(a.freeze_lag_gap)}</span>{' '}
            低く据え置かれていたため、前営業日の intraday equity（
            {fmtUsd(a.pnl_today_baseline, 0)}）を基準に補正 →{' '}
            <span className={pnlText(a.pnl_today_abs)}>
              {fmtSignedUsd(a.pnl_today_abs)} ({fmtPct(a.pnl_today_pct)})
            </span>{' '}
            が実勢の当日 P&amp;L。equity・保有・unrealized 等の実報告値は不変。
          </div>
        ) : legacySuspect ? (
          <div className="mb-3 rounded-lg border border-warn/25 bg-warn/[0.06] px-3 py-2 text-[11px] leading-relaxed text-muted">
            <span className="text-warn font-medium">
              ⚠ 「今日 {fmtSignedUsd(todayAbs)}」は当日の実損益ではなく基準ずれの可能性。
            </span>{' '}
            保有ポジションの当日値洗い (intraday) は{' '}
            <span className={pnlText(heldTodayMove)}>{fmtSignedUsd(heldTodayMove)}</span>{' '}
            のみ。ヘッドラインとの差{' '}
            <span className="text-cardfg">{fmtSignedUsd(todayAbs - heldTodayMove)}</span>{' '}
            は前日クローズ (last_equity {fmtUsd(a.last_equity, 0)}) を基準にした差で、
            Alpaca の日次終値が実質 equity ({fmtUsd(a.equity, 0)}) より低く据え置かれている場合に生じます
            （データ凍結期の値洗いラグ由来。日次終値が更新されれば解消）。
          </div>
        ) : null}

        <div className="grid grid-cols-2 sm:grid-cols-4 gap-2">
          <Kpi label="現金" value={fmtUsd(a.cash, 0)} />
          <Kpi label="買付余力" value={fmtUsd(a.buying_power, 0)} />
          <Kpi
            label="ポジション"
            value={String(s.n_positions)}
            sub={`L${s.n_long} / S${s.n_short}`}
          />
          <Kpi
            label="含み損益"
            value={fmtSignedUsd(s.unrealized_pl_total)}
            tone={pnlText(s.unrealized_pl_total)}
            sub={
              s.win_rate_pct != null
                ? `勝率 ${s.win_rate_pct}% (${s.n_winning}/${s.n_positions})`
                : undefined
            }
          />
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

      {/* equity curve */}
      <section className="bg-card rounded-xl p-4 shadow-lg">
        <EquityChart curve={payload.equity_curve} />
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
