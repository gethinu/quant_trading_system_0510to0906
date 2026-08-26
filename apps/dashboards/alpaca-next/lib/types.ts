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
  measured: boolean;
  /** Measurement producer and exact signals run, when applicable. */
  source?: string;
  source_run_id?: string | null;
  source_observed_at?: string | null;
  /** Why a phase is unavailable; never replace this state with a guessed zero. */
  unmeasured_reason?: string;
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
  source_signals_run_id?: string | null;
  source_signals_generated_at?: string | null;
  source_signals_sha256?: string;
  source_recon_sha256?: string;
  materialized_at?: string;
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
  funnel?: {
    target: number | null;
    filter_pass: number | null;
    setup_pass: number | null;
    candidate_count: number | null;
    entry_count: number | null;
    exit_count?: number | null;
  };
}

export interface Hedge {
  symbol: string | null;
  side: Side | string | null;
  entry_price?: number | null;
}

/**
 * `core/final_allocation.py:_apply_portfolio_caps()` が吐く枠レポート。
 * held(保有) / caps(上限) / allow(空き) / kept(採用) / trimmed(枠で落とした) の
 * 5 点セットで「なぜ Entry がこの本数なのか」を artifact 側から説明できる。
 * 旧 artifact には無いので全て optional。
 */
export interface CapsSide {
  long: number;
  short: number;
  total: number;
}

export interface SignalsCaps {
  applied?: boolean;
  held?: CapsSide;
  /** system 帰属が付かなかった保有。held と同数なら system 枠は素通りしている。 */
  held_unmapped?: CapsSide;
  caps?: {
    max_total?: number;
    max_long?: number;
    max_short?: number;
    gross_cap_usd?: number;
    net_cap_usd?: number;
    equity_base_usd?: number;
    equity_source?: string;
  };
  allow?: CapsSide;
  kept?: CapsSide;
  trimmed?: {
    long_count?: number;
    short_count?: number;
    total?: number;
    gross_exposure?: number;
    net_exposure?: number;
  };
  new_long_usd?: number;
  new_short_usd?: number;
}

export interface SignalsPortfolio {
  total_signals: number;
  total_notional_usd: number;
  hedge: Hedge | null;
  caps?: SignalsCaps | null;
}

// --- 枠 / フロービューが読む sidecar artifact ---------------------------------
// どちらも既存のパイプライン出力 (新規 artifact は作っていない)。
//   paper_orders_YYYYMMDD.json        … 当日エントリーの発注結果 (+ skip_reason)
//   exit_orders_YYYYMMDD_*.json       … 当日の broker 実測ポジション + エグジット
// bundle manifest に載る日は hash 検証済み、載らない日は date 一致だけで採用する
// (どちらの状態かは UI 側で必ずラベルする)。

export interface PaperOrderRow {
  symbol: string;
  system: string | null;
  side: string | null;
  qty?: number | null;
  order_type?: string | null;
  limit_price?: number | null;
  notional_usd?: number | null;
  status?: string | null;
  /** null = 実際に submit された。値がある = submit 直前に落ちた理由。 */
  skip_reason?: string | null;
  dry_run?: boolean | null;
  error?: string | null;
}

export interface PaperOrdersPayload {
  version?: string;
  date: string;
  source_signals_run_id?: string | null;
  /** "submitted" (22:35 の実発注) | "dry_run" (06:00 の提案)。 */
  mode?: string | null;
  count?: number;
  submitted?: number;
  failed?: number;
  skipped?: number;
  orders: PaperOrderRow[];
}

export interface ExitPositionRow {
  symbol: string;
  system?: string | null;
  side?: string | null;
  qty?: number | null;
  avg_entry_price?: number | null;
  market_value?: number | null;
  unrealized_pl?: number | null;
  entry_date?: string | null;
}

export interface ExitOrderRow {
  symbol: string;
  system?: string | null;
  side?: string | null;
  /** time_based / flatten_all = 決済。protect_* = 常駐保護注文 (当日は減らない)。 */
  reason?: string | null;
  status?: string | null;
  skip_reason?: string | null;
  dry_run?: boolean | null;
}

export interface ExitOrdersPayload {
  version?: string;
  date: string;
  /** "proposal" (06:4x の broker read) | "execution" (22:4x の実発注)。 */
  role?: string | null;
  mode?: string | null;
  written_at?: string | null;
  count?: number;
  submitted?: number;
  positions: ExitPositionRow[];
  exits: ExitOrderRow[];
  unassigned_positions?: ExitPositionRow[];
}

export interface SignalsMeta {
  cli_version: string;
  run_id: string;
  elapsed_seconds: number | null;
  publish_status?: 'ok' | 'partial' | 'failed' | 'not_attempted' | 'unknown';
  publish_delivery?: {
    state:
      | 'primary_accepted'
      | 'all_accepted'
      | 'fallback_accepted'
      | 'partial'
      | 'all_failed'
      | 'not_configured'
      | 'not_attempted'
      | 'unknown';
    attempted_at: string | null;
    channels: Record<
      string,
      {
        state: 'accepted' | 'failed' | 'not_configured';
        status_code: number | null;
      }
    >;
  };
}

