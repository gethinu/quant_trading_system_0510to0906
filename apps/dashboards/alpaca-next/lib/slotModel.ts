import type {
  AlpacaSnapshot,
  ExitOrdersPayload,
  PaperOrdersPayload,
  SignalsCaps,
  SignalsPayload,
} from './types';

/**
 * 枠 (スロット) モデル — 「なぜ Entry が 0 なのか」を推測なしで読ませるための
 * 表示モデル。docs/SLOT_MODEL_AND_DASHBOARD_REDESIGN_20260826.md §1 の抽出結果。
 *
 * ここは **表示用の定数** であって配分エンジンの入力ではない。実行時の正は
 *   config/config.yaml risk.max_positions / risk.portfolio
 *   config/settings.py  ui.long_allocations · short_allocations
 *   core/final_allocation.py _resolve_max_positions / _load_portfolio_caps /
 *                            _apply_portfolio_caps / _sort_final_frame
 * にある。定数が実行時とズレたら黙って嘘をつかないよう、artifact 側の
 * portfolio.caps と突き合わせる自己検査 (buildSelfChecks) を必ず併記する。
 *
 * 優先度 = side 昇順 → system 番号昇順 ("long" < "short")。この並びのまま
 * _apply_portfolio_caps() が **末尾から捨てる** ので、S5 と S7 が構造的に
 * 最初の犠牲になる。
 */
export interface SystemSlotSpec {
  id: string;
  /** today_signals / pipeline 側のキー (sys1..sys7)。 */
  key: string;
  short: string;
  side: 'long' | 'short';
  slots: number;
  priority: number;
  weight: number;
  desc: string;
  order: string;
}

export const SYSTEM_SLOT_MODEL: SystemSlotSpec[] = [
  { id: 'system1', key: 'sys1', short: 'S1', side: 'long', slots: 10, priority: 1, weight: 0.25, desc: 'ロング・トレンド', order: 'market' },
  { id: 'system3', key: 'sys3', short: 'S3', side: 'long', slots: 10, priority: 2, weight: 0.25, desc: 'ロング・押し目買い', order: 'limit 前日終値 −7%' },
  { id: 'system4', key: 'sys4', short: 'S4', side: 'long', slots: 10, priority: 3, weight: 0.25, desc: 'ロング・低ボラ', order: 'market' },
  { id: 'system5', key: 'sys5', short: 'S5', side: 'long', slots: 10, priority: 4, weight: 0.25, desc: 'ロング・ADX リバーサル', order: 'limit 前日終値 −3%' },
  { id: 'system2', key: 'sys2', short: 'S2', side: 'short', slots: 10, priority: 5, weight: 0.40, desc: 'ショート RSI スラスト', order: 'limit 前日終値 +4%' },
  { id: 'system6', key: 'sys6', short: 'S6', side: 'short', slots: 10, priority: 6, weight: 0.40, desc: 'ショート・ミーンリバージョン', order: 'limit 前日終値 +5%' },
  { id: 'system7', key: 'sys7', short: 'S7', side: 'short', slots: 10, priority: 7, weight: 0.20, desc: 'SPY カタストロフィーヘッジ', order: 'market (SPY)' },
];

const KNOWN_SYSTEMS = new Set(SYSTEM_SLOT_MODEL.map((s) => s.id));

/** 決済 = ポジションが実際に減る理由。protect_* は常駐注文を置くだけ。 */
const CLOSE_REASONS = new Set(['time_based', 'flatten_all']);

export type Side = 'long' | 'short';

/** 保有の観測点。allocator が見たのと同じ「寄り前」か、引け後の実測か。 */
export type HoldingsBasis = 'pre_entry' | 'post_entry';
export type EntriesBasis = 'submitted' | 'proposed';
export type ExitsBasis = 'executed' | 'proposed' | 'realized' | 'none';
export type BookBasis = 'measured' | 'projected';

export interface EntryItem {
  symbol: string;
  /** null = 実際に submit された (枠を増やす)。値あり = submit 直前に落ちた。 */
  skipReason: string | null;
  skipCategory: string | null;
}

