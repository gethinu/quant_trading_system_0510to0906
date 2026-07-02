import fs from 'node:fs';
import path from 'node:path';
import type {
  PipelinePayload,
  SystemPipeline,
  SystemPipelinePhase,
  CoverageDay,
  SystemStat,
} from './types';

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

function pickLatest(dir: string, re: RegExp): string | null {
  const files = fs
    .readdirSync(dir)
    .filter((f) => re.test(f))
    .sort();
  return files.length > 0 ? files[files.length - 1] : null;
}

/**
 * Convert a legacy polygon_daily_coverage_*.json day into the new pipeline
 * shape so the dashboard can render one consistent layout during the Pack4
 * transition window (生成側が両 schema を書き出す期間)。
 * Only universe → gate → final can be reconstructed from the legacy schema.
 */
function legacyToPipeline(day: CoverageDay): PipelinePayload {
  const universe = day.n_candidates_total || 0;
  const systems: Record<string, SystemPipeline> = {};

  for (const [sysId, stat] of Object.entries(day.survival_by_system || {})) {
    const s = stat as SystemStat;
    const gateCount = s.count ?? null;
    const phases: SystemPipelinePhase[] = [
      {
        name: 'Tgt',
        label: 'Tgt',
        condition: 'ユニバース対象銘柄数',
        count: universe,
        measured: true,
        ratio_of_prev: null,
        ratio_of_universe: universe ? 1 : null,
      },
      {
        name: 'FILpass',
        label: 'FILpass',
        condition: 'price + DollarVolume (legacy coverage gate)',
        count: gateCount,
        measured: gateCount != null,
        ratio_of_prev: gateCount != null && universe ? gateCount / universe : null,
        ratio_of_universe:
          gateCount != null && universe ? gateCount / universe : null,
      },
    ];
    systems[sysId] = { system_id: sysId, phases, final_signals: null };
  }

  return {
    date: day.date,
    provider: 'polygon_grouped_daily',
    schema: 'legacy_coverage',
    systems,
    notes: [
      '旧 coverage schema からの fallback 表示 (Tgt → FILpass のみ復元可)。',
      'phases は絞込透明性のための参考数値 (evaluation ではない)。',
    ],
    from_legacy: true,
  };
}

function readLegacyDay(dir: string, file: string): CoverageDay {
  const raw = fs.readFileSync(path.join(dir, file), 'utf-8');
  const j = JSON.parse(raw);
  if (j.history && Array.isArray(j.history) && j.history.length > 0) {
    return j.history[j.history.length - 1] as CoverageDay;
  }
  return j as CoverageDay;
}

/**
 * Load the latest signal-pipeline payload.
 * Prefers the new pipeline_YYYYMMDD.json (schema signal_pipeline/v1); when
 * absent, falls back to the legacy polygon_daily_coverage_*.json so the
 * transition is smooth. Returns null only when neither schema is available.
 */
export function loadPipeline(): PipelinePayload | null {
  const dirs = tryDirs();

  // 1) new schema preferred
  for (const d of dirs) {
    const f = pickLatest(d, /pipeline_\d{8}\.json$/);
    if (f) {
      try {
        const raw = fs.readFileSync(path.join(d, f), 'utf-8');
        return JSON.parse(raw) as PipelinePayload;
      } catch {
        // fall through to legacy
      }
    }
  }

  // 2) legacy fallback
  for (const d of dirs) {
    const f = pickLatest(d, /polygon_daily_coverage_\d{8}\.json$/);
    if (f) {
      try {
        return legacyToPipeline(readLegacyDay(d, f));
      } catch {
        // fall through
      }
    }
  }

  return null;
}
