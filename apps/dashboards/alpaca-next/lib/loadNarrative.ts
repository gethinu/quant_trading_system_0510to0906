import fs from 'node:fs';
import path from 'node:path';
import type { Narrative } from './types';

const REPO_ROOT = path.resolve(process.cwd(), '..', '..', '..');

function tryDirs(): string[] {
  const cands = [
    path.join(REPO_ROOT, 'results_csv'),
    path.join(process.cwd(), 'mock'),
  ];
  return cands.filter((p) => fs.existsSync(p));
}

function pickNarrativeFiles(dir: string): string[] {
  return fs
    .readdirSync(dir)
    .filter((f) => /narrative_\d{8}\.json$/.test(f))
    .sort();
}

/**
 * Load the latest narrative_YYYYMMDD.json (AI narrator 出力)。
 * Prefers results_csv/ (produced by scripts/generate_narrative.py),
 * falls back to ./mock. Returns null when no narrative exists so the
 * NarrativeCard stays hidden (narrator は optional な layer)。
 */
export function loadNarrative(): Narrative | null {
  const dirs = tryDirs();
  let picked: { dir: string; f: string } | null = null;
  for (const d of dirs) {
    const files = pickNarrativeFiles(d);
    if (files.length > 0) {
      picked = { dir: d, f: files[files.length - 1] };
      break;
    }
  }
  if (!picked) return null;

  try {
    const raw = fs.readFileSync(path.join(picked.dir, picked.f), 'utf-8');
    const n = JSON.parse(raw) as Narrative;
    // headline も summary も無い空 narrative は非表示扱い
    if (!n.headline && !n.summary) return null;
    return n;
  } catch {
    return null;
  }
}
