import fs from 'node:fs';
import path from 'node:path';
import type { CoveragePayload, CoverageDay } from './types';

const REPO_ROOT = path.resolve(process.cwd(), '..', '..', '..');

function tryDirs(): string[] {
  const cands = [
    path.join(REPO_ROOT, 'results_csv'),
    path.join(process.cwd(), 'mock'),
  ];
  return cands.filter((p) => fs.existsSync(p));
}

function pickJsonFiles(dir: string): string[] {
  return fs
    .readdirSync(dir)
    .filter((f) => /polygon_daily_coverage_\d{8}\.json$/.test(f))
    .sort();
}

/**
 * Load the last 7 days of polygon_daily_coverage_*.json.
 * Falls back to ./mock if results_csv is empty / missing.
 */
export function loadCoverage(): CoveragePayload {
  const dirs = tryDirs();
  let files: { dir: string; f: string }[] = [];
  for (const d of dirs) {
    const fs2 = pickJsonFiles(d);
    if (fs2.length > 0) {
      files = fs2.map((f) => ({ dir: d, f }));
      break;
    }
  }

  if (files.length === 0) {
    return { history: [] };
  }

  const last7 = files.slice(-7);
  const history: CoverageDay[] = last7.map(({ dir, f }) => {
    const raw = fs.readFileSync(path.join(dir, f), 'utf-8');
    const j = JSON.parse(raw);
    // Support both shapes: {date, survival_by_system, ...} and nested one
    if (j.history && Array.isArray(j.history) && j.history.length > 0) {
      return j.history[j.history.length - 1] as CoverageDay;
    }
    return j as CoverageDay;
  });

  return { history };
}
