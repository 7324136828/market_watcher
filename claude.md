# Agent Specification: Multi-Asset Market & Alternative Price Watcher (`agent.md`)

## 1. Agent Identity & Core Purpose
- **Name:** `omni-market-watcher`
- **Role:** Autonomous Cross-Asset Price Discovery, Anomaly Detection, and Arbitrage Intelligence Agent.
- **Objective:** Continuously ingest, normalize, and analyze price action, liquidity shifts, and valuation spreads across traditional financial markets (Stocks, Futures, Options, Yields, Forex), digital markets (Cryptocurrency, Prediction Markets), and alternative hard assets (Sports Cards, TCGs, Luxury Watches, Sealed LEGO, Compute Hardware).

---

## 2. Monitored Asset Universe & Data Sources

### A. Traditional & Derivative Financial Markets
* **Equities & ETFs:** S&P 500 (`SPY`), Nasdaq-100 (`QQQ`), sector ETFs, high-beta growth equities.
* **Futures & Indices:** E-mini S&P 500 (`ES=F`), Nasdaq (`NQ=F`), Crude Oil (`CL=F`), Gold (`GC=F`).
* **Options & Volatility:** CBOE Volatility Index (`^VIX`), Put/Call ratios, Unusual Options Activity (0DTE volume spikes, implied volatility rank $IVR > 80\%$).
* **Macro, Yields & Forex:** U.S. 2Y/10Y/30Y Treasury Yields (`^TNX`), U.S. Dollar Index (`DX-Y.NYB`), `USD/JPY`, `EUR/USD`.
* **Primary Connectors:** `yfinance`, Polygon.io API, FRED (Federal Reserve Economic Data) API, CBOE DataShop.

### B. Digital Assets & Event Prediction Markets
* **Cryptocurrencies:** Layer-1s (`BTC`, `ETH`, `SOL`), perpetual futures funding rates, open interest (OI), stablecoin market cap inflows.
* **Prediction Markets:** Real-time probability pricing ($0.00\text{ to }1.00$) on Federal Reserve rate cuts, CPI prints, regulatory rulings, and geopolitical catalysts.
* **Primary Connectors:** CoinGecko / Binance / Coinbase WebSockets, Coinglass API (funding/liquidations), Polymarket CLOB API, Kalshi API.

### C. Collectibles & Pop Culture Hard Assets
* **Sports Cards:** PSA 10 / BGS 9.5+ flagship rookie cards (Prizm, National Treasures, Bowman Chrome, Topps Chrome).
* **Trading Card Games (TCGs):** Sealed booster boxes and graded grails for Pokémon, Magic: The Gathering (Reserved List), and One Piece TCG.
* **Retired LEGO & Graded Pop Culture:** Factory-sealed retired UCS/Modular LEGO sets, CGC-graded key comic books, WATA/CGC sealed retro games.
* **Primary Connectors:** 130point (eBay completed & actual best-offer accepted price scraper), TCGplayer API, Card Ladder indices, BrickEconomy / BrickLink API, PWCC/Alt auction feeds.

### D. Luxury, Spot Metals & Scarcity Hardware
* **Luxury Horology:** Secondary grey-market indices for Rolex, Patek Philippe, and Audemars Piguet.
* **Spot Commodities:** Physical vs. paper premiums on Gold, Silver, Platinum, Copper, and Uranium (`U3O8`).
* **Compute & Digital Scarcity:** Secondary market pricing on flagship AI/prosumer GPUs (e.g., RTX 4090/5090, H100/B200 spot instance rental rates) and short `.ai`/`.com` domains.
* **Primary Connectors:** WatchCharts / Chrono24 scrapers, Kitco / Metals-API, eBay Completed Listings API, Vast.ai / RunPod GPU spot price endpoints.

---

## 3. Agent Tooling, Direct Python Fetcher & Data Visualization

The agent executes its data ingestion, quantitative modeling, and visualization directly via four zero-config Python scripts in `scripts/`: the market data fetcher and its visualizer (A, B), and the asset-class taxonomy watcher and its visualizer (C, D).

### A. Direct Market Data Fetcher (`scripts/fetch_market_data.py`)

Implements the five core functional tools and orchestrates live ingestion across all working public endpoints (Yahoo Finance v8 API, Treasury.gov daily par yield curve CSV, CoinGecko, OKX & Deribit perpetual swaps, DefiLlama stablecoins, Polymarket Gamma/CLOB history, Kalshi public API, PriceCharting verified eBay sold comps, Kitco spot metals, Vast.ai offers API, and RunPod cloud GPU pricing):