export interface ExitItem {
  symbol: string;
  reason: string;
  /** close = ポジションが減る。protect = 常駐保護注文を置くだけ。 */
  kind: 'close' | 'protect';
}

export interface SystemSlotRow {
  spec: SystemSlotSpec;
  heldSymbols: string[];
  held: number;
  candidates: number | null;
  entries: EntryItem[];
  /** 実際に枠を増やす本数 (skip されなかったエントリー)。 */
  netEntries: number;
  /** 左列（昨日ポジション）に居た銘柄の決済。ここだけが − として算術に効く。 */
  closes: ExitItem[];
  /** 左列の観測時刻より前に閉じた決済。既に左列から抜けているので − しない。 */
  closedBeforeRead: ExitItem[];
  protects: ExitItem[];
  /** 引け後実測の保有 (取れた日だけ)。 */
  measuredNow: number | null;
  projectedNow: number;
  used: number;
  free: number;
  over: number;
  why: { tone: 'ok' | 'warn' | 'blocked' | 'muted'; text: string };
  skipSummary: { category: string; label: string; count: number }[];
}

export interface OrphanRow {
  symbols: string[];
  count: number;
  side: Side;
}

export interface SlotFlowBasis {
  holdings: HoldingsBasis | null;
  holdingsSource: string | null;
  holdingsObservedAt: string | null;
  entries: EntriesBasis | null;
  entriesSource: string | null;
  exits: ExitsBasis;
  exitsSource: string | null;
  todayBook: BookBasis;
  /** sidecar を bundle manifest の content hash で検証できたか。 */
  sidecarVerified: boolean;
}

export interface PoolMeter {
  key: string;
  label: string;
  side: Side;
  held: number;
  cap: number;
  kept: number;
  allow: number;
  trimmed: number | null;
  note: string;
  exhausted: boolean;
}

export interface SelfCheck {
  label: string;
  value: string;
  pass: boolean;
}

export interface SlotFlowModel {
  date: string;
  caps: SignalsCaps | null;
  meters: PoolMeter[];
  rows: SystemSlotRow[];
  orphan: OrphanRow | null;
  basis: SlotFlowBasis;
  totals: {
    held: number;
    closes: number;
    closedBeforeRead: number;
    protects: number;
    entriesProposed: number;
    entriesNet: number;
    projectedNow: number;
    measuredNow: number | null;
  };
  selfChecks: SelfCheck[];
  /** 表示できない致命的な欠落 (今日の signals が無い等)。 */
  unavailable: string | null;
  headline: string | null;
}

const SKIP_LABELS: Record<string, string> = {
  already_held: '既に保有',
  standing_cap: 'system 枠上限',
  wash_trade_conflict: 'ウォッシュ回避',
  'skip:below_min_notional': '最小注文額 未満',
  'skip:below_1_share': '1 株未満',
};

export function skipCategoryOf(reason: string | null | undefined): string | null {
  if (!reason) return null;
  const head = reason.split(':')[0];
  if (head === 'skip') {
    // skip:below_min_notional のように 2 語で 1 つの理由。
    const rest = reason.split(':')[1] ?? '';
    return `skip:${rest}`;
  }
  return head;
}

export function skipLabelOf(category: string): string {
  return SKIP_LABELS[category] ?? category;
}

function normalizeSystem(raw: string | null | undefined): string | null {
  if (!raw) return null;
  const v = String(raw).trim().toLowerCase();
  return KNOWN_SYSTEMS.has(v) ? v : null;
}

function normalizeSide(raw: string | null | undefined): Side | null {
  if (!raw) return null;
  const v = String(raw).trim().toLowerCase();
  if (v === 'long' || v === 'buy') return 'long';
  if (v === 'short' || v === 'sell') return 'short';
  return null;
}

function uniq(values: string[]): string[] {
  return [...new Set(values)];
}

