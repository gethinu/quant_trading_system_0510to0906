use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::io::{self, Read};

#[derive(Debug, Clone, Deserialize)]
struct Opportunity {
    date: String,
    system: String,
    side: String,
    symbol: String,
    entry_date: String,
    exit_date: String,
    entry_price: f64,
    stop_price: f64,
    exit_price: f64,
    risk_pct: f64,
    max_pct: f64,
    max_positions: i64,
    is_valid: bool,
}

#[derive(Debug, Deserialize)]
struct InputPayload {
    dates: Vec<String>,
    systems_order: Vec<String>,
    initial_capital: f64,
    allocations: HashMap<String, f64>,
    long_share: f64,
    short_share: f64,
    allow_gross_leverage: bool,
    opportunities: Vec<Opportunity>,
}

#[derive(Debug, Serialize)]
struct TradeOut {
    system: String,
    side: String,
    symbol: String,
    entry_date: String,
    exit_date: String,
    entry_price: f64,
    exit_price: f64,
    shares: i64,
    pnl: f64,
    #[serde(rename = "return_%")]
    return_pct: f64,
}

#[derive(Debug, Clone)]
struct ActivePosition {
    system: String,
    side: String,
    symbol: String,
    exit_date: String,
    pnl: f64,
    cost: f64,
}

#[derive(Debug, Serialize)]
struct OutputPayload {
    trades: Vec<TradeOut>,
}

fn calculate_position_size(
    capital: f64,
    entry_price: f64,
    stop_price: f64,
    risk_pct: f64,
    max_pct: f64,
) -> i64 {
    if !(capital.is_finite()
        && entry_price.is_finite()
        && stop_price.is_finite()
        && risk_pct.is_finite()
        && max_pct.is_finite())
    {
        return 0;
    }
    if capital <= 0.0 || entry_price <= 0.0 || stop_price <= 0.0 {
        return 0;
    }

    let risk_pct = risk_pct.max(0.0);
    let max_pct = max_pct.max(0.0);
    if risk_pct == 0.0 || max_pct == 0.0 {
        return 0;
    }

    let risk_per_trade = capital * risk_pct;
    let max_position_value = capital * max_pct;
    let risk_per_share = (entry_price - stop_price).abs();
    if risk_per_share <= 0.0 {
        return 0;
    }

    let shares_by_risk = (risk_per_trade / risk_per_share).floor() as i64;
    let shares_by_capital = (max_position_value / entry_price).floor() as i64;
    let mut shares = shares_by_risk.min(shares_by_capital);
    if shares <= 0 && risk_per_trade >= risk_per_share {
        shares = 1;
    }
    shares.max(0)
}

fn symbol_open_in_active(active_positions: &[ActivePosition], symbol: &str) -> bool {
    active_positions.iter().any(|p| p.symbol == symbol)
}

fn get_bucket_used(bucket_used_value: &HashMap<String, f64>, side: &str) -> f64 {
    *bucket_used_value.get(side).unwrap_or(&0.0)
}