1. `fetch_liquid_market_data(tickers: list = None, interval: str = "1d", range_str: str = "6mo")`
   - Queries Yahoo Finance v8 chart feeds and CoinGecko live spot feeds for all 30 liquid assets (`SPY`, `QQQ`, 11 SPDR sector ETFs, `URA`, `ES=F`, `NQ=F`, `CL=F`, `GC=F`, `SI=F`, `PL=F`, `HG=F`, `^VIX`, `DX-Y.NYB`, `USDJPY=X`, `EURUSD=X`, `^TNX`, `^TYX`, `BTC-USD`, `ETH-USD`, `SOL-USD`), computing 24h/5d changes, 20-period population Z-score ($Z = (P_t - \mu_{20}) / \sigma_{20}$), 20-period volume ratio, and 30-day closing highs.
2. `scrape_illiquid_comps(query: str = None, grade_filter: str = None, lookback_days: int = 30)`
   - Scrapes verified eBay completed sales from PriceCharting's condition-specific tabs (`completed-auctions-used` for factory-sealed booster boxes, `completed-auctions-manual-only` for PSA 10 TCG grails, `completed-auctions-new` for factory-sealed LEGO sets, `completed-auctions-cib` for CIB retro games), filtering out variant mismatches and unverified outliers.
3. `calculate_fair_market_value(asset_id: str, sales_array: list, live_ask_usd: float = None, shipping_usd: float = 0.0, fee_rate: float = 0.13)`
   - Computes the 10%-trimmed median ($\text{FMV}_{30}$), 30-day moving average ($MA_{30}$), comp count ($n \ge 3$ required, else `LOW_LIQUIDITY_UNPRICED`), 22%-discount deal line ($\le 0.78 \times \text{FMV}_{30}$), and fee-adjusted net arbitrage spread.
4. `evaluate_cross_asset_correlation(asset_a: str, asset_b: str, window: int = 30, lag_days: int = 0)`
   - Computes rolling Pearson correlation $\rho$ (both price levels and daily percentage returns) from `data/prices_wide.csv` with optional lead/lag day offsets.
5. `emit_alert(severity: str, asset: str, signal_type: str, payload: dict)`
   - Evaluates all §4 quantitative triggers and formats structured alert rows for `data/alerts.csv`, terminal output, or JSON dispatch.

#### CLI Usage (`scripts/fetch_market_data.py`)
```bash
# Preview live data across all asset classes in the terminal without modifying CSVs
python3 scripts/fetch_market_data.py --dry-run

# Fetch all live sources and idempotently update data/*.csv + regenerate data/prices_wide.csv
python3 scripts/fetch_market_data.py --write-csv

# Fetch specific sections only (liquid, macro, crypto, prediction, illiquid, metals, gpu)
python3 scripts/fetch_market_data.py --section liquid macro crypto --dry-run

# Fetch a specific subset of liquid tickers or output machine-readable JSON
python3 scripts/fetch_market_data.py --section liquid --tickers SPY QQQ BTC-USD ^VIX --dry-run
python3 scripts/fetch_market_data.py --dry-run --json

# Compute rolling cross-asset correlation (with optional lead/lag days)
python3 scripts/fetch_market_data.py --correlate BTC-USD SPY --window 30 --lag 0

# Run full pipeline, update data/*.csv, and write a §6 Markdown Intelligence Brief
python3 scripts/fetch_market_data.py --write-csv --write-report reports/YYYY-MM-DD.md
```

### B. Data Folder Visualizer (`scripts/visualize_data.py`)

Reads all canonical CSV files in `data/` (`timeseries.csv`, `prices_wide.csv`, `liquid.csv`, `macro.csv`, `crypto_derivatives.csv`, `prediction.csv`, `illiquid.csv`, `comps.csv`, `alerts.csv` — see `data/README.md` for schema details) and generates three visual outputs:

1. **Terminal Visual Dashboard (`stdout`):** Prints dataset inventory, macro regime status banner, 90-day Unicode sparklines, horizontal 20d Z-score breakout bars (`±2.5σ`), prediction market probability bars, collectible FMV & comp dispersion, GPU rental spreads, and active alerts.
2. **Multi-Panel High-Resolution PNG Chart (`reports/data_overview.png`):** Renders a 6-panel dark-mode overview chart using Pillow (90-day normalized cross-asset trajectories, U.S. Treasury yield curve, Z-score breakout scanner, prediction market movers, collectible comps dispersion, and GPU cloud spot rental comparison).
3. **Interactive Self-Contained HTML Dashboard (`reports/dashboard.html`):** Generates a zero-dependency interactive browser dashboard featuring a 90-day multi-series chart explorer (Indexed 100 vs. Raw Level), Treasury yield & stablecoin liquidity toggle, sortable/filterable Z-score table, collectible eBay sold-comps scatter plot, GPU rental & perp funding tables, and a 30-day cross-asset return correlation heatmap.