/**
 * 保有を「寄り前 (allocator が見たのと同じ観測点)」で取る。
 *
 *   1st: exit_orders_*_proposal.json  … 06:4x JST の broker read = 当日エントリー前
 *   2nd: exit_orders_*_execution.json … 22:4x の exit stage 冒頭 read (entry の前)
 *   3rd: alpaca_snapshot              … 引け後実測。**entry 後**なので basis が変わる
 */
function pickHoldings(
  exitProposal: ExitOrdersPayload | null,
  exitExecution: ExitOrdersPayload | null,
  snapshot: AlpacaSnapshot | null,
  expectedHeldTotal: number | null,
): {
  rows: { symbol: string; system: string | null; side: Side | null }[];
  basis: HoldingsBasis;
  source: string;
  observedAt: string | null;
} | null {
  const candidates = [exitProposal, exitExecution].filter(
    (p): p is ExitOrdersPayload => !!p && Array.isArray(p.positions) && p.positions.length > 0,
  );
  // 06:4x の proposal と 22:4x の execution は **別の観測時刻** の broker read。
  // caps (= allocator が実際に見た保有) と件数が一致する方を採る。そうしないと
  // 上の枠メーターと下の system 別内訳が別の瞬間を指し、合計が合わなくなる。
  const matched =
    expectedHeldTotal != null
      ? candidates.find((p) => p.positions.length === expectedHeldTotal)
      : undefined;
  const chosen = matched ?? candidates[0];
  if (chosen) {
    return {
      rows: chosen.positions.map((p) => ({
        symbol: p.symbol,
        system: normalizeSystem(p.system),
        side: normalizeSide(p.side),
      })),
      basis: 'pre_entry',
      source: `exit_orders (${chosen.role ?? 'unknown'})`,
      observedAt: chosen.written_at ?? null,
    };
  }
  if (snapshot && Array.isArray(snapshot.positions) && snapshot.positions.length > 0) {
    return {
      rows: snapshot.positions.map((p) => ({
        symbol: p.symbol,
        system: normalizeSystem(p.system),
        side: normalizeSide(p.side),
      })),
      basis: 'post_entry',
      source: 'alpaca_snapshot',
      observedAt: snapshot.generated_at ?? null,
    };
  }
  return null;
}

function pickEntries(
  paperOrders: PaperOrdersPayload | null,
  signals: SignalsPayload,
): {
  bySystem: Map<string, EntryItem[]>;
  basis: EntriesBasis;
  source: string;
} {
  const bySystem = new Map<string, EntryItem[]>();
  if (paperOrders && Array.isArray(paperOrders.orders) && paperOrders.orders.length > 0) {
    for (const order of paperOrders.orders) {
      const sys = normalizeSystem(order.system);
      if (!sys) continue;
      const list = bySystem.get(sys) ?? [];
      const skipReason = order.skip_reason ?? null;
      list.push({ symbol: order.symbol, skipReason, skipCategory: skipCategoryOf(skipReason) });
      bySystem.set(sys, list);
    }
    // dry_run では skip_reason が常に null なので「submitted」を名乗らせない。
    const submitted = paperOrders.mode === 'submitted';
    return {
      bySystem,
      basis: submitted ? 'submitted' : 'proposed',
      source: `paper_orders (${paperOrders.mode ?? 'unknown'})`,
    };
  }
  for (const spec of SYSTEM_SLOT_MODEL) {
    const sys = signals.systems?.[spec.key];
    if (!sys || !Array.isArray(sys.signals)) continue;
    const list = sys.signals.map((s) => ({
      symbol: s.symbol,
      skipReason: null,
      skipCategory: null,
    }));
    if (list.length > 0) bySystem.set(spec.id, list);
  }
  return { bySystem, basis: 'proposed', source: 'today_signals' };
}

/**
 * エグジットは 2 種類あり、出所が違う。混ぜると「昨日 − out + in = 今日」が合わない。
 *
 *   決済 (ポジションが減る) … 当日約定した分は **約定台帳が唯一の正**。前日以前に
 *       置いた常駐 protect 注文が場中に約定した分は exit_orders_* に一切現れず、
 *       exit_orders だけを見ると − を取りこぼす。
 *   保護注文 (減らない)     … その日に置いた常駐注文。exit_orders_* にしかない。
 *
 * なので closes = 約定台帳 (まだ無い朝は exit_orders の決済提案)、
 * protects = exit_orders、と別々に取る。
 */
