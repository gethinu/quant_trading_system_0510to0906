export type Status = 'ok' | 'warn' | 'fail';

export interface SystemStat {
  ratio: number;
  status: Status;
  count?: number;
  threshold?: number;
}

export interface CoverageDay {
  date: string;
  n_candidates_total: number;
  survival_by_system: Record<string, SystemStat>;
}

export interface CoveragePayload {
  history: CoverageDay[];
}

// --- Today's Signals (today_signals_YYYYMMDD.json, schema version 1.0) ------

export type Side = 'BUY' | 'SELL';

export interface Signal {
  symbol: string;
  side: Side;
  entry_price: number | null;
  weight: number | null;
  rank: number | null;
  reason: string | null;
}

export interface SystemSignals {
  signals: Signal[];
  n_candidates_input: number;
  n_signals_output: number;
  gate_survival_ratio: number;
}

export interface Hedge {
  symbol: string | null;
  side: Side | string | null;
  entry_price?: number | null;
}

export interface SignalsPortfolio {
  total_signals: number;
  total_notional_usd: number;
  hedge: Hedge | null;
}

export interface SignalsMeta {
  cli_version: string;
  run_id: string;
  elapsed_seconds: number | null;
  publish_status?: 'ok' | 'partial' | 'failed';
}

export interface SignalsPayload {
  version: string;
  date: string;
  generated_at: string;
  provider: string;
  systems: Record<string, SystemSignals>;
  portfolio: SignalsPortfolio;
  meta: SignalsMeta;
}

// --- Narrative (narrative_YYYYMMDD.json, AI narrator 出力) ------------------

export interface Narrative {
  date: string;
  headline: string;
  summary: string;
  per_symbol_reasons?: Record<string, string>;
  model?: string;
  cost_usd?: number;
  elapsed_seconds?: number;
}