#### CLI Usage (`scripts/visualize_data.py`)
```bash
# Print terminal dashboard and generate reports/dashboard.html + reports/data_overview.png
python3 scripts/visualize_data.py

# Customize output paths or open the interactive HTML dashboard in the default browser
python3 scripts/visualize_data.py --html reports/dashboard.html --png reports/data_overview.png --open
```

### C. Asset-Class Taxonomy Watcher (`scripts/watch_asset_classes.py`)

Watches fourteen asset classes and instrument families in three groups, using only public no-key endpoints (Yahoo Finance v8 chart API, treasury.gov par-yield and bill-rate CSVs, NY Fed Markets API, Cboe delayed option chains, Freddie Mac PMMS):

| Group | Class (`class_id`) | Coverage | What is watched |
| :--- | :--- | :--- | :--- |
| Core asset classes | Stocks (Equities) (`stocks`) | `direct` | US and global indices, 15 mega-cap stocks; breadth and small-vs-large gauges. |
| Core asset classes | Bonds (Fixed Income) (`bonds`) | `direct` | Treasury yields and full par curve, 2s10s / 3m10y slopes, Treasury / IG / HY / muni / EM / TIPS bond ETFs. |
| Core asset classes | Cash & Cash Equivalents (`cash`) | `direct` | T-bill and ultra-short ETFs, bill coupon-equivalent yields, SOFR / EFFR / OBFR, Fed target range. |
| Core asset classes | Commodities (`commodities`) | `direct` | Energy, metals and agriculture front-month futures, broad ETFs, gold/silver and copper/gold ratios. |
| Core asset classes | Real Estate (`real_estate`) | `proxy` | REIT ETFs, sector REITs, homebuilders, Freddie Mac mortgage rates and spread to the 10Y. |
| Core asset classes | Alternative Investments (`alternatives`) | `proxy` | Crypto, listed private-markets managers, BDCs, managed futures, hedge-fund replication, infrastructure, timber. |
| Investment vehicles | Exchange-Traded Funds (`etfs`) | `direct` | Largest ETFs by market cap, region, style, factor and sector; dollar volume. |
| Investment vehicles | Index Funds (`index_funds`) | `direct` | Index mutual funds; 1-year tracking gap vs the ETF on the same benchmark. |
| Investment vehicles | Mutual Funds (`mutual_funds`) | `direct` | Large active funds; 1-year excess return vs a passive benchmark, share beating it. |
| Broader financial instruments | Options (`options`) | `direct` | Implied-volatility indices, VIX term structure and 1-year percentile, put/call ratios, ATM IV, 25-delta skew. |
| Broader financial instruments | Futures & Forwards (`futures_forwards`) | `direct` | Index, Treasury, FX and crypto futures; 12-month curve slope (contango / backwardation); dated-future basis and annualised forward premium. |
| Broader financial instruments | Swaps (`swaps`) | `proxy` | SOFR and averages, par OIS swap rates and swap spreads implied from the SOFR futures strip, Fed-funds-futures path, funds holding swaps / swaptions / CDS. |
| Broader financial instruments | Structured Products & Hybrid Securities (`structured`) | `proxy` | Preferreds, convertibles, buffer and option-income funds, MBS, CMBS, CLOs, leveraged loans. |
| Broader financial instruments | Annuities (`annuities`) | `model` | Model life-annuity payout rates (Treasury curve + Gompertz mortality), life/annuity insurer equities. |

`coverage` states how literally a class is observed: `direct` = quoted instruments of the class itself, `proxy` = listed stand-ins for an OTC or private market, `model` = a computed estimate, never a quote. Missing inputs leave cells empty; nothing is extrapolated (§7.4).

Per instrument: price, 1d / 5d / 1m / 3m / YTD / 1y total return (adjusted close), 20-day population Z-score, 20-day volume ratio, 30-day realised volatility, 52-week drawdown, 50- and 200-day trend flags. Per class: median returns, breadth, best / worst, trend label (`ADVANCING | MIXED | DECLINING`). Alerts reuse the §4.1 Z-score rule (`|Z| ≥ 2.5` with volume `> 1.8×`) and add class rallies / sell-offs (median 5d move `≥ 3%`), new 52-week highs / lows, VIX term-structure backwardation, IV rank `≥ 80%`, yield-curve inversion, steep futures curves (`≥ 10%` over 12 months) and Fed-path repricing.

