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

## Asset-class watch files (`scripts/watch_asset_classes.py`)

Same conventions as above. `class_id` is the stable slug of an asset class: `stocks`, `bonds`, `cash`,
`commodities`, `real_estate`, `alternatives`, `etfs`, `index_funds`, `mutual_funds`, `options`,
`futures_forwards`, `swaps`, `structured`, `annuities`. `group` is one of `Core asset classes`,
`Investment vehicles`, `Broader financial instruments`. A ticker may appear under more than one class.

### `asset_classes.csv` — instrument snapshot
`date,timestamp_utc,group,class_id,asset_class,sub_class,asset_id,asset_name,instrument_type,unit,price,change_1d_abs,change_1d_pct,change_5d_pct,change_1m_pct,change_3m_pct,change_ytd_pct,change_1y_pct,z_score_20d,volume,vol_ratio_20d,realized_vol_30d_pct,drawdown_52w_pct,above_50dma,above_200dma,last_bar_date,source`
- `price` is the latest raw close; `unit` says what it is: `usd`, `pct` (a yield), `index`, `fx`.
- `change_*_pct` are **total returns** from dividend-adjusted closes over 1 / 5 / 21 / 63 sessions, since the
  last close of the prior year, and over the trailing year. `change_1d_abs` is in price units (for a yield,
  percentage points).
- `z_score_20d` and `vol_ratio_20d` are defined as in `liquid.csv`. `realized_vol_30d_pct` = population σ of
  the last 30 daily log returns × √252. `drawdown_52w_pct` = latest close vs the trailing-year closing high.
- `above_50dma` / `above_200dma` are `true` / `false`, empty when there is too little history.

### `asset_class_summary.csv` — one row per class per date
`date,timestamp_utc,group,class_id,asset_class,coverage,n_instruments,median_change_1d_pct,median_change_5d_pct,median_change_1m_pct,median_change_3m_pct,median_change_ytd_pct,median_change_1y_pct,pct_up_1d,pct_above_50dma,median_realized_vol_30d_pct,best_1d_id,best_1d_pct,worst_1d_id,worst_1d_pct,max_abs_z_id,max_abs_z,trend`
- `coverage` ∈ `direct` (quoted instruments of the class) | `proxy` (listed stand-ins for an OTC or private
  market) | `model` (computed estimate, not a quote).
- `trend` ∈ `ADVANCING` (median 1m ≥ +2% and ≥ 60% above the 50-day average) | `DECLINING` (≤ −2% and ≤ 40%)
  | `MIXED` | `UNRATED`. For `options` the instruments are volatility indices, so the labels are
  `VOL RISING` / `VOL FALLING`.

### `asset_class_metrics.csv` — class-specific derived metrics (long format)
`date,timestamp_utc,class_id,asset_class,metric_id,metric_name,value,unit,source,note`
`unit` ∈ `pct`, `bps`, `pts` (percentage points), `ratio`, `usd`, `contracts`, `count`. Stable `metric_id`
families: `ust_par_<tenor>`, `curve_2s10s_bps`, `tbill_<n>w_yield_pct`, `sofr_pct`, `effr_pct`,
`mortgage_30y_pct`, `tracking_gap_1y_<fund>_pts`, `excess_return_1y_<fund>_pts`, `vix_term_ratio`,
`vix_percentile_1y_pct`, `put_call_volume_ratio_<sym>`, `put_call_oi_ratio_<sym>`, `iv30_<sym>_pct`,
`atm_iv_<sym>_pct`, `skew_25d_<sym>_pts`, `term_structure_12m_<root>_pct`, `basis_<root>_pct`,
`implied_carry_<root>_pct`, `sofr_fwd_3m_<yyyy_mm>_pct`, `sofr_swap_implied_<n>y_pct`,
`swap_spread_<n>y_bps`, `fed_funds_implied_<n>m_pct`, `fed_cuts_priced_12m`, `spia_payout_age<nn>_pct`,
`spia_monthly_income_age<nn>_usd`.
- Model metrics say so in `source` / `note`: implied swap rates come from the SOFR futures strip with no
  convexity adjustment; annuity payouts are actuarially fair values from the Treasury par curve and a
  Gompertz mortality law (m = 88, b = 10), before insurer loads — neither is a market quote.

### `asset_class_alerts.csv`
`date,timestamp_utc,severity,asset_id,asset_class,signal_type,value,threshold,details`
`signal_type` slugs: `z_score_volume_breakout`, `z_score_extreme`, `new_52w_high`, `new_52w_low`,
`class_rally`, `class_selloff`, `vix_term_backwardation`, `iv_rank_high`, `yield_curve_inverted`,
`steep_contango`, `steep_backwardation`, `fed_path_repricing`. Only a full run (all classes) replaces a
date's alerts.

### `asset_classes_history.csv` — wide price history
`date,<asset_id>,…` — one column per watched instrument, dividend-adjusted daily closes for the trailing
year. Regenerated on every run (not append-only); empty where an instrument did not trade that day.
