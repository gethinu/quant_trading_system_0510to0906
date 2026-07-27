/**
 * データ鮮度の判定ロジック (2026-07-22 の publish 取りこぼし incident 由来)。
 *
 * ダッシュは静的 export なので `loadSignals()` は build 時にしか走らない = 配信中の
 * サイトは最後に push された `data/` で凍結する。publish と ntfy は別プロセスなので、
 * 「ntfy は最新なのにサイトだけ古い」が起こり得る。そこでブラウザ側 (build 時は "今" を
 * 知らない) で「今なら出ているはずの日付」と突き合わせて遅れを検出する。
 *
 * 判定は保守的 (false positive を出さない): 週末を飛ばし、当日分は日次バッチが publish
 * する余裕を見て PUBLISH_HOUR_JST まで「まだ出ていなくて当然」とみなす。
 *
 * ここは純ロジック + hook のみ。表示は FreshnessBanner / StatusSummary が持つ。
 */

'use client';

import { useEffect, useState } from 'react';

const PUBLISH_HOUR_JST = 9; // daily batch publishes by ~07:00 JST; 9 adds margin

/** JST calendar date (YYYY-MM-DD) + hour, independent of the viewer's timezone. */
export function jstNow(): { date: string; hour: number } {
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
export function dayIndex(iso: string): number {
  const [y, m, d] = iso.split('-').map(Number);
  return Math.round(Date.UTC(y, m - 1, d) / 86_400_000);
}

export function isoFromIndex(idx: number): string {
  return new Date(idx * 86_400_000).toISOString().slice(0, 10);
}

/** Step back to the most recent weekday (Mon–Fri) on or before `iso`. */
export function toWeekday(iso: string): string {
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
export function fmtGenerated(generatedAt?: string | null): string | null {
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

export interface Behind {
  expected: string;
  days: number;
}

/**
 * 配信中のデータが何営業日遅れているか。
 *
 * `checked` は「ブラウザ側で実際に突き合わせたか」。静的 HTML の初回 paint では必ず
 * false で、この間に「最新」と断言すると嘘になる (build 時は「今」を知らない) ので、
 * 表示側は checked=false のうちは判定を保留すること。
 */
export function useFreshness(date: string | null): {
  checked: boolean;
  behind: Behind | null;
} {
  const [state, setState] = useState<{ checked: boolean; behind: Behind | null }>({
    checked: false,
    behind: null,
  });

  useEffect(() => {
    if (!date) {
      setState({ checked: false, behind: null });
      return;
    }
    const { date: nowDate, hour } = jstNow();
    const expected = expectedFreshDate(nowDate, hour);
    const days = dayIndex(expected) - dayIndex(date);
    setState({ checked: true, behind: days > 0 ? { expected, days } : null });
  }, [date]);

  return state;
}