function pickExits(
  exitExecution: ExitOrdersPayload | null,
  exitProposal: ExitOrdersPayload | null,
  snapshot: AlpacaSnapshot | null,
  date: string,
): { bySystem: Map<string, ExitItem[]>; basis: ExitsBasis; source: string } {
  const bySystem = new Map<string, ExitItem[]>();
  const push = (sys: string, item: ExitItem) => {
    const list = bySystem.get(sys) ?? [];
    list.push(item);
    bySystem.set(sys, list);
  };
  const sources: string[] = [];

  // --- 決済: 当日 exit_session の約定台帳 (約定単位なので銘柄で dedup) ---------
  const closed = snapshot?.realized?.closed_trades ?? [];
  const seenClose = new Set<string>();
  for (const trade of closed) {
    if (trade.exit_session !== date) continue;
    const sys = normalizeSystem(trade.system) ?? 'orphan';
    const key = `${sys}|${trade.symbol}`;
    if (seenClose.has(key)) continue;
    seenClose.add(key);
    push(sys, { symbol: trade.symbol, reason: trade.exit_reason ?? 'closed', kind: 'close' });
  }
  const ledgerClosed = seenClose.size > 0;
  if (ledgerClosed) sources.push('alpaca_snapshot.realized (決済)');

  // --- 保護注文 + (台帳がまだ無い朝の) 決済提案 -------------------------------
  const payload = exitExecution ?? exitProposal;
  if (payload && Array.isArray(payload.exits)) {
    for (const exit of payload.exits) {
      const reason = exit.reason ?? 'unknown';
      const isClose = CLOSE_REASONS.has(reason);
      // 台帳から決済が取れている日は exit_orders 側の決済が二重計上になるので捨てる。
      if (isClose && ledgerClosed) continue;
      const sys = normalizeSystem(exit.system) ?? 'orphan';
      push(sys, { symbol: exit.symbol, reason, kind: isClose ? 'close' : 'protect' });
    }
    if (payload.exits.length > 0) {
      sources.push(`exit_orders (${payload.role ?? 'unknown'})`);
    }
  }

  if (bySystem.size === 0) return { bySystem, basis: 'none', source: '' };
  const basis: ExitsBasis = ledgerClosed
    ? 'realized'
    : payload?.role === 'execution'
      ? 'executed'
      : 'proposed';
  return { bySystem, basis, source: sources.join(' + ') };
}

function candidatesOf(signals: SignalsPayload, key: string): number | null {
  const sys = signals.systems?.[key];
  if (!sys) return null;
  const fromFunnel = sys.funnel?.candidate_count;
  if (typeof fromFunnel === 'number' && Number.isFinite(fromFunnel)) return fromFunnel;
  if (typeof sys.n_candidates_input === 'number') return sys.n_candidates_input;
  return null;
}

