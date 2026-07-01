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

// AI narrator 出力 (common/narrator.py -> meta.narrative へ merge)
export interface Narrative {
  headline: string;
  summary: string;
  per_symbol_reasons: Record<string, string>;
  model?: string;
  cost_usd?: number;
  elapsed_seconds?: number;
  warnings?: string[];
  configured?: boolean;
  fallback?: boolean;
}

export interface SignalsMeta {
  cli_version: string;
  run_id: string;
  elapsed_seconds: number | null;
  publish_status?: 'ok' | 'partial' | 'failed';
  narrative?: Narrative;
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

// --- Today's Orders Preview (orders_preview_YYYYMMDD_${equity}.json) --------

export type OrderTier = 'small' | 'medium' | 'large';

export interface PreviewOrder {
  symbol: string;
  side: string; // 'buy' | 'sell'
  notional_usd: number;
  qty: number;
  fractional: boolean;
  order_type: string;
  system: string | null;
  weight: number | null;
  rank: number | null;
  client_order_id: string;
}

export interface PreviewSkipped {
  symbol: string;
  reason: string;
  system?: string | null;
  weight?: number | null;
}

export interface OrdersPreviewSummary {
  total_notional: number;
  n_orders: number;
  n_skipped: number;
  hedge_notional: number;
}

export interface OrdersPreview {
  date: string;
  account_equity: number;
  tier: OrderTier | string;
  orders: PreviewOrder[];
  skipped: PreviewSkipped[];
  summary: OrdersPreviewSummary;
}
