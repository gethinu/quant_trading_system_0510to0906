import { createHash } from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import { loadAlpaca } from './loadAlpaca';
import { loadNarrative } from './loadNarrative';
import { loadPipeline } from './loadPipeline';
import { loadSignals } from './loadSignals';
import type {
  AlpacaSnapshot,
  DashboardBundleManifest,
  Narrative,
  NotifyDelivery,
  PipelinePayload,
  SignalsPayload,
} from './types';

const REPO_ROOT = path.resolve(process.cwd(), '..', '..', '..');

export interface DashboardBundleLoad {
  signals: SignalsPayload | null;
  pipeline: PipelinePayload | null;
  narrative: Narrative | null;
  notifyDelivery: NotifyDelivery | null;
  alpaca: AlpacaSnapshot | null;
  manifest: DashboardBundleManifest | null;
  issues: string[];
}

export function tryDirs(): string[] {
  return [
    path.join(process.cwd(), 'data'),
    path.join(REPO_ROOT, 'results_csv'),
    path.join(process.cwd(), 'mock'),
  ].filter((candidate) => fs.existsSync(candidate));
}

function latestManifest(dir: string): string | null {
  const files = fs
    .readdirSync(dir)
    .filter((file) => /dashboard_bundle_\d{8}\.json$/.test(file))
    .sort();
  return files.at(-1) ?? null;
}

/**
 * Hash with CRLF normalized to LF, matching prepare_dashboard_bundle.py.
 * The producer writes these files on Windows (CRLF in its working tree) while
 * git stores and checks out LF, so a raw-byte digest never matches here and
 * would fail-close the page on a valid publish.
 */
const CR = 0x0d;
const LF = 0x0a;

export function sha256(file: string): string {
  const buf = fs.readFileSync(file);
  // Drop each CR that immediately precedes an LF, without building strings
  // (avoids any encoding round-trip on binary-ish content).
  const out = Buffer.allocUnsafe(buf.length);
  let n = 0;
  for (let i = 0; i < buf.length; i += 1) {
    if (buf[i] === CR && i + 1 < buf.length && buf[i + 1] === LF) continue;
    out[n] = buf[i];
    n += 1;
  }
  return createHash('sha256').update(out.subarray(0, n)).digest('hex');
}

function safeArtifactPath(dir: string, name: string): string | null {
  if (!name || path.basename(name) !== name) return null;
  const resolved = path.resolve(dir, name);
  return path.dirname(resolved) === path.resolve(dir) ? resolved : null;
}

function readManifest(file: string): DashboardBundleManifest | null {
  try {
    const parsed = JSON.parse(fs.readFileSync(file, 'utf-8')) as unknown;
    if (
      !parsed ||
      typeof parsed !== 'object' ||
      (parsed as DashboardBundleManifest).schema !== 'dashboard_bundle/v1'
    ) {
      return null;
    }
    return parsed as DashboardBundleManifest;
  } catch {
    return null;
  }
}

/**
 * Load signals and pipeline from one manifest-selected directory and verify
 * their exact hashes and run lineage.  Legacy data remains readable, but is
 * explicitly marked unverified instead of being silently mixed by date.
 */
