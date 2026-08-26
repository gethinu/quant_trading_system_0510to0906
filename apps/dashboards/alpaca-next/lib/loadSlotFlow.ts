import fs from 'node:fs';
import path from 'node:path';
import { sha256, tryDirs } from './loadDashboardBundle';
import type {
  DashboardBundleManifest,
  ExitOrdersPayload,
  PaperOrdersPayload,
} from './types';

/**
 * 枠 / フロービューが読む sidecar artifact のローダー。
 *
 * 契約:
 *   - **日付が完全一致するファイルしか読まない**。「昨日の paper_orders と今日の
 *     caps」のような無言の混在は、まさにこのダッシュボードが潰したい誤読なので、
 *     日付が違うファイルは存在しないものとして扱う。
 *   - **manifest に載っている sidecar は content hash を検証する**。載っていない日
 *     (producer が未更新の環境) は date 一致だけで採用し、UI 側で「未検証」と言う。
 *   - 無ければ null を返すだけ。bundle の fail-closed 判定には一切影響しない
 *     (これらは補助 artifact で、欠けても signals/pipeline の表示は止めない)。
 *   - 探索先は loadDashboardBundle と同じ 3 か所。data/ が本番 (Vercel が build 時に
 *     読むのはここだけ)、results_csv/ はローカル開発時の生成元。
 */
function readDated<T extends { date?: string }>(
  name: string,
  date: string,
  expectedSha: string | null,
): { payload: T; verified: boolean } | null {
  for (const dir of tryDirs()) {
    const file = path.join(dir, name);
    if (!fs.existsSync(file)) continue;
    let verified = false;
    if (expectedSha) {
      // manifest が hash を持っているなら一致を要求する。違えば stale/partial
      // publish なので、この dir のファイルは無かったことにして次を探す。
      if (sha256(file) !== expectedSha) continue;
      verified = true;
    }
    try {
      const parsed = JSON.parse(fs.readFileSync(file, 'utf-8')) as T;
      if (parsed?.date !== date) continue;
      return { payload: parsed, verified };
    } catch {
      continue;
    }
  }
  return null;
}

export interface SlotFlowArtifacts {
  paperOrders: PaperOrdersPayload | null;
  exitProposal: ExitOrdersPayload | null;
  exitExecution: ExitOrdersPayload | null;
  /** 見つからなかったファイル名 (UI が「未 publish」と正直に言うため)。 */
  missing: string[];
  /** 1 つでも manifest の hash で検証できたか。 */
  verified: boolean;
}

export function loadSlotFlowArtifacts(
  date: string | null,
  manifest: DashboardBundleManifest | null,
): SlotFlowArtifacts {
  if (!date || !/^\d{4}-\d{2}-\d{2}$/.test(date)) {
    return {
      paperOrders: null,
      exitProposal: null,
      exitExecution: null,
      missing: [],
      verified: false,
    };
  }
  const compact = date.replace(/-/g, '');
  const paperName = `paper_orders_${compact}.json`;
  const proposalName = `exit_orders_${compact}_proposal.json`;
  const executionName = `exit_orders_${compact}_execution.json`;
  // manifest の date が今日と違う日は、その hash を当てにしない (別日の契約)。
  const specs = manifest?.date === date ? (manifest.files ?? {}) : {};
  const shaOf = (key: string, name: string): string | null =>
    specs[key]?.name === name ? specs[key].sha256 : null;

  const paper = readDated<PaperOrdersPayload>(
    paperName,
    date,
    shaOf('paper_orders', paperName),
  );
  const proposal = readDated<ExitOrdersPayload>(
    proposalName,
    date,
    shaOf('exit_orders_proposal', proposalName),
  );
  let execution = readDated<ExitOrdersPayload>(
    executionName,
    date,
    shaOf('exit_orders_execution', executionName),
  );
  if (!execution) {
    // _execution が無い環境でも、role=execution の plain 版が残っていることがある。
    const plain = readDated<ExitOrdersPayload>(`exit_orders_${compact}.json`, date, null);
    if (plain?.payload.role === 'execution') execution = plain;
  }

  const missing: string[] = [];
  if (!paper) missing.push(paperName);
  if (!proposal && !execution) missing.push(proposalName);

  return {
    paperOrders: paper?.payload ?? null,
    exitProposal: proposal?.payload ?? null,
    exitExecution: execution?.payload ?? null,
    missing,
    verified: Boolean(paper?.verified || proposal?.verified || execution?.verified),
  };
}
