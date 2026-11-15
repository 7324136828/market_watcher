# `data/` — data dictionary

Machine-readable output of `omni-market-watcher` (see `claude.md` for the agent spec). One snapshot per
daily run; every number in `reports/YYYY-MM-DD.md` is also written here so it can be charted over time.

## Conventions

- UTF-8, comma-separated, one header row. Fields containing commas are quoted.
- `date` — the run date in **America/New_York** (`YYYY-MM-DD`). `timestamp_utc` — ISO 8601 UTC instant the
  data was collected. Backfilled historical rows carry a `date` but an empty `timestamp_utc`.
- Numeric columns are plain numbers: no `$`, `%`, thousands separators or units. Percentages are in percent
  units (`2.5` = 2.5%), yields in percent, rate changes in basis points where the column says `bps`,
  probabilities in 0–1. An empty cell means **unavailable** — never `N/A`, never a placeholder `0`.
- One row per asset (or item, or contract) per date. A re-run on the same date replaces that date's rows;
  rows for earlier dates are never edited.

## Stable IDs (never renamed)

| Kind | ID form | Examples |
| :--- | :--- | :--- |
| Liquid assets | the ticker | `SPY`, `QQQ`, `XLK`, `ES=F`, `GC=F`, `^VIX`, `DX-Y.NYB`, `USDJPY=X`, `BTC-USD` |
| Treasury yields | `US2Y`, `US10Y`, `US30Y` | par yields from treasury.gov |
| Aggregates | descriptive caps | `STABLECOIN-MCAP`, `CRYPTO-TOTAL-MCAP`, `BTC-DOMINANCE`, `XAU-SPOT` |
| Illiquid items | slug from `watchlist.md` | `lego-75192-ucs-millennium-falcon-sealed` |
| Prediction contracts | venue slug / ticker | `will-bitcoin-dip-to-60k-by-december-31-2026-from-july-21`, `KXFEDDECISION-26DEC-H25` |
| GPU rental rates | `gpu-rental-<gpu>-<venue>` | `gpu-rental-h100-sxm-vast`, `gpu-rental-rtx-5090-runpod-secure` |

## Files

### `timeseries.csv` — master long-format file
`date,timestamp_utc,asset_id,asset_name,category,metric,value,unit,source`
One row per numeric value per asset per date; every number in the other files appears here. `metric` values
in use: `price`, `yield_pct`, `change_24h_pct`, `change_5d_pct`, `change_48h_bps`, `z_score_20d`, `volume`,
`vol_ratio_20d`, `vol_ratio_20d`, `high_30d`, `market_cap_usd`, `share_pct`, `stablecoin_mcap_usd`,
`funding_rate_pct`, `open_interest_usd`, `open_interest_contracts`, `probability`, `change_24h_pts`,
`volume_24h_usd`, `fmv_30d_usd`, `n_comps`, `rental_usd_per_gpu_hr`. `unit` is one of `usd`, `pct`, `bps`,
`sigma`, `ratio`, `index`, `rate`, `prob`, `pts`, `shares`, `count`, `contracts`, `usd_per_hr`,
`pct_per_8h`.

### `liquid.csv` — liquid-market snapshot
`date,timestamp_utc,asset_id,category,price,change_24h_pct,change_5d_pct,z_score_20d,volume,vol_ratio_20d,source`
- `z_score_20d` = (P_t − μ₂₀) / σ₂₀ over the trailing **20 daily closes including P_t**, population σ.
- `vol_ratio_20d` = latest bar volume ÷ mean volume of the trailing 20 bars. Empty for FX, yields and the VIX
  (no usable volume). Futures volume is thin and unreliable.
- `change_5d_pct` is measured over 5 trading sessions for exchange-traded assets and 5 calendar days for crypto.
- Rows with `source` ending in `backfill` are historical daily closes, not live snapshots.

### `macro.csv` — one row per date
`date,timestamp_utc,regime_status,us2y_pct,us10y_pct,us30y_pct,us10y_change_48h_bps,dxy,dxy_30d_high,vix,usdjpy,eurusd,btc_funding_pct,stablecoin_mcap_usd,risk_off_flag,sources`
- `regime_status` ∈ `RISK-ON | NEUTRAL | RISK-OFF | VOLATILITY EXPANSION`; `risk_off_flag` is `true`/`false`.
- `us10y_change_48h_bps` = change in the 10Y par yield over the prior **two trading sessions** (holidays skipped).
- `btc_funding_pct` is the current realized 8-hour perpetual funding rate (OKX BTC-USDT-SWAP) in percent.

### `crypto_derivatives.csv`
`date,timestamp_utc,asset_id,venue,funding_rate_pct,open_interest_usd,source`
`funding_rate_pct` is per funding interval (8h on OKX and Deribit), in percent.

### `prediction.csv`
`date,timestamp_utc,market_id,question,venue,probability,change_24h_pts,volume_usd,url`
`probability` 0–1 (Polymarket outcome price / Kalshi last trade). `change_24h_pts` is in percentage points
(Polymarket: CLOB price history 24h back; Kalshi: `previous_price`). `volume_usd` is 24-hour volume.

### `illiquid.csv`
`date,timestamp_utc,item_id,category,grade_condition,live_ask_usd,shipping_usd,fmv_30d_usd,n_comps,net_spread_pct,status,url`
- `fmv_30d_usd` = 10%-trimmed median of verified sold comps in the last 30 days; `n_comps` is how many
  comps it rests on. Needs ≥3 comps, else `status` = `LOW_LIQUIDITY_UNPRICED` (spec §7.4).
- `status` ∈ `PRICED` (30-day window) | `PRICED_90D_WINDOW` | `LOW_LIQUIDITY_UNPRICED`.
- `net_spread_pct` = FMV₃₀ × (1 − 0.13) − (ask + shipping), as a percent of (ask + shipping). Empty when no
  live ask could be verified.

### `comps.csv` — the evidence behind every FMV
`date_collected,item_id,sale_date,sale_price_usd,shipping_usd,grade_condition,platform,url`
Append-only; one row per verified sold comp, deduplicated by `url`.

### `alerts.csv`
`date,timestamp_utc,severity,asset_id,category,signal_type,value,threshold,details`
`severity` ∈ `high | medium | info`. `signal_type` is a stable slug, e.g. `risk_off_liquidity_warning`,
`z_score_breakout_near_trigger`, `probability_shift_24h`, `stablecoin_supply_contraction`,
`thirty_day_high_breakout`, `no_live_ask_data`, `arbitrage_deal`.

### `prices_wide.csv` — spreadsheet-friendly
Regenerated each run from `timeseries.csv`: one row per date, one column per `asset_id` holding `price`
(or `yield_pct` for yields), plus `<item_id>_fmv` for each illiquid item's `fmv_30d_usd`. Easiest file for
line charts; do not hand-edit.

### `*_v1.csv`
Superseded first-run files kept for reference (`liquid_v1.csv`, `illiquid_v1.csv`, `prediction_v1.csv`).
They used a `date_utc` header and the UTC run date; the current files use `date` + `timestamp_utc`.

### `watchlist.md`
The illiquid watchlist: up to 5 items per category, each with the permanent `item_id` slug used above.
IDs are never renamed or reused; at most two items may be swapped per run, with the reason logged.
