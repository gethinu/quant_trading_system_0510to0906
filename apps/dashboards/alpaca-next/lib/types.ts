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

// --- Signal Pipeline (pipeline_YYYYMMDD.json, schema signal_pipeline/v1) -----
// user 指摘に基づき「単一 survival rate」を捨て、universe → setup → filter → ...
// → final の phase 別絞込フローを可視化する。ratio は評価軸ではなく参考数値。

export interface SystemPipelinePhase {
  name: string;
  label: string;
  condition?: string;
  /** grouped-daily で実測できた phase のみ数値。未計測は null。 */
  count: number | null;
  measured?: boolean;
  /** 直前の計測済 phase に対する通過率 (参考数値)。 */
  ratio_of_prev: number | null;
  /** universe に対する通過率 (参考数値)。 */
  ratio_of_universe: number | null;
}

export interface SystemPipeline {
  system_id: string;
  phases: SystemPipelinePhase[];
  final_signals: number | null;
}

export interface PipelinePayload {
  date: string;
  provider?: string;
  schema?: string;
  systems: Record<string, SystemPipeline>;
  notes?: string[];
  /** 旧 coverage schema から fallback 生成された場合 true。 */
  from_legacy?: boolean;
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
