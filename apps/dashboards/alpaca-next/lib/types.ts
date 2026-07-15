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

// --- Alpaca account snapshot (alpaca_snapshot_YYYYMMDD.json, schema v1) ------
// scripts/export_alpaca_snapshot.py の read-only 出力。account / equity 曲線 /
// exposure / 保有一覧 (system tag + エグジット予定) を 1 ファイルに集約。

export interface AlpacaAccount {
  equity: number | null;
  last_equity: number | null;
  cash: number | null;
  buying_power: number | null;
  long_market_value: number | null;
  short_market_value: number | null;
  pnl_today_abs: number | null;
  pnl_today_pct: number | null;
  /** freeze-aware baseline の provenance (新 exporter のみ; 旧 snapshot では欠落)。
   *  pnl_today_abs/pct は基準補正後の値。raw は補正前 (daily-close 基準)。 */
  pnl_today_abs_raw?: number | null;
  pnl_today_pct_raw?: number | null;
  /** "last_equity" (平常) | "freeze_adjusted" (凍結ラグ補正)。 */
  pnl_today_basis?: string | null;
  /** pnl_today の差の基準に使った equity。 */
  pnl_today_baseline?: number | null;
  /** 補正時のみ: 前営業日 intraday − last_equity (据え置き幅)。 */
  freeze_lag_gap?: number | null;
  unrealized_pl_total: number | null;
  status: string;
  trading_blocked: boolean;
  pattern_day_trader: boolean;
}

export interface EquityPoint {
  t: string;
  equity: number;
  pl: number | null;
  pl_pct: number | null;
  /** running peak up to this point (drawdown band の上端)。 */
  peak?: number;
  /** peak からの下落率 (%)。負値 = 含み drawdown。 */
  dd_pct?: number;
  /** 末尾の live intraday equity point のみ true。 */
  live?: boolean;
}

export interface EquityCurve {
  timeframe: string;
  period: string;
  base_value: number | null;
  points: EquityPoint[];
  peak_equity: number | null;
  max_drawdown_pct: number | null;
  period_return_pct: number | null;
  source: string;
}

export interface SystemExposure {
  long_usd: number;
  short_usd: number;
  count: number;
  unrealized_pl: number;
  pct_of_gross: number;
}

export interface AlpacaExposure {
  long_usd: number;
  short_usd: number;
  gross_usd: number;
  net_usd: number;
  gross_pct: number | null;
  net_pct: number | null;
  gross_cap_pct: number;
  net_cap_pct: number;
  by_system: Record<string, SystemExposure>;
}

export interface PnlExtreme {
  symbol: string;
  pl: number;
  pl_pct: number | null;
}

export interface AlpacaSummary {
  n_positions: number;
  n_long: number;
  n_short: number;
  n_winning: number;
  n_losing: number;
  win_rate_pct: number | null;
  unrealized_pl_total: number;
  exit_soon_count: number;
  biggest_winner: PnlExtreme | null;
  biggest_loser: PnlExtreme | null;
}

export type PositionSide = 'long' | 'short';

export interface AlpacaPosition {
  symbol: string;
  system: string;
  side: PositionSide;
  qty: number;
  avg_entry_price: number;
  current_price: number | null;
  lastday_price: number | null;
  market_value: number;
  cost_basis: number | null;
  unrealized_pl: number;
  unrealized_pl_pct: number | null;
  intraday_pl: number | null;
  intraday_pl_pct: number | null;
  entry_date: string | null;
  holding_days: number | null;
  max_holding_days: number;
  days_remaining: number | null;
  exit_date: string | null;
  /** "time" | "trailing" | "stop" | "spy_hedge" | "unknown" */
  exit_type: string;
  /** now エグジット条件成立時のみ "time_based" 等。 */
  exit_expected: string | null;
  stop_price_est: number | null;
  target_price_est: number | null;
  distance_to_stop_pct: number | null;
  distance_to_target_pct: number | null;
}

export interface AlpacaReconciliation {
  signals_date: string | null;
  signals_total: number | null;
  signals_buy: number | null;
  signals_sell: number | null;
  orders_date: string | null;
  orders_submitted: number | null;
  held_now: number;
  held_from_signals: number | null;
  note: string | null;
}

export interface AlpacaSnapshot {
  schema: string;
  date: string;
  generated_at: string;
  provider: string;
  account: AlpacaAccount;
  equity_curve: EquityCurve;
  exposure: AlpacaExposure;
  summary: AlpacaSummary;
  positions: AlpacaPosition[];
  reconciliation: AlpacaReconciliation;
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