fn main() -> Result<(), String> {
    let mut input = String::new();
    io::stdin()
        .read_to_string(&mut input)
        .map_err(|e| format!("stdin read failed: {e}"))?;

    let payload: InputPayload =
        serde_json::from_str(&input).map_err(|e| format!("input parse failed: {e}"))?;

    let mut opportunities_by_key: HashMap<(String, String), Vec<Opportunity>> = HashMap::new();
    for opp in payload.opportunities {
        opportunities_by_key
            .entry((opp.date.clone(), opp.system.clone()))
            .or_default()
            .push(opp);
    }

    let mut long_share = payload.long_share;
    let mut short_share = payload.short_share;
    if long_share < 0.0 || short_share < 0.0 || (long_share + short_share).abs() <= f64::EPSILON {
        long_share = 0.5;
        short_share = 0.5;
    }

    let share_sum = long_share + short_share;
    let mut long_capital = payload.initial_capital * (long_share / share_sum);
    let mut short_capital = payload.initial_capital * (short_share / share_sum);

    let mut system_used_value: HashMap<String, f64> = payload
        .systems_order
        .iter()
        .map(|s| (s.clone(), 0.0))
        .collect();
    let mut bucket_used_value: HashMap<String, f64> = HashMap::from([
        ("long".to_string(), 0.0),
        ("short".to_string(), 0.0),
    ]);
    let mut active_positions: Vec<ActivePosition> = Vec::new();
    let mut trades: Vec<TradeOut> = Vec::new();

    for current_date in &payload.dates {
        let mut next_active: Vec<ActivePosition> = Vec::with_capacity(active_positions.len());
        for p in active_positions.drain(..) {
            if p.exit_date == *current_date {
                let used = system_used_value.entry(p.system.clone()).or_insert(0.0);
                *used = (*used - p.cost).max(0.0);
                let b_used = bucket_used_value.entry(p.side.clone()).or_insert(0.0);
                *b_used = (*b_used - p.cost).max(0.0);
                if p.side == "short" {
                    short_capital += p.pnl;
                } else {
                    long_capital += p.pnl;
                }
            } else if p.exit_date > *current_date {
                next_active.push(p);
            }
        }
        active_positions = next_active;

        for sys_name in &payload.systems_order {
            let key = (current_date.clone(), sys_name.clone());
            let Some(cands) = opportunities_by_key.get(&key) else {
                continue;
            };
            if cands.is_empty() {
                continue;
            }

            let max_positions = cands[0].max_positions.max(0);
            let active_same_count = active_positions
                .iter()
                .filter(|p| p.system == *sys_name)
                .count() as i64;
            let slots = (max_positions - active_same_count).max(0);
            if slots <= 0 {
                continue;
            }

            let scan_len = usize::min(cands.len(), slots as usize);
            for opp in cands.iter().take(scan_len) {
                if symbol_open_in_active(&active_positions, &opp.symbol) {
                    continue;
                }
                if !opp.is_valid {
                    continue;
                }

                let bucket_capital = if opp.side == "short" {
                    short_capital
                } else {
                    long_capital
                };
                let shares_std = calculate_position_size(
                    bucket_capital,
                    opp.entry_price,
                    opp.stop_price,
                    opp.risk_pct,
                    opp.max_pct,
                );
                if shares_std <= 0 {
                    continue;
                }

                let alloc_cap = payload.allocations.get(sys_name).copied().unwrap_or(0.0) * bucket_capital;
                let used_by_system = *system_used_value.get(sys_name).unwrap_or(&0.0);
                let alloc_rem = (alloc_cap - used_by_system).max(0.0);
                let global_rem = if payload.allow_gross_leverage {
                    f64::INFINITY
                } else {
                    (bucket_capital - get_bucket_used(&bucket_used_value, &opp.side)).max(0.0)
                };

                let max_by_alloc = if opp.entry_price != 0.0 {
                    (alloc_rem / opp.entry_price.abs()).floor() as i64
                } else {
                    0
                };
                let max_by_global = if opp.entry_price != 0.0 {
                    (global_rem / opp.entry_price.abs()).floor() as i64
                } else {
                    0
                };
                let shares_cap = shares_std.min(max_by_alloc).min(max_by_global).max(0);
                if shares_cap <= 0 {
                    continue;
                }

                let pnl = if opp.side == "short" {
                    (opp.entry_price - opp.exit_price) * shares_cap as f64
                } else {
                    (opp.exit_price - opp.entry_price) * shares_cap as f64
                };
                let return_pct = if bucket_capital != 0.0 {
                    (pnl / bucket_capital) * 100.0
                } else {
                    0.0
                };

                trades.push(TradeOut {
                    system: sys_name.clone(),
                    side: opp.side.clone(),
                    symbol: opp.symbol.clone(),
                    entry_date: opp.entry_date.clone(),
                    exit_date: opp.exit_date.clone(),
                    entry_price: opp.entry_price,
                    exit_price: opp.exit_price,
                    shares: shares_cap,
                    pnl,
                    return_pct,
                });

                let cost = opp.entry_price.abs() * shares_cap as f64;
                active_positions.push(ActivePosition {
                    system: sys_name.clone(),
                    side: opp.side.clone(),
                    symbol: opp.symbol.clone(),
                    exit_date: opp.exit_date.clone(),
                    pnl,
                    cost,
                });
                let used = system_used_value.entry(sys_name.clone()).or_insert(0.0);
                *used += cost;
                let b_used = bucket_used_value.entry(opp.side.clone()).or_insert(0.0);
                *b_used += cost;
            }
        }
    }

    let output = OutputPayload { trades };
    let rendered =
        serde_json::to_string(&output).map_err(|e| format!("output encode failed: {e}"))?;
    print!("{rendered}");
    Ok(())
}