export function loadDashboardBundle(): DashboardBundleLoad {
  for (const dir of tryDirs()) {
    const manifestName = latestManifest(dir);
    if (!manifestName) continue;
    const manifest = readManifest(path.join(dir, manifestName));
    if (!manifest) {
      return {
        signals: null,
        pipeline: null,
        narrative: null,
        notifyDelivery: null,
        alpaca: null,
        manifest: null,
        issues: ['bundle manifest のJSON/schemaが不正です。表示を停止しました。'],
      };
    }

    const signalSpec = manifest.files?.today_signals;
    const pipelineSpec = manifest.files?.pipeline;
    const signalPath = safeArtifactPath(dir, signalSpec?.name ?? '');
    const pipelinePath = safeArtifactPath(dir, pipelineSpec?.name ?? '');
    const issues: string[] = [...(manifest.warnings ?? [])];
    if (!signalPath || !pipelinePath || !signalSpec || !pipelineSpec) {
      return {
        signals: null,
        pipeline: null,
        narrative: null,
        notifyDelivery: null,
        alpaca: null,
        manifest,
        issues: ['bundle manifest に必須ファイルがありません。表示を停止しました。'],
      };
    }
    if (!fs.existsSync(signalPath) || !fs.existsSync(pipelinePath)) {
      return {
        signals: null,
        pipeline: null,
        narrative: null,
        notifyDelivery: null,
        alpaca: null,
        manifest,
        issues: ['bundle manifest の参照ファイルがありません。表示を停止しました。'],
      };
    }
    if (sha256(signalPath) !== signalSpec.sha256 || sha256(pipelinePath) !== pipelineSpec.sha256) {
      return {
        signals: null,
        pipeline: null,
        narrative: null,
        notifyDelivery: null,
        alpaca: null,
        manifest,
        issues: ['bundle のcontent hashが不一致です。stale/partial publishとして表示を停止しました。'],
      };
    }
    const optionalPaths: Record<string, string> = {};
    for (const key of ['narrative', 'alpaca_snapshot', 'notify_delivery']) {
      const spec = manifest.files?.[key];
      if (!spec) continue;
      const artifact = safeArtifactPath(dir, spec.name);
      if (!artifact || !fs.existsSync(artifact) || sha256(artifact) !== spec.sha256) {
        issues.push(`${key} のcontent hashがbundle manifestと不一致です。`);
      } else {
        optionalPaths[key] = artifact;
      }
    }

    try {
      const signals = JSON.parse(fs.readFileSync(signalPath, 'utf-8')) as SignalsPayload;
      const pipeline = JSON.parse(
        fs.readFileSync(pipelinePath, 'utf-8'),
      ) as PipelinePayload;
      const runId = signals.meta?.run_id;
      if (
        signals.date !== manifest.date ||
        pipeline.date !== manifest.date ||
        runId !== manifest.source_run_id ||
        pipeline.source_signals_run_id !== runId
      ) {
        return {
          signals: null,
          pipeline: null,
          narrative: null,
          notifyDelivery: null,
          alpaca: null,
          manifest,
          issues: ['bundle のdate/run_id lineageが不一致です。表示を停止しました。'],
        };
      }
      if (pipeline.source_signals_sha256 !== signalSpec.sha256) {
        issues.push('pipeline のsource_signals_sha256がmanifestと不一致です。');
      }
      if (manifest.measurement?.funnel_measured < 34) {
        issues.push(
          `funnel coverage不足: ${manifest.measurement.funnel_measured}/${manifest.measurement.funnel_total}`,
        );
      }
      if (manifest.measurement?.exit_measured < 7) {
        issues.push(`Exit coverage不足: ${manifest.measurement.exit_measured}/7`);
      }
      let narrative: Narrative | null = null;
      let alpaca: AlpacaSnapshot | null = null;
      let notifyDelivery: NotifyDelivery | null = null;
      if (optionalPaths.notify_delivery) {
        try {
          const parsed = JSON.parse(
            fs.readFileSync(optionalPaths.notify_delivery, 'utf-8'),
          ) as NotifyDelivery;
          // 別 run の配信状態を今日の表示に混ぜない。
          const sameRun =
            !parsed.source_signals_run_id || parsed.source_signals_run_id === runId;
          if (parsed.date === manifest.date && sameRun) {
            notifyDelivery = parsed;
          } else {
            issues.push('実績通知のdate/runがbundle契約と不一致です。');
          }
        } catch {
          issues.push('実績通知の配信状態をparseできませんでした。');
        }
      }
      if (optionalPaths.narrative) {
        try {
          const parsed = JSON.parse(
            fs.readFileSync(optionalPaths.narrative, 'utf-8'),
          ) as Narrative;
          if (parsed.date === manifest.date && (parsed.headline || parsed.summary)) {
            narrative = parsed;
          } else {
            issues.push('narrative のdate/contentがbundle契約と不一致です。');
          }
        } catch {
          issues.push('narrative をparseできないため、このcardだけ非表示にしました。');
        }
      }
      if (optionalPaths.alpaca_snapshot) {
        try {
          const parsed = JSON.parse(
            fs.readFileSync(optionalPaths.alpaca_snapshot, 'utf-8'),
          ) as AlpacaSnapshot;
          if (parsed.date === manifest.date && typeof parsed.account?.equity === 'number') {
            alpaca = parsed;
          } else {
            issues.push('alpaca snapshot のdate/schemaがbundle契約と不一致です。');
          }
        } catch {
          issues.push('alpaca snapshotをparseできないため、このcardだけ非表示にしました。');
        }
      }
      return { signals, pipeline, narrative, notifyDelivery, alpaca, manifest, issues };
    } catch {
      return {
        signals: null,
        pipeline: null,
        narrative: null,
        notifyDelivery: null,
        alpaca: null,
        manifest,
        issues: ['bundle payloadをparseできません。表示を停止しました。'],
      };
    }
  }

  const signals = loadSignals();
  let pipeline = loadPipeline();
  const issues = ['bundle manifest 未生成の旧データです。date/run/hashは未検証です。'];
  if (signals && pipeline && signals.date !== pipeline.date) {
    issues.push(`signals=${signals.date} / pipeline=${pipeline.date} の日付が不一致です。`);
    pipeline = null;
  } else if (
    signals &&
    pipeline?.source_signals_run_id &&
    pipeline.source_signals_run_id !== signals.meta?.run_id
  ) {
    issues.push('signals / pipeline のrun_idが不一致です。');
    pipeline = null;
  }
  return {
    signals,
    pipeline,
    narrative: loadNarrative(),
    // legacy 経路 (manifest 無し) には実績通知の sidecar が無い。
    // 「未取得」を「未送信」と偽らないため null のままにする。
    notifyDelivery: null,
    alpaca: loadAlpaca(),
    manifest: null,
    issues,
  };
}