function buildMeters(caps: SignalsCaps | null): PoolMeter[] {
  const c = caps?.caps ?? {};
  const held = caps?.held;
  const allow = caps?.allow;
  const kept = caps?.kept;
  const trimmed = caps?.trimmed ?? {};
  const defs: {
    key: string;
    label: string;
    side: Side;
    cap?: number;
    held?: number;
    allow?: number;
    kept?: number;
    trimmed?: number;
  }[] = [
    {
      key: 'long',
      label: 'ロング枠',
      side: 'long',
      cap: c.max_long,
      held: held?.long,
      allow: allow?.long,
      kept: kept?.long,
      trimmed: trimmed.long_count,
    },
    {
      key: 'short',
      label: 'ショート枠',
      side: 'short',
      cap: c.max_short,
      held: held?.short,
      allow: allow?.short,
      kept: kept?.short,
      trimmed: trimmed.short_count,
    },
    {
      key: 'total',
      label: '合計枠',
      side: 'long',
      cap: c.max_total,
      held: held?.total,
      allow: allow?.total,
      kept: kept?.total,
      trimmed: trimmed.total,
    },
  ];
  const meters: PoolMeter[] = [];
  for (const d of defs) {
    if (typeof d.cap !== 'number' || typeof d.held !== 'number') continue;
    const allowN = typeof d.allow === 'number' ? d.allow : Math.max(0, d.cap - d.held);
    const keptN = typeof d.kept === 'number' ? d.kept : 0;
    const exhausted = allowN > 0 && keptN >= allowN;
    const trimmedN = typeof d.trimmed === 'number' ? d.trimmed : null;
    let note: string;
    if (exhausted) {
      note = `空き ${allowN} を本日 ${keptN} 本で使い切り${
        trimmedN ? `。${trimmedN} 件は枠で不採用` : ''
      }`;
    } else if (keptN === 0) {
      note = `空き ${allowN}。本日の採用 0 本`;
    } else {
      note = `空き ${allowN} のうち ${keptN} 本を採用。まだ ${allowN - keptN} 枠`;
    }
    meters.push({
      key: d.key,
      label: d.label,
      side: d.side,
      held: d.held,
      cap: d.cap,
      kept: keptN,
      allow: allowN,
      trimmed: trimmedN,
      note,
      exhausted,
    });
  }
  // 合計枠が空いていても片側が満杯なら詰まっている。どちらが効いたかを明記する。
  const total = meters.find((m) => m.key === 'total');
  if (total && !total.exhausted) {
    const binding = meters.filter((m) => m.key !== 'total' && m.exhausted).map((m) => m.label);
    total.note =
      binding.length > 0
        ? `合計 ${total.cap} のうち ${total.held} 使用。合計枠では詰まっていない（効いたのは ${binding.join(' / ')}）`
        : `合計 ${total.cap} のうち ${total.held} 使用。本日は合計枠でも詰まっていない`;
  }
  return meters;
}

function buildSelfChecks(
  caps: SignalsCaps | null,
  rows: SystemSlotRow[],
  heldShown: number | null,
): SelfCheck[] {
  const checks: SelfCheck[] = [];
  const sumLong = SYSTEM_SLOT_MODEL.filter((s) => s.side === 'long').reduce((a, s) => a + s.slots, 0);
  const sumShort = SYSTEM_SLOT_MODEL.filter((s) => s.side === 'short').reduce((a, s) => a + s.slots, 0);
  const c = caps?.caps;
  if (c?.max_long != null) {
    checks.push({
      label: 'long 系統の枠合計 = max_long',
      value: `${sumLong} = ${c.max_long}`,
      pass: sumLong === c.max_long,
    });
  }
  if (c?.max_short != null) {
    checks.push({
      label: 'short 系統の枠合計 = max_short',
      value: `${sumShort} = ${c.max_short}`,
      pass: sumShort === c.max_short,
    });
  }
  if (c?.max_total != null) {
    checks.push({
      label: '枠合計 = max_total',
      value: `${sumLong + sumShort} = ${c.max_total}`,
      pass: sumLong + sumShort === c.max_total,
    });
  }
  if (c?.max_long != null && caps?.held?.long != null && caps?.allow?.long != null) {
    checks.push({
      label: 'allow.long = max_long − held.long',
      value: `${c.max_long - caps.held.long} = ${caps.allow.long}`,
      pass: c.max_long - caps.held.long === caps.allow.long,
    });
  }
  if (c?.max_short != null && caps?.held?.short != null && caps?.allow?.short != null) {
    checks.push({
      label: 'allow.short = max_short − held.short',
      value: `${c.max_short - caps.held.short} = ${caps.allow.short}`,
      pass: c.max_short - caps.held.short === caps.allow.short,
    });
  }
  const entriesTotal = rows.reduce((a, r) => a + r.entries.length, 0);
  if (caps?.kept?.total != null) {
    checks.push({
      label: '画面のエントリー合計 = caps.kept.total',
      value: `${entriesTotal} = ${caps.kept.total}`,
      pass: entriesTotal === caps.kept.total,
    });
  }
  if (caps?.held?.total != null && heldShown != null) {
    checks.push({
      label: '画面の保有合計 = caps.held.total（同じ観測時刻を見ているか）',
      value: `${heldShown} = ${caps.held.total}`,
      pass: heldShown === caps.held.total,
    });
  }
  if (caps?.held_unmapped?.total != null && caps?.held?.total != null) {
    const shadowed = caps.held_unmapped.total === caps.held.total && caps.held.total > 0;
    checks.push({
      label: 'system 枠が効いている (held_unmapped < held)',
      value: `held_unmapped ${caps.held_unmapped.total} / held ${caps.held.total}`,
      pass: !shadowed,
    });
  }
  return checks;
}