/**
 * execution summary (夜の実績通知) の ntfy 配信状態。
 * signals の meta.publish_delivery は朝の予告便専用で、open run は publish_signals を
 * 呼ばないため、実績通知の成否はこの sidecar にしか残らない。
 * accepted は ntfy server が受理した意味であり、端末到達の保証ではない。
 */
export interface NotifyDelivery {
  schema: 'notify_delivery/v1';
  kind: string;
  date: string;
  source_signals_run_id: string | null;
  state: 'accepted' | 'failed' | 'not_configured' | 'not_attempted';
  attempted_at: string | null;
  channels: Record<string, { state: string; status_code: number | null }>;
}

export interface DashboardBundleManifest {
  schema: 'dashboard_bundle/v1';
  date: string;
  source_run_id: string;
  materialized_at: string;
  files: Record<string, { name: string; sha256: string }>;
  sources?: Record<string, { name: string; sha256: string }>;
  measurement: {
    funnel_measured: number;
    funnel_total: number;
    exit_measured: number;
  };
  warnings: string[];
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
  /** ticker rename の旧 symbol 経由で system を引いた場合の旧 symbol。 */
  renamed_from?: string | null;
  holding_days: number | null;
  max_holding_days: number;
  days_remaining: number | null;
  exit_date: string | null;
  /** "time" | "trailing" | "stop" | "spy_hedge" | "unknown" */
  exit_type: string;
  /** now エグジット条件成立時のみ "time_based" 等。 */
  exit_expected: string | null;
  /**
   * 当日 exit artifact と exact-date で突合した broker 送信状態。
   * pending_execution = 計画済みだが夜の実発注 run がまだ = 失敗ではない。
   */
  exit_execution_state?: ExitExecutionState | null;
  stop_price_est: number | null;
  target_price_est: number | null;
  distance_to_stop_pct: number | null;
  distance_to_target_pct: number | null;
}

export type ExitExecutionState =
  | 'submitted'
  | 'failed'
  | 'not_submitted'
  | 'pending_execution'
  | 'not_planned'
  | 'unmeasured';

export interface AlpacaExitExecution {
  measured: boolean;
  date: string;
  /** 当日 artifact の役割。proposal = 夜の実発注前。 */
  role: 'proposal' | 'execution' | null;
  time_exit_due: number | null;
  time_exit_unsubmitted: number | null;
  execution_health:
    | 'ok'
    | 'awaiting_execution'
    | 'blocked_unsubmitted_time_exit'
    | 'unmeasured';
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
  entry_order_id?: string | null;
  /** ticker rename で統合した場合の元の symbol 群 (canonical 以外)。 */
  symbol_aliases?: string[];
  /** system を何を根拠に付けたか ("entry_order" が trade 単位の確定根拠)。 */
  system_source?: string | null;
  /** system が付かなかった理由 (system が null の時だけ入る)。 */
  system_unknown_reason?: string | null;
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
  /** 建玉の食い違いを ticker rename の「対」に組んだ仮説 (断定ではない)。 */
  rename_candidates?: {
    from_symbol: string;
    to_symbol: string;
    qty: number;
    evidence: string;
    confirmed: boolean;
  }[];
}

/** system 帰属の内訳。unknown を「なぜ不明か」まで割る。 */
export interface ExitAttribution {
  n_trades: number;
  n_attributed: number;
  n_unknown: number;
  n_ground_truth: number;
  ground_truth_pct: number | null;
  by_source: { source: string; label: string; n_trades: number }[];
  unknown_by_reason: {
    reason: string;
    label: string;
    n_trades: number;
    realized_pl: number;
    symbols: string[];
    entry_sessions: string[];
  }[];
}

/** exit 理由別の件数。母数は **全決済** (履歴表の直近 N 件ではない)。 */
export interface ExitReasonTotal {
  reason: string | null;
  n_trades: number;
  realized_pl: number;
}

/** ticker rename の統合結果。手動マップであることを画面から隠さない。 */
export interface ExitRenames {
  source: string;
  /** 常に false = broker 側に corporate action の裏づけが無い。 */
  confirmed_by_broker: boolean;
  applied: {
    alias: string;
    canonical: string;
    qty?: number;
    observed_qty?: number;
    evidence?: string;
    corroboration?: string;
  }[];
  rejected: { alias?: string; canonical?: string; rejected_reason: string }[];
  /** 統合によって復元された決済の本数。 */
  n_synthesized_trades: number;
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
  /** 旧 snapshot には無い → 内訳を出さない (0 と混同しないため)。 */
  attribution?: ExitAttribution | null;
  /** 全決済が母数の exit 理由内訳。旧 snapshot には無い。 */
  by_exit_reason?: ExitReasonTotal[];
  /** ticker rename の統合結果。旧 snapshot には無い。 */
  renames?: ExitRenames | null;
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
  /** 当日の期限 exit が提案どまりか、broker 送信済みか。 */
  exit_execution?: AlpacaExitExecution | null;
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