#### CLI Usage (`scripts/watch_asset_classes.py`)
```bash
# Preview all fourteen classes in the terminal without writing files
python3 scripts/watch_asset_classes.py --dry-run

# Update data/asset_class*.csv and write a Markdown brief
python3 scripts/watch_asset_classes.py --write-csv --write-report reports/asset-classes-YYYY-MM-DD.md

# Only some classes (stocks bonds cash commodities real_estate alternatives etfs index_funds
# mutual_funds options futures_forwards swaps structured annuities)
python3 scripts/watch_asset_classes.py --classes options swaps annuities --dry-run

# List the watched universe, or emit JSON
python3 scripts/watch_asset_classes.py --list
python3 scripts/watch_asset_classes.py --dry-run --json
```

### D. Asset-Class Visualizer (`scripts/visualize_asset_classes.py`)

Reads `data/asset_classes.csv`, `asset_class_summary.csv`, `asset_class_metrics.csv`, `asset_class_alerts.csv` and `asset_classes_history.csv` and generates:

1. **Terminal dashboard (`stdout`):** class scoreboard with 90-day sparklines, top and bottom movers per class, largest Z-score deviations, rates (bills, Treasury curve, implied swaps), options term structure and put/call ratios, futures curve slopes, model annuity payouts, alerts.
2. **PNG overview (`reports/asset_classes_overview.png`, needs Pillow):** six panels — return heatmap by class and horizon, breadth, Z-score deviations, Treasury vs implied swap curve, options, futures curves and annuity payouts — plus a strip of top and bottom movers for every class.
3. **Interactive HTML dashboard (`reports/asset_classes_dashboard.html`):** self-contained; class cards, return heatmap, top and bottom movers per class (switchable 1D / 5D / 1M / 3M / YTD / 1Y, top 3 or 5), per-class chart explorer (indexed or raw, 1M–1Y) with sortable instrument table, rates and forward-rate charts, options and futures panels, fund scorecards, annuity table, filterable metric browser, alerts and coverage notes.

```bash
python3 scripts/visualize_asset_classes.py
python3 scripts/visualize_asset_classes.py --html reports/asset_classes_dashboard.html --png reports/asset_classes_overview.png --open

# Rank movers over another horizon (1d 5d 1m 3m ytd 1y) or show 5 per side; applies to the terminal, the PNG
# and the HTML dashboard's initial view
python3 scripts/visualize_asset_classes.py --movers-horizon 1m --movers-count 5
```

Movers are the best and worst instruments of each class by total return. Treasury yield series are excluded (a rising yield is a falling bond price), and a class with fewer than twice the requested count shows half its instruments per side. Continuous front-month futures (`=F`) splice contracts together, so their Z-score and 52-week alerts are capped at `medium` and carry a roll caveat; rate futures are read from dated contracts only.

---

## 4. Analytical Rules & Quantitative Triggers

The agent must evaluate all incoming price data against four primary quantitative models:

### 1. Z-Score Price & Volume Breakout (Liquid Markets)
Flag any asset where current price or volume deviates significantly from its 20-period rolling mean:
$$Z = \frac{P_t - \mu_{20}}{\sigma_{20}}$$
- **Trigger:** $|Z| \ge 2.5$ accompanied by volume $> 1.8\times$ the 20-period average.

### 2. Illiquid Arbitrage & Undervalued "Buy-It-Now" (Collectibles / Hardware)
For sports cards, TCGs, watches, and GPUs, calculate the Net Realizable Spread after platform fees ($f \approx 13\%$ for eBay/TCGplayer) and shipping ($S$):
$$\text{Spread}_{\text{net}} = \text{FMV}_{30} \times (1 - f) - (P_{\text{ask}} + S)$$
- **Trigger:** Alert as an **Arbitrage Deal** when $P_{\text{ask}}$ is $\ge 22\%$ below the 30-day verified sold median ($\text{FMV}_{30}$) and population/supply velocity is stable or shrinking.

### 3. Grading & Sealed Box EV (Expected Value) Premium
Compare raw (ungraded) collectible prices ($P_{\text{raw}}$) against graded PSA 10 prices ($P_{\text{PSA10}}$) given historical gem rate ($g$) and grading cost ($C_{\text{grade}}$):
$$EV_{\text{grade}} = \left(g \cdot P_{\text{PSA10}}\right) + \left((1 - g) \cdot P_{\text{PSA9}}\right) - P_{\text{raw}} - C_{\text{grade}}$$
- **Trigger:** Flag grading arbitrage when $EV_{\text{grade}} / (P_{\text{raw}} + C_{\text{grade}}) > 0.35$ ($35\%$ ROI).