/**
 * 「候補 N → エントリー M」の差を、推測ではなく **その system が配分を受けた時点で
 * 枠が残っていたか** から判定する。優先度は side 昇順 → system 番号昇順なので、
 * 自分より優先度が高い system が使った本数を引いた残りが自分の取り分になる。
 *
 *   remaining = allow(side の空き) − 優先度上位が採用した本数
 *   proposed >= remaining なら「枠で切られた」、そうでなければ枠以外の理由。
 *
 * 枠以外の脱落は artifact に理由が残っていない (重複除去 / サイジング予算切れ)。
 * そこは分かったふりをせず「記録なし」と書く。
 */
function buildWhy(
  spec: SystemSlotSpec,
  candidates: number | null,
  proposed: number,
  poolAllow: number | null,
  usedByHigher: number,
  higherUsers: string[],
): SystemSlotRow['why'] {
  if (candidates === 0) {
    return { tone: 'muted', text: '候補 0（セットアップ未成立）— 枠ではなく相場条件' };
  }
  if (candidates == null) {
    return { tone: 'muted', text: '候補数が artifact に無いため理由を判定できません' };
  }
  if (proposed >= candidates) {
    return { tone: 'ok', text: `候補 ${candidates} 本をすべて採用` };
  }
  const dropped = candidates - proposed;
  const sideLabel = spec.side === 'long' ? 'ロング枠' : 'ショート枠';
  const remaining = poolAllow == null ? null : Math.max(0, poolAllow - usedByHigher);
  if (remaining != null && proposed >= remaining) {
    const who =
      higherUsers.length > 0
        ? `空き ${poolAllow} のうち ${higherUsers.join(' / ')} が先に埋め、この system に残ったのは ${remaining}`
        : `空き ${poolAllow} を使い切り`;
    return {
      tone: proposed === 0 ? 'blocked' : 'warn',
      text: `候補 ${candidates} → ${proposed} 本。残り ${dropped} 件は ✗ ${sideLabel} 切れ（${who}）`,
    };
  }
  return {
    tone: 'warn',
    text: `候補 ${candidates} → ${proposed} 本。残り ${dropped} 件は枠ではなく配分段で脱落（artifact に理由の記録なし）`,
  };
}

