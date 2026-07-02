import fs from 'node:fs';
import path from 'node:path';
import type { SignalsPayload } from './types';

const REPO_ROOT = path.resolve(process.cwd(), '..', '..', '..');

function tryDirs(): string[] {
  const cands = [
    // committed snapshot published by scripts/publish_data_to_vercel.ps1 —
    // the only source that exists in the Vercel build (results_csv is gitignored).
    path.join(process.cwd(), 'data'),
    path.join(REPO_ROOT, 'results_csv'),
    path.join(process.cwd(), 'mock'),
  ];
  return cands.filter((p) => fs.existsSync(p));
}

/**
 * Extract the YYYYMMDD date from a `today_signals_YYYYMMDD.json` filename.
 * Returns null if the pattern doesn't match. Numeric parse allows chronological
 * (not lexical) ordering — critical when a stub for a future date lingers on
 * disk (see 2026-07-02 incident: a 20260702 stub outranked the 20260701 real
 * data because plain `.sort()` picks lexically-largest, not most-recent).
 */
function extractSignalDate(filename: string): number | null {
  const m = filename.match(/today_signals_(\d{8})\.json$/);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

/**
 * A signals file is "usable" if it's plausibly non-empty. We check two things:
 *   1. file size > MIN_BYTES (500) — an empty/stub JSON like `{"systems":{}}`
 *      compresses to ~50-200 bytes.
 *   2. if the JSON parses cleanly, `portfolio.total_signals > 0` OR any
 *      per-system `signals` array is non-empty.
 * Files that pass (1) but not (2) are still considered — some legitimate real
 * payloads report total_signals=0 on quiet days, and we don't want to skip
 * those. But a stub with only skeleton keys will fail both.
 */
const MIN_USABLE_BYTES = 500;

function isUsableSignalFile(fullPath: string): boolean {
  try {
    const stat = fs.statSync(fullPath);
    if (stat.size < MIN_USABLE_BYTES) {
      // Small files might still be legit on very quiet days; peek at content.
      const raw = fs.readFileSync(fullPath, 'utf-8');
      const j = JSON.parse(raw);
      const total = j?.portfolio?.total_signals;
      if (typeof total === 'number' && total > 0) return true;
      const sys = j?.systems;
      if (sys && typeof sys === 'object') {
        for (const key of Object.keys(sys)) {
          const arr = sys[key]?.signals;
          if (Array.isArray(arr) && arr.length > 0) return true;
        }
      }
      return false;
    }
    return true;
  } catch {
    return false;
  }
}

/**
 * Return today_signals files in the directory, sorted by embedded YYYYMMDD
 * DESCENDING (newest first). Non-matching files are dropped.
 */
function pickSignalFiles(dir: string): string[] {
  const entries = fs
    .readdirSync(dir)
    .map((f) => ({ f, d: extractSignalDate(f) }))
    .filter((e): e is { f: string; d: number } => e.d !== null);
  entries.sort((a, b) => b.d - a.d); // newest first
  return entries.map((e) => e.f);
}

/**
 * Load the latest today_signals_YYYYMMDD.json (schema version 1.0).
 * Prefers results_csv/ (produced by `app_today_signals.py --headless`),
 * falls back to ./mock when no real signals exist yet.
 * Returns null if nothing is available so the UI can render an empty state.
 *
 * Selection: iterates candidate files in date-descending order and picks the
 * first that passes `isUsableSignalFile`. This defends against stale stub
 * files sitting alongside real data (2026-07-02 incident).
 */
export function loadSignals(): SignalsPayload | null {
  const dirs = tryDirs();
  let picked: { dir: string; f: string } | null = null;
  outer: for (const d of dirs) {
    const files = pickSignalFiles(d);
    for (const f of files) {
      const full = path.join(d, f);
      if (isUsableSignalFile(full)) {
        picked = { dir: d, f };
        break outer;
      }
    }
  }
  if (!picked) return null;

  try {
    const raw = fs.readFileSync(path.join(picked.dir, picked.f), 'utf-8');
    return JSON.parse(raw) as SignalsPayload;
  } catch {
    return null;
  }
}
