import fs from 'node:fs';
import path from 'node:path';
import type { OrdersPreview } from './types';

const REPO_ROOT = path.resolve(process.cwd(), '..', '..', '..');

// account_equity scale ごとの dashboard タブ (small/medium/large)。
export const SCALES = [
  { key: 'small', equity: 1000, label: '$1k' },
  { key: 'medium', equity: 10000, label: '$10k' },
  { key: 'large', equity: 100000, label: '$100k' },
] as const;

function tryDirs(): string[] {
  const cands = [
    path.join(REPO_ROOT, 'results_csv'),
    path.join(process.cwd(), 'mock'),
  ];
  return cands.filter((p) => fs.existsSync(p));
}

/** results_csv (実データ) → mock の順で最新日の preview を探す。 */
function pickForEquity(dir: string, equity: number): OrdersPreview | null {
  const re = new RegExp(`^orders_preview_(\\d{8})_${equity}\\.json$`);
  const files = fs
    .readdirSync(dir)
    .filter((f) => re.test(f))
    .sort();
  if (files.length === 0) return null;
  try {
    const raw = fs.readFileSync(path.join(dir, files[files.length - 1]), 'utf-8');
    return JSON.parse(raw) as OrdersPreview;
  } catch {
    return null;
  }
}

/**
 * Load orders preview JSON for each scale (small/medium/large).
 * Prefers results_csv/ (produced by `paper_trading_dryrun.py`),
 * falls back to ./mock. Returns a scale->preview map (missing => null).
 * These are DRY-RUN previews only — never submitted automatically.
 */
export function loadOrdersPreview(): Record<string, OrdersPreview | null> {
  const dirs = tryDirs();
  const out: Record<string, OrdersPreview | null> = {};
  for (const s of SCALES) {
    let found: OrdersPreview | null = null;
    for (const d of dirs) {
      found = pickForEquity(d, s.equity);
      if (found) break;
    }
    out[s.key] = found;
  }
  return out;
}
