'use client';

import { useEffect, useState } from 'react';

/**
 * Data-freshness provenance + client-side staleness warning.
 *
 * Why this exists (2026-07-22 incident)
 * -------------------------------------
 * The dashboard is a STATIC export: `loadSignals()` runs at build time, so the
 * deployed site is frozen to whatever `data/` held at the last git push. The daily
 * publish and the ntfy notification live in DIFFERENT processes (publish in the
 * 06:00 wrapper's last step, ntfy in the child pipeline). When the wrapper died
 * mid-run the orphaned child still ntfy'd fresh data while the publish was lost,
 * so ntfy said 07-22 while the site served the 07-21 build with no visible clue.
 *
 * Two defenses here:
 *   1. Always surface the served data's provenance — date, run_id (the SAME id
 *      ntfy prints, so the two can be cross-checked at a glance), and generated_at.
 *   2. Compare, IN THE BROWSER (build-time can't know "now"), the served date to
 *      the freshest weekday we'd expect by now in JST, and show a warning banner
 *      if the site is behind. Deliberately conservative to avoid false positives:
 *      weekend-aware, and it treats "today" as not-yet-expected until the daily
 *      batch has had time to publish (PUBLISH_HOUR_JST).
 */

const PUBLISH_HOUR_JST = 9; // daily batch publishes by ~07:00 JST; 9 adds margin

/** JST calendar date (YYYY-MM-DD) + hour, independent of the viewer's timezone. */
function jstNow(): { date: string; hour: number } {
  const parts = new Intl.DateTimeFormat('en-CA', {
    timeZone: 'Asia/Tokyo',
    year: 'numeric',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    hour12: false,
  }).formatToParts(new Date());
  const get = (t: string) => parts.find((p) => p.type === t)?.value ?? '';
  return { date: `${get('year')}-${get('month')}-${get('day')}`, hour: Number(get('hour')) };
}

/** whole-day index for a YYYY-MM-DD string (UTC midnight), for day arithmetic. */
function dayIndex(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number);
  return Math.round(Date.UTC(y, m - 1, d) / 86_400_000);
}

function isoFromIndex(idx: number): string {
  return new Date(idx * 86_400_000).toISOString().slice(0, 10);
}

/** Step back to the most recent weekday (Mon–Fri) on or before `iso`. */
function toWeekday(iso: string): string {
  let idx = dayIndex(iso);
  // getUTCDay: 0=Sun, 6=Sat
  while (true) {
    const day = new Date(idx * 86_400_000).getUTCDay();
    if (day !== 0 && day !== 6) return isoFromIndex(idx);
    idx -= 1;
  }
}

/**
 * The freshest data date we'd expect to be published by now. If we're before the
 * daily publish hour, today's run may not have landed yet, so step back a day
 * first. Then snap to the most recent weekday (US market / batch cadence).
 */
export function expectedFreshDate(nowDate: string, nowHour: number): string {
  const base = nowHour < PUBLISH_HOUR_JST ? isoFromIndex(dayIndex(nowDate) - 1) : nowDate;
  return toWeekday(base);
}

/** JST-localized, human generated_at (falls back to the raw string). */
function fmtGenerated(generatedAt?: string | null): string | null {
  if (!generatedAt) return null;
  const d = new Date(generatedAt);
  if (Number.isNaN(d.getTime())) return generatedAt;
  return new Intl.DateTimeFormat('ja-JP', {
    timeZone: 'Asia/Tokyo',
    month: '2-digit',
    day: '2-digit',
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  }).format(d);
}

interface Props {
  date: string | null;
  runId?: string | null;
  generatedAt?: string | null;
}

export function FreshnessBanner({ date, runId, generatedAt }: Props) {
  // behind = weekdays the served build lags the expected fresh date (0 = fresh).
  const [behind, setBehind] = useState<{ expected: string; days: number } | null>(null);

  useEffect(() => {
    if (!date) return;
    const { date: nowDate, hour } = jstNow();
    const expected = expectedFreshDate(nowDate, hour);
    const days = dayIndex(expected) - dayIndex(date);
    setBehind(days > 0 ? { expected, days } : null);
  }, [date]);

  if (!date) return null;

  const gen = fmtGenerated(generatedAt);
  const provenance = (
    <div className="text-[10px] text-muted tabular-nums">
      as of <span className="text-cardfg">{date}</span>
      {runId ? (
        <>
          {' · '}run <span className="text-cardfg">{runId}</span>
        </>
      ) : null}
      {gen ? <> · gen {gen} JST</> : null}
    </div>
  );

  if (!behind) {
    return <div className="mb-3">{provenance}</div>;
  }

  return (
    <div className="mb-3">
      <div
        role="alert"
        className="mb-1 rounded-md border border-warn/40 bg-warn/10 px-3 py-2 text-xs text-warn"
      >
        <span className="font-semibold">⚠ データが古い可能性</span>{' '}
        <span className="text-cardfg">
          表示 {date} / 想定 {behind.expected}
          （{behind.days} 営業日遅れ）
        </span>
        。ダッシュの publish が取りこぼされた可能性があります。最新は ntfy を確認してください。
      </div>
      {provenance}
    </div>
  );
}
