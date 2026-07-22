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
  /** 当日損益。**計測できない時は null** (架空の 0 や基準ずれの数字を出さない)。 */
  pnl_today_abs: number | null;
  pnl_today_pct: number | null;
  /** "prev_session_intraday" (唯一の正) | "unavailable" (出せない)。 */
  pnl_today_basis?: string | null;
  /** false の時は pnl_today_abs/pct を **表示してはいけない**。 */
  pnl_today_measured?: boolean | null;
  /** 差の基準に使った equity と、その所属セッション。 */
  pnl_today_baseline?: number | null;
  pnl_today_baseline_session?: string | null;
  pnl_today_session?: string | null;
  /** measured=false の時だけ入る、出せない理由。 */
  pnl_today_unavailable_reason?: string | null;
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

/** 期間切替 1 レンジ分。points が空 = その期間はデータ無し (0 で埋めない)。 */
export interface EquityRange {
  label: string;
  timeframe: string;
  points: EquityPoint[];
  peak_equity: number | null;
  max_drawdown_pct: number | null;
  period_return_pct: number | null;
  start: string | null;
  end: string | null;
  n_points: number;
  /** "intraday" (5Min, live equity と同一基準) | "broker_daily" (日次系列)。
   *  この 2 つは上場廃止建玉の扱いが違うので水準が一致しない。混ぜて差を取らない。 */
  basis?: 'intraday' | 'broker_daily' | string;
}

/** live equity と broker 日次系列の水準差を事実で分解したもの。 */
export interface EquityBasis {
  /** 上場廃止 (INACTIVE) で売却不能な建玉の時価。equity には載るが日次系列には載らない。 */
  frozen_market_value: number;
  frozen_symbols: string[];
  n_frozen: number;
  /** equity − 日次系列の最終値。 */
  daily_series_gap: number | null;
  /** 差のうち上場廃止建玉で説明できない残り (最終日次点以降の値動きを含む)。 */
  residual_usd: number | null;
  last_daily_equity: number | null;
  last_daily_session?: string | null;
}

export type EquityRangeKey = '1D' | '1W' | '1M' | '3M' | 'ALL';

/** 当日損益を 1 つの定義に統一したブロック。
 *  total_pl = 現在 equity − 前セッション終値 equity (**同一 intraday 基準**)。
 *  measured=false の時 total_pl は必ず null = 「出せない」。 */
export interface PnlToday {
  session_date: string | null;
  equity_now: number | null;
  baseline_equity: number | null;
  baseline_session: string | null;
  total_pl: number | null;
  total_pl_pct: number | null;
  /** 確定分。exit 台帳が未計測なら null。 */
  realized_pl: number | null;
  /** total − realized = 保有ポジションの当日値洗い。 */
  unrealized_delta: number | null;
  basis: string;
  measured: boolean;
  reason: string | null;
}

/** 決済済みトレード 1 本 (exit_ledger_YYYYMMDD.json 由来)。 */
export interface ClosedTrade {
  symbol: string;
  side: 'long' | 'short';
  qty: number;
  system: string | null;
  entry_time: string;
  /** 立会日 (ET)。日次集計はこれで束ねる。 */
  entry_session?: string;
  entry_price: number;
  exit_time: string;
  exit_session?: string;
  exit_price: number;
  holding_days: number;
  realized_pl: number;
  realized_pl_pct: number | null;
  exit_reason: string | null;
  exit_order_id: string | null;
}

export interface RealizedSummary {
  n_trades: number;
  total_realized_pl: number | null;
  win_rate_pct: number | null;
  n_wins: number;
  n_losses: number;
  avg_win: number | null;
  avg_loss: number | null;
  best: { symbol: string; realized_pl: number } | null;
  worst: { symbol: string; realized_pl: number } | null;
}

export interface RealizedDay {
  t: string;
  realized_pl: number;
  realized_pl_cum: number;
}

/** exit 計測の素性。complete=false なら取りこぼしを正直に出すこと。 */
export interface ExitMeasurement {
  measured: boolean;
  complete: boolean;
  reasons: string[];
  fills_seen: number;
  coverage_start: string | null;
  coverage_end: string | null;
  unmeasured_symbols: string[];
  discrepancies: {
    symbol: string;
    reconstructed_qty: number;
    broker_qty: number;
    reason: string;
  }[];
}

export interface ExitIntentRecon {
  session_date: string;
  /** "before_open" | "open" | "closed" | "unknown"。 */
  session_state?: string;
  n_intended: number;
  n_filled: number;
  /** 立会が終わった上で約定していない = 取りこぼし。 */
  intended_not_filled: { symbol: string; reason: string | null }[];
  /** まだ執行機会が来ていない分 (取りこぼしではない)。 */
  intended_pending?: { symbol: string; reason: string | null }[];
  filled_not_intended: string[];
  fully_reconciled: boolean;
  /** 立会が終わっていて判定できたか。 */
  evaluated?: boolean;
}

/** 台帳側の「当日」ブロック。session_state が closed 以外なら途中経過。 */
export interface LedgerToday {
  date: string;
  realized_pl: number | null;
  n_closed: number;
  measured: boolean;
  reasons: string[];
  session_state?: string;
  final?: boolean;
  pending_exit_intents?: number;
}

/** 実現損益ブロック。available=false = 台帳未生成 = 「未計測」と表示する。 */
export interface RealizedBlock {
  available: boolean;
  measured: boolean;
  complete?: boolean;
  /** 台帳の日付が snapshot の日付と違う = 当日分は再計測されていない。 */
  stale?: boolean;
  reason: string | null;
  ledger_date: string | null;
  ledger_run_id: string | null;
  ledger_generated_at?: string | null;
  all_time: RealizedSummary | null;
  by_day: RealizedDay[];
  by_system: Record<string, RealizedSummary>;
  closed_trades: ClosedTrade[];
  n_closed_trades_total?: number;
  measurement: ExitMeasurement | null;
  exit_intent_reconciliation?: ExitIntentRecon | null;
  today?: LedgerToday | null;
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
  /** 期間切替用 (1日/1週/1月/3月/全期間)。旧 snapshot には無い。 */
  equity_ranges?: Partial<Record<EquityRangeKey, EquityRange>> | null;
  /** equity の水準差 (上場廃止建玉) の分解。旧 snapshot には無い。 */
  equity_basis?: EquityBasis | null;
  /** 当日損益の唯一の定義。旧 snapshot には無い → 「計測不可」表示。 */
  pnl_today?: PnlToday | null;
  /** 実現損益 + 決済済みトレード履歴。旧 snapshot には無い。 */
  realized?: RealizedBlock | null;
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