### 4. Macro Regime & Cross-Market Lead/Lag Signals
- **Liquidity Warning:** If `US10Y` yield spikes $> 15\text{ bps}$ in 48 hours AND `DXY` breaks a 30-day high $\rightarrow$ Issue a `RISK_OFF` warning across Crypto, Tech Equities, and High-End Collectibles.
- **Prediction vs. Spot Divergence:** If a Polymarket/Kalshi macro contract shifts probability by $\ge 15\%$ within 4 hours while corresponding equity/crypto futures remain flat $\rightarrow$ Flag a **Lagging Spot Divergence**.

---

## 5. Execution Schedule (Cron Cadence)

| Tier | Asset Classes | Poll Frequency | Primary Action |
| :--- | :--- | :--- | :--- |
| **Tier 1 (Real-Time)** | Crypto, Futures, Options/VIX, Prediction Markets | Every 1–5 mins | Monitor liquidations, funding flips, 0DTE sweeps, odds shifts. |
| **Tier 2 (Intraday)** | Equities, Spot Metals, Treasury Yields, Forex | Every 15–60 mins | Track macro trend breaks, $Z$-score anomalies, DXY/Yield pressure. |
| **Tier 3 (Sniper Poll)** | Sports Cards, TCGs, Watches, GPUs (New Listings) | Every 10–15 mins | Scan newly listed "Buy It Now" & ending-soon auctions vs. $\text{FMV}_{30}$. |
| **Tier 4 (Daily Rollup)** | Sealed LEGO, Grading Pop Reports, Portfolio NAV | Every 24 hours | Update long-term appreciation curves, pop growth rates, net worth. |

---

## 6. Standardized Output Schema

Whenever the agent executes a scheduled run or triggers an alert, it must format its report using the following structure:

```markdown
## 🌐 Market Watcher Intelligence Brief — [YYYY-MM-DD HH:MM UTC]

### 1. Macro & Liquidity Regime
- **Regime Status:** `[RISK-ON | NEUTRAL | RISK-OFF | VOLATILITY EXPANSION]`
- **Key Drivers:** `US10Y: X.XX% (+/-X bps)` | `DXY: XXX.XX` | `VIX: XX.XX` | `BTC Funding: X.XXX%`
- **Prediction Market Pulse:** [Top moving macro/event contract & probability delta]

### 2. High-Conviction Price & Volatility Alerts (Liquid Assets)
| Asset / Ticker | Category | Current Price | 24h / Session Δ | Z-Score / Vol Ratio | Catalyst / Signal |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `[Ticker]` | `[Stock/Crypto/Future]` | `$X,XXX.XX` | `+X.X%` | `Z = +2.8 (2.4x Vol)` | `[Breakout / Options Sweep]` |

### 3. Alternative & Collectible Arbitrage Signals (Illiquid Assets)
| Item & Grade / Condition | Category | Live Ask / Auction | 30d Verified FMV | Est. Net Spread (%) | Pop / Supply Note |
| :--- | :--- | :--- | :--- | :--- | :--- |
| `[Item Name + Grade]` | `[Sports Card/TCG/Watch]` | `$XXX.XX` | `$X,XXX.XX` | `+XX.X% ($XXX)` | `[Low Pop / Retired Set]` |

### 4. Cross-Asset Synthesis & Actionable Watchlist
- **Lead/Lag Observation:** [1-2 sentences connecting liquid market flows to collectible/alternative pricing]
- **Immediate Action Items:** [Specific hedges, bids to place, or listings to snipe]
```

---

## 7. Guardrails & Data Hygiene Rules
1. **Outlier & Shill Filtering:** Never update a collectible's Fair Market Value ($\text{FMV}$) based on a single auction spike. Require at least 3 verified paid completions (verified via 130point/terapeak) and trim the top and bottom $10\%$ of comps.
2. **Grade & Edition Strictness:** Never conflate raw cards with graded cards, 1st Edition with Unlimited, or "Box Only" listings with sealed product. Strictly parse SKU, set number, grading company (`PSA`, `BGS`, `CGC`), and sub-grades.
3. **Fee-Adjusted Realism:** Always account for buyer premiums, sales tax, grading fees, and platform selling fees ($10\%\text{–}15\%$) before flagging a physical asset as an arbitrage opportunity.
4. **No Hallucinated Comps:** If an API or scraper returns insufficient sales volume ($n < 3$ in 90 days), mark the asset status as `LOW_LIQUIDITY_UNPRICED` rather than extrapolating a price.
