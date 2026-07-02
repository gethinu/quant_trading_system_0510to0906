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

function pickSignalFiles(dir: string): string[] {
  return fs
    .readdirSync(dir)
    .filter((f) => /today_signals_\d{8}\.json$/.test(f))
    .sort();
}

/**
 * Load the latest today_signals_YYYYMMDD.json (schema version 1.0).
 * Prefers results_csv/ (produced by `app_today_signals.py --headless`),
 * falls back to ./mock when no real signals exist yet.
 * Returns null if nothing is available so the UI can render an empty state.
 */
export function loadSignals(): SignalsPayload | null {
  const dirs = tryDirs();
  let picked: { dir: string; f: string } | null = null;
  for (const d of dirs) {
    const files = pickSignalFiles(d);
    if (files.length > 0) {
      picked = { dir: d, f: files[files.length - 1] };
      break;
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
