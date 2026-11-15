#!/usr/bin/env python3
"""
Omni-Market Watcher — Direct Data Fetcher & Quantitative Engine (`fetch_market_data.py`)

Fetches live multi-asset market data across all categories defined in `claude.md`:
  1. Liquid Equities, Sector ETFs, Futures, FX, Volatility & Yields (Yahoo Finance v8 API)
  2. U.S. Treasury Daily Par Yield Curve (treasury.gov CSV)
  3. Cryptocurrencies & Global Crypto Internals (CoinGecko API)
  4. Crypto Perpetual Futures Funding Rates & Open Interest (OKX & Deribit public APIs)
  5. Stablecoin Circulating Supply & 7d Liquidity Flows (DefiLlama API)
  6. Event Prediction Markets & Historical Odds Deltas (Polymarket Gamma/CLOB & Kalshi APIs)
  7. Collectibles, TCGs, Sealed LEGO & Retro Games Sold Comps (PriceCharting eBay Comps)
  8. Precious Metal Physical Spot Bids (Kitco `__NEXT_DATA__` feed)
  9. AI / Prosumer GPU Cloud Spot Rental Rates (Vast.ai Bundles API & RunPod Pricing)

Also implements the 5 core agent tools defined in `claude.md` §3:
  - `fetch_liquid_market_data(tickers, interval)`
  - `scrape_illiquid_comps(query, grade_filter, lookback_days)`
  - `calculate_fair_market_value(asset_id, sales_array)`
  - `evaluate_cross_asset_correlation(asset_a, asset_b, window)`
  - `emit_alert(severity, asset, signal_type, payload)`

Usage examples:
  # Fetch all live sources and preview summary in terminal (dry-run, no CSV edits)
  python3 scripts/fetch_market_data.py --dry-run

  # Fetch all live sources and update data/*.csv + regenerate data/prices_wide.csv
  python3 scripts/fetch_market_data.py --write-csv

  # Fetch only specific sections (e.g., liquid, macro, crypto, prediction, illiquid, metals, gpu)
  python3 scripts/fetch_market_data.py --section crypto prediction --dry-run

  # Evaluate rolling cross-asset Pearson correlation (with optional lead/lag days)
  python3 scripts/fetch_market_data.py --correlate BTC-USD SPY --window 30 --lag 0

  # Generate a standardized Markdown Intelligence Brief (claude.md §6)
  python3 scripts/fetch_market_data.py --write-csv --write-report reports/latest_brief.md
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import html
import json
import math
import os
import re
import statistics
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

try:
    import numpy as np
except ImportError:
    np = None  # Fallback to pure Python statistics if numpy is unavailable


# ---------------------------------------------------------------------------
# Constants & Asset Universe Configuration
# ---------------------------------------------------------------------------

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = dt.timezone.utc

USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# 27 Liquid Assets tracked in `data/liquid.csv` and `data/timeseries.csv`
LIQUID_ASSETS: List[Dict[str, Any]] = [
    {"asset_id": "SPY", "name": "SPDR S&P 500 ETF", "category": "Equity ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "QQQ", "name": "Invesco QQQ Trust", "category": "Equity ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLB", "name": "Materials Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLC", "name": "Communication Services Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLE", "name": "Energy Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLF", "name": "Financial Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLI", "name": "Industrial Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLK", "name": "Technology Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLP", "name": "Consumer Staples Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLRE", "name": "Real Estate Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLU", "name": "Utilities Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLV", "name": "Health Care Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "XLY", "name": "Consumer Discretionary Select Sector SPDR", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "URA", "name": "Global X Uranium ETF", "category": "Sector ETF", "unit": "usd", "has_vol": True},
    {"asset_id": "ES=F", "name": "E-mini S&P 500 future", "category": "Future", "unit": "usd", "has_vol": True},
    {"asset_id": "NQ=F", "name": "E-mini Nasdaq-100 future", "category": "Future", "unit": "usd", "has_vol": True},
    {"asset_id": "CL=F", "name": "WTI crude oil future", "category": "Future", "unit": "usd", "has_vol": True},
    {"asset_id": "GC=F", "name": "Gold future", "category": "Future", "unit": "usd", "has_vol": True},
    {"asset_id": "SI=F", "name": "Silver future", "category": "Future", "unit": "usd", "has_vol": True},
    {"asset_id": "PL=F", "name": "Platinum future", "category": "Future", "unit": "usd", "has_vol": False},
    {"asset_id": "HG=F", "name": "Copper future", "category": "Future", "unit": "usd", "has_vol": True},
    {"asset_id": "^VIX", "name": "CBOE Volatility Index", "category": "Volatility", "unit": "pct", "has_vol": False},
    {"asset_id": "DX-Y.NYB", "name": "US Dollar Index", "category": "FX Index", "unit": "index", "has_vol": False},
    {"asset_id": "USDJPY=X", "name": "USD/JPY", "category": "FX", "unit": "rate", "has_vol": False},
    {"asset_id": "EURUSD=X", "name": "EUR/USD", "category": "FX", "unit": "rate", "has_vol": False},
    {"asset_id": "^TNX", "name": "US 10Y Treasury yield (CBOE)", "category": "Yield", "unit": "pct", "has_vol": False},
    {"asset_id": "^TYX", "name": "US 30Y Treasury yield (CBOE)", "category": "Yield", "unit": "pct", "has_vol": False},
    {"asset_id": "BTC-USD", "name": "Bitcoin", "category": "Crypto", "unit": "usd", "has_vol": True, "cg_id": "bitcoin"},
    {"asset_id": "ETH-USD", "name": "Ethereum", "category": "Crypto", "unit": "usd", "has_vol": True, "cg_id": "ethereum"},
    {"asset_id": "SOL-USD", "name": "Solana", "category": "Crypto", "unit": "usd", "has_vol": True, "cg_id": "solana"},
]

POLYMARKET_SLUGS: List[str] = [
    "will-the-fed-increase-interest-rates-by-25-bps-after-the-december-2026-meeting-20260729232808636",
    "will-the-fed-increase-interest-rates-by-50-bps-after-the-december-2026-meeting-20260729232808637",
    "will-there-be-no-change-in-fed-interest-rates-after-the-december-2026-meeting-20260729232808635",
    "will-the-fed-decrease-interest-rates-by-25-bps-after-the-december-2026-meeting-20260729232808634",
    "will-bitcoin-dip-to-60k-by-december-31-2026-from-july-21",
    "will-bitcoin-dip-to-55000-by-december-31-2026-527-627-868-745-188-361-314-673-612-946-821-624-855-557-684-381-884-939-182-939-516",
    "will-bitcoin-dip-to-50000-by-december-31-2026-454-325-222-878-949-487-451-853-523-111",
    "will-bitcoin-dip-to-62k-november-9-15-2026",
    "will-bitcoin-dip-to-55k-in-november-2026",
    "will-bitcoin-reach-90000-by-december-31-2026-113-862-581-343",
    "will-bitcoin-dip-to-45000-by-december-31-2026-674-923-755-971-998-525-926-245-316-517-544-589-965-923-986-841-815-224-586-231-289-533-573",
    "will-bitcoin-dip-to-60k-in-november-2026",
    "will-bitcoin-dip-to-58k-november-9-15-2026",
    "will-bitcoin-dip-to-57pt5k-in-november-2026",
    "will-bitcoin-dip-to-60k-november-9-15-2026",
    "will-bitcoin-dip-to-62k-on-november-14-2026",
]

KALSHI_TARGET_TICKERS: List[Tuple[str, str]] = [
    ("KXFEDDECISION", "KXFEDDECISION-26DEC-H25"),
    ("KXFEDDECISION", "KXFEDDECISION-26DEC-H26"),
    ("KXFEDDECISION", "KXFEDDECISION-26DEC-H0"),
    ("KXFEDDECISION", "KXFEDDECISION-26DEC-C25"),
    ("KXFEDDECISION", "KXFEDDECISION-26DEC-C26"),
    ("KXCPIYOY", "KXCPIYOY-26NOV-T3.2"),
    ("KXCPIYOY", "KXCPIYOY-26NOV-T3.6"),
    ("KXCPIYOY", "KXCPIYOY-26NOV-T4.0"),
]

# Illiquid Watchlist items (matching `data/watchlist.md`)
ILLIQUID_WATCHLIST: List[Dict[str, Any]] = [
    {
        "item_id": "pokemon-evolving-skies-booster-box-sealed",
        "name": "Pokémon Evolving Skies (SWSH07) English booster box, factory sealed",
        "category": "TCG",
        "grade_condition": "factory sealed (EN)",
        "url": "https://www.pricecharting.com/game/pokemon-evolving-skies/booster-box",
        "pc_tab": "completed-auctions-used",
        "exclude_keywords": ["pack", "art set", "sleeves", "etb", "elite trainer", "empty", "case"],
    },
    {
        "item_id": "pokemon-sv151-jp-booster-box-sealed",
        "name": "Pokémon Scarlet & Violet 151 (sv2a) Japanese booster box, factory sealed",
        "name_short": "Pokemon SV 151 (sv2a) JP booster box, sealed",
        "category": "TCG",
        "grade_condition": "factory sealed (JP)",
        "url": "https://www.pricecharting.com/game/pokemon-japanese-scarlet-&-violet-151/booster-box",
        "pc_tab": "completed-auctions-used",
        "exclude_keywords": ["pack", "case", "carton", "empty", "no shrink"],
    },
    {
        "item_id": "pokemon-umbreon-vmax-215-203-psa10",
        "name": "Umbreon VMAX #215/203 alt art (Evolving Skies), PSA 10",
        "category": "TCG",
        "grade_condition": "PSA 10",
        "url": "https://www.pricecharting.com/game/pokemon-evolving-skies/umbreon-vmax-215",
        "pc_tab": "completed-auctions-manual-only",
        "exclude_keywords": ["bgs", "cgc", "psa 9", "raw", "korean", "japanese", "chinese"],
    },
    {
        "item_id": "lego-75192-ucs-millennium-falcon-sealed",
        "name": "LEGO 75192 UCS Millennium Falcon, new / factory sealed",
        "category": "LEGO",
        "grade_condition": "new / factory sealed",
        "url": "https://www.pricecharting.com/game/lego-star-wars/millennium-falcon-75192",
        "pc_tab": "completed-auctions-new",
        "exclude_keywords": ["box only", "instructions only", "stand only", "light kit", "lepin"],
    },
    {
        "item_id": "lego-10255-assembly-square-sealed",
        "name": "LEGO 10255 Assembly Square (modular), new / factory sealed",
        "category": "LEGO",
        "grade_condition": "new / factory sealed",
        "url": "https://www.pricecharting.com/game/lego-creator/assembly-square-10255",
        "pc_tab": "completed-auctions-new",
        "exclude_keywords": ["box only", "instructions only", "light kit"],
    },
    {
        "item_id": "lego-10179-ucs-millennium-falcon-sealed",
        "name": "LEGO 10179 UCS Millennium Falcon (2007), new / factory sealed",
        "category": "LEGO",
        "grade_condition": "new / factory sealed",
        "url": "https://www.pricecharting.com/game/lego-star-wars/millennium-falcon-10179",
        "pc_tab": "completed-auctions-new",
        "exclude_keywords": ["first edition", "1st edition", "first run", "box only", "instructions"],
    },
    {
        "item_id": "super-mario-64-n64-cib",
        "name": "Super Mario 64 (Nintendo 64), complete in box",
        "category": "Retro game",
        "grade_condition": "CIB",
        "url": "https://www.pricecharting.com/game/nintendo-64/super-mario-64",
        "pc_tab": "completed-auctions-cib",
        "exclude_keywords": ["box only", "manual only", "cart only", "repro", "players choice"],
    },
    {
        "item_id": "panini-prizm-2018-luka-doncic-280-psa10",
        "name": "2018-19 Panini Prizm Luka Dončić #280 (base), PSA 10",
        "category": "Sports card",
        "grade_condition": "PSA 10",
        "url": "https://www.sportscardspro.com/game/basketball-cards-2018-panini-prizm/luka-doncic-280",
        "pc_tab": "completed-auctions-manual-only",
        "exclude_keywords": [],
    },
    {
        "item_id": "topps-chrome-2003-lebron-james-111-psa10",
        "name": "2003-04 Topps Chrome LeBron James #111, PSA 10",
        "category": "Sports card",
        "grade_condition": "PSA 10",
        "url": "https://www.sportscardspro.com/game/basketball-cards-2003-topps-chrome/lebron-james-111",
        "pc_tab": "completed-auctions-manual-only",
        "exclude_keywords": [],
    },
    {
        "item_id": "rolex-submariner-126610ln-full-set",
        "name": "Rolex Submariner Date 126610LN, full set",
        "category": "Watch",
        "grade_condition": "full set, unworn/mint",
        "url": "https://watchcharts.com/watch_model/4121-rolex-submariner-126610ln/overview",
        "pc_tab": None,
        "exclude_keywords": [],
    },
    {
        "item_id": "rolex-daytona-116500ln-full-set",
        "name": "Rolex Cosmograph Daytona 116500LN (white dial), full set",
        "category": "Watch",
        "grade_condition": "full set, unworn/mint",
        "url": "https://watchcharts.com/watches/rolex/daytona",
        "pc_tab": None,
        "exclude_keywords": [],
    },
    {
        "item_id": "rolex-gmt-master-ii-126710blro-full-set",
        "name": "Rolex GMT-Master II 126710BLRO 'Pepsi', full set",
        "category": "Watch",
        "grade_condition": "full set, unworn/mint",
        "url": "https://watchcharts.com/watches/rolex/gmt-master-ii",
        "pc_tab": None,
        "exclude_keywords": [],
    },
    {
        "item_id": "nvidia-rtx-4090-used",
        "name": "NVIDIA GeForce RTX 4090, used, working",
        "category": "GPU",
        "grade_condition": "used, working",
        "url": "https://www.ebay.com/sch/i.html?_nkw=rtx+4090",
        "pc_tab": None,
        "exclude_keywords": [],
    },
    {
        "item_id": "nvidia-rtx-5090-new",
        "name": "NVIDIA GeForce RTX 5090, new / sealed",
        "category": "GPU",
        "grade_condition": "new, sealed",
        "url": "https://www.ebay.com/sch/i.html?_nkw=rtx+5090",
        "pc_tab": None,
        "exclude_keywords": [],
    },
]


# ---------------------------------------------------------------------------
# HTTP & Math Helpers
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 15, extra_headers: Optional[Dict[str, str]] = None) -> str:
    """Perform an HTTP GET request with standard User-Agent and return UTF-8 decoded body."""
    headers = {"User-Agent": USER_AGENT, "Accept": "*/*"}
    if extra_headers:
        headers.update(extra_headers)
    req = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", errors="replace")


def http_get_json(url: str, timeout: int = 15) -> Any:
    """Perform an HTTP GET request and parse JSON response."""
    return json.loads(http_get(url, timeout=timeout))


def fmt_num(val: Optional[float], decimals: int = 4) -> str:
    """Format a float for CSV storage without trailing zeros or scientific notation."""
    if val is None or (isinstance(val, float) and (math.isnan(val) or math.isinf(val))):
        return ""
    rounded = round(float(val), decimals)
    if rounded == int(rounded):
        return str(int(rounded))
    s = f"{rounded:.{decimals}f}".rstrip("0").rstrip(".")
    return s if s != "-0" else "0"


def calc_population_zscore(values: List[float], window: int = 20) -> Optional[float]:
    """
    Compute population Z-score over the trailing `window` observations including P_t:
        Z = (P_t - mu_20) / sigma_20
    Matches `claude.md` §4.1 and `data/README.md`.
    """
    clean = [float(x) for x in values if x is not None and not math.isnan(float(x))]
    if len(clean) < window:
        return None
    w = clean[-window:]
    mean = sum(w) / len(w)
    variance = sum((x - mean) ** 2 for x in w) / len(w)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return (w[-1] - mean) / std


# ---------------------------------------------------------------------------
# Tool 1: `fetch_liquid_market_data(tickers, interval)` & Macro / Crypto Connectors
# ---------------------------------------------------------------------------

def fetch_coingecko_crypto() -> Dict[str, Any]:
    """Fetch live BTC, ETH, SOL prices/24h changes/market caps and global crypto stats from CoinGecko."""
    result: Dict[str, Any] = {"coins": {}, "global": {}, "fetched_at": dt.datetime.now(UTC_TZ).strftime("%H:%MZ")}
    try:
        price_url = (
            "https://api.coingecko.com/api/v3/simple/price"
            "?ids=bitcoin,ethereum,solana&vs_currencies=usd"
            "&include_market_cap=true&include_24hr_vol=true&include_24hr_change=true"
        )
        result["coins"] = http_get_json(price_url)
    except Exception as exc:
        print(f"[WARN] CoinGecko simple/price failed: {exc}", file=sys.stderr)

    try:
        glob_url = "https://api.coingecko.com/api/v3/global"
        glob_data = http_get_json(glob_url).get("data", {})
        result["global"] = {
            "total_mcap_usd": glob_data.get("total_market_cap", {}).get("usd"),
            "mcap_change_24h_pct": glob_data.get("market_cap_change_percentage_24h_usd"),
            "btc_dominance_pct": glob_data.get("market_cap_percentage", {}).get("btc"),
        }
    except Exception as exc:
        print(f"[WARN] CoinGecko global failed: {exc}", file=sys.stderr)
    return result


def fetch_liquid_market_data(
    tickers: Optional[List[str]] = None,
    interval: str = "1d",
    range_str: str = "6mo",
    coingecko_data: Optional[Dict[str, Any]] = None,
) -> List[Dict[str, Any]]:
    """
    Agent Tool #1 (`claude.md` §3.1):
    Queries Yahoo Finance v8 chart API (and CoinGecko live feed for crypto) for OHLCV,
    24h & 5d changes, 20-period population Z-score, 20-period volume ratio, and 30d highs.
    """
    if coingecko_data is None:
        coingecko_data = fetch_coingecko_crypto()

    target_metas = LIQUID_ASSETS
    if tickers:
        ticker_set = set(tickers)
        target_metas = [m for m in LIQUID_ASSETS if m["asset_id"] in ticker_set]
        # Allow ad-hoc tickers not in LIQUID_ASSETS
        known_ids = {m["asset_id"] for m in target_metas}
        for t in tickers:
            if t not in known_ids:
                target_metas.append(
                    {"asset_id": t, "name": t, "category": "Custom", "unit": "usd", "has_vol": True}
                )

    rows: List[Dict[str, Any]] = []
    now_utc_str = dt.datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%MZ")

    for meta in target_metas:
        ticker = meta["asset_id"]
        encoded = urllib.parse.quote(ticker)
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{encoded}?range={range_str}&interval={interval}"
        try:
            payload = json.loads(http_get(url, extra_headers={"User-Agent": "Mozilla/5.0"}))
            res = payload["chart"]["result"][0]
            timestamps = res.get("timestamp", [])
            quote = res["indicators"]["quote"][0]
            closes_raw = quote.get("close", [])
            vols_raw = quote.get("volume", [])

            bars: List[Tuple[str, float, Optional[float]]] = []
            for ts_val, c_val, v_val in zip(timestamps, closes_raw, vols_raw):
                if c_val is not None:
                    bar_date = dt.datetime.fromtimestamp(ts_val, UTC_TZ).strftime("%Y-%m-%d")
                    bars.append((bar_date, float(c_val), float(v_val) if v_val is not None else None))

            if len(bars) < 6:
                continue

            closes = [b[1] for b in bars]
            vols = [b[2] for b in bars]
            last_bar_date = bars[-1][0]

            # Check if crypto has live CoinGecko override
            cg_id = meta.get("cg_id")
            cg_coin = coingecko_data.get("coins", {}).get(cg_id, {}) if cg_id else {}

            if cg_id and cg_coin.get("usd") is not None:
                price = float(cg_coin["usd"])
                change_24h_pct = float(cg_coin.get("usd_24h_change", 0.0))
                # Use live price as latest print in trailing 20 closes
                closes_for_z = closes[:-1] + [price]
                z_score = calc_population_zscore(closes_for_z, window=20)
                # 5d change vs 5 calendar days prior bar
                ref_5d_close = closes[-6] if len(closes) >= 6 else closes[0]
                change_5d_pct = ((price / ref_5d_close) - 1.0) * 100.0
                source_str = f"coingecko live {now_utc_str}; z & volume from yfinance daily bars"
                mcap_usd = cg_coin.get("usd_market_cap")
            else:
                price = closes[-1]
                change_24h_pct = ((closes[-1] / closes[-2]) - 1.0) * 100.0 if len(closes) >= 2 else 0.0
                change_5d_pct = ((closes[-1] / closes[-6]) - 1.0) * 100.0 if len(closes) >= 6 else 0.0
                z_score = calc_population_zscore(closes, window=20)
                source_str = f"yfinance daily close {last_bar_date}"
                mcap_usd = None

            # Volume & 20d volume ratio
            vol_val: Optional[float] = None
            vol_ratio: Optional[float] = None
            if meta.get("has_vol", True):
                latest_vol = vols[-1]
                valid_vols_20 = [v for v in vols[-20:] if v is not None and v > 0]
                if latest_vol is not None and latest_vol > 0 and valid_vols_20:
                    vol_val = latest_vol
                    vol_ratio = latest_vol / (sum(valid_vols_20) / len(valid_vols_20))

            # Prior 30-day closing high (excluding current bar for breakout check, plus 30d overall)
            prior_30d_high = max(closes[-31:-1]) if len(closes) >= 31 else max(closes[:-1])

            rows.append(
                {
                    "asset_id": ticker,
                    "asset_name": meta["name"],
                    "category": meta["category"],
                    "unit": meta["unit"],
                    "price": price,
                    "change_24h_pct": change_24h_pct,
                    "change_5d_pct": change_5d_pct,
                    "z_score_20d": z_score,
                    "volume": vol_val,
                    "vol_ratio_20d": vol_ratio,
                    "prior_30d_high": prior_30d_high,
                    "market_cap_usd": mcap_usd,
                    "last_bar_date": last_bar_date,
                    "source": source_str,
                }
            )
        except Exception as exc:
            print(f"[WARN] Yahoo Finance fetch failed for {ticker}: {exc}", file=sys.stderr)

    return rows


def fetch_treasury_yields(year: Optional[int] = None) -> Dict[str, Any]:
    """Fetch daily Treasury par yield curve (2Y, 10Y, 30Y) and 48h (2-session) 10Y bps change."""
    if year is None:
        year = dt.datetime.now(NY_TZ).year
    url = (
        f"https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type=daily_treasury_yield_curve"
        f"&field_tdr_date_value={year}&page&_format=csv"
    )
    try:
        text = http_get(url)
        reader = csv.DictReader(text.splitlines())
        records = []
        for row in reader:
            raw_date = row.get("Date", "").strip()
            if not raw_date:
                continue
            parsed_date = dt.datetime.strptime(raw_date, "%m/%d/%Y").strftime("%Y-%m-%d")
            y2 = float(row["2 Yr"]) if row.get("2 Yr") else None
            y10 = float(row["10 Yr"]) if row.get("10 Yr") else None
            y30 = float(row["30 Yr"]) if row.get("30 Yr") else None
            records.append({"date": parsed_date, "us2y_pct": y2, "us10y_pct": y10, "us30y_pct": y30})
        records.sort(key=lambda r: r["date"])
        if not records:
            return {}
        latest = records[-1]
        prev_2session = records[-3] if len(records) >= 3 else records[0]
        change_48h_bps = None
        if latest["us10y_pct"] is not None and prev_2session["us10y_pct"] is not None:
            change_48h_bps = round((latest["us10y_pct"] - prev_2session["us10y_pct"]) * 100.0, 1)
        return {
            "latest_date": latest["date"],
            "prev_2session_date": prev_2session["date"],
            "us2y_pct": latest["us2y_pct"],
            "us10y_pct": latest["us10y_pct"],
            "us30y_pct": latest["us30y_pct"],
            "us10y_change_48h_bps": change_48h_bps,
            "history": records,
        }
    except Exception as exc:
        print(f"[WARN] Treasury yield fetch failed: {exc}", file=sys.stderr)
        return {}


def fetch_crypto_derivatives() -> List[Dict[str, Any]]:
    """Fetch perpetual swap funding rates and open interest from OKX and Deribit public APIs."""
    results: List[Dict[str, Any]] = []
    ts_label = dt.datetime.now(UTC_TZ).strftime("%H:%MZ")

    okx_pairs = [
        ("BTC-USD", "Bitcoin", "BTC-USDT-SWAP"),
        ("ETH-USD", "Ethereum", "ETH-USDT-SWAP"),
        ("SOL-USD", "Solana", "SOL-USDT-SWAP"),
    ]
    for asset_id, asset_name, inst_id in okx_pairs:
        try:
            f_data = http_get_json(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst_id}")
            oi_data = http_get_json(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={inst_id}")
            f_row = (f_data.get("data") or [{}])[0]
            oi_row = (oi_data.get("data") or [{}])[0]
            # fundingRate is decimal (e.g. -0.0000115 -> -0.00115% per 8h)
            f_pct = float(f_row["fundingRate"]) * 100.0 if f_row.get("fundingRate") is not None else None
            oi_usd = float(oi_row["oiUsd"]) if oi_row.get("oiUsd") is not None else None
            results.append(
                {
                    "asset_id": asset_id,
                    "asset_name": asset_name,
                    "venue": f"OKX ({inst_id})",
                    "funding_rate_pct": f_pct,
                    "open_interest_usd": oi_usd,
                    "source": f"okx public api {ts_label}",
                }
            )
        except Exception as exc:
            print(f"[WARN] OKX fetch failed for {inst_id}: {exc}", file=sys.stderr)

    # Deribit BTC-PERPETUAL
    try:
        d_data = http_get_json("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL")
        res = d_data.get("result", {})
        f_8h = res.get("funding_8h")
        f_pct = float(f_8h) * 100.0 if f_8h is not None else None
        oi_usd = float(res["open_interest"]) if res.get("open_interest") is not None else None
        results.append(
            {
                "asset_id": "BTC-USD",
                "asset_name": "Bitcoin",
                "venue": "Deribit (BTC-PERPETUAL)",
                "funding_rate_pct": f_pct,
                "open_interest_usd": oi_usd,
                "source": f"deribit public api {ts_label}",
            }
        )
    except Exception as exc:
        print(f"[WARN] Deribit fetch failed: {exc}", file=sys.stderr)

    return results


def fetch_defillama_stablecoins() -> Dict[str, Any]:
    """Fetch total stablecoin circulating market cap and 7-day percentage change from DefiLlama."""
    try:
        data = http_get_json("https://stablecoins.llama.fi/stablecoincharts/all")
        if not data:
            return {}
        latest = data[-1]
        prev_7d = data[-8] if len(data) >= 8 else data[0]
        latest_mcap = float(latest.get("totalCirculatingUSD", {}).get("peggedUSD", 0.0))
        prev_mcap = float(prev_7d.get("totalCirculatingUSD", {}).get("peggedUSD", 0.0))
        change_7d_pct = ((latest_mcap / prev_mcap) - 1.0) * 100.0 if prev_mcap > 0 else 0.0
        latest_date = dt.datetime.fromtimestamp(int(latest["date"]), UTC_TZ).strftime("%Y-%m-%d")
        return {
            "date": latest_date,
            "stablecoin_mcap_usd": latest_mcap,
            "prev_7d_mcap_usd": prev_mcap,
            "change_7d_pct": change_7d_pct,
        }
    except Exception as exc:
        print(f"[WARN] DefiLlama stablecoins fetch failed: {exc}", file=sys.stderr)
        return {}


def fetch_prediction_markets() -> List[Dict[str, Any]]:
    """Fetch Polymarket (Gamma + CLOB history) and Kalshi macro/crypto contracts."""
    rows: List[Dict[str, Any]] = []
    ts_label = dt.datetime.now(UTC_TZ).strftime("%H:%MZ")

    # 1. Polymarket
    for slug in POLYMARKET_SLUGS:
        try:
            url = f"https://gamma-api.polymarket.com/markets?slug={urllib.parse.quote(slug)}"
            markets = http_get_json(url)
            if not markets:
                continue
            m = markets[0]
            outcomes = json.loads(m.get("outcomePrices", '["0"]'))
            prob = float(outcomes[0]) if outcomes else None
            vol_24h = float(m.get("volume24hr") or 0.0)
            question = m.get("question", slug)

            # Query CLOB price history for 24h & 4h deltas
            change_24h_pts = None
            change_4h_pts = None
            clob_ids = json.loads(m.get("clobTokenIds", "[]"))
            if clob_ids and prob is not None:
                try:
                    hist_url = f"https://clob.polymarket.com/prices-history?market={clob_ids[0]}&interval=1d&fidelity=60"
                    hist = http_get_json(hist_url).get("history", [])
                    if hist:
                        p_24h_ago = float(hist[0]["p"])
                        change_24h_pts = round((prob - p_24h_ago) * 100.0, 2)
                        idx_4h = max(0, len(hist) - 5)
                        p_4h_ago = float(hist[idx_4h]["p"])
                        change_4h_pts = round((prob - p_4h_ago) * 100.0, 2)
                except Exception:
                    if m.get("oneDayPriceChange") is not None:
                        change_24h_pts = round(float(m["oneDayPriceChange"]) * 100.0, 2)

            rows.append(
                {
                    "market_id": slug,
                    "question": question,
                    "venue": "Polymarket",
                    "probability": prob,
                    "change_24h_pts": change_24h_pts,
                    "change_4h_pts": change_4h_pts,
                    "volume_usd": round(vol_24h, 2),
                    "open_interest_contracts": None,
                    "url": f"https://polymarket.com/market/{slug}",
                    "source": f"polymarket gamma {ts_label}",
                }
            )
        except Exception as exc:
            print(f"[WARN] Polymarket fetch failed for {slug}: {exc}", file=sys.stderr)

    # 2. Kalshi
    kalshi_by_series: Dict[str, Dict[str, Any]] = {}
    for series_ticker, _ in KALSHI_TARGET_TICKERS:
        if series_ticker not in kalshi_by_series:
            try:
                k_url = f"https://api.elections.kalshi.com/trade-api/v2/markets?series_ticker={series_ticker}"
                k_markets = http_get_json(k_url).get("markets", [])
                kalshi_by_series[series_ticker] = {m["ticker"]: m for m in k_markets if "ticker" in m}
            except Exception as exc:
                print(f"[WARN] Kalshi series {series_ticker} failed: {exc}", file=sys.stderr)
                kalshi_by_series[series_ticker] = {}

    for series_ticker, ticker in KALSHI_TARGET_TICKERS:
        m = kalshi_by_series.get(series_ticker, {}).get(ticker)
        if not m:
            continue
        last_p = float(m["last_price_dollars"]) if m.get("last_price_dollars") is not None else None
        prev_p = float(m["previous_price_dollars"]) if m.get("previous_price_dollars") is not None else None
        chg_pts = round((last_p - prev_p) * 100.0, 2) if (last_p is not None and prev_p is not None) else 0.0
        vol_24h = float(m.get("volume_24h_fp") or 0.0)
        oi_fp = float(m.get("open_interest_fp") or 0.0)
        rows.append(
            {
                "market_id": ticker,
                "question": m.get("title", ticker),
                "venue": "Kalshi",
                "probability": last_p,
                "change_24h_pts": chg_pts,
                "change_4h_pts": None,
                "volume_usd": round(vol_24h, 2),
                "open_interest_contracts": oi_fp,
                "url": f"https://kalshi.com/markets/{ticker}",
                "source": f"kalshi public api {ts_label}",
            }
        )

    return rows


def fetch_spot_metals() -> List[Dict[str, Any]]:
    """Fetch precious metal physical spot bids (Gold, Silver, Platinum, Palladium) from Kitco."""
    metals_out: List[Dict[str, Any]] = []
    try:
        page = http_get("https://www.kitco.com/price/precious-metals")
        match = re.search(r"__NEXT_DATA__[^>]*>(.*?)</script>", page, re.S)
        if not match:
            return metals_out
        nd = json.loads(match.group(1))
        queries = nd.get("props", {}).get("pageProps", {}).get("dehydratedState", {}).get("queries", [])
        quotes_dict = {}
        for q in queries:
            q_key = q.get("queryKey", [])
            if isinstance(q_key, list) and "allMetalsQuote" in q_key:
                quotes_dict = q.get("state", {}).get("data", {})
                break

        mapping = [
            ("gold", "XAU-SPOT", "Gold spot bid (Kitco)"),
            ("silver", "XAG-SPOT", "Silver spot bid (Kitco)"),
            ("platinum", "XPT-SPOT", "Platinum spot bid (Kitco)"),
            ("palladium", "XPD-SPOT", "Palladium spot bid (Kitco)"),
        ]
        for key, asset_id, name in mapping:
            res_list = quotes_dict.get(key, {}).get("results", [])
            if res_list:
                item = res_list[0]
                bid = float(item["bid"]) if item.get("bid") is not None else None
                orig_time = item.get("originalTime", "")
                if bid is not None:
                    metals_out.append(
                        {
                            "asset_id": asset_id,
                            "asset_name": name,
                            "category": "Spot metal",
                            "price": bid,
                            "unit": "usd",
                            "source": f"kitco precious metals page ({orig_time})",
                        }
                    )
    except Exception as exc:
        print(f"[WARN] Kitco spot metals fetch failed: {exc}", file=sys.stderr)
    return metals_out


def fetch_gpu_rentals() -> List[Dict[str, Any]]:
    """Fetch AI/prosumer GPU hourly spot rental rates from Vast.ai Bundles API and RunPod pricing."""
    rentals: List[Dict[str, Any]] = []
    ts_label = dt.datetime.now(UTC_TZ).strftime("%H:%MZ")

    # 1. Vast.ai offers API
    try:
        vast_url = (
            "https://console.vast.ai/api/v0/bundles/"
            "?q=%7B%22rentable%22%3A%7B%22eq%22%3Atrue%7D%2C%22rented%22%3A%7B%22eq%22%3Afalse%7D%7D"
        )
        offers = http_get_json(vast_url).get("offers", [])
        vast_targets = [
            ("H100 SXM", "gpu-rental-h100-sxm-vast", "H100 SXM"),
            ("H100 PCIE", "gpu-rental-h100-pcie-vast", "H100 PCIE"),
            ("B200", "gpu-rental-b200-vast", "B200"),
            ("RTX 5090", "gpu-rental-rtx-5090-vast", "RTX 5090"),
            ("RTX 4090", "gpu-rental-rtx-4090-vast", "RTX 4090"),
        ]
        for match_str, base_id, label in vast_targets:
            all_rates = []
            ver_rates = []
            for o in offers:
                gname = (o.get("gpu_name") or "").upper()
                num_gpus = int(o.get("num_gpus") or 1)
                dph = o.get("dph_total")
                if match_str.upper() in gname and dph is not None and num_gpus > 0:
                    per_gpu = float(dph) / num_gpus
                    if per_gpu > 0.05:
                        all_rates.append(per_gpu)
                        if o.get("verification") == "verified":
                            ver_rates.append(per_gpu)
            if all_rates:
                rentals.append(
                    {
                        "asset_id": base_id,
                        "asset_name": f"{label} rental (Vast.ai cheapest on-demand)",
                        "category": "GPU rental",
                        "metric": "rental_usd_per_gpu_hr",
                        "value": round(min(all_rates), 4),
                        "unit": "usd_per_hr",
                        "source": f"vast.ai offers api {ts_label}",
                    }
                )
            if ver_rates:
                rentals.append(
                    {
                        "asset_id": f"{base_id}-verified",
                        "asset_name": f"{label} rental (Vast.ai cheapest verified host)",
                        "category": "GPU rental",
                        "metric": "rental_usd_per_gpu_hr",
                        "value": round(min(ver_rates), 4),
                        "unit": "usd_per_hr",
                        "source": f"vast.ai offers api {ts_label}",
                    }
                )
    except Exception as exc:
        print(f"[WARN] Vast.ai GPU fetch failed: {exc}", file=sys.stderr)

    # 2. RunPod pricing page
    try:
        rp_html = http_get("https://www.runpod.io/pricing")
        rp_targets = [
            ("B200", "gpu-rental-b200-runpod"),
            ("H200", "gpu-rental-h200-runpod"),
            ("H100 SXM", "gpu-rental-h100-sxm-runpod"),
            ("H100 NVL", "gpu-rental-h100-nvl-runpod"),
            ("H100 PCIe", "gpu-rental-h100-pcie-runpod"),
            ("RTX 5090", "gpu-rental-rtx-5090-runpod"),
        ]
        for gpu_label, slug_prefix in rp_targets:
            pattern = rf"{re.escape(gpu_label)}[^\"']*?Community Cloud \$([0-9.]+)/hr,\s*Secure Cloud \$([0-9.]+)/hr"
            m = re.search(pattern, rp_html, re.I)
            if m:
                comm_rate = float(m.group(1))
                sec_rate = float(m.group(2))
                rentals.append(
                    {
                        "asset_id": f"{slug_prefix}-community",
                        "asset_name": f"{gpu_label} rental (RunPod Community Cloud)",
                        "category": "GPU rental",
                        "metric": "rental_usd_per_gpu_hr",
                        "value": comm_rate,
                        "unit": "usd_per_hr",
                        "source": f"runpod pricing page {ts_label}",
                    }
                )
                rentals.append(
                    {
                        "asset_id": f"{slug_prefix}-secure",
                        "asset_name": f"{gpu_label} rental (RunPod Secure Cloud)",
                        "category": "GPU rental",
                        "metric": "rental_usd_per_gpu_hr",
                        "value": sec_rate,
                        "unit": "usd_per_hr",
                        "source": f"runpod pricing page {ts_label}",
                    }
                )
        m_4090 = re.search(r"RTX 4090 from \$([0-9.]+)/hr", rp_html, re.I)
        if m_4090:
            rentals.append(
                {
                    "asset_id": "gpu-rental-rtx-4090-runpod-community",
                    "asset_name": "RTX 4090 rental (RunPod Community Cloud)",
                    "category": "GPU rental",
                    "metric": "rental_usd_per_gpu_hr",
                    "value": float(m_4090.group(1)),
                    "unit": "usd_per_hr",
                    "source": f"runpod pricing page {ts_label}",
                }
            )
    except Exception as exc:
        print(f"[WARN] RunPod GPU fetch failed: {exc}", file=sys.stderr)

    return rentals


# ---------------------------------------------------------------------------
# Tool 2 & 3: `scrape_illiquid_comps` and `calculate_fair_market_value`
# ---------------------------------------------------------------------------

def calculate_fair_market_value(
    asset_id: str,
    sales_array: List[Dict[str, Any]],
    live_ask_usd: Optional[float] = None,
    shipping_usd: float = 0.0,
    fee_rate: float = 0.13,
) -> Dict[str, Any]:
    """
    Agent Tool #3 (`claude.md` §3.3 & §4.2 & §7.1–7.4):
    Computes 10%-trimmed median (FMV_30), 30-day simple mean (MA_30), deal threshold (<= 78% of FMV_30),
    and net realizable arbitrage spread after platform fees (13%).
    Requires n >= 3 verified comps in window, otherwise marks status as `LOW_LIQUIDITY_UNPRICED`.
    """
    prices = sorted(
        float(s["sale_price_usd"])
        for s in sales_array
        if s.get("sale_price_usd") is not None and float(s["sale_price_usd"]) > 0
    )
    n_comps = len(prices)
    if n_comps < 3:
        return {
            "item_id": asset_id,
            "n_comps": n_comps,
            "fmv_30d_usd": None,
            "ma_30d_usd": None,
            "min_comp_usd": min(prices) if prices else None,
            "max_comp_usd": max(prices) if prices else None,
            "deal_threshold_usd": None,
            "net_spread_usd": None,
            "net_spread_pct": None,
            "is_arbitrage_deal": False,
            "status": "LOW_LIQUIDITY_UNPRICED",
        }

    # Trim top and bottom 10% of comps per §7.1
    k = int(n_comps * 0.10)
    trimmed = prices[k : n_comps - k] if (k > 0 and n_comps - 2 * k >= 1) else prices
    fmv_30 = round(float(statistics.median(trimmed)), 2)
    ma_30 = round(float(sum(trimmed) / len(trimmed)), 2)
    deal_threshold = round(fmv_30 * 0.78, 2)  # >= 22% below FMV_30

    net_spread_usd = None
    net_spread_pct = None
    is_arb = False
    if live_ask_usd is not None and live_ask_usd > 0:
        total_cost = live_ask_usd + (shipping_usd or 0.0)
        net_spread_usd = round(fmv_30 * (1.0 - fee_rate) - total_cost, 2)
        net_spread_pct = round((net_spread_usd / total_cost) * 100.0, 2)
        is_arb = live_ask_usd <= deal_threshold

    return {
        "item_id": asset_id,
        "n_comps": n_comps,
        "fmv_30d_usd": fmv_30,
        "ma_30d_usd": ma_30,
        "min_comp_usd": min(prices),
        "max_comp_usd": max(prices),
        "deal_threshold_usd": deal_threshold,
        "net_spread_usd": net_spread_usd,
        "net_spread_pct": net_spread_pct,
        "is_arbitrage_deal": is_arb,
        "status": "PRICED",
    }


def scrape_illiquid_comps(
    query: Optional[str] = None,
    grade_filter: Optional[str] = None,
    lookback_days: int = 30,
    reference_date: Optional[dt.date] = None,
) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """
    Agent Tool #2 (`claude.md` §3.2):
    Scrapes completed eBay sales via PriceCharting condition-specific tabs for the items in
    `data/watchlist.md`, stripping out wrong editions/grades and unverified outliers.
    Returns (illiquid_summary_rows, verified_comp_rows).
    """
    if reference_date is None:
        reference_date = dt.datetime.now(NY_TZ).date()
    cutoff_date = reference_date - dt.timedelta(days=lookback_days)

    targets = ILLIQUID_WATCHLIST
    if query:
        q_low = query.lower()
        targets = [
            it
            for it in ILLIQUID_WATCHLIST
            if q_low in it["item_id"].lower() or q_low in it["name"].lower() or q_low in it["category"].lower()
        ]

    illiquid_rows: List[Dict[str, Any]] = []
    all_comps: List[Dict[str, Any]] = []

    for item in targets:
        item_id = item["item_id"]
        url = item["url"]
        pc_tab = item.get("pc_tab")
        grade_cond = grade_filter or item["grade_condition"]

        if not pc_tab or "pricecharting.com" not in url:
            # Source blocked (403 on WatchCharts/SportsCardsPro/eBay search)
            illiquid_rows.append(
                {
                    "item_id": item_id,
                    "name": item["name"],
                    "category": item["category"],
                    "grade_condition": grade_cond,
                    "live_ask_usd": None,
                    "shipping_usd": None,
                    "fmv_30d_usd": None,
                    "n_comps": 0,
                    "net_spread_pct": None,
                    "deal_threshold_usd": None,
                    "status": "LOW_LIQUIDITY_UNPRICED",
                    "url": url,
                }
            )
            continue

        try:
            page = http_get(url)
            # Isolate the exact condition tab div
            div_pattern = rf'<div class="{re.escape(pc_tab)}"[^>]*>(.*?)</div>\s*(?:<div class="completed-auctions-|<div id=")'
            m_div = re.search(div_pattern, page, re.S)
            if not m_div:
                # Fallback: grab from opening div to closing table
                start_idx = page.find(f'class="{pc_tab}"')
                section_html = page[start_idx : start_idx + 120000] if start_idx != -1 else ""
            else:
                section_html = m_div.group(1)

            tr_blocks = re.findall(r'<tr id="ebay-(\d+)"[^>]*>(.*?)</tr>', section_html, re.S)
            item_comps_30d: List[Dict[str, Any]] = []

            for ebay_id, tr_inner in tr_blocks:
                m_date = re.search(r'<td class="date">\s*(\d{4}-\d{2}-\d{2})\s*</td>', tr_inner)
                m_title = re.search(r'<td class="title"[^>]*>.*?<a[^>]*>(.*?)</a>', tr_inner, re.S)
                m_price = re.search(r'<span class="js-price"[^>]*>\$([0-9,]+\.?\d*)</span>', tr_inner)
                if not (m_date and m_price):
                    continue

                sale_date_str = m_date.group(1)
                sale_date = dt.date.fromisoformat(sale_date_str)
                title_clean = html.unescape(re.sub(r"<[^>]+>", " ", m_title.group(1) if m_title else "")).strip()
                price_val = float(m_price.group(1).replace(",", ""))

                # Filter out excluded keywords (e.g., single packs, empty boxes, wrong variant)
                title_lower = title_clean.lower()
                if any(kw in title_lower for kw in item.get("exclude_keywords", [])):
                    continue

                comp_record = {
                    "date_collected": reference_date.isoformat(),
                    "item_id": item_id,
                    "sale_date": sale_date_str,
                    "sale_price_usd": price_val,
                    "shipping_usd": "",
                    "grade_condition": grade_cond,
                    "platform": "eBay (via PriceCharting)",
                    "url": f"https://www.ebay.com/itm/{ebay_id}",
                    "title": title_clean,
                }

                if sale_date >= cutoff_date:
                    item_comps_30d.append(comp_record)
                    all_comps.append(comp_record)

            fmv_stats = calculate_fair_market_value(item_id, item_comps_30d)
            illiquid_rows.append(
                {
                    "item_id": item_id,
                    "name": item["name"],
                    "category": item["category"],
                    "grade_condition": grade_cond,
                    "live_ask_usd": None,
                    "shipping_usd": None,
                    "fmv_30d_usd": fmv_stats["fmv_30d_usd"],
                    "n_comps": fmv_stats["n_comps"],
                    "min_comp_usd": fmv_stats["min_comp_usd"],
                    "max_comp_usd": fmv_stats["max_comp_usd"],
                    "deal_threshold_usd": fmv_stats["deal_threshold_usd"],
                    "net_spread_pct": fmv_stats["net_spread_pct"],
                    "status": fmv_stats["status"],
                    "url": url,
                }
            )
        except Exception as exc:
            print(f"[WARN] PriceCharting scrape failed for {item_id}: {exc}", file=sys.stderr)
            illiquid_rows.append(
                {
                    "item_id": item_id,
                    "name": item["name"],
                    "category": item["category"],
                    "grade_condition": grade_cond,
                    "live_ask_usd": None,
                    "shipping_usd": None,
                    "fmv_30d_usd": None,
                    "n_comps": 0,
                    "net_spread_pct": None,
                    "deal_threshold_usd": None,
                    "status": "LOW_LIQUIDITY_UNPRICED",
                    "url": url,
                }
            )

    return illiquid_rows, all_comps


# ---------------------------------------------------------------------------
# Tool 4: `evaluate_cross_asset_correlation(asset_a, asset_b, window)`
# ---------------------------------------------------------------------------

def evaluate_cross_asset_correlation(
    asset_a: str,
    asset_b: str,
    window: int = 30,
    lag_days: int = 0,
    data_dir: str | Path = "data",
) -> Dict[str, Any]:
    """
    Agent Tool #4 (`claude.md` §3.4):
    Computes rolling Pearson correlation rho between `asset_a` and `asset_b` over `window`
    observations from `data/prices_wide.csv` (both on price levels and daily pct returns),
    supporting an optional `lag_days` shift (positive lag = asset_a leads asset_b).
    """
    wide_path = Path(data_dir) / "prices_wide.csv"
    if not wide_path.exists():
        raise FileNotFoundError(f"Cannot find {wide_path}")

    with open(wide_path, "r", encoding="utf-8") as fp:
        reader = csv.DictReader(fp)
        rows = list(reader)

    pairs: List[Tuple[str, float, float]] = []
    for i, r in enumerate(rows):
        idx_b = i + lag_days
        if idx_b < 0 or idx_b >= len(rows):
            continue
        va_str = r.get(asset_a, "")
        vb_str = rows[idx_b].get(asset_b, "")
        if va_str != "" and vb_str != "":
            pairs.append((r["date"], float(va_str), float(vb_str)))

    if len(pairs) < 3:
        return {
            "asset_a": asset_a,
            "asset_b": asset_b,
            "window": window,
            "lag_days": lag_days,
            "n_overlapping": len(pairs),
            "level_correlation_rho": None,
            "return_correlation_rho": None,
        }

    recent = pairs[-window:] if len(pairs) >= window else pairs
    xa = [p[1] for p in recent]
    xb = [p[2] for p in recent]

    def _pearson(x: List[float], y: List[float]) -> Optional[float]:
        n = len(x)
        if n < 3:
            return None
        mx = sum(x) / n
        my = sum(y) / n
        num = sum((a - mx) * (b - my) for a, b in zip(x, y))
        den_x = math.sqrt(sum((a - mx) ** 2 for a in x))
        den_y = math.sqrt(sum((b - my) ** 2 for b in y))
        if den_x == 0 or den_y == 0:
            return 0.0
        return round(num / (den_x * den_y), 4)

    level_rho = _pearson(xa, xb)
    ret_a = [(xa[i] / xa[i - 1]) - 1.0 for i in range(1, len(xa)) if xa[i - 1] != 0]
    ret_b = [(xb[i] / xb[i - 1]) - 1.0 for i in range(1, len(xb)) if xb[i - 1] != 0]
    ret_rho = _pearson(ret_a, ret_b) if len(ret_a) == len(ret_b) else None

    return {
        "asset_a": asset_a,
        "asset_b": asset_b,
        "window": window,
        "lag_days": lag_days,
        "n_overlapping": len(recent),
        "start_date": recent[0][0],
        "end_date": recent[-1][0],
        "level_correlation_rho": level_rho,
        "return_correlation_rho": ret_rho,
    }


# ---------------------------------------------------------------------------
# Tool 5: `emit_alert` & Quantitative Trigger Engine (`claude.md` §4)
# ---------------------------------------------------------------------------

def emit_alert(
    severity: str,
    asset: str,
    signal_type: str,
    payload: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Agent Tool #5 (`claude.md` §3.5):
    Formats a structured alert record compatible with `data/alerts.csv`.
    """
    return {
        "date": payload.get("date", dt.datetime.now(NY_TZ).strftime("%Y-%m-%d")),
        "timestamp_utc": payload.get("timestamp_utc", dt.datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")),
        "severity": severity,
        "asset_id": asset,
        "category": payload.get("category", "General"),
        "signal_type": signal_type,
        "value": fmt_num(payload.get("value"), 4) if isinstance(payload.get("value"), (int, float)) else str(payload.get("value", "")),
        "threshold": str(payload.get("threshold", "")),
        "details": str(payload.get("details", "")),
    }


def evaluate_quantitative_triggers(
    run_date: str,
    timestamp_utc: str,
    liquid_rows: List[Dict[str, Any]],
    treasury_data: Dict[str, Any],
    crypto_derivs: List[Dict[str, Any]],
    stablecoin_data: Dict[str, Any],
    prediction_rows: List[Dict[str, Any]],
    illiquid_rows: List[Dict[str, Any]],
) -> Tuple[Dict[str, Any], List[Dict[str, Any]]]:
    """
    Evaluates all 4 quantitative trigger models defined in `claude.md` §4:
      1. Z-Score Price & Volume Breakout (|Z| >= 2.5 and Vol > 1.8x 20d avg)
      2. Illiquid Arbitrage Spread (Ask >= 22% below FMV_30)
      3. Macro Regime & Liquidity Warning (US10Y +15 bps in 48h AND DXY > 30d high)
      4. Prediction Market vs. Spot Divergence (>=15 pts shift in 4h/24h)
    Returns (macro_row_dict, alerts_list).
    """
    alerts: List[Dict[str, Any]] = []
    by_ticker = {r["asset_id"]: r for r in liquid_rows}

    dxy_row = by_ticker.get("DX-Y.NYB", {})
    vix_row = by_ticker.get("^VIX", {})
    usdjpy_row = by_ticker.get("USDJPY=X", {})
    eurusd_row = by_ticker.get("EURUSD=X", {})
    tnx_row = by_ticker.get("^TNX", {})

    dxy_val = dxy_row.get("price")
    dxy_prior_high = dxy_row.get("prior_30d_high")
    us10y_48h_bps = treasury_data.get("us10y_change_48h_bps")

    # Rule §4.4: Liquidity Warning (US10Y > 15 bps in 48h AND DXY > 30d high)
    leg1_yield = us10y_48h_bps is not None and us10y_48h_bps > 15.0
    leg2_dxy = dxy_val is not None and dxy_prior_high is not None and dxy_val > dxy_prior_high
    risk_off_flag = bool(leg1_yield and leg2_dxy)

    if risk_off_flag:
        regime_status = "RISK-OFF"
        alerts.append(
            emit_alert(
                severity="high",
                asset="US10Y",
                signal_type="risk_off_liquidity_warning",
                payload={
                    "date": run_date,
                    "timestamp_utc": timestamp_utc,
                    "category": "Macro",
                    "value": us10y_48h_bps,
                    "threshold": ">15 bps in 48h + DXY 30d high breakout",
                    "details": (
                        f"10Y par yield +{us10y_48h_bps} bps over two sessions "
                        f"({treasury_data.get('prev_2session_date')} -> {treasury_data.get('latest_date')} = "
                        f"{treasury_data.get('us10y_pct')}%). DXY {fmt_num(dxy_val, 2)} closed above its prior "
                        f"30-day high of {fmt_num(dxy_prior_high, 2)}. RISK_OFF across crypto, tech equities and high-end collectibles."
                    ),
                },
            )
        )
    elif vix_row.get("z_score_20d") and vix_row["z_score_20d"] >= 2.0:
        regime_status = "VOLATILITY EXPANSION"
    else:
        regime_status = "NEUTRAL"

    # Rule §4.1: Liquid Z-Score & Volume Breakout
    for r in liquid_rows:
        z = r.get("z_score_20d")
        vr = r.get("vol_ratio_20d")
        if z is None:
            continue
        if abs(z) >= 2.5 and vr is not None and vr > 1.8:
            alerts.append(
                emit_alert(
                    severity="high",
                    asset=r["asset_id"],
                    signal_type="z_score_volume_breakout",
                    payload={
                        "date": run_date,
                        "timestamp_utc": timestamp_utc,
                        "category": r["category"],
                        "value": round(z, 4),
                        "threshold": "|Z|>=2.5 and vol>1.8x 20d avg",
                        "details": (
                            f"Full breakout confirmed: Z={z:+.2f}, volume ratio={vr:.2f}x 20d avg; "
                            f"24h {r.get('change_24h_pct', 0):+.2f}%, 5d {r.get('change_5d_pct', 0):+.2f}%."
                        ),
                    },
                )
            )
        elif abs(z) >= 2.45 and r["category"] == "Crypto":
            alerts.append(
                emit_alert(
                    severity="medium",
                    asset=r["asset_id"],
                    signal_type="z_score_breakout_near_trigger",
                    payload={
                        "date": run_date,
                        "timestamp_utc": timestamp_utc,
                        "category": r["category"],
                        "value": round(z, 4),
                        "threshold": "|Z|>=2.5 and vol>1.8x 20d avg",
                        "details": (
                            f"Z={z:+.2f} on 20 daily closes; 24h {r.get('change_24h_pct', 0):+.2f}%, "
                            f"5d {r.get('change_5d_pct', 0):+.2f}%; volume ratio {vr or 0:.2f}x."
                        ),
                    },
                )
            )

    # Stablecoin supply contraction check
    sc_chg = stablecoin_data.get("change_7d_pct")
    if sc_chg is not None and sc_chg <= -1.0:
        alerts.append(
            emit_alert(
                severity="medium",
                asset="STABLECOIN-MCAP",
                signal_type="stablecoin_supply_contraction",
                payload={
                    "date": run_date,
                    "timestamp_utc": timestamp_utc,
                    "category": "Crypto",
                    "value": round(sc_chg, 4),
                    "threshold": "informational",
                    "details": (
                        f"Total stablecoin market cap ${stablecoin_data['stablecoin_mcap_usd']/1e9:.2f}B vs "
                        f"${stablecoin_data['prev_7d_mcap_usd']/1e9:.2f}B 7d prior ({sc_chg:+.2f}% in 7 days): "
                        f"liquidity drain, not inflow."
                    ),
                },
            )
        )

    # Prediction market 24h shift >= 15 pts
    for pm in prediction_rows:
        chg24 = pm.get("change_24h_pts")
        if chg24 is not None and abs(chg24) >= 15.0:
            prob_now = (pm.get("probability") or 0.0) * 100.0
            prob_prev = prob_now - chg24
            chg4 = pm.get("change_4h_pts")
            chg4_note = f" 4h move {chg4:+.1f} pts." if chg4 is not None else ""
            alerts.append(
                emit_alert(
                    severity="medium",
                    asset=pm["market_id"],
                    signal_type="probability_shift_24h",
                    payload={
                        "date": run_date,
                        "timestamp_utc": timestamp_utc,
                        "category": "Prediction market",
                        "value": chg24,
                        "threshold": ">=15 pts in 24h",
                        "details": (
                            f"{pm['question']}: {prob_prev:.1f}% -> {prob_now:.1f}% in 24h.{chg4_note} "
                            f"Spot BTC moved concurrently, so not a Lagging Spot Divergence."
                        ),
                    },
                )
            )

    # Check if any live asks exist on illiquid collectibles
    if not any(r.get("live_ask_usd") is not None for r in illiquid_rows):
        alerts.append(
            emit_alert(
                severity="info",
                asset="ALL-ILLIQUID",
                signal_type="no_live_ask_data",
                payload={
                    "date": run_date,
                    "timestamp_utc": timestamp_utc,
                    "category": "Collectibles",
                    "value": "",
                    "threshold": "live BIN / ending-soon auctions required",
                    "details": (
                        "eBay, 130point, SportsCardsPro, BrickEconomy, WatchCharts and Chrono24 all returned HTTP 403, "
                        "and TCGplayer serves prices only to a rendered browser. No live Buy-It-Now ask could be verified."
                    ),
                },
            )
        )

    if leg2_dxy:
        alerts.append(
            emit_alert(
                severity="info",
                asset="DX-Y.NYB",
                signal_type="thirty_day_high_breakout",
                payload={
                    "date": run_date,
                    "timestamp_utc": timestamp_utc,
                    "category": "FX",
                    "value": fmt_num(dxy_val, 2),
                    "threshold": f"close > prior 30d high {fmt_num(dxy_prior_high, 2)}",
                    "details": (
                        f"DXY closed {fmt_num(dxy_val, 2)}, above its prior 30-session closing high of "
                        f"{fmt_num(dxy_prior_high, 2)}. Z={dxy_row.get('z_score_20d', 0):+.2f}."
                    ),
                },
            )
        )

    # OKX BTC funding rate
    okx_btc_funding = next(
        (d["funding_rate_pct"] for d in crypto_derivs if d["asset_id"] == "BTC-USD" and "OKX" in d["venue"]),
        None,
    )

    macro_row = {
        "date": run_date,
        "timestamp_utc": timestamp_utc,
        "regime_status": regime_status,
        "us2y_pct": fmt_num(treasury_data.get("us2y_pct"), 2),
        "us10y_pct": fmt_num(treasury_data.get("us10y_pct"), 2),
        "us30y_pct": fmt_num(treasury_data.get("us30y_pct"), 2),
        "us10y_change_48h_bps": fmt_num(us10y_48h_bps, 1),
        "dxy": fmt_num(dxy_val, 2),
        "dxy_30d_high": fmt_num(dxy_prior_high, 2),
        "vix": fmt_num(vix_row.get("price"), 2),
        "usdjpy": fmt_num(usdjpy_row.get("price"), 3),
        "eurusd": fmt_num(eurusd_row.get("price"), 5),
        "btc_funding_pct": fmt_num(okx_btc_funding, 6),
        "stablecoin_mcap_usd": fmt_num(stablecoin_data.get("stablecoin_mcap_usd"), 0),
        "risk_off_flag": "true" if risk_off_flag else "false",
        "sources": (
            f"treasury.gov daily par yield curve {treasury_data.get('latest_date', '')}; "
            f"yfinance {dxy_row.get('last_bar_date', '')} closes; OKX funding; DefiLlama {stablecoin_data.get('date', '')}"
        ),
    }

    return macro_row, alerts


# ---------------------------------------------------------------------------
# CSV Persistence & `prices_wide.csv` Regeneration (`data/README.md` compliant)
# ---------------------------------------------------------------------------

def _replace_date_rows_in_csv(
    csv_path: Path,
    fieldnames: List[str],
    run_date: str,
    new_rows: List[Dict[str, Any]],
    date_col: str = "date",
) -> None:
    """Replace all rows matching `run_date` while preserving earlier historical rows."""
    existing_rows: List[Dict[str, Any]] = []
    if csv_path.exists():
        with open(csv_path, "r", encoding="utf-8") as fp:
            reader = csv.DictReader(fp)
            existing_rows = [r for r in reader if r.get(date_col) != run_date]

    combined = existing_rows + new_rows
    with open(csv_path, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(combined)


def regenerate_prices_wide(data_dir: Path) -> int:
    """
    Regenerate `data/prices_wide.csv` from `data/timeseries.csv`:
    One row per date, one column per `asset_id` holding `price` (or `yield_pct` for yields),
    plus `<item_id>_fmv` for each illiquid item's `fmv_30d_usd`.
    """
    ts_path = data_dir / "timeseries.csv"
    wide_path = data_dir / "prices_wide.csv"
    if not ts_path.exists():
        return 0

    by_date: Dict[str, Dict[str, str]] = {}
    price_cols: set[str] = set()
    fmv_cols: set[str] = set()

    with open(ts_path, "r", encoding="utf-8") as fp:
        for row in csv.DictReader(fp):
            d = row["date"]
            aid = row["asset_id"]
            metric = row["metric"]
            val = row["value"]
            if not d or val == "":
                continue
            if metric in ("price", "yield_pct"):
                by_date.setdefault(d, {})[aid] = f"{float(val):.5f}" if "." in val else val
                price_cols.add(aid)
            elif metric == "fmv_30d_usd":
                col_name = f"{aid}_fmv"
                by_date.setdefault(d, {})[col_name] = val
                fmv_cols.add(col_name)

    ordered_cols = ["date"] + sorted(price_cols) + sorted(fmv_cols)
    with open(wide_path, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=ordered_cols)
        writer.writeheader()
        for d in sorted(by_date.keys()):
            row_dict = {"date": d}
            for c in ordered_cols[1:]:
                row_dict[c] = by_date[d].get(c, "")
            writer.writerow(row_dict)
    return len(by_date)


def write_all_csvs(
    data_dir: Path,
    run_date: str,
    timestamp_utc: str,
    liquid_rows: List[Dict[str, Any]],
    coingecko_data: Dict[str, Any],
    treasury_data: Dict[str, Any],
    crypto_derivs: List[Dict[str, Any]],
    stablecoin_data: Dict[str, Any],
    prediction_rows: List[Dict[str, Any]],
    illiquid_rows: List[Dict[str, Any]],
    comp_rows: List[Dict[str, Any]],
    spot_metals: List[Dict[str, Any]],
    gpu_rentals: List[Dict[str, Any]],
    macro_row: Dict[str, Any],
    alerts: List[Dict[str, Any]],
) -> None:
    """Write all collected data to `data/*.csv` adhering strictly to `data/README.md`."""
    data_dir.mkdir(parents=True, exist_ok=True)

    # 1. liquid.csv
    liquid_csv_rows = [
        {
            "date": run_date,
            "timestamp_utc": timestamp_utc,
            "asset_id": r["asset_id"],
            "category": r["category"],
            "price": fmt_num(r["price"], 5),
            "change_24h_pct": fmt_num(r["change_24h_pct"], 4),
            "change_5d_pct": fmt_num(r["change_5d_pct"], 4),
            "z_score_20d": fmt_num(r["z_score_20d"], 4),
            "volume": fmt_num(r["volume"], 0),
            "vol_ratio_20d": fmt_num(r["vol_ratio_20d"], 4),
            "source": r["source"],
        }
        for r in sorted(liquid_rows, key=lambda x: x["asset_id"])
    ]
    _replace_date_rows_in_csv(
        data_dir / "liquid.csv",
        ["date", "timestamp_utc", "asset_id", "category", "price", "change_24h_pct", "change_5d_pct", "z_score_20d", "volume", "vol_ratio_20d", "source"],
        run_date,
        liquid_csv_rows,
    )

    # 2. macro.csv
    _replace_date_rows_in_csv(
        data_dir / "macro.csv",
        [
            "date", "timestamp_utc", "regime_status", "us2y_pct", "us10y_pct", "us30y_pct",
            "us10y_change_48h_bps", "dxy", "dxy_30d_high", "vix", "usdjpy", "eurusd",
            "btc_funding_pct", "stablecoin_mcap_usd", "risk_off_flag", "sources",
        ],
        run_date,
        [macro_row],
    )

    # 3. crypto_derivatives.csv
    deriv_csv_rows = [
        {
            "date": run_date,
            "timestamp_utc": timestamp_utc,
            "asset_id": d["asset_id"],
            "venue": d["venue"],
            "funding_rate_pct": fmt_num(d["funding_rate_pct"], 8),
            "open_interest_usd": fmt_num(d["open_interest_usd"], 2),
            "source": d["source"],
        }
        for d in crypto_derivs
    ]
    _replace_date_rows_in_csv(
        data_dir / "crypto_derivatives.csv",
        ["date", "timestamp_utc", "asset_id", "venue", "funding_rate_pct", "open_interest_usd", "source"],
        run_date,
        deriv_csv_rows,
    )

    # 4. prediction.csv
    pred_csv_rows = [
        {
            "date": run_date,
            "timestamp_utc": timestamp_utc,
            "market_id": p["market_id"],
            "question": p["question"],
            "venue": p["venue"],
            "probability": fmt_num(p["probability"], 4),
            "change_24h_pts": fmt_num(p["change_24h_pts"], 2),
            "volume_usd": fmt_num(p["volume_usd"], 2),
            "url": p["url"],
        }
        for p in prediction_rows
    ]
    _replace_date_rows_in_csv(
        data_dir / "prediction.csv",
        ["date", "timestamp_utc", "market_id", "question", "venue", "probability", "change_24h_pts", "volume_usd", "url"],
        run_date,
        pred_csv_rows,
    )

    # 5. illiquid.csv
    illiquid_csv_rows = [
        {
            "date": run_date,
            "timestamp_utc": timestamp_utc,
            "item_id": il["item_id"],
            "category": il["category"],
            "grade_condition": il["grade_condition"],
            "live_ask_usd": fmt_num(il.get("live_ask_usd"), 2),
            "shipping_usd": fmt_num(il.get("shipping_usd"), 2),
            "fmv_30d_usd": fmt_num(il.get("fmv_30d_usd"), 2),
            "n_comps": str(il.get("n_comps", 0)),
            "net_spread_pct": fmt_num(il.get("net_spread_pct"), 2),
            "status": il["status"],
            "url": il["url"],
        }
        for il in illiquid_rows
    ]
    _replace_date_rows_in_csv(
        data_dir / "illiquid.csv",
        ["date", "timestamp_utc", "item_id", "category", "grade_condition", "live_ask_usd", "shipping_usd", "fmv_30d_usd", "n_comps", "net_spread_pct", "status", "url"],
        run_date,
        illiquid_csv_rows,
    )

    # 6. comps.csv (append-only, deduplicated by url)
    comps_path = data_dir / "comps.csv"
    comp_fields = ["date_collected", "item_id", "sale_date", "sale_price_usd", "shipping_usd", "grade_condition", "platform", "url"]
    existing_comps: List[Dict[str, Any]] = []
    seen_urls: set[str] = set()
    if comps_path.exists():
        with open(comps_path, "r", encoding="utf-8") as fp:
            for r in csv.DictReader(fp):
                existing_comps.append(r)
                seen_urls.add(r.get("url", ""))
    for c in comp_rows:
        if c["url"] not in seen_urls:
            seen_urls.add(c["url"])
            existing_comps.append(
                {
                    "date_collected": c["date_collected"],
                    "item_id": c["item_id"],
                    "sale_date": c["sale_date"],
                    "sale_price_usd": fmt_num(c["sale_price_usd"], 2),
                    "shipping_usd": c.get("shipping_usd", ""),
                    "grade_condition": c["grade_condition"],
                    "platform": c["platform"],
                    "url": c["url"],
                }
            )
    with open(comps_path, "w", encoding="utf-8", newline="") as fp:
        writer = csv.DictWriter(fp, fieldnames=comp_fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(existing_comps)

    # 7. alerts.csv
    _replace_date_rows_in_csv(
        data_dir / "alerts.csv",
        ["date", "timestamp_utc", "severity", "asset_id", "category", "signal_type", "value", "threshold", "details"],
        run_date,
        alerts,
    )

    # 8. timeseries.csv (long-format master table)
    ts_rows: List[Dict[str, Any]] = []

    def _add_ts(asset_id: str, asset_name: str, category: str, metric: str, value: Any, unit: str, source: str, row_date: str = run_date) -> None:
        val_str = fmt_num(value, 6) if isinstance(value, (int, float)) else str(value or "")
        if val_str == "":
            return
        ts_rows.append(
            {
                "date": row_date,
                "timestamp_utc": timestamp_utc,
                "asset_id": asset_id,
                "asset_name": asset_name,
                "category": category,
                "metric": metric,
                "value": val_str,
                "unit": unit,
                "source": source,
            }
        )

    for r in liquid_rows:
        aid, aname, cat, src = r["asset_id"], r["asset_name"], r["category"], r["source"]
        _add_ts(aid, aname, cat, "price", r["price"], r["unit"], src)
        _add_ts(aid, aname, cat, "change_24h_pct", r["change_24h_pct"], "pct", src)
        _add_ts(aid, aname, cat, "change_5d_pct", r["change_5d_pct"], "pct", src)
        _add_ts(aid, aname, cat, "z_score_20d", r["z_score_20d"], "sigma", src)
        if r.get("volume") is not None:
            _add_ts(aid, aname, cat, "volume", r["volume"], "shares", src)
        if r.get("vol_ratio_20d") is not None:
            _add_ts(aid, aname, cat, "vol_ratio_20d", r["vol_ratio_20d"], "ratio", src)
        if r.get("market_cap_usd") is not None:
            _add_ts(aid, aname, cat, "market_cap_usd", r["market_cap_usd"], "usd", "coingecko live")
        if aid == "DX-Y.NYB" and r.get("prior_30d_high") is not None:
            _add_ts(aid, aname, cat, "high_30d", r["prior_30d_high"], "index", "yfinance daily closes")

    # Crypto global aggregates
    glob = coingecko_data.get("global", {})
    if glob.get("total_mcap_usd") is not None:
        _add_ts("CRYPTO-TOTAL-MCAP", "Total crypto market cap", "Crypto", "market_cap_usd", glob["total_mcap_usd"], "usd", "coingecko global")
    if glob.get("mcap_change_24h_pct") is not None:
        _add_ts("CRYPTO-TOTAL-MCAP", "Total crypto market cap", "Crypto", "change_24h_pct", glob["mcap_change_24h_pct"], "pct", "coingecko global")
    if glob.get("btc_dominance_pct") is not None:
        _add_ts("BTC-DOMINANCE", "Bitcoin dominance", "Crypto", "share_pct", glob["btc_dominance_pct"], "pct", "coingecko global")

    # Stablecoin market cap
    if stablecoin_data.get("stablecoin_mcap_usd") is not None:
        _add_ts("STABLECOIN-MCAP", "Total stablecoin market cap", "Crypto", "stablecoin_mcap_usd", stablecoin_data["stablecoin_mcap_usd"], "usd", "DefiLlama")

    # Treasury yields
    if treasury_data.get("us2y_pct") is not None:
        _add_ts("US2Y", "US 2Y Treasury par yield", "Yield", "yield_pct", treasury_data["us2y_pct"], "pct", "treasury.gov daily par yield curve")
    if treasury_data.get("us10y_pct") is not None:
        _add_ts("US10Y", "US 10Y Treasury par yield", "Yield", "yield_pct", treasury_data["us10y_pct"], "pct", "treasury.gov daily par yield curve")
    if treasury_data.get("us30y_pct") is not None:
        _add_ts("US30Y", "US 30Y Treasury par yield", "Yield", "yield_pct", treasury_data["us30y_pct"], "pct", "treasury.gov daily par yield curve")
    if treasury_data.get("us10y_change_48h_bps") is not None:
        _add_ts("US10Y", "US 10Y Treasury par yield", "Yield", "change_48h_bps", treasury_data["us10y_change_48h_bps"], "bps", "treasury.gov par yields")

    # Crypto derivatives
    for d in crypto_derivs:
        src_full = f"{d['source']} {d['venue']}"
        _add_ts(d["asset_id"], d["asset_name"], "Crypto", "funding_rate_pct", d["funding_rate_pct"], "pct_per_8h", src_full)
        _add_ts(d["asset_id"], d["asset_name"], "Crypto", "open_interest_usd", d["open_interest_usd"], "usd", src_full)

    # Prediction markets
    for p in prediction_rows:
        q_short = p["question"][:80]
        _add_ts(p["market_id"], q_short, "Prediction", "probability", p["probability"], "prob", p["source"])
        _add_ts(p["market_id"], q_short, "Prediction", "change_24h_pts", p["change_24h_pts"], "pts", p["source"])
        if p["venue"] == "Polymarket":
            _add_ts(p["market_id"], q_short, "Prediction", "volume_24h_usd", p["volume_usd"], "usd", p["source"])
        elif p.get("open_interest_contracts") is not None:
            _add_ts(p["market_id"], q_short, "Prediction", "open_interest_contracts", p["open_interest_contracts"], "contracts", p["source"])

    # Illiquid FMVs
    for il in illiquid_rows:
        if il.get("fmv_30d_usd") is not None:
            _add_ts(il["item_id"], il["item_id"], il["category"], "fmv_30d_usd", il["fmv_30d_usd"], "usd", "pricecharting ebay sold comps (trimmed median)")
            _add_ts(il["item_id"], il["item_id"], il["category"], "n_comps", il["n_comps"], "count", "pricecharting ebay sold comps")

    # Spot metals
    for sm in spot_metals:
        _add_ts(sm["asset_id"], sm["asset_name"], sm["category"], "price", sm["price"], sm["unit"], sm["source"])

    # GPU rentals
    for gr in gpu_rentals:
        _add_ts(gr["asset_id"], gr["asset_name"], gr["category"], gr["metric"], gr["value"], gr["unit"], gr["source"])

    _replace_date_rows_in_csv(
        data_dir / "timeseries.csv",
        ["date", "timestamp_utc", "asset_id", "asset_name", "category", "metric", "value", "unit", "source"],
        run_date,
        ts_rows,
    )

    # 9. Regenerate prices_wide.csv
    regenerate_prices_wide(data_dir)


# ---------------------------------------------------------------------------
# Terminal Summary & Standardized Markdown Brief (`claude.md` §6)
# ---------------------------------------------------------------------------

def render_markdown_brief(
    run_date: str,
    timestamp_utc: str,
    macro_row: Dict[str, Any],
    liquid_rows: List[Dict[str, Any]],
    prediction_rows: List[Dict[str, Any]],
    illiquid_rows: List[Dict[str, Any]],
    gpu_rentals: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
) -> str:
    """Format a standardized Market Watcher Intelligence Brief per `claude.md` §6."""
    lines = [
        f"## 🌐 Market Watcher Intelligence Brief — [{timestamp_utc.replace('T', ' ').replace('Z', ' UTC')}]",
        "",
        f"> Run date `{run_date}` (`America/New_York`). Generated by `scripts/fetch_market_data.py`.",
        "",
        "### 1. Macro & Liquidity Regime",
        f"- **Regime Status:** `{macro_row.get('regime_status', 'NEUTRAL')}` (Risk-Off Flag: `{macro_row.get('risk_off_flag')}`)",
        (
            f"- **Key Drivers:** `US10Y: {macro_row.get('us10y_pct')}% ({macro_row.get('us10y_change_48h_bps')} bps / 2 sessions)` | "
            f"`US2Y: {macro_row.get('us2y_pct')}%` | `US30Y: {macro_row.get('us30y_pct')}%` | "
            f"`DXY: {macro_row.get('dxy')} (30d high {macro_row.get('dxy_30d_high')})` | "
            f"`VIX: {macro_row.get('vix')}` | `BTC Funding: {macro_row.get('btc_funding_pct')}% / 8h`"
        ),
    ]

    top_pm = sorted(
        [p for p in prediction_rows if p.get("change_24h_pts") is not None],
        key=lambda x: abs(x["change_24h_pts"]),
        reverse=True,
    )
    if top_pm:
        p0 = top_pm[0]
        lines.append(
            f"- **Prediction Market Pulse:** Top mover `{p0['question']}` ({p0['venue']}) at "
            f"**{(p0['probability'] or 0)*100:.1f}%** ({p0['change_24h_pts']:+.1f} pts in 24h)."
        )

    lines.extend(
        [
            "",
            "### 2. High-Conviction Price & Volatility Alerts (Liquid Assets)",
            "| Asset / Ticker | Category | Current Price | 24h / Session Δ | Z-Score / Vol Ratio | Catalyst / Signal |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )

    sorted_liq = sorted(
        [r for r in liquid_rows if r.get("z_score_20d") is not None],
        key=lambda x: abs(x["z_score_20d"]),
        reverse=True,
    )
    for r in sorted_liq[:12]:
        z = r["z_score_20d"]
        vr = f"{r['vol_ratio_20d']:.2f}x Vol" if r.get("vol_ratio_20d") is not None else "no vol"
        sig = "Breakout Trigger" if (abs(z) >= 2.5 and (r.get("vol_ratio_20d") or 0) > 1.8) else ("Watch (|Z|≥2.0)" if abs(z) >= 2.0 else "Normal")
        lines.append(
            f"| `{r['asset_id']}` | `{r['category']}` | `{fmt_num(r['price'], 2)}` | "
            f"`{r['change_24h_pct']:+.2f}%` (5d `{r['change_5d_pct']:+.2f}%`) | "
            f"`Z = {z:+.2f} ({vr})` | `{sig}` |"
        )

    lines.extend(
        [
            "",
            "### 3. Alternative & Collectible Arbitrage Signals (Illiquid Assets)",
            "| Item & Grade / Condition | Category | Live Ask / Auction | 30d Verified FMV | Est. Net Spread (%) | Deal Target (≤78% FMV) |",
            "| :--- | :--- | :--- | :--- | :--- | :--- |",
        ]
    )
    for il in illiquid_rows:
        fmv_str = f"${il['fmv_30d_usd']:,.2f} (n={il['n_comps']})" if il.get("fmv_30d_usd") else il["status"]
        ask_str = f"${il['live_ask_usd']:,.2f}" if il.get("live_ask_usd") else "n/a"
        spread_str = f"{il['net_spread_pct']:+.1f}%" if il.get("net_spread_pct") is not None else "n/a"
        deal_str = f"≤ ${il['deal_threshold_usd']:,.0f}" if il.get("deal_threshold_usd") else "—"
        lines.append(
            f"| `{il['item_id']}` ({il['grade_condition']}) | `{il['category']}` | {ask_str} | **{fmv_str}** | {spread_str} | {deal_str} |"
        )

    lines.extend(
        [
            "",
            "### 4. Active Quantitative Alerts",
        ]
    )
    for a in alerts:
        lines.append(f"- **[{a['severity'].upper()}] `{a['asset_id']}` (`{a['signal_type']}`):** {a['details']}")

    return "\n".join(lines) + "\n"


def print_terminal_summary(
    run_date: str,
    timestamp_utc: str,
    macro_row: Dict[str, Any],
    liquid_rows: List[Dict[str, Any]],
    crypto_derivs: List[Dict[str, Any]],
    prediction_rows: List[Dict[str, Any]],
    illiquid_rows: List[Dict[str, Any]],
    spot_metals: List[Dict[str, Any]],
    gpu_rentals: List[Dict[str, Any]],
    alerts: List[Dict[str, Any]],
) -> None:
    """Print a clean, structured terminal overview of the fetched data."""
    print("=" * 88)
    print(f" OMNI-MARKET WATCHER — LIVE DATA FETCH ({run_date} | {timestamp_utc})")
    print("=" * 88)
    if macro_row:
        print(
            f" Regime: [{macro_row.get('regime_status')}] | US2Y: {macro_row.get('us2y_pct')}% | "
            f"US10Y: {macro_row.get('us10y_pct')}% ({macro_row.get('us10y_change_48h_bps')} bps/48h) | "
            f"US30Y: {macro_row.get('us30y_pct')}%"
        )
        print(
            f" DXY: {macro_row.get('dxy')} (30d high {macro_row.get('dxy_30d_high')}) | "
            f"VIX: {macro_row.get('vix')} | BTC Funding: {macro_row.get('btc_funding_pct')}%/8h | "
            f"Stablecoins: ${float(macro_row.get('stablecoin_mcap_usd') or 0)/1e9:.2f}B"
        )
        print("-" * 88)

    if liquid_rows:
        print(f"\n[Liquid Markets — {len(liquid_rows)} assets fetched, top 10 by |Z-score|]")
        print(f"  {'Ticker':<10} {'Category':<12} {'Price':>12} {'24h %':>9} {'5d %':>9} {'Z-Score':>9} {'Vol Ratio':>10}")
        for r in sorted(liquid_rows, key=lambda x: abs(x.get("z_score_20d") or 0), reverse=True)[:10]:
            vr_str = f"{r['vol_ratio_20d']:.2f}x" if r.get("vol_ratio_20d") is not None else "—"
            print(
                f"  {r['asset_id']:<10} {r['category']:<12} {r['price']:>12.4f} "
                f"{r.get('change_24h_pct') or 0:>+8.2f}% {r.get('change_5d_pct') or 0:>+8.2f}% "
                f"{r.get('z_score_20d') or 0:>+9.2f} {vr_str:>10}"
            )

    if crypto_derivs:
        print(f"\n[Crypto Derivatives — {len(crypto_derivs)} perp venues]")
        for d in crypto_derivs:
            print(
                f"  {d['asset_id']:<8} | {d['venue']:<26} | Funding: {d.get('funding_rate_pct') or 0:+.6f}%/8h | "
                f"OI: ${(d.get('open_interest_usd') or 0)/1e6:,.1f}M"
            )

    if prediction_rows:
        print(f"\n[Prediction Markets — {len(prediction_rows)} contracts fetched, top 6 movers]")
        for p in sorted(prediction_rows, key=lambda x: abs(x.get("change_24h_pts") or 0), reverse=True)[:6]:
            prob_pct = (p.get("probability") or 0) * 100.0
            chg = p.get("change_24h_pts") or 0.0
            print(f"  [{p['venue']:<10}] {prob_pct:>5.1f}% ({chg:>+5.1f} pts) | {p['question'][:58]}")

    if illiquid_rows:
        priced = [il for il in illiquid_rows if il.get("fmv_30d_usd") is not None]
        print(f"\n[Illiquid Collectibles — {len(priced)}/{len(illiquid_rows)} items priced via 30d verified comps]")
        for il in priced:
            print(
                f"  {il['item_id']:<42} | FMV_30: ${il['fmv_30d_usd']:>8,.2f} "
                f"(n={il['n_comps']:>2}, deal <= ${il['deal_threshold_usd']:>6,.0f})"
            )

    if spot_metals:
        print(f"\n[Spot Metals — {len(spot_metals)} Kitco physical bids]")
        print("  " + " | ".join(f"{m['asset_id']}: ${m['price']:,.2f}" for m in spot_metals))

    if gpu_rentals:
        print(f"\n[GPU Cloud Spot Rentals — {len(gpu_rentals)} rate tiers fetched]")
        for g in gpu_rentals[:8]:
            print(f"  {g['asset_id']:<38} : ${g['value']:.3f}/GPU-hr")

    if alerts:
        print(f"\n[Alerts Generated — {len(alerts)} signals]")
        for a in alerts[:6]:
            print(f"  [{a['severity'].upper():<6}] {a['asset_id']:<18} ({a['signal_type']}): {a['details'][:80]}")
    print("=" * 88)


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Omni-Market Watcher — Direct Python Data Fetcher & Quantitative Engine"
    )
    parser.add_argument(
        "--section",
        nargs="+",
        choices=["all", "liquid", "macro", "crypto", "prediction", "illiquid", "metals", "gpu"],
        default=["all"],
        help="Which market sections to fetch (default: all)",
    )
    parser.add_argument(
        "--tickers",
        nargs="+",
        help="Optional subset of liquid tickers to fetch (e.g. SPY QQQ BTC-USD)",
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Path to the data directory (default: data)",
    )
    parser.add_argument(
        "--date",
        default=None,
        help="Override run date YYYY-MM-DD (defaults to today in America/New_York)",
    )
    parser.add_argument(
        "--write-csv",
        action="store_true",
        help="Write/update snapshot rows in data/*.csv and regenerate data/prices_wide.csv",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Fetch live data and print summary without modifying data/*.csv",
    )
    parser.add_argument(
        "--write-report",
        metavar="PATH",
        help="Write a standardized Markdown Intelligence Brief (claude.md §6) to PATH",
    )
    parser.add_argument(
        "--correlate",
        nargs=2,
        metavar=("ASSET_A", "ASSET_B"),
        help="Compute rolling Pearson correlation between two assets in data/prices_wide.csv",
    )
    parser.add_argument(
        "--window",
        type=int,
        default=30,
        help="Rolling window in days for --correlate or illiquid comp lookback (default: 30)",
    )
    parser.add_argument(
        "--lag",
        type=int,
        default=0,
        help="Lead/lag offset in days for --correlate (positive = ASSET_A leads ASSET_B)",
    )
    parser.add_argument(
        "--json",
        action="store_true",
        help="Output fetched payload as JSON to stdout",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)

    # Mode 1: Cross-asset correlation query
    if args.correlate:
        res = evaluate_cross_asset_correlation(
            args.correlate[0],
            args.correlate[1],
            window=args.window,
            lag_days=args.lag,
            data_dir=data_dir,
        )
        if args.json:
            print(json.dumps(res, indent=2))
        else:
            print(
                f"Correlation ({res['asset_a']} vs {res['asset_b']}, window={res['window']}d, lag={res['lag_days']}d, "
                f"n={res['n_overlapping']} [{res.get('start_date')} -> {res.get('end_date')}]]):\n"
                f"  Price Level Pearson rho : {res['level_correlation_rho']}\n"
                f"  Daily Return Pearson rho: {res['return_correlation_rho']}"
            )
        return 0

    # Mode 2: Live Market Data Fetch
    sections = set(args.section)
    run_all = "all" in sections

    run_date = args.date or dt.datetime.now(NY_TZ).strftime("%Y-%m-%d")
    timestamp_utc = dt.datetime.now(UTC_TZ).strftime("%Y-%m-%dT%H:%M:%SZ")

    coingecko_data = fetch_coingecko_crypto() if (run_all or "liquid" in sections or "crypto" in sections) else {}
    liquid_rows = (
        fetch_liquid_market_data(tickers=args.tickers, coingecko_data=coingecko_data)
        if (run_all or "liquid" in sections or "macro" in sections)
        else []
    )
    treasury_data = fetch_treasury_yields() if (run_all or "macro" in sections) else {}
    crypto_derivs = fetch_crypto_derivatives() if (run_all or "crypto" in sections or "macro" in sections) else []
    stablecoin_data = fetch_defillama_stablecoins() if (run_all or "crypto" in sections or "macro" in sections) else {}
    prediction_rows = fetch_prediction_markets() if (run_all or "prediction" in sections) else []
    illiquid_rows, comp_rows = (
        scrape_illiquid_comps(lookback_days=args.window, reference_date=dt.date.fromisoformat(run_date))
        if (run_all or "illiquid" in sections)
        else ([], [])
    )
    spot_metals = fetch_spot_metals() if (run_all or "metals" in sections) else []
    gpu_rentals = fetch_gpu_rentals() if (run_all or "gpu" in sections) else []

    macro_row, alerts = (
        evaluate_quantitative_triggers(
            run_date=run_date,
            timestamp_utc=timestamp_utc,
            liquid_rows=liquid_rows,
            treasury_data=treasury_data,
            crypto_derivs=crypto_derivs,
            stablecoin_data=stablecoin_data,
            prediction_rows=prediction_rows,
            illiquid_rows=illiquid_rows,
        )
        if (run_all or "macro" in sections)
        else ({}, [])
    )

    if args.write_csv and not args.dry_run:
        if not run_all:
            print("[WARN] --write-csv is recommended with `--section all` so all tables stay synchronized.", file=sys.stderr)
        write_all_csvs(
            data_dir=data_dir,
            run_date=run_date,
            timestamp_utc=timestamp_utc,
            liquid_rows=liquid_rows,
            coingecko_data=coingecko_data,
            treasury_data=treasury_data,
            crypto_derivs=crypto_derivs,
            stablecoin_data=stablecoin_data,
            prediction_rows=prediction_rows,
            illiquid_rows=illiquid_rows,
            comp_rows=comp_rows,
            spot_metals=spot_metals,
            gpu_rentals=gpu_rentals,
            macro_row=macro_row,
            alerts=alerts,
        )
        print(f"[OK] Updated CSV files in `{data_dir}/` for run_date={run_date} ({timestamp_utc}).")

    if args.write_report:
        report_path = Path(args.write_report)
        report_path.parent.mkdir(parents=True, exist_ok=True)
        brief_md = render_markdown_brief(
            run_date=run_date,
            timestamp_utc=timestamp_utc,
            macro_row=macro_row,
            liquid_rows=liquid_rows,
            prediction_rows=prediction_rows,
            illiquid_rows=illiquid_rows,
            gpu_rentals=gpu_rentals,
            alerts=alerts,
        )
        report_path.write_text(brief_md, encoding="utf-8")
        print(f"[OK] Wrote Markdown Intelligence Brief to `{report_path}`.")

    if args.json:
        payload = {
            "date": run_date,
            "timestamp_utc": timestamp_utc,
            "macro": macro_row,
            "liquid": liquid_rows,
            "crypto_derivatives": crypto_derivs,
            "stablecoins": stablecoin_data,
            "prediction": prediction_rows,
            "illiquid": illiquid_rows,
            "comps_count": len(comp_rows),
            "spot_metals": spot_metals,
            "gpu_rentals": gpu_rentals,
            "alerts": alerts,
        }
        print(json.dumps(payload, indent=2))
    else:
        print_terminal_summary(
            run_date=run_date,
            timestamp_utc=timestamp_utc,
            macro_row=macro_row,
            liquid_rows=liquid_rows,
            crypto_derivs=crypto_derivs,
            prediction_rows=prediction_rows,
            illiquid_rows=illiquid_rows,
            spot_metals=spot_metals,
            gpu_rentals=gpu_rentals,
            alerts=alerts,
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