export function buildSlotFlowModel(input: {
  signals: SignalsPayload | null;
  snapshot: AlpacaSnapshot | null;
  paperOrders: PaperOrdersPayload | null;
  exitProposal: ExitOrdersPayload | null;
  exitExecution: ExitOrdersPayload | null;
  sidecarVerified?: boolean;
}): SlotFlowModel {
  const { signals, snapshot, paperOrders, exitProposal, exitExecution } = input;
  const sidecarVerified = input.sidecarVerified ?? false;
  const empty: SlotFlowModel = {
    date: signals?.date ?? '',
    caps: null,
    meters: [],
    rows: [],
    orphan: null,
    basis: {
      holdings: null,
      holdingsSource: null,
      holdingsObservedAt: null,
      entries: null,
      entriesSource: null,
      exits: 'none',
      exitsSource: null,
      todayBook: 'projected',
      sidecarVerified: false,
    },
    totals: {
      held: 0,
      closes: 0,
      closedBeforeRead: 0,
      protects: 0,
      entriesProposed: 0,
      entriesNet: 0,
      projectedNow: 0,
      measuredNow: null,
    },
    selfChecks: [],
    unavailable: null,
    headline: null,
  };
  if (!signals) {
    return { ...empty, unavailable: '当日の today_signals が読めないため枠を表示できません。' };
  }
  const caps = signals.portfolio?.caps ?? null;
  if (!caps?.caps || !caps.held) {
    return {
      ...empty,
      date: signals.date,
      unavailable:
        'today_signals に portfolio.caps がありません（旧 artifact）。枠メーターは当日分の再生成後に表示されます。',
    };
  }

  const holdings = pickHoldings(
    exitProposal,
    exitExecution,
    snapshot,
    caps.held?.total ?? null,
  );
  const entries = pickEntries(paperOrders, signals);
  const exits = pickExits(exitExecution, exitProposal, snapshot, signals.date);

  const heldBySystem = new Map<string, string[]>();
  const orphanSymbols: string[] = [];
  for (const row of holdings?.rows ?? []) {
    if (row.system) {
      const list = heldBySystem.get(row.system) ?? [];
      list.push(row.symbol);
      heldBySystem.set(row.system, list);
    } else {
      orphanSymbols.push(row.symbol);
    }
  }

  // 引け後実測の「今日の保有」。holdings が post_entry ならそれ自体が実測。
  const measuredBySystem = new Map<string, number>();
  let measuredTotal: number | null = null;
  const measuredSource =
    holdings?.basis === 'post_entry' ? holdings.rows : snapshot?.positions ?? null;
  const entriesAreSubmitted = entries.basis === 'submitted';
  if (measuredSource && (holdings?.basis === 'post_entry' || entriesAreSubmitted)) {
    measuredTotal = 0;
    for (const p of measuredSource) {
      const sys = normalizeSystem((p as { system?: string | null }).system) ?? 'orphan';
      measuredBySystem.set(sys, (measuredBySystem.get(sys) ?? 0) + 1);
      measuredTotal += 1;
    }
  }

  const meters = buildMeters(caps);
  const poolByside: Record<Side, PoolMeter | undefined> = {
    long: meters.find((m) => m.key === 'long'),
    short: meters.find((m) => m.key === 'short'),
  };

  const proposedBySystem = new Map<string, number>();
  for (const spec of SYSTEM_SLOT_MODEL) {
    proposedBySystem.set(spec.id, (entries.bySystem.get(spec.id) ?? []).length);
  }

  const rows: SystemSlotRow[] = SYSTEM_SLOT_MODEL.map((spec) => {
    const heldSymbols = uniq(heldBySystem.get(spec.id) ?? []);
    const entryItems = entries.bySystem.get(spec.id) ?? [];
    const exitItems = exits.bySystem.get(spec.id) ?? [];
    const heldSet = new Set(heldSymbols);
    // 決済は「左列に居た銘柄」だけを − に数える。保有 read より前に閉じた銘柄は
    // すでに左列から抜けているので、引くと二重計上になる (実測と合わなくなる)。
    const allCloses = exitItems.filter((e) => e.kind === 'close');
    const closes = allCloses.filter((e) => heldSet.has(e.symbol));
    const closedBeforeRead = allCloses.filter((e) => !heldSet.has(e.symbol));
    const protects = exitItems.filter((e) => e.kind === 'protect');
    const netEntries = entryItems.filter((e) => e.skipReason == null).length;
    const candidates = candidatesOf(signals, spec.key);

    const pool = poolByside[spec.side];
    const higher = SYSTEM_SLOT_MODEL.filter(
      (s) => s.side === spec.side && s.priority < spec.priority && (proposedBySystem.get(s.id) ?? 0) > 0,
    );
    const usedByHigher = higher.reduce((a, s) => a + (proposedBySystem.get(s.id) ?? 0), 0);
    const why = buildWhy(
      spec,
      candidates,
      entryItems.length,
      pool?.allow ?? null,
      usedByHigher,
      higher.map((s) => `${s.short} ${proposedBySystem.get(s.id)}`),
    );

    const skipCounts = new Map<string, number>();
    for (const e of entryItems) {
      if (!e.skipCategory) continue;
      skipCounts.set(e.skipCategory, (skipCounts.get(e.skipCategory) ?? 0) + 1);
    }
    const skipSummary = [...skipCounts.entries()]
      .sort((a, b) => b[1] - a[1])
      .map(([category, count]) => ({ category, label: skipLabelOf(category), count }));

    const held = heldSymbols.length;
    // holdings が引け後実測 (post_entry) の日は「保有 = 本日分込み」なので
    // 二重計上しないよう used は held のみにする。
    const used = holdings?.basis === 'post_entry' ? held : held + netEntries;
    const projectedNow = held - closes.length + netEntries;
    return {
      spec,
      heldSymbols,
      held,
      candidates,
      entries: entryItems,
      netEntries,
      closes,
      closedBeforeRead,
      protects,
      measuredNow: measuredBySystem.has(spec.id) ? (measuredBySystem.get(spec.id) as number) : null,
      projectedNow,
      used,
      free: Math.max(0, spec.slots - used),
      over: Math.max(0, used - spec.slots),
      why,
      skipSummary,
    };
  });

  const totals = {
    held: rows.reduce((a, r) => a + r.held, 0) + orphanSymbols.length,
    closes: rows.reduce((a, r) => a + r.closes.length, 0),
    closedBeforeRead: rows.reduce((a, r) => a + r.closedBeforeRead.length, 0),
    protects: rows.reduce((a, r) => a + r.protects.length, 0),
    entriesProposed: rows.reduce((a, r) => a + r.entries.length, 0),
    entriesNet: rows.reduce((a, r) => a + r.netEntries, 0),
    projectedNow: rows.reduce((a, r) => a + r.projectedNow, 0) + orphanSymbols.length,
    measuredNow: measuredTotal,
  };

  const basis: SlotFlowBasis = {
    holdings: holdings?.basis ?? null,
    holdingsSource: holdings?.source ?? null,
    holdingsObservedAt: holdings?.observedAt ?? null,
    entries: entries.basis,
    entriesSource: entries.source,
    exits: exits.basis,
    exitsSource: exits.source || null,
    todayBook: measuredTotal != null ? 'measured' : 'projected',
    sidecarVerified,
  };

  // 見出し: 「なぜ 0 なのか」を 1 文で先に答える。
  const blocked = rows.filter((r) => r.why.tone === 'blocked');
  let headline: string | null = null;
  const keptTotal = caps.kept?.total ?? totals.entriesProposed;
  if (blocked.length > 0) {
    const pool = poolByside[blocked[0].spec.side];
    const sideLabel = blocked[0].spec.side === 'long' ? 'ロング枠' : 'ショート枠';
    headline =
      `本日のエントリーは ${keptTotal} 本。` +
      `${blocked.map((r) => `${r.spec.short} は候補 ${r.candidates} → 0 本`).join('、')}。` +
      `理由は候補の質ではなく ${sideLabel} が満杯（保有 ${pool?.held ?? '—'} / 上限 ${
        pool?.cap ?? '—'
      } → 空き ${pool?.allow ?? '—'} を優先度上位が先に取った）。`;
  } else if (keptTotal === 0) {
    headline = '本日のエントリーは 0 本。下の枠ビューで、枠切れか候補 0 かを確認できます。';
  } else {
    headline = `本日のエントリーは ${keptTotal} 本。枠で不採用になった候補は下の各行に理由を出しています。`;
  }

  return {
    date: signals.date,
    caps,
    meters,
    rows,
    orphan:
      orphanSymbols.length > 0
        ? { symbols: uniq(orphanSymbols), count: orphanSymbols.length, side: 'long' }
        : null,
    basis,
    totals,
    selfChecks: buildSelfChecks(caps, rows, holdings ? totals.held : null),
    unavailable: null,
    headline,
  };
}
