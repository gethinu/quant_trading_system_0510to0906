import fs from 'node:fs';
import path from 'node:path';
import type { AlpacaSnapshot } from './types';

const REPO_ROOT = path.resolve(process.cwd(), '..', '..', '..');

/**
 * Candidate directories, most-authoritative first:
 *   1. data/    — committed snapshot published by publish_data_to_vercel.ps1.
 *                 The ONLY source present in the Vercel build (results_csv is
 *                 gitignored).
 *   2. results_csv/ — local dev / build machine (export_alpaca_snapshot.py 出力).
 *   3. mock/    — placeholder so the tab renders before first real publish.
 */
function tryDirs(): string[] {
  const cands = [
    path.join(process.cwd(), 'data'),
    path.join(REPO_ROOT, 'results_csv'),
    path.join(process.cwd(), 'mock'),
  ];
  return cands.filter((p) => fs.existsSync(p));
}

/** Extract YYYYMMDD from alpaca_snapshot_YYYYMMDD.json (numeric, not lexical). */
function extractDate(filename: string): number | null {
  const m = filename.match(/alpaca_snapshot_(\d{8})\.json$/);
  if (!m) return null;
  const n = Number(m[1]);
  return Number.isFinite(n) ? n : null;
}

/**
 * A snapshot is "usable" if it parses and reports an account.equity number.
 * Guards against a truncated/empty stub lingering next to real data.
 */
function isUsable(fullPath: string): boolean {
  try {
    const raw = fs.readFileSync(fullPath, 'utf-8');
    const j = JSON.parse(raw);
    return typeof j?.account?.equity === 'number';
  } catch {
    return false;
  }
}

/**
 * Load the latest alpaca_snapshot_YYYYMMDD.json across candidate dirs.
 * Iterates date-descending and picks the first usable file. Returns null so the
 * UI can render an empty state when no snapshot exists yet.
 */
export function loadAlpaca(): AlpacaSnapshot | null {
  for (const dir of tryDirs()) {
    const files = fs
      .readdirSync(dir)
      .map((f) => ({ f, d: extractDate(f) }))
      .filter((e): e is { f: string; d: number } => e.d !== null)
      .sort((a, b) => b.d - a.d);
    for (const { f } of files) {
      const full = path.join(dir, f);
      if (isUsable(full)) {
        try {
          return JSON.parse(fs.readFileSync(full, 'utf-8')) as AlpacaSnapshot;
        } catch {
          // try next candidate
        }
      }
    }
  }
  return null;
}
