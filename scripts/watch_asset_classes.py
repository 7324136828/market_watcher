#!/usr/bin/env python3
"""
watch_asset_classes.py — Asset-Class Taxonomy Watcher for `omni-market-watcher`.

Watches fourteen asset classes / instrument families, organised in three groups:

  Core asset classes            Stocks (Equities) · Bonds (Fixed Income) · Cash & Cash Equivalents ·
                                Commodities · Real Estate · Alternative Investments
  Investment vehicles           Exchange-Traded Funds (ETFs) · Index Funds · Mutual Funds
  Broader financial instruments Options · Futures & Forwards · Swaps ·
                                Structured Products & Hybrid Securities · Annuities

For every class it tracks a basket of representative, publicly quoted instruments (price, 1d / 5d / 1m /
3m / YTD / 1y total-return changes, 20-day Z-score, volume ratio, 30-day realised volatility, 52-week
drawdown) and a set of class-specific derived metrics:

  * Bonds        Treasury par curve, 2s10s / 3m10y slopes, credit-vs-Treasury relative return
  * Cash         T-bill coupon-equivalent yields, SOFR / EFFR / OBFR, Fed target range
  * Commodities  gold/silver and copper/gold ratios, Brent-WTI spread
  * Real estate  Freddie Mac PMMS mortgage rates and the spread to the 10Y Treasury
  * Index funds  1-year tracking gap against the benchmark ETF
  * Mutual funds 1-year excess return against a benchmark, share of funds beating it
  * Options      VIX term structure, 1-year VIX percentile (IV rank), put/call ratios, ATM IV, 25-delta skew,
                 0DTE volume share (Cboe delayed option chains)
  * Futures      12-month roll yield (contango / backwardation), index-futures basis, FX forward premium
  * Swaps        SOFR-futures strip -> implied par OIS swap rates and swap spreads, Fed-funds-futures path
  * Annuities    model-estimated life-annuity payout rates from the Treasury curve + Gompertz mortality

Where a class has no free public price feed (annuity quotes, OTC swap and CDS quotes, structured notes),
the script watches the closest listed proxies and says so: every class carries a `coverage` tag of
`direct`, `proxy` or `model`, and nothing is extrapolated when a source is unavailable (cells stay empty).

Sources (no API keys): Yahoo Finance v8 chart API, treasury.gov daily par-yield and bill-rate CSVs,
NY Fed Markets API (reference rates), Cboe delayed-quote option chains, Freddie Mac PMMS history CSV.

Usage:
  python3 scripts/watch_asset_classes.py --dry-run
  python3 scripts/watch_asset_classes.py --write-csv
  python3 scripts/watch_asset_classes.py --write-csv --write-report reports/asset-classes-YYYY-MM-DD.md
  python3 scripts/watch_asset_classes.py --classes stocks bonds options --dry-run
  python3 scripts/watch_asset_classes.py --list
  python3 scripts/watch_asset_classes.py --dry-run --json
"""

from __future__ import annotations

import argparse
import calendar
import csv
import datetime as dt
import json
import math
import re
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = dt.timezone.utc
USER_AGENT = "Mozilla/5.0"
MONTH_CODES = "FGHJKMNQUVXZ"

# ---------------------------------------------------------------------------
# Asset-class universe
# ---------------------------------------------------------------------------
# Each instrument: (ticker, display name, sub-class, unit). unit: usd | pct (a yield) | index | fx

GROUPS = ["Core asset classes", "Investment vehicles", "Broader financial instruments"]

UNIVERSE: Dict[str, Dict[str, Any]] = {
    "stocks": {
        "name": "Stocks (Equities)",
        "group": GROUPS[0],
        "coverage": "direct",
        "note": "Headline indices plus the largest US-listed companies.",
        "instruments": [
            ("^GSPC", "S&P 500", "US index", "index"),
            ("^DJI", "Dow Jones Industrial Average", "US index", "index"),
            ("^IXIC", "Nasdaq Composite", "US index", "index"),
            ("^RUT", "Russell 2000", "US index", "index"),
            ("^FTSE", "FTSE 100", "Global index", "index"),
            ("^GDAXI", "DAX", "Global index", "index"),
            ("^STOXX50E", "Euro Stoxx 50", "Global index", "index"),
            ("^N225", "Nikkei 225", "Global index", "index"),
            ("^HSI", "Hang Seng", "Global index", "index"),
            ("AAPL", "Apple", "Mega-cap stock", "usd"),
            ("MSFT", "Microsoft", "Mega-cap stock", "usd"),
            ("NVDA", "NVIDIA", "Mega-cap stock", "usd"),
            ("AMZN", "Amazon", "Mega-cap stock", "usd"),
            ("GOOGL", "Alphabet", "Mega-cap stock", "usd"),
            ("META", "Meta Platforms", "Mega-cap stock", "usd"),
            ("TSLA", "Tesla", "Mega-cap stock", "usd"),
            ("AVGO", "Broadcom", "Mega-cap stock", "usd"),
            ("BRK-B", "Berkshire Hathaway B", "Mega-cap stock", "usd"),
            ("JPM", "JPMorgan Chase", "Mega-cap stock", "usd"),
            ("LLY", "Eli Lilly", "Mega-cap stock", "usd"),
            ("V", "Visa", "Mega-cap stock", "usd"),
            ("XOM", "Exxon Mobil", "Mega-cap stock", "usd"),
            ("WMT", "Walmart", "Mega-cap stock", "usd"),
            ("UNH", "UnitedHealth", "Mega-cap stock", "usd"),
        ],
    },
    "bonds": {
        "name": "Bonds (Fixed Income)",
        "group": GROUPS[0],
        "coverage": "direct",
        "note": "Treasury yields are direct; corporate, municipal, EM and securitised bonds are watched "
        "through the largest bond ETFs because individual bonds trade over the counter.",
        "instruments": [
            ("^IRX", "13-Week T-Bill Yield", "Treasury yield", "pct"),
            ("^FVX", "5-Year Treasury Yield", "Treasury yield", "pct"),
            ("^TNX", "10-Year Treasury Yield", "Treasury yield", "pct"),
            ("^TYX", "30-Year Treasury Yield", "Treasury yield", "pct"),
            ("SHY", "iShares 1-3Y Treasury", "Treasury ETF", "usd"),
            ("IEF", "iShares 7-10Y Treasury", "Treasury ETF", "usd"),
            ("TLT", "iShares 20+Y Treasury", "Treasury ETF", "usd"),
            ("TIP", "iShares TIPS", "Inflation-linked", "usd"),
            ("AGG", "iShares Core US Aggregate", "Broad market", "usd"),
            ("BND", "Vanguard Total Bond Market", "Broad market", "usd"),
            ("LQD", "iShares IG Corporate", "Investment-grade credit", "usd"),
            ("HYG", "iShares High Yield Corporate", "High-yield credit", "usd"),
            ("JNK", "SPDR High Yield", "High-yield credit", "usd"),
            ("MUB", "iShares National Muni", "Municipal", "usd"),
            ("EMB", "iShares USD Emerging Markets", "Emerging-market debt", "usd"),
            ("BNDX", "Vanguard Total International Bond", "International", "usd"),
            ("FLOT", "iShares Floating Rate", "Floating rate", "usd"),
        ],
    },
    "cash": {
        "name": "Cash & Cash Equivalents",
        "group": GROUPS[0],
        "coverage": "direct",
        "note": "Money-market funds hold a constant $1 NAV, so the class is watched through T-bill and "
        "ultra-short ETFs plus the bill and overnight reference rates that set cash yields.",
        "instruments": [
            ("BIL", "SPDR 1-3 Month T-Bill", "T-bill ETF", "usd"),
            ("SGOV", "iShares 0-3 Month Treasury", "T-bill ETF", "usd"),
            ("SHV", "iShares Short Treasury", "T-bill ETF", "usd"),
            ("USFR", "WisdomTree Floating Rate Treasury", "Floating-rate Treasury", "usd"),
            ("ICSH", "iShares Ultra Short-Term Bond", "Ultra-short bond", "usd"),
            ("JPST", "JPMorgan Ultra-Short Income", "Ultra-short bond", "usd"),
            ("MINT", "PIMCO Enhanced Short Maturity", "Ultra-short bond", "usd"),
        ],
    },
    "commodities": {
        "name": "Commodities",
        "group": GROUPS[0],
        "coverage": "direct",
        "note": "Front-month futures for energy, metals and agriculture plus broad commodity ETFs.",
        "instruments": [
            ("CL=F", "WTI Crude Oil", "Energy", "usd"),
            ("BZ=F", "Brent Crude Oil", "Energy", "usd"),
            ("NG=F", "Natural Gas", "Energy", "usd"),
            ("RB=F", "RBOB Gasoline", "Energy", "usd"),
            ("HO=F", "Heating Oil", "Energy", "usd"),
            ("GC=F", "Gold", "Precious metal", "usd"),
            ("SI=F", "Silver", "Precious metal", "usd"),
            ("PL=F", "Platinum", "Precious metal", "usd"),
            ("PA=F", "Palladium", "Precious metal", "usd"),
            ("HG=F", "Copper", "Industrial metal", "usd"),
            ("ZC=F", "Corn", "Agriculture", "usd"),
            ("ZW=F", "Wheat", "Agriculture", "usd"),
            ("ZS=F", "Soybeans", "Agriculture", "usd"),
            ("KC=F", "Coffee", "Agriculture", "usd"),
            ("SB=F", "Sugar", "Agriculture", "usd"),
            ("CC=F", "Cocoa", "Agriculture", "usd"),
            ("CT=F", "Cotton", "Agriculture", "usd"),
            ("LE=F", "Live Cattle", "Agriculture", "usd"),
            ("DBC", "Invesco DB Commodity Index", "Broad basket ETF", "usd"),
            ("GLD", "SPDR Gold Shares", "Physical gold ETF", "usd"),
            ("SLV", "iShares Silver Trust", "Physical silver ETF", "usd"),
            ("USO", "United States Oil Fund", "Oil ETF", "usd"),
        ],
    },
    "real_estate": {
        "name": "Real Estate",
        "group": GROUPS[0],
        "coverage": "proxy",
        "note": "Direct property is illiquid and appraisal-priced; the class is watched through listed REITs, "
        "REIT ETFs and homebuilders, plus Freddie Mac mortgage rates.",
        "instruments": [
            ("VNQ", "Vanguard Real Estate", "REIT ETF", "usd"),
            ("IYR", "iShares US Real Estate", "REIT ETF", "usd"),
            ("XLRE", "Real Estate Select Sector SPDR", "REIT ETF", "usd"),
            ("SCHH", "Schwab US REIT", "REIT ETF", "usd"),
            ("VNQI", "Vanguard Global ex-US Real Estate", "International REIT ETF", "usd"),
            ("REM", "iShares Mortgage Real Estate", "Mortgage REIT ETF", "usd"),
            ("ITB", "iShares US Home Construction", "Homebuilders", "usd"),
            ("XHB", "SPDR S&P Homebuilders", "Homebuilders", "usd"),
            ("PLD", "Prologis", "Industrial REIT", "usd"),
            ("AMT", "American Tower", "Tower REIT", "usd"),
            ("EQIX", "Equinix", "Data-centre REIT", "usd"),
            ("DLR", "Digital Realty", "Data-centre REIT", "usd"),
            ("WELL", "Welltower", "Healthcare REIT", "usd"),
            ("SPG", "Simon Property Group", "Retail REIT", "usd"),
            ("O", "Realty Income", "Net-lease REIT", "usd"),
            ("PSA", "Public Storage", "Storage REIT", "usd"),
            ("MAA", "Mid-America Apartment Communities", "Residential REIT", "usd"),
            ("VICI", "VICI Properties", "Gaming REIT", "usd"),
        ],
    },
    "alternatives": {
        "name": "Alternative Investments",
        "group": GROUPS[0],
        "coverage": "proxy",
        "note": "Crypto is direct. Private equity, private credit and hedge funds publish no daily prices, so "
        "they are watched through listed managers, BDCs and liquid-alternative ETFs. Collectibles and GPU "
        "rentals are covered by scripts/fetch_market_data.py.",
        "instruments": [
            ("BTC-USD", "Bitcoin", "Crypto", "usd"),
            ("ETH-USD", "Ethereum", "Crypto", "usd"),
            ("SOL-USD", "Solana", "Crypto", "usd"),
            ("BX", "Blackstone", "Listed private-markets manager", "usd"),
            ("KKR", "KKR", "Listed private-markets manager", "usd"),
            ("APO", "Apollo Global", "Listed private-markets manager", "usd"),
            ("ARES", "Ares Management", "Listed private-markets manager", "usd"),
            ("PSP", "Invesco Global Listed Private Equity", "Listed private equity", "usd"),
            ("BIZD", "VanEck BDC Income", "Private credit (BDCs)", "usd"),
            ("ARCC", "Ares Capital", "Private credit (BDCs)", "usd"),
            ("QAI", "NYLI Hedge Multi-Strategy Tracker", "Hedge-fund replication", "usd"),
            ("DBMF", "iMGP DBi Managed Futures", "Managed futures", "usd"),
            ("KMLM", "KraneShares Mount Lucas Managed Futures", "Managed futures", "usd"),
            ("CTA", "Simplify Managed Futures", "Managed futures", "usd"),
            ("BTAL", "AGF US Market Neutral Anti-Beta", "Market neutral", "usd"),
            ("MNA", "NYLI Merger Arbitrage", "Merger arbitrage", "usd"),
            ("IGF", "iShares Global Infrastructure", "Infrastructure", "usd"),
            ("AMLP", "Alerian MLP", "Energy infrastructure (MLPs)", "usd"),
            ("WOOD", "iShares Global Timber & Forestry", "Timber", "usd"),
        ],
    },
    "etfs": {
        "name": "Exchange-Traded Funds (ETFs)",
        "group": GROUPS[1],
        "coverage": "direct",
        "note": "The largest and most traded equity ETFs across market cap, region, style, factor and sector.",
        "instruments": [
            ("SPY", "SPDR S&P 500", "US large cap", "usd"),
            ("IVV", "iShares Core S&P 500", "US large cap", "usd"),
            ("VOO", "Vanguard S&P 500", "US large cap", "usd"),
            ("VTI", "Vanguard Total Stock Market", "US total market", "usd"),
            ("QQQ", "Invesco QQQ", "US large-cap growth", "usd"),
            ("DIA", "SPDR Dow Jones Industrial", "US large cap", "usd"),
            ("IWM", "iShares Russell 2000", "US small cap", "usd"),
            ("RSP", "Invesco S&P 500 Equal Weight", "US equal weight", "usd"),
            ("VEA", "Vanguard Developed Markets", "International developed", "usd"),
            ("EFA", "iShares MSCI EAFE", "International developed", "usd"),
            ("VWO", "Vanguard Emerging Markets", "Emerging markets", "usd"),
            ("EEM", "iShares MSCI Emerging Markets", "Emerging markets", "usd"),
            ("VUG", "Vanguard Growth", "Style: growth", "usd"),
            ("VTV", "Vanguard Value", "Style: value", "usd"),
            ("MTUM", "iShares MSCI USA Momentum", "Factor: momentum", "usd"),
            ("QUAL", "iShares MSCI USA Quality", "Factor: quality", "usd"),
            ("USMV", "iShares MSCI USA Min Vol", "Factor: low volatility", "usd"),
            ("SCHD", "Schwab US Dividend Equity", "Dividend", "usd"),
            ("SMH", "VanEck Semiconductor", "Thematic", "usd"),
            ("ARKK", "ARK Innovation", "Thematic (active)", "usd"),
            ("XLK", "Technology Select Sector SPDR", "Sector", "usd"),
            ("XLF", "Financial Select Sector SPDR", "Sector", "usd"),
            ("XLE", "Energy Select Sector SPDR", "Sector", "usd"),
            ("XLV", "Health Care Select Sector SPDR", "Sector", "usd"),
        ],
    },
    "index_funds": {
        "name": "Index Funds",
        "group": GROUPS[1],
        "coverage": "direct",
        "note": "Index mutual funds, priced once a day at NAV. Each is compared with the ETF tracking the "
        "same benchmark.",
        "instruments": [
            ("VFIAX", "Vanguard 500 Index Admiral", "S&P 500", "usd"),
            ("FXAIX", "Fidelity 500 Index", "S&P 500", "usd"),
            ("SWPPX", "Schwab S&P 500 Index", "S&P 500", "usd"),
            ("VTSAX", "Vanguard Total Stock Market Admiral", "US total market", "usd"),
            ("FSKAX", "Fidelity Total Market Index", "US total market", "usd"),
            ("VIMAX", "Vanguard Mid-Cap Index Admiral", "US mid cap", "usd"),
            ("VSMAX", "Vanguard Small-Cap Index Admiral", "US small cap", "usd"),
            ("VTIAX", "Vanguard Total International Admiral", "International", "usd"),
            ("FTIHX", "Fidelity Total International Index", "International", "usd"),
            ("VBTLX", "Vanguard Total Bond Market Admiral", "US bonds", "usd"),
            ("FXNAX", "Fidelity US Bond Index", "US bonds", "usd"),
        ],
    },
    "mutual_funds": {
        "name": "Mutual Funds",
        "group": GROUPS[1],
        "coverage": "direct",
        "note": "Large actively managed mutual funds, priced once a day at NAV, each measured against a "
        "passive benchmark ETF.",
        "instruments": [
            ("FCNTX", "Fidelity Contrafund", "US large-cap growth", "usd"),
            ("AGTHX", "American Funds Growth Fund of America", "US large-cap growth", "usd"),
            ("PRGFX", "T. Rowe Price Growth Stock", "US large-cap growth", "usd"),
            ("TRBCX", "T. Rowe Price Blue Chip Growth", "US large-cap growth", "usd"),
            ("VPMAX", "Vanguard PRIMECAP Admiral", "US large-cap growth", "usd"),
            ("AWSHX", "American Funds Washington Mutual", "US large-cap value", "usd"),
            ("DODGX", "Dodge & Cox Stock", "US large-cap value", "usd"),
            ("OAKMX", "Oakmark Fund", "US large-cap value", "usd"),
            ("DODFX", "Dodge & Cox International Stock", "International equity", "usd"),
            ("ABALX", "American Funds American Balanced", "Balanced", "usd"),
            ("VWELX", "Vanguard Wellington", "Balanced", "usd"),
            ("VWINX", "Vanguard Wellesley Income", "Conservative allocation", "usd"),
            ("VFIFX", "Vanguard Target Retirement 2050", "Target date", "usd"),
            ("PTTRX", "PIMCO Total Return Institutional", "Core-plus bond", "usd"),
            ("DODIX", "Dodge & Cox Income", "Core-plus bond", "usd"),
            ("FAGIX", "Fidelity Capital & Income", "High-yield bond", "usd"),
        ],
    },
    "options": {
        "name": "Options",
        "group": GROUPS[2],
        "coverage": "direct",
        "note": "Implied-volatility indices are the instruments here (a rise means options got more "
        "expensive). Put/call ratios, ATM IV and skew come from Cboe delayed option chains.",
        "instruments": [
            ("^VIX", "Cboe Volatility Index (30d S&P 500)", "Equity implied vol", "index"),
            ("^VIX9D", "Cboe 9-Day Volatility", "Equity implied vol", "index"),
            ("^VIX3M", "Cboe 3-Month Volatility", "Equity implied vol", "index"),
            ("^VIX6M", "Cboe 6-Month Volatility", "Equity implied vol", "index"),
            ("^VXN", "Cboe Nasdaq-100 Volatility", "Equity implied vol", "index"),
            ("^VXD", "Cboe DJIA Volatility", "Equity implied vol", "index"),
            ("^VVIX", "Cboe VIX of VIX", "Vol of vol", "index"),
            ("^SKEW", "Cboe SKEW Index", "Tail-risk pricing", "index"),
            ("^MOVE", "ICE BofA MOVE (Treasury vol)", "Rates implied vol", "index"),
            ("^OVX", "Cboe Crude Oil Volatility", "Commodity implied vol", "index"),
            ("^GVZ", "Cboe Gold Volatility", "Commodity implied vol", "index"),
        ],
    },
    "futures_forwards": {
        "name": "Futures & Forwards",
        "group": GROUPS[2],
        "coverage": "direct",
        "note": "Front-month index, Treasury, FX and crypto futures (commodity futures sit under Commodities). "
        "Forwards trade over the counter, so forward pricing is read from dated futures against spot.",
        "instruments": [
            ("ES=F", "E-mini S&P 500", "Equity index future", "index"),
            ("NQ=F", "E-mini Nasdaq-100", "Equity index future", "index"),
            ("YM=F", "E-mini Dow", "Equity index future", "index"),
            ("RTY=F", "E-mini Russell 2000", "Equity index future", "index"),
            ("ZT=F", "2-Year T-Note", "Treasury future", "usd"),
            ("ZF=F", "5-Year T-Note", "Treasury future", "usd"),
            ("ZN=F", "10-Year T-Note", "Treasury future", "usd"),
            ("ZB=F", "30-Year T-Bond", "Treasury future", "usd"),
            ("6E=F", "Euro FX", "FX future", "fx"),
            ("6J=F", "Japanese Yen", "FX future", "fx"),
            ("6B=F", "British Pound", "FX future", "fx"),
            ("BTC=F", "CME Bitcoin", "Crypto future", "usd"),
        ],
    },
    "swaps": {
        "name": "Swaps",
        "group": GROUPS[2],
        "coverage": "proxy",
        "note": "Swap and CDS quotes are dealer/OTC data with no free feed. Interest-rate swaps are watched "
        "through par OIS rates implied from the dated SOFR futures strip, Fed-funds futures, and listed funds "
        "that hold rate swaps, swaptions or CDS. Currency, equity and total-return swaps are not covered.",
        "instruments": [
            ("ZQ=F", "30-Day Fed Funds Future", "OIS pricing", "usd"),
            ("LQDH", "iShares Interest Rate Hedged Corporate Bond", "Holds pay-fixed rate swaps", "usd"),
            ("HYGH", "iShares Interest Rate Hedged High Yield Bond", "Holds pay-fixed rate swaps", "usd"),
            ("PFIX", "Simplify Interest Rate Hedge", "Holds payer swaptions", "usd"),
            ("IVOL", "Quadratic Interest Rate Volatility & Inflation Hedge", "Holds OTC rate options", "usd"),
            ("CDX", "Simplify High Yield (credit-hedge overlay)", "Holds CDS / total-return swaps", "usd"),
        ],
    },
    "structured": {
        "name": "Structured Products & Hybrid Securities",
        "group": GROUPS[2],
        "coverage": "proxy",
        "note": "Bank-issued structured notes have no public quotes. The class is watched through listed "
        "funds of hybrids (preferreds, convertibles), defined-outcome and option-income strategies, and "
        "securitised credit (MBS, CLO, CMBS, leveraged loans).",
        "instruments": [
            ("PFF", "iShares Preferred & Income", "Preferred (hybrid)", "usd"),
            ("PGX", "Invesco Preferred", "Preferred (hybrid)", "usd"),
            ("PFFD", "Global X US Preferred", "Preferred (hybrid)", "usd"),
            ("FPE", "First Trust Preferred Securities", "Preferred (hybrid)", "usd"),
            ("CWB", "SPDR Convertible Securities", "Convertible (hybrid)", "usd"),
            ("ICVT", "iShares Convertible Bond", "Convertible (hybrid)", "usd"),
            ("BUFR", "FT Vest Laddered Buffer", "Defined outcome (buffer)", "usd"),
            ("PJAN", "Innovator US Equity Power Buffer Jan", "Defined outcome (buffer)", "usd"),
            ("JEPI", "JPMorgan Equity Premium Income (ELNs)", "Option income", "usd"),
            ("JEPQ", "JPMorgan Nasdaq Equity Premium Income", "Option income", "usd"),
            ("QYLD", "Global X Nasdaq 100 Covered Call", "Option income", "usd"),
            ("XYLD", "Global X S&P 500 Covered Call", "Option income", "usd"),
            ("DIVO", "Amplify CWP Enhanced Dividend Income", "Option income", "usd"),
            ("MBB", "iShares MBS", "Agency MBS", "usd"),
            ("VMBS", "Vanguard Mortgage-Backed Securities", "Agency MBS", "usd"),
            ("CMBS", "iShares CMBS", "Commercial MBS", "usd"),
            ("JAAA", "Janus Henderson AAA CLO", "CLO", "usd"),
            ("JBBB", "Janus Henderson B-BBB CLO", "CLO", "usd"),
            ("CLOA", "iShares AAA CLO Active", "CLO", "usd"),
            ("BKLN", "Invesco Senior Loan", "Leveraged loans", "usd"),
            ("SRLN", "SPDR Blackstone Senior Loan", "Leveraged loans", "usd"),
        ],
    },
    "annuities": {
        "name": "Annuities",
        "group": GROUPS[2],
        "coverage": "model",
        "note": "Annuities are insurance contracts with no market price or free quote feed. The script "
        "estimates actuarially fair life-annuity payout rates from the Treasury curve and a Gompertz "
        "mortality law (no insurer loads or credit spread, so not a quote), and watches the listed "
        "life/annuity insurers that write them.",
        "instruments": [
            ("MET", "MetLife", "Life/annuity insurer", "usd"),
            ("PRU", "Prudential Financial", "Life/annuity insurer", "usd"),
            ("CRBG", "Corebridge Financial", "Life/annuity insurer", "usd"),
            ("EQH", "Equitable Holdings", "Life/annuity insurer", "usd"),
            ("LNC", "Lincoln National", "Life/annuity insurer", "usd"),
            ("PFG", "Principal Financial", "Life/annuity insurer", "usd"),
            ("JXN", "Jackson Financial", "Life/annuity insurer", "usd"),
            ("BHF", "Brighthouse Financial", "Life/annuity insurer", "usd"),
            ("KIE", "SPDR S&P Insurance", "Insurance ETF", "usd"),
            ("IAK", "iShares US Insurance", "Insurance ETF", "usd"),
        ],
    },
}

CLASS_ORDER = list(UNIVERSE.keys())

# Benchmark ETFs for index funds (tracking gap) and mutual funds (excess return).
INDEX_FUND_BENCHMARKS = {
    "VFIAX": "SPY", "FXAIX": "SPY", "SWPPX": "SPY", "VTSAX": "VTI", "FSKAX": "VTI", "VIMAX": "VO",
    "VSMAX": "VB", "VTIAX": "VXUS", "FTIHX": "VXUS", "VBTLX": "AGG", "FXNAX": "AGG",
}
MUTUAL_FUND_BENCHMARKS = {
    "FCNTX": "SPY", "AGTHX": "SPY", "PRGFX": "SPY", "TRBCX": "SPY", "VPMAX": "SPY", "AWSHX": "SPY",
    "DODGX": "SPY", "OAKMX": "SPY", "DODFX": "VXUS", "ABALX": "AOR", "VWELX": "AOR", "VWINX": "AOK",
    "VFIFX": "AOA", "PTTRX": "AGG", "DODIX": "AGG", "FAGIX": "HYG",
}
# Tickers fetched for calculations only (not listed under a class unless they also appear above).
HELPER_TICKERS = ["VO", "VB", "VXUS", "AOR", "AOK", "AOA", "^NDX", "EURUSD=X", "GBPUSD=X", "USDJPY=X"]

OPTION_CHAIN_UNDERLYINGS = ["SPY", "QQQ", "IWM", "TLT", "GLD"]

# 12-month futures term structures: (root, exchange suffix, tradable months, label)
TERM_STRUCTURES = [
    ("CL", "NYM", list(range(1, 13)), "WTI crude"),
    ("NG", "NYM", list(range(1, 13)), "Natural gas"),
    ("GC", "CMX", [2, 4, 6, 8, 12], "Gold"),
    ("SI", "CMX", [3, 5, 7, 9, 12], "Silver"),
    ("HG", "CMX", [3, 5, 7, 9, 12], "Copper"),
    ("ZC", "CBT", [3, 5, 7, 9, 12], "Corn"),
]
# Dated future vs spot: (root, suffix, months, spot ticker, label, expiry rule, invert spot)
BASIS_PAIRS = [
    ("ES", "CME", [3, 6, 9, 12], "^GSPC", "S&P 500", "third_friday", False),
    ("NQ", "CME", [3, 6, 9, 12], "^NDX", "Nasdaq-100", "third_friday", False),
    ("6E", "CME", [3, 6, 9, 12], "EURUSD=X", "EUR/USD", "imm", False),
    ("6B", "CME", [3, 6, 9, 12], "GBPUSD=X", "GBP/USD", "imm", False),
    ("6J", "CME", [3, 6, 9, 12], "USDJPY=X", "JPY/USD", "imm", True),
    ("BTC", "CME", list(range(1, 13)), "BTC-USD", "Bitcoin", "last_friday", False),
]

PAR_CURVE_TENORS = [
    ("1 Mo", "1m", 1 / 12), ("2 Mo", "2m", 2 / 12), ("3 Mo", "3m", 0.25), ("4 Mo", "4m", 4 / 12),
    ("6 Mo", "6m", 0.5), ("1 Yr", "1y", 1.0), ("2 Yr", "2y", 2.0), ("3 Yr", "3y", 3.0), ("5 Yr", "5y", 5.0),
    ("7 Yr", "7y", 7.0), ("10 Yr", "10y", 10.0), ("20 Yr", "20y", 20.0), ("30 Yr", "30y", 30.0),
]

# Alert thresholds (Z-score rule mirrors claude.md §4.1)
Z_TRIGGER = 2.5
VOL_RATIO_TRIGGER = 1.8
CLASS_MOVE_5D_PCT = 3.0
IV_RANK_TRIGGER = 80.0
ROLL_YIELD_EXTREME_PCT = 10.0
STALE_DAYS = 10


# ---------------------------------------------------------------------------
# HTTP + small helpers
# ---------------------------------------------------------------------------

def http_get(url: str, timeout: int = 25, retries: int = 4) -> str:
    """GET with a browser User-Agent; retries on 429 / 5xx / transient network errors."""
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
        time.sleep(1.5 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError(f"GET failed: {url}")


def warn(msg: str) -> None:
    print(f"[WARN] {msg}", file=sys.stderr)


def to_float(val: Any) -> Optional[float]:
    try:
        if val is None or val == "":
            return None
        f = float(val)
        return f if math.isfinite(f) else None
    except (TypeError, ValueError):
        return None


def median(vals: List[Optional[float]]) -> Optional[float]:
    clean = [v for v in vals if v is not None]
    return statistics.median(clean) if clean else None


def fmt(val: Optional[float], decimals: int = 2, suffix: str = "", sign: bool = False) -> str:
    if val is None:
        return "—"
    return f"{val:+,.{decimals}f}{suffix}" if sign else f"{val:,.{decimals}f}{suffix}"


def fmt_price(val: float) -> str:
    """Readable price at any magnitude (65,480 / 356.30 / 1.1040 / 0.006273)."""
    if abs(val) >= 1000:
        return f"{val:,.0f}"
    if abs(val) >= 10:
        return f"{val:,.2f}"
    return f"{val:,.4f}" if abs(val) >= 0.1 else f"{val:.4g}"


def third_weekday(year: int, month: int, weekday: int) -> dt.date:
    """Third given weekday (Mon=0) of a month — IMM dates use Wednesday, equity expiries Friday."""
    first = dt.date(year, month, 1)
    offset = (weekday - first.weekday()) % 7
    return first + dt.timedelta(days=offset + 14)


def last_friday(year: int, month: int) -> dt.date:
    d = dt.date(year, month, calendar.monthrange(year, month)[1])
    return d - dt.timedelta(days=(d.weekday() - 4) % 7)


def add_months(year: int, month: int, n: int) -> Tuple[int, int]:
    idx = year * 12 + (month - 1) + n
    return idx // 12, idx % 12 + 1


def contract_ticker(root: str, year: int, month: int, suffix: str) -> str:
    return f"{root}{MONTH_CODES[month - 1]}{year % 100:02d}.{suffix}"


def expiry_date(rule: str, year: int, month: int) -> dt.date:
    if rule == "third_friday":
        return third_weekday(year, month, 4)
    if rule == "imm":  # CME FX: second business day before the third Wednesday
        return third_weekday(year, month, 2) - dt.timedelta(days=2)
    return last_friday(year, month)


# ---------------------------------------------------------------------------
# Yahoo Finance price history
# ---------------------------------------------------------------------------

def fetch_yahoo_history(ticker: str, range_str: str = "1y") -> Optional[Dict[str, Any]]:
    """Daily bars from the Yahoo v8 chart API. Returns None when the symbol has no usable data."""
    url = (
        f"https://query1.finance.yahoo.com/v8/finance/chart/{urllib.parse.quote(ticker)}"
        f"?range={range_str}&interval=1d"
    )
    try:
        res = json.loads(http_get(url))["chart"]["result"][0]
    except Exception as exc:
        warn(f"Yahoo fetch failed for {ticker}: {exc}")
        return None
    meta = res.get("meta", {})
    offset = int(meta.get("gmtoffset") or 0)
    stamps = res.get("timestamp") or []
    quote = (res.get("indicators", {}).get("quote") or [{}])[0]
    closes = quote.get("close") or []
    vols = quote.get("volume") or [None] * len(closes)
    adj_block = res.get("indicators", {}).get("adjclose") or [{}]
    adjs = adj_block[0].get("adjclose") or [None] * len(closes)
    by_date: Dict[str, Tuple[float, float, Optional[float]]] = {}
    for ts, c, a, v in zip(stamps, closes, adjs, vols):
        if c is None or c <= 0:
            continue
        day = dt.datetime.fromtimestamp(ts + offset, UTC_TZ).strftime("%Y-%m-%d")
        by_date[day] = (float(c), float(a) if a and a > 0 else float(c), float(v) if v else None)
    if not by_date:
        return None
    dates = sorted(by_date)
    return {
        "ticker": ticker,
        "dates": dates,
        "close": [by_date[d][0] for d in dates],
        "adj": [by_date[d][1] for d in dates],
        "volume": [by_date[d][2] for d in dates],
        "name": meta.get("longName") or meta.get("shortName") or ticker,
        "instrument_type": meta.get("instrumentType") or "",
    }


def fetch_many(tickers: List[str], range_str: str = "1y", workers: int = 6) -> Dict[str, Dict[str, Any]]:
    unique = list(dict.fromkeys(tickers))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        results = list(pool.map(lambda t: fetch_yahoo_history(t, range_str), unique))
    return {t: r for t, r in zip(unique, results) if r}


def compute_snapshot(series: Dict[str, Any], today: dt.date) -> Optional[Dict[str, Any]]:
    """Per-instrument metrics. Returns are total return (adjusted close); price is the raw close."""
    close, adj, vols, dates = series["close"], series["adj"], series["volume"], series["dates"]
    n = len(close)
    if n < 2:
        return None
    last_date = dt.date.fromisoformat(dates[-1])
    if (today - last_date).days > STALE_DAYS:
        warn(f"{series['ticker']}: last bar {dates[-1]} is stale, skipped")
        return None

    def ret(k: int) -> Optional[float]:
        return (adj[-1] / adj[-1 - k] - 1.0) * 100.0 if n > k else None

    ytd = None
    prior_year = [i for i, d in enumerate(dates) if d[:4] < dates[-1][:4]]
    if prior_year:
        ytd = (adj[-1] / adj[prior_year[-1]] - 1.0) * 100.0

    z = None
    if n >= 20:
        window = close[-20:]
        sd = statistics.pstdev(window)
        if sd > 0:
            z = (close[-1] - statistics.fmean(window)) / sd

    vol_ratio = None
    recent = [v for v in vols[-20:] if v]
    if vols[-1] and recent:
        vol_ratio = vols[-1] / (sum(recent) / len(recent))

    rvol = None
    if n >= 31:
        rets = [math.log(adj[i] / adj[i - 1]) for i in range(n - 30, n)]
        rvol = statistics.pstdev(rets) * math.sqrt(252) * 100.0

    high_52w, low_52w = max(close), min(close)
    full_year = n >= 200
    return {
        "price": close[-1],
        "change_1d_abs": close[-1] - close[-2],
        "change_1d_pct": ret(1),
        "change_5d_pct": ret(5),
        "change_1m_pct": ret(21),
        "change_3m_pct": ret(63),
        "change_ytd_pct": ytd,
        "change_1y_pct": (adj[-1] / adj[0] - 1.0) * 100.0 if full_year else None,
        "z_score_20d": z,
        "volume": vols[-1],
        "vol_ratio_20d": vol_ratio,
        "realized_vol_30d_pct": rvol,
        "drawdown_52w_pct": (close[-1] / high_52w - 1.0) * 100.0,
        "above_50dma": (close[-1] > statistics.fmean(close[-50:])) if n >= 50 else None,
        "above_200dma": (close[-1] > statistics.fmean(close[-200:])) if n >= 200 else None,
        "is_52w_high": full_year and close[-1] >= high_52w,
        "is_52w_low": full_year and close[-1] <= low_52w,
        "last_bar_date": dates[-1],
    }


# ---------------------------------------------------------------------------
# Non-Yahoo sources
# ---------------------------------------------------------------------------

def _treasury_csv(kind: str, year: int) -> List[Dict[str, str]]:
    url = (
        "https://home.treasury.gov/resource-center/data-chart-center/interest-rates/"
        f"daily-treasury-rates.csv/{year}/all?type={kind}&field_tdr_date_value={year}&page&_format=csv"
    )
    return list(csv.DictReader(http_get(url).splitlines()))


def _latest_two(kind: str, today: dt.date) -> List[Dict[str, str]]:
    """Newest two rows of a treasury.gov daily CSV (newest first), spanning a year boundary if needed."""
    rows = _treasury_csv(kind, today.year)
    if len(rows) < 2:
        rows = rows + _treasury_csv(kind, today.year - 1)
    return rows[:2]


def fetch_par_curve(today: dt.date) -> Dict[str, Any]:
    """Daily Treasury par yield curve: {date, points: [(slug, years, yield, prev_yield)]}."""
    try:
        rows = _latest_two("daily_treasury_yield_curve", today)
        latest, prev = rows[0], (rows[1] if len(rows) > 1 else {})
        points = []
        for col, slug, years in PAR_CURVE_TENORS:
            y = to_float(latest.get(col))
            if y is not None:
                points.append((slug, years, y, to_float(prev.get(col))))
        date = dt.datetime.strptime(latest["Date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        return {"date": date, "points": points}
    except Exception as exc:
        warn(f"Treasury par curve fetch failed: {exc}")
        return {}


def fetch_bill_rates(today: dt.date) -> Dict[str, Any]:
    """Daily Treasury bill coupon-equivalent yields: {date, rates: {weeks: yield}}."""
    try:
        latest = _latest_two("daily_treasury_bill_rates", today)[0]
        rates = {}
        for weeks in (4, 8, 13, 26, 52):
            y = to_float(latest.get(f"{weeks} WEEKS COUPON EQUIVALENT"))
            if y is not None:
                rates[weeks] = y
        date = dt.datetime.strptime(latest["Date"], "%m/%d/%Y").strftime("%Y-%m-%d")
        return {"date": date, "rates": rates}
    except Exception as exc:
        warn(f"Treasury bill rates fetch failed: {exc}")
        return {}


def fetch_reference_rates() -> Dict[str, Dict[str, Any]]:
    """NY Fed reference rates keyed by type (SOFR, EFFR, OBFR, SOFRAI, ...)."""
    try:
        data = json.loads(http_get("https://markets.newyorkfed.org/api/rates/all/latest.json"))
        return {r["type"]: r for r in data.get("refRates", [])}
    except Exception as exc:
        warn(f"NY Fed reference rates fetch failed: {exc}")
        return {}


def fetch_mortgage_rates() -> Dict[str, Any]:
    """Freddie Mac Primary Mortgage Market Survey: latest and prior-week 30Y / 15Y fixed rates."""
    try:
        rows = list(csv.DictReader(http_get("https://www.freddiemac.com/pmms/docs/PMMS_history.csv").splitlines()))
        rows = [r for r in rows if to_float(r.get("pmms30")) is not None]
        latest, prev = rows[-1], rows[-2]
        return {
            "date": dt.datetime.strptime(latest["date"], "%m/%d/%Y").strftime("%Y-%m-%d"),
            "rate_30y": to_float(latest["pmms30"]),
            "rate_15y": to_float(latest.get("pmms15")),
            "prev_30y": to_float(prev["pmms30"]),
            "year_ago_30y": to_float(rows[-53]["pmms30"]) if len(rows) > 53 else None,
        }
    except Exception as exc:
        warn(f"Freddie Mac PMMS fetch failed: {exc}")
        return {}


OPTION_SYMBOL_RE = re.compile(r"^([A-Z]+)(\d{6})([CP])(\d{8})$")


def fetch_option_chain_stats(symbol: str) -> Optional[Dict[str, Any]]:
    """Put/call ratios, ATM IV, 25-delta skew and 0DTE share from a Cboe delayed option chain."""
    try:
        payload = json.loads(http_get(f"https://cdn.cboe.com/api/global/delayed_quotes/options/{symbol}.json", timeout=60))
        data = payload["data"]
        spot = to_float(data.get("current_price"))
        quote_date = dt.date.fromisoformat(str(data.get("last_trade_time") or payload["timestamp"])[:10])
        vol = {"C": 0.0, "P": 0.0}
        oi = {"C": 0.0, "P": 0.0}
        zero_dte_vol = 0.0
        by_expiry: Dict[dt.date, List[Tuple[str, float, float, float]]] = {}
        for opt in data.get("options", []):
            m = OPTION_SYMBOL_RE.match(opt.get("option", ""))
            if not m or m.group(1) != symbol:
                continue
            expiry = dt.datetime.strptime(m.group(2), "%y%m%d").date()
            side, strike = m.group(3), int(m.group(4)) / 1000.0
            v, o = to_float(opt.get("volume")) or 0.0, to_float(opt.get("open_interest")) or 0.0
            vol[side] += v
            oi[side] += o
            if expiry == quote_date:
                zero_dte_vol += v
            iv, delta = to_float(opt.get("iv")), to_float(opt.get("delta"))
            if iv and iv > 0 and delta is not None:
                by_expiry.setdefault(expiry, []).append((side, strike, iv, delta))
        if vol["C"] <= 0 or spot is None:
            return None
        stats: Dict[str, Any] = {
            "symbol": symbol,
            "quote_date": quote_date.isoformat(),
            "spot": spot,
            "iv30_pct": to_float(data.get("iv30")),
            "put_call_volume_ratio": vol["P"] / vol["C"],
            "put_call_oi_ratio": oi["P"] / oi["C"] if oi["C"] > 0 else None,
            "total_volume": vol["C"] + vol["P"],
            # after the close the chain no longer lists that day's expiries, so zero means "not observable"
            "zero_dte_volume_share_pct": zero_dte_vol / (vol["C"] + vol["P"]) * 100.0 if zero_dte_vol > 0 else None,
            "atm_iv_pct": None,
            "skew_25d_pts": None,
            "expiry_used": None,
        }
        # Monthly-ish expiry closest to 30 days out for ATM IV and 25-delta skew
        candidates = [e for e in by_expiry if (e - quote_date).days >= 7]
        if candidates:
            expiry = min(candidates, key=lambda e: abs((e - quote_date).days - 30))
            legs = by_expiry[expiry]
            calls = [leg for leg in legs if leg[0] == "C"]
            puts = [leg for leg in legs if leg[0] == "P"]
            if calls and puts:
                atm_c = min(calls, key=lambda leg: abs(leg[1] - spot))
                atm_p = min(puts, key=lambda leg: abs(leg[1] - spot))
                c25 = min(calls, key=lambda leg: abs(leg[3] - 0.25))
                p25 = min(puts, key=lambda leg: abs(leg[3] + 0.25))
                stats["atm_iv_pct"] = (atm_c[2] + atm_p[2]) / 2.0 * 100.0
                stats["skew_25d_pts"] = (p25[2] - c25[2]) * 100.0
                stats["expiry_used"] = expiry.isoformat()
        return stats
    except Exception as exc:
        warn(f"Cboe option chain fetch failed for {symbol}: {exc}")
        return None


# ---------------------------------------------------------------------------
# Derived metrics
# ---------------------------------------------------------------------------

def metric(cls: str, metric_id: str, name: str, value: Optional[float], unit: str, source: str, note: str = "") -> Dict[str, Any]:
    return {"asset_class": cls, "metric_id": metric_id, "metric_name": name, "value": value, "unit": unit,
            "source": source, "note": note}


def interp_curve(points: List[Tuple[float, float]], t: float) -> float:
    """Linear interpolation on (years, yield); flat beyond the ends."""
    if t <= points[0][0]:
        return points[0][1]
    for (t0, y0), (t1, y1) in zip(points, points[1:]):
        if t <= t1:
            return y0 + (y1 - y0) * (t - t0) / (t1 - t0)
    return points[-1][1]


def life_annuity_payout_pct(age: int, curve: List[Tuple[float, float]], modal_age: float = 88.0,
                            dispersion: float = 10.0) -> float:
    """
    Actuarially fair annual payout (% of premium) of a single-premium immediate life-only annuity paid
    monthly. Survival follows the Gompertz law p(x,t) = exp(e^((x-m)/b) * (1 - e^(t/b))); payments are
    discounted on the Treasury par curve (used as a zero curve, semi-annual compounding).
    """
    factor = 0.0
    for k in range(1, (115 - age) * 12 + 1):
        t = k / 12.0
        survive = math.exp(math.exp((age - modal_age) / dispersion) * (1.0 - math.exp(t / dispersion)))
        discount = (1.0 + interp_curve(curve, t) / 200.0) ** (-2.0 * t)
        factor += survive * discount / 12.0
    return 100.0 / factor


def rel(snaps: Dict[str, Dict[str, Any]], a: str, b: str, key: str) -> Optional[float]:
    if a in snaps and b in snaps and snaps[a].get(key) is not None and snaps[b].get(key) is not None:
        return snaps[a][key] - snaps[b][key]
    return None


def last_common_close(a: Optional[Dict[str, Any]], b: Optional[Dict[str, Any]]) -> Optional[Tuple[str, float, float]]:
    """Closes of two series on their most recent shared date."""
    if not a or not b:
        return None
    b_by_date = dict(zip(b["dates"], b["close"]))
    for d, c in zip(reversed(a["dates"]), reversed(a["close"])):
        if d in b_by_date:
            return d, c, b_by_date[d]
    return None


def build_derivative_contracts(today: dt.date) -> Dict[str, Any]:
    """Work out which dated futures contracts are needed for term structures, basis and rate strips."""
    plan: Dict[str, Any] = {"term": [], "basis": [], "sr3": [], "zq": []}
    for root, suffix, months, label in TERM_STRUCTURES:
        y, m = add_months(today.year, today.month, 2)
        while m not in months:
            y, m = add_months(y, m, 1)
        plan["term"].append({"root": root, "label": label, "near": contract_ticker(root, y, m, suffix),
                             "far": contract_ticker(root, y + 1, m, suffix), "near_month": f"{y}-{m:02d}"})
    for root, suffix, months, spot, label, rule, invert in BASIS_PAIRS:
        y, m = today.year, today.month
        while m not in months or (expiry_date(rule, y, m) - today).days < 7:
            y, m = add_months(y, m, 1)
        plan["basis"].append({"root": root, "label": label, "ticker": contract_ticker(root, y, m, suffix),
                              "spot": spot, "invert": invert, "expiry": expiry_date(rule, y, m)})
    # SOFR strip: contract month M covers IMM(M) -> IMM(M+3); start with the first quarter not yet begun
    y, m = today.year, today.month
    while m not in (3, 6, 9, 12) or third_weekday(y, m, 2) <= today:
        y, m = add_months(y, m, 1)
    for _ in range(20):
        ny, nm = add_months(y, m, 3)
        plan["sr3"].append({"ticker": contract_ticker("SR3", y, m, "CME"), "start": third_weekday(y, m, 2),
                            "end": third_weekday(ny, nm, 2)})
        y, m = ny, nm
    for k in range(1, 13):
        y, m = add_months(today.year, today.month, k)
        plan["zq"].append({"ticker": contract_ticker("ZQ", y, m, "CBT"), "month": f"{y}-{m:02d}", "ahead": k})
    return plan


def implied_swap_curve(strip: List[Dict[str, Any]], today: dt.date, overnight_pct: float) -> List[Tuple[int, float]]:
    """
    Par OIS swap rates implied by the 3-month SOFR futures strip (no convexity adjustment).
    Discount factors compound the stub at the overnight rate and each futures-implied forward rate
    over its reference quarter (ACT/360). Returns [(tenor_years, par_rate_pct, ...)] for 1/2/3/5Y.
    """
    out: List[Tuple[int, float]] = []
    if not strip:
        return out
    df = 1.0 / (1.0 + overnight_pct / 100.0 * (strip[0]["start"] - today).days / 360.0)
    annuity = 0.0
    for i, leg in enumerate(strip, start=1):
        tau = (leg["end"] - leg["start"]).days / 360.0
        df /= 1.0 + leg["fwd_pct"] / 100.0 * tau
        annuity += tau * df
        if i in (4, 8, 12, 20):
            # the par rate is measured from the strip start, so rebase the discount factors to it
            stub_df = 1.0 / (1.0 + overnight_pct / 100.0 * (strip[0]["start"] - today).days / 360.0)
            out.append((i // 4, (stub_df - df) / annuity * 100.0))
    return out


def compute_metrics(
    hist: Dict[str, Dict[str, Any]],
    snaps: Dict[str, Dict[str, Any]],
    ctx: Dict[str, Any],
    classes: List[str],
) -> List[Dict[str, Any]]:
    """Class-specific derived metrics. Anything whose inputs are missing is simply left out."""
    out: List[Dict[str, Any]] = []
    today: dt.date = ctx["today"]
    curve, bills, ref, pmms = ctx["par_curve"], ctx["bill_rates"], ctx["ref_rates"], ctx["mortgage"]
    par = {slug: y for slug, _, y, _ in curve.get("points", [])}
    par_prev = {slug: p for slug, _, _, p in curve.get("points", [])}
    par_src = f"treasury.gov par yield curve {curve.get('date', '')}".strip()

    def add(cls: str, *args: Any, **kw: Any) -> None:
        if cls in classes and args[2] is not None:
            out.append(metric(cls, *args, **kw))

    def px(t: str) -> Optional[float]:
        return snaps[t]["price"] if t in snaps else None

    # --- Stocks -----------------------------------------------------------
    megacaps = [t for t, _, sub, _ in UNIVERSE["stocks"]["instruments"] if sub == "Mega-cap stock" and t in snaps]
    if megacaps:
        above = [snaps[t]["above_50dma"] for t in megacaps if snaps[t]["above_50dma"] is not None]
        if above:
            add("stocks", "megacap_above_50dma_pct", "Mega-caps above their 50-day average",
                sum(above) / len(above) * 100.0, "pct", "yahoo daily closes")
    add("stocks", "small_vs_large_1m_pts", "Russell 2000 minus S&P 500, 1-month return",
        rel(snaps, "^RUT", "^GSPC", "change_1m_pct"), "pts", "yahoo daily closes")
    add("stocks", "equal_vs_cap_weight_1m_pts", "Equal-weight (RSP) minus cap-weight (SPY), 1-month return",
        rel(snaps, "RSP", "SPY", "change_1m_pct"), "pts", "yahoo daily closes", note="breadth gauge")

    # --- Bonds ------------------------------------------------------------
    for slug, _, y, prev in curve.get("points", []):
        add("bonds", f"ust_par_{slug}", f"Treasury par yield {slug.upper()}", y, "pct", par_src,
            note=f"prior session {prev}" if prev is not None else "")
    if "2y" in par and "10y" in par:
        add("bonds", "curve_2s10s_bps", "2s10s curve slope", (par["10y"] - par["2y"]) * 100.0, "bps", par_src)
    if "3m" in par and "10y" in par:
        add("bonds", "curve_3m10y_bps", "3m10y curve slope", (par["10y"] - par["3m"]) * 100.0, "bps", par_src)
    if par.get("10y") is not None and par_prev.get("10y") is not None:
        add("bonds", "ust_10y_change_1d_bps", "10Y par yield, 1-session change",
            (par["10y"] - par_prev["10y"]) * 100.0, "bps", par_src)
    add("bonds", "high_yield_vs_treasury_1m_pts", "High yield (HYG) minus 7-10Y Treasuries (IEF), 1-month return",
        rel(snaps, "HYG", "IEF", "change_1m_pct"), "pts", "yahoo daily closes",
        note="credit-risk appetite proxy; negative = credit lagging")
    add("bonds", "ig_vs_treasury_1m_pts", "IG corporates (LQD) minus 7-10Y Treasuries (IEF), 1-month return",
        rel(snaps, "LQD", "IEF", "change_1m_pct"), "pts", "yahoo daily closes")

    # --- Cash -------------------------------------------------------------
    bill_src = f"treasury.gov bill rates {bills.get('date', '')}".strip()
    for weeks, y in bills.get("rates", {}).items():
        add("cash", f"tbill_{weeks}w_yield_pct", f"{weeks}-week T-bill coupon-equivalent yield", y, "pct", bill_src)
    for key, label in (("SOFR", "SOFR (secured overnight)"), ("EFFR", "Effective Fed funds rate"),
                       ("OBFR", "Overnight bank funding rate")):
        if key in ref:
            add("cash", f"{key.lower()}_pct", label, to_float(ref[key].get("percentRate")), "pct",
                f"NY Fed {ref[key].get('effectiveDate', '')}")
    if "EFFR" in ref:
        add("cash", "fed_target_lower_pct", "Fed funds target range, lower bound",
            to_float(ref["EFFR"].get("targetRateFrom")), "pct", "NY Fed")
        add("cash", "fed_target_upper_pct", "Fed funds target range, upper bound",
            to_float(ref["EFFR"].get("targetRateTo")), "pct", "NY Fed")

    # --- Commodities ------------------------------------------------------
    if px("GC=F") and px("SI=F"):
        add("commodities", "gold_silver_ratio", "Gold / silver ratio", px("GC=F") / px("SI=F"), "ratio", "yahoo front-month futures")
    if px("HG=F") and px("GC=F"):
        add("commodities", "copper_gold_ratio_x1000", "Copper / gold ratio (x1000)", px("HG=F") / px("GC=F") * 1000.0,
            "ratio", "yahoo front-month futures", note="growth-vs-safety gauge")
    if px("BZ=F") and px("CL=F"):
        add("commodities", "brent_wti_spread_usd", "Brent minus WTI", px("BZ=F") - px("CL=F"), "usd", "yahoo front-month futures")

    # --- Real estate ------------------------------------------------------
    if pmms:
        src = f"Freddie Mac PMMS {pmms['date']}"
        add("real_estate", "mortgage_30y_pct", "30-year fixed mortgage rate", pmms["rate_30y"], "pct", src)
        add("real_estate", "mortgage_15y_pct", "15-year fixed mortgage rate", pmms["rate_15y"], "pct", src)
        if pmms.get("prev_30y") is not None:
            add("real_estate", "mortgage_30y_change_1w_bps", "30-year mortgage rate, 1-week change",
                (pmms["rate_30y"] - pmms["prev_30y"]) * 100.0, "bps", src)
        if pmms.get("year_ago_30y") is not None:
            add("real_estate", "mortgage_30y_change_1y_bps", "30-year mortgage rate, 1-year change",
                (pmms["rate_30y"] - pmms["year_ago_30y"]) * 100.0, "bps", src)
        if par.get("10y") is not None:
            add("real_estate", "mortgage_spread_10y_pts", "30-year mortgage rate minus 10Y Treasury",
                pmms["rate_30y"] - par["10y"], "pts", f"{src}; {par_src}")
    add("real_estate", "reits_vs_sp500_3m_pts", "REITs (VNQ) minus S&P 500 (SPY), 3-month return",
        rel(snaps, "VNQ", "SPY", "change_3m_pct"), "pts", "yahoo daily closes")

    # --- Alternatives -----------------------------------------------------
    add("alternatives", "btc_realized_vol_30d_pct", "Bitcoin 30-day realised volatility",
        snaps.get("BTC-USD", {}).get("realized_vol_30d_pct"), "pct", "yahoo daily closes")
    add("alternatives", "managed_futures_vs_sp500_1m_pts", "Managed futures (DBMF) minus S&P 500 (SPY), 1-month return",
        rel(snaps, "DBMF", "SPY", "change_1m_pct"), "pts", "yahoo daily closes", note="diversifier check")
    add("alternatives", "listed_pe_vs_sp500_3m_pts", "Listed private equity (PSP) minus S&P 500 (SPY), 3-month return",
        rel(snaps, "PSP", "SPY", "change_3m_pct"), "pts", "yahoo daily closes")
    add("alternatives", "bdc_vs_high_yield_3m_pts", "BDCs (BIZD) minus high-yield bonds (HYG), 3-month return",
        rel(snaps, "BIZD", "HYG", "change_3m_pct"), "pts", "yahoo daily closes", note="private-credit stress proxy")

    # --- ETFs -------------------------------------------------------------
    etf_dollar_vol = [snaps[t]["price"] * snaps[t]["volume"] for t, *_ in UNIVERSE["etfs"]["instruments"]
                      if t in snaps and snaps[t].get("volume")]
    if etf_dollar_vol:
        add("etfs", "tracked_dollar_volume_usd", "Dollar volume across tracked ETFs (latest session)",
            sum(etf_dollar_vol), "usd", "yahoo daily bars")
    add("etfs", "growth_vs_value_1m_pts", "Growth (VUG) minus value (VTV), 1-month return",
        rel(snaps, "VUG", "VTV", "change_1m_pct"), "pts", "yahoo daily closes")
    add("etfs", "emerging_vs_developed_1m_pts", "Emerging (VWO) minus developed ex-US (VEA), 1-month return",
        rel(snaps, "VWO", "VEA", "change_1m_pct"), "pts", "yahoo daily closes")

    # --- Index funds / mutual funds ---------------------------------------
    for fund, bench in INDEX_FUND_BENCHMARKS.items():
        add("index_funds", f"tracking_gap_1y_{fund.lower()}_pts", f"{fund} minus {bench}, 1-year total return",
            rel(snaps, fund, bench, "change_1y_pct"), "pts", "yahoo adjusted closes",
            note="index fund vs the ETF on the same benchmark")
    excess = []
    for fund, bench in MUTUAL_FUND_BENCHMARKS.items():
        gap = rel(snaps, fund, bench, "change_1y_pct")
        add("mutual_funds", f"excess_return_1y_{fund.lower()}_pts", f"{fund} minus {bench}, 1-year total return",
            gap, "pts", "yahoo adjusted closes", note="active fund vs passive benchmark ETF")
        if gap is not None:
            excess.append(gap)
    if excess:
        add("mutual_funds", "funds_beating_benchmark_1y_pct", "Active funds ahead of their benchmark over 1 year",
            sum(1 for g in excess if g > 0) / len(excess) * 100.0, "pct", "yahoo adjusted closes",
            note=f"{len(excess)} funds")
        add("mutual_funds", "median_excess_return_1y_pts", "Median 1-year excess return of active funds",
            statistics.median(excess), "pts", "yahoo adjusted closes")

    # --- Options ----------------------------------------------------------
    vix_hist = hist.get("^VIX")
    if vix_hist and "^VIX" in snaps:
        closes = vix_hist["close"]
        rank = sum(1 for c in closes if c <= closes[-1]) / len(closes) * 100.0
        add("options", "vix_percentile_1y_pct", "VIX percentile over the past year (IV rank)", rank, "pct",
            "yahoo daily closes")
    if px("^VIX") and px("^VIX3M"):
        add("options", "vix_term_ratio", "VIX / VIX3M term-structure ratio", px("^VIX") / px("^VIX3M"), "ratio",
            "yahoo daily closes", note="above 1 = backwardation (near-term stress)")
    if px("^VIX9D") and px("^VIX"):
        add("options", "vix9d_vix_ratio", "VIX9D / VIX ratio", px("^VIX9D") / px("^VIX"), "ratio", "yahoo daily closes")
    for st in ctx["option_chains"]:
        sym, src = st["symbol"], f"Cboe delayed chain {st['quote_date']}"
        low = sym.lower()
        add("options", f"put_call_volume_ratio_{low}", f"{sym} put/call volume ratio", st["put_call_volume_ratio"], "ratio", src)
        add("options", f"put_call_oi_ratio_{low}", f"{sym} put/call open-interest ratio", st["put_call_oi_ratio"], "ratio", src)
        add("options", f"iv30_{low}_pct", f"{sym} 30-day implied volatility", st["iv30_pct"], "pct", src)
        add("options", f"atm_iv_{low}_pct", f"{sym} at-the-money IV ({st['expiry_used']} expiry)", st["atm_iv_pct"], "pct", src)
        add("options", f"skew_25d_{low}_pts", f"{sym} 25-delta put minus call IV", st["skew_25d_pts"], "pts", src,
            note="positive = downside protection costs more")
        add("options", f"zero_dte_share_{low}_pct", f"{sym} share of volume in same-day expiries",
            st["zero_dte_volume_share_pct"], "pct", src)
        add("options", f"option_volume_{low}", f"{sym} option contracts traded", st["total_volume"], "contracts", src)

    # --- Futures & forwards -----------------------------------------------
    plan = ctx["contracts"]
    for ts in plan["term"]:
        near, far = hist.get(ts["near"]), hist.get(ts["far"])
        pair = last_common_close(near, far)
        if pair:
            roll = (pair[2] / pair[1] - 1.0) * 100.0
            add("futures_forwards", f"term_structure_12m_{ts['root'].lower()}_pct",
                f"{ts['label']}: 12-month futures curve slope", roll, "pct",
                f"yahoo {ts['near']} vs {ts['far']} ({pair[0]})",
                note="positive = contango (far above near), negative = backwardation")
    for bp in plan["basis"]:
        fut, spot = hist.get(bp["ticker"]), hist.get(bp["spot"])
        pair = last_common_close(fut, spot)
        if pair:
            spot_px = 1.0 / pair[2] if bp["invert"] else pair[2]
            days = max((bp["expiry"] - dt.date.fromisoformat(pair[0])).days, 1)
            basis = (pair[1] / spot_px - 1.0) * 100.0
            add("futures_forwards", f"basis_{bp['root'].lower()}_pct", f"{bp['label']}: dated future over spot",
                basis, "pct", f"yahoo {bp['ticker']} vs {bp['spot']} ({pair[0]})",
                note=f"expires {bp['expiry']}; closes are not time-synchronised")
            add("futures_forwards", f"implied_carry_{bp['root'].lower()}_pct",
                f"{bp['label']}: annualised forward premium", basis * 365.0 / days, "pct",
                f"yahoo {bp['ticker']} vs {bp['spot']} ({pair[0]})",
                note="futures-implied financing rate net of income (FX: interest-rate differential)")

    # --- Swaps ------------------------------------------------------------
    sofr = to_float(ref.get("SOFR", {}).get("percentRate"))
    if "SOFR" in ref:
        add("swaps", "sofr_pct", "SOFR (floating-leg reference rate)", sofr, "pct",
            f"NY Fed {ref['SOFR'].get('effectiveDate', '')}")
    if "SOFRAI" in ref:
        src = f"NY Fed {ref['SOFRAI'].get('effectiveDate', '')}"
        for days in (30, 90, 180):
            add("swaps", f"sofr_avg_{days}d_pct", f"SOFR {days}-day compounded average",
                to_float(ref["SOFRAI"].get(f"average{days}day")), "pct", src)
    strip = []
    for leg in plan["sr3"]:
        h = hist.get(leg["ticker"])
        if not h:
            break  # the strip must be contiguous
        strip.append({**leg, "fwd_pct": 100.0 - h["close"][-1]})
    for leg in strip:
        add("swaps", f"sofr_fwd_3m_{leg['start'].strftime('%Y_%m')}_pct",
            f"Implied 3-month SOFR starting {leg['start'].strftime('%b %Y')}", leg["fwd_pct"], "pct",
            f"yahoo {leg['ticker']}")
    if strip and sofr is not None:
        for tenor, rate in implied_swap_curve(strip, today, sofr):
            add("swaps", f"sofr_swap_implied_{tenor}y_pct", f"Implied {tenor}-year SOFR OIS par swap rate", rate, "pct",
                "SOFR futures strip (yahoo) + NY Fed SOFR", note="model: no convexity adjustment, ACT/360 quarterly")
            tsy = par.get(f"{tenor}y")
            if tsy is not None:
                # put the swap rate on the Treasury's semi-annual bond-equivalent basis before differencing
                quarterly_365 = rate * 365.0 / 360.0
                bey = 2.0 * ((1.0 + quarterly_365 / 400.0) ** 2 - 1.0) * 100.0
                add("swaps", f"swap_spread_{tenor}y_bps", f"{tenor}-year swap spread (implied swap minus Treasury)",
                    (bey - tsy) * 100.0, "bps", f"SOFR futures strip; {par_src}",
                    note="model estimate; negative spreads are normal for SOFR swaps")
    effr = to_float(ref.get("EFFR", {}).get("percentRate"))
    for leg in plan["zq"]:
        h = hist.get(leg["ticker"])
        if h and leg["ahead"] in (3, 6, 12):
            implied = 100.0 - h["close"][-1]
            add("swaps", f"fed_funds_implied_{leg['ahead']}m_pct", f"Fed funds rate implied for {leg['month']}",
                implied, "pct", f"yahoo {leg['ticker']}")
            if leg["ahead"] == 12 and effr is not None:
                add("swaps", "fed_cuts_priced_12m", "25 bp Fed cuts priced over 12 months", (effr - implied) / 0.25,
                    "count", f"yahoo {leg['ticker']}; NY Fed EFFR", note="negative = hikes priced")

    # --- Structured products & hybrids ------------------------------------
    add("structured", "preferreds_vs_agg_3m_pts", "Preferreds (PFF) minus core bonds (AGG), 3-month return",
        rel(snaps, "PFF", "AGG", "change_3m_pct"), "pts", "yahoo adjusted closes")
    add("structured", "convertibles_vs_sp500_3m_pts", "Convertibles (CWB) minus S&P 500 (SPY), 3-month return",
        rel(snaps, "CWB", "SPY", "change_3m_pct"), "pts", "yahoo adjusted closes")
    for fund, under, kind in (("BUFR", "SPY", "Buffer ETF"), ("JEPI", "SPY", "Equity-premium income"),
                              ("XYLD", "SPY", "Covered call"), ("QYLD", "QQQ", "Covered call")):
        f3, u3 = snaps.get(fund, {}).get("change_3m_pct"), snaps.get(under, {}).get("change_3m_pct")
        if f3 is not None and u3 is not None:
            add("structured", f"{fund.lower()}_vs_{under.lower()}_3m_pts",
                f"{kind} ({fund}) minus {under}, 3-month return", f3 - u3, "pts", "yahoo adjusted closes",
                note=f"{fund} {f3:+.1f}% vs {under} {u3:+.1f}%")
    add("structured", "clo_bbb_vs_aaa_3m_pts", "BBB CLOs (JBBB) minus AAA CLOs (JAAA), 3-month return",
        rel(snaps, "JBBB", "JAAA", "change_3m_pct"), "pts", "yahoo adjusted closes", note="securitised-credit risk appetite")
    add("structured", "mbs_vs_treasury_1m_pts", "Agency MBS (MBB) minus 7-10Y Treasuries (IEF), 1-month return",
        rel(snaps, "MBB", "IEF", "change_1m_pct"), "pts", "yahoo adjusted closes")

    # --- Annuities --------------------------------------------------------
    zero_curve = [(years, y) for _, years, y, _ in curve.get("points", [])]
    if len(zero_curve) >= 5:
        for age in (60, 65, 70, 75):
            payout = life_annuity_payout_pct(age, zero_curve)
            add("annuities", f"spia_payout_age{age}_pct", f"Life annuity payout rate, age {age} (model)", payout, "pct",
                f"model: {par_src} + Gompertz mortality (m=88, b=10)",
                note="actuarially fair, life-only, unisex; real quotes are lower after insurer loads")
            add("annuities", f"spia_monthly_income_age{age}_usd", f"Monthly income per $100,000 premium, age {age} (model)",
                payout * 1000.0 / 12.0, "usd", f"model: {par_src} + Gompertz mortality (m=88, b=10)")
        add("annuities", "ust_5y_reference_pct", "5-year Treasury yield (fixed-annuity rate reference)", par.get("5y"),
            "pct", par_src, note="multi-year guaranteed annuity rates are set off this part of the curve")
    add("annuities", "insurers_vs_sp500_1m_pts", "Insurers (KIE) minus S&P 500 (SPY), 1-month return",
        rel(snaps, "KIE", "SPY", "change_1m_pct"), "pts", "yahoo adjusted closes")
    return out


# ---------------------------------------------------------------------------
# Class summaries + alerts
# ---------------------------------------------------------------------------

def summarise_classes(rows: List[Dict[str, Any]], classes: List[str]) -> List[Dict[str, Any]]:
    out = []
    for cls in classes:
        members = [r for r in rows if r["class_id"] == cls]
        if not members:
            continue
        d1 = [r["change_1d_pct"] for r in members if r["change_1d_pct"] is not None]
        above = [r["above_50dma"] for r in members if r["above_50dma"] is not None]
        ranked = sorted((r for r in members if r["change_1d_pct"] is not None), key=lambda r: r["change_1d_pct"])
        by_z = sorted((r for r in members if r["z_score_20d"] is not None), key=lambda r: abs(r["z_score_20d"]))
        med_1m = median([r["change_1m_pct"] for r in members])
        breadth = sum(above) / len(above) * 100.0 if above else None
        if med_1m is None or breadth is None:
            trend = "UNRATED"
        elif med_1m >= 2.0 and breadth >= 60.0:
            trend = "ADVANCING"
        elif med_1m <= -2.0 and breadth <= 40.0:
            trend = "DECLINING"
        else:
            trend = "MIXED"
        if cls == "options" and trend != "MIXED":  # the instruments are volatility indices, not prices
            trend = {"ADVANCING": "VOL RISING", "DECLINING": "VOL FALLING"}.get(trend, trend)
        meta = UNIVERSE[cls]
        out.append({
            "class_id": cls, "asset_class": meta["name"], "group": meta["group"], "coverage": meta["coverage"],
            "n_instruments": len(members),
            "median_change_1d_pct": median(d1),
            "median_change_5d_pct": median([r["change_5d_pct"] for r in members]),
            "median_change_1m_pct": med_1m,
            "median_change_3m_pct": median([r["change_3m_pct"] for r in members]),
            "median_change_ytd_pct": median([r["change_ytd_pct"] for r in members]),
            "median_change_1y_pct": median([r["change_1y_pct"] for r in members]),
            "pct_up_1d": sum(1 for v in d1 if v > 0) / len(d1) * 100.0 if d1 else None,
            "pct_above_50dma": breadth,
            "median_realized_vol_30d_pct": median([r["realized_vol_30d_pct"] for r in members]),
            "best_1d_id": ranked[-1]["asset_id"] if ranked else "",
            "best_1d_pct": ranked[-1]["change_1d_pct"] if ranked else None,
            "worst_1d_id": ranked[0]["asset_id"] if ranked else "",
            "worst_1d_pct": ranked[0]["change_1d_pct"] if ranked else None,
            "max_abs_z_id": by_z[-1]["asset_id"] if by_z else "",
            "max_abs_z": by_z[-1]["z_score_20d"] if by_z else None,
            "trend": trend,
        })
    return out


def build_alerts(rows: List[Dict[str, Any]], summaries: List[Dict[str, Any]], metrics: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []

    def emit(severity: str, asset_id: str, cls: str, signal: str, value: Optional[float], threshold: str, details: str) -> None:
        alerts.append({"severity": severity, "asset_id": asset_id, "asset_class": cls, "signal_type": signal,
                       "value": value, "threshold": threshold, "details": details})

    # an instrument listed under two classes (e.g. an ETF) should alert once
    seen: set = set()
    for r in rows:
        if r["asset_id"] in seen:
            continue
        z, vr = r["z_score_20d"], r["vol_ratio_20d"]
        name = f"{r['asset_name']} ({r['asset_id']})"
        flagged = False
        if z is not None and abs(z) >= Z_TRIGGER:
            flagged = True
            if vr is not None and vr > VOL_RATIO_TRIGGER:
                emit("high", r["asset_id"], r["asset_class"], "z_score_volume_breakout", z,
                     f"|Z| >= {Z_TRIGGER} and volume > {VOL_RATIO_TRIGGER}x 20d avg",
                     f"{name} at {fmt_price(r['price'])}: Z={z:+.2f} on {vr:.1f}x average volume, 1d {fmt(r['change_1d_pct'], 2, '%', True)}.")
            else:
                emit("medium", r["asset_id"], r["asset_class"], "z_score_extreme", z, f"|Z| >= {Z_TRIGGER}",
                     f"{name} at {fmt_price(r['price'])}: Z={z:+.2f} vs its 20-day mean, 1d {fmt(r['change_1d_pct'], 2, '%', True)}"
                     + (f", volume {vr:.1f}x average." if vr is not None else " (no usable volume)."))
        if r["is_52w_high"] or r["is_52w_low"]:
            flagged = True
            side = "high" if r["is_52w_high"] else "low"
            emit("info", r["asset_id"], r["asset_class"], f"new_52w_{side}", r["price"], f"close at 1-year {side}",
                 f"{name} closed at a 1-year {side} of {fmt_price(r['price'])}.")
        if flagged:
            seen.add(r["asset_id"])

    for s in summaries:
        m5 = s["median_change_5d_pct"]
        if m5 is not None and abs(m5) >= CLASS_MOVE_5D_PCT and s["class_id"] != "options":
            kind = "class_rally" if m5 > 0 else "class_selloff"
            emit("medium", s["class_id"], s["asset_class"], kind, m5, f"class median 5d move >= {CLASS_MOVE_5D_PCT}%",
                 f"{s['asset_class']}: median instrument {m5:+.2f}% over 5 sessions; "
                 f"{fmt(s['pct_above_50dma'], 0, '%')} above their 50-day average.")

    by_id = {m["metric_id"]: m for m in metrics}

    def val(mid: str) -> Optional[float]:
        return by_id[mid]["value"] if mid in by_id else None

    if val("vix_term_ratio") is not None and val("vix_term_ratio") > 1.0:
        emit("high", "^VIX", "Options", "vix_term_backwardation", val("vix_term_ratio"), "VIX / VIX3M > 1",
             f"VIX is above 3-month VIX (ratio {val('vix_term_ratio'):.3f}): near-term protection is bid over longer-dated.")
    if val("vix_percentile_1y_pct") is not None and val("vix_percentile_1y_pct") >= IV_RANK_TRIGGER:
        emit("medium", "^VIX", "Options", "iv_rank_high", val("vix_percentile_1y_pct"), f"1y percentile >= {IV_RANK_TRIGGER:.0f}%",
             f"VIX sits at the {val('vix_percentile_1y_pct'):.0f}th percentile of its past year.")
    for mid, label in (("curve_2s10s_bps", "2s10s"), ("curve_3m10y_bps", "3m10y")):
        if val(mid) is not None and val(mid) < 0:
            emit("medium", mid.upper(), "Bonds (Fixed Income)", "yield_curve_inverted", val(mid), "slope < 0 bps",
                 f"Treasury {label} slope is inverted at {val(mid):+.0f} bps.")
    for m in metrics:
        if m["metric_id"].startswith("term_structure_12m_") and abs(m["value"]) >= ROLL_YIELD_EXTREME_PCT:
            shape = "contango" if m["value"] > 0 else "backwardation"
            emit("info", m["metric_id"], "Futures & Forwards", f"steep_{shape}", m["value"],
                 f"|12m curve slope| >= {ROLL_YIELD_EXTREME_PCT:.0f}%",
                 f"{m['metric_name']} is {m['value']:+.1f}% ({shape}); {m['source']}.")
    if val("fed_cuts_priced_12m") is not None and abs(val("fed_cuts_priced_12m")) >= 2.0:
        n = val("fed_cuts_priced_12m")
        emit("info", "ZQ", "Swaps", "fed_path_repricing", n, ">= 2 moves priced in 12 months",
             f"Fed funds futures price {abs(n):.1f} {'cuts' if n > 0 else 'hikes'} of 25 bp over the next 12 months.")
    order = {"high": 0, "medium": 1, "info": 2}
    alerts.sort(key=lambda a: (order[a["severity"]], a["signal_type"], a["asset_class"], a["asset_id"]))
    return alerts


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------

def run_watch(classes: List[str]) -> Dict[str, Any]:
    now_utc = dt.datetime.now(UTC_TZ)
    run_date = now_utc.astimezone(NY_TZ).strftime("%Y-%m-%d")
    today = now_utc.date()

    contracts = build_derivative_contracts(today)
    year_tickers = [t for cls in CLASS_ORDER for t, *_ in UNIVERSE[cls]["instruments"]] + HELPER_TICKERS
    short_tickers = (
        [c for ts in contracts["term"] for c in (ts["near"], ts["far"])]
        + [bp["ticker"] for bp in contracts["basis"]]
        + [leg["ticker"] for leg in contracts["sr3"]]
        + [leg["ticker"] for leg in contracts["zq"]]
    )
    hist = fetch_many(year_tickers, "1y")
    hist.update(fetch_many(short_tickers, "1mo"))

    snaps = {}
    for t in year_tickers:
        if t in hist:
            snap = compute_snapshot(hist[t], today)
            if snap:
                snaps[t] = snap

    rows: List[Dict[str, Any]] = []
    for cls in classes:
        meta = UNIVERSE[cls]
        for ticker, name, sub, unit in meta["instruments"]:
            if ticker not in snaps:
                continue
            rows.append({
                "class_id": cls, "group": meta["group"], "asset_class": meta["name"], "sub_class": sub,
                "asset_id": ticker, "asset_name": name, "instrument_type": hist[ticker]["instrument_type"],
                "unit": unit, "source": f"yahoo daily close {snaps[ticker]['last_bar_date']}", **snaps[ticker],
            })

    with ThreadPoolExecutor(max_workers=3) as pool:
        chains = [c for c in pool.map(fetch_option_chain_stats, OPTION_CHAIN_UNDERLYINGS) if c] if "options" in classes else []

    ctx = {
        "today": today,
        "par_curve": fetch_par_curve(today),
        "bill_rates": fetch_bill_rates(today),
        "ref_rates": fetch_reference_rates(),
        "mortgage": fetch_mortgage_rates() if "real_estate" in classes else {},
        "option_chains": chains,
        "contracts": contracts,
    }
    metrics = compute_metrics(hist, snaps, ctx, classes)
    summaries = summarise_classes(rows, classes)
    alerts = build_alerts(rows, summaries, metrics)

    wanted = [t for t in dict.fromkeys(r["asset_id"] for r in rows)]
    missing = [
        f"{UNIVERSE[cls]['name']}: {t}" for cls in classes for t, *_ in UNIVERSE[cls]["instruments"] if t not in snaps
    ]
    return {
        "run_date": run_date,
        "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "classes": classes,
        "rows": rows,
        "summaries": summaries,
        "metrics": metrics,
        "alerts": alerts,
        "missing": missing,
        "history": {t: hist[t] for t in wanted},
    }


# ---------------------------------------------------------------------------
# CSV output
# ---------------------------------------------------------------------------

SNAPSHOT_COLS = [
    "date", "timestamp_utc", "group", "class_id", "asset_class", "sub_class", "asset_id", "asset_name",
    "instrument_type", "unit", "price", "change_1d_abs", "change_1d_pct", "change_5d_pct", "change_1m_pct",
    "change_3m_pct", "change_ytd_pct", "change_1y_pct", "z_score_20d", "volume", "vol_ratio_20d",
    "realized_vol_30d_pct", "drawdown_52w_pct", "above_50dma", "above_200dma", "last_bar_date", "source",
]
SUMMARY_COLS = [
    "date", "timestamp_utc", "group", "class_id", "asset_class", "coverage", "n_instruments",
    "median_change_1d_pct", "median_change_5d_pct", "median_change_1m_pct", "median_change_3m_pct",
    "median_change_ytd_pct", "median_change_1y_pct", "pct_up_1d", "pct_above_50dma",
    "median_realized_vol_30d_pct", "best_1d_id", "best_1d_pct", "worst_1d_id", "worst_1d_pct",
    "max_abs_z_id", "max_abs_z", "trend",
]
METRIC_COLS = ["date", "timestamp_utc", "class_id", "asset_class", "metric_id", "metric_name", "value", "unit", "source", "note"]
ALERT_COLS = ["date", "timestamp_utc", "severity", "asset_id", "asset_class", "signal_type", "value", "threshold", "details"]


def cell(val: Any) -> str:
    """CSV cell: empty for unavailable, plain numbers, lower-case booleans."""
    if val is None:
        return ""
    if isinstance(val, bool):
        return "true" if val else "false"
    if isinstance(val, float):
        return f"{val:.6g}" if abs(val) < 1e15 else f"{val:.0f}"
    return str(val)


def upsert_date_rows(path: Path, header: List[str], new_rows: List[Dict[str, Any]], run_date: str,
                     replace_class_ids: Optional[List[str]] = None) -> None:
    """Replace this run date's rows (optionally only for the given classes); keep earlier dates untouched."""
    kept: List[Dict[str, str]] = []
    if path.exists():
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                same_date = row.get("date") == run_date
                in_scope = replace_class_ids is None or row.get("class_id") in replace_class_ids
                if not (same_date and in_scope):
                    kept.append(row)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in kept:
            writer.writerow({k: row.get(k, "") for k in header})
        for row in new_rows:
            writer.writerow({k: cell(row.get(k)) for k in header})


def write_history_wide(path: Path, history: Dict[str, Dict[str, Any]], partial: bool) -> None:
    """One column per instrument, one row per date, adjusted closes for the trailing year."""
    columns: Dict[str, Dict[str, str]] = {}
    if partial and path.exists():  # a --classes run must not drop the other classes' columns
        with path.open(newline="", encoding="utf-8") as fh:
            for row in csv.DictReader(fh):
                for col, v in row.items():
                    if col != "date" and v != "":
                        columns.setdefault(col, {})[row["date"]] = v
    for ticker, h in history.items():
        columns[ticker] = {d: f"{a:.6g}" for d, a in zip(h["dates"], h["adj"])}
    dates = sorted({d for col in columns.values() for d in col})[-262:]
    tickers = list(columns)
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.writer(fh)
        writer.writerow(["date"] + tickers)
        for d in dates:
            writer.writerow([d] + [columns[t].get(d, "") for t in tickers])


def write_csvs(result: Dict[str, Any], data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"date": result["run_date"], "timestamp_utc": result["timestamp_utc"]}
    partial = set(result["classes"]) != set(CLASS_ORDER)
    scope = result["classes"] if partial else None
    names = {cls: UNIVERSE[cls]["name"] for cls in CLASS_ORDER}
    upsert_date_rows(data_dir / "asset_classes.csv", SNAPSHOT_COLS, [{**stamp, **r} for r in result["rows"]],
                     result["run_date"], scope)
    upsert_date_rows(data_dir / "asset_class_summary.csv", SUMMARY_COLS, [{**stamp, **s} for s in result["summaries"]],
                     result["run_date"], scope)
    upsert_date_rows(
        data_dir / "asset_class_metrics.csv", METRIC_COLS,
        [{**stamp, **m, "class_id": m["asset_class"], "asset_class": names[m["asset_class"]]} for m in result["metrics"]],
        result["run_date"], scope,
    )
    if not partial:  # alerts span classes, so only a full run may replace the day's alert set
        upsert_date_rows(data_dir / "asset_class_alerts.csv", ALERT_COLS, [{**stamp, **a} for a in result["alerts"]],
                         result["run_date"])
    write_history_wide(data_dir / "asset_classes_history.csv", result["history"], partial)


# ---------------------------------------------------------------------------
# Markdown brief + terminal output
# ---------------------------------------------------------------------------

def render_markdown(result: Dict[str, Any]) -> str:
    L: List[str] = []
    stamp = result["timestamp_utc"].replace("T", " ").replace("Z", " UTC")
    L.append(f"## 🧭 Asset-Class Watch Brief — [{stamp}]")
    L.append("")
    L.append(f"> Run date `{result['run_date']}` (`America/New_York`). Generated by `scripts/watch_asset_classes.py`. "
             f"{len(result['rows'])} instruments across {len(result['summaries'])} asset classes, "
             f"{len(result['metrics'])} derived metrics, {len(result['alerts'])} alerts.")
    L.append("")
    L.append("### 1. Asset-Class Scoreboard")
    L.append("Median total return of each class's tracked instruments. Breadth = share above their 50-day average.")
    for group in GROUPS:
        members = [s for s in result["summaries"] if s["group"] == group]
        if not members:
            continue
        L.append("")
        L.append(f"**{group}**")
        L.append("")
        L.append("| Asset class | Coverage | n | 1d | 5d | 1m | 3m | YTD | 1y | Breadth | Trend |")
        L.append("| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: | ---: | ---: | :--- |")
        for s in members:
            L.append(
                f"| {s['asset_class']} | `{s['coverage']}` | {s['n_instruments']} | "
                + " | ".join(fmt(s[k], 2, "%", True) for k in (
                    "median_change_1d_pct", "median_change_5d_pct", "median_change_1m_pct",
                    "median_change_3m_pct", "median_change_ytd_pct", "median_change_1y_pct"))
                + f" | {fmt(s['pct_above_50dma'], 0, '%')} | `{s['trend']}` |"
            )
    L.append("")
    L.append("### 2. Largest Deviations (|Z| vs 20-day mean)")
    L.append("")
    L.append("| Instrument | Asset class | Price | 1d | 5d | Z-score | Vol ratio | 52w drawdown |")
    L.append("| :--- | :--- | ---: | ---: | ---: | ---: | ---: | ---: |")
    seen: set = set()
    ranked = sorted((r for r in result["rows"] if r["z_score_20d"] is not None), key=lambda r: -abs(r["z_score_20d"]))
    for r in ranked:
        if r["asset_id"] in seen:
            continue
        seen.add(r["asset_id"])
        L.append(
            f"| `{r['asset_id']}` {r['asset_name']} | {r['asset_class']} | {fmt_price(r['price'])} | "
            f"{fmt(r['change_1d_pct'], 2, '%', True)} | {fmt(r['change_5d_pct'], 2, '%', True)} | "
            f"{fmt(r['z_score_20d'], 2, '', True)} | {fmt(r['vol_ratio_20d'], 2, 'x')} | {fmt(r['drawdown_52w_pct'], 1, '%')} |"
        )
        if len(seen) >= 15:
            break
    L.append("")
    L.append("### 3. Class-Specific Metrics")
    for cls in result["classes"]:
        ms = [m for m in result["metrics"] if m["asset_class"] == cls]
        if not ms:
            continue
        shown = [m for m in ms if not m["metric_id"].startswith(("sofr_fwd_3m_", "ust_par_"))]
        L.append("")
        L.append(f"**{UNIVERSE[cls]['name']}** — {UNIVERSE[cls]['note']}")
        L.append("")
        L.append("| Metric | Value | Source | Note |")
        L.append("| :--- | ---: | :--- | :--- |")
        if cls == "bonds":
            pts = [m for m in ms if m["metric_id"].startswith("ust_par_")]
            if pts:
                curve_txt = " · ".join(f"{m['metric_id'][8:].upper()} {m['value']:.2f}" for m in pts)
                L.append(f"| Treasury par curve (%) | {curve_txt} | {pts[0]['source']} | |")
        for m in shown:
            L.append(f"| {m['metric_name']} | {format_metric(m)} | {m['source']} | {m['note']} |")
    L.append("")
    L.append("### 4. Alerts")
    L.append("")
    if result["alerts"]:
        L.append("| Severity | Asset | Asset class | Signal | Details |")
        L.append("| :--- | :--- | :--- | :--- | :--- |")
        for a in result["alerts"]:
            L.append(f"| `{a['severity'].upper()}` | `{a['asset_id']}` | {a['asset_class']} | `{a['signal_type']}` | {a['details']} |")
    else:
        L.append("No triggers fired.")
    L.append("")
    L.append("### 5. Coverage & Data Gaps")
    L.append("")
    L.append("- `direct` = quoted instruments of the class itself · `proxy` = listed stand-ins for an OTC or "
             "private market · `model` = computed estimate, not a quote.")
    for cls in result["classes"]:
        if UNIVERSE[cls]["coverage"] != "direct":
            L.append(f"- **{UNIVERSE[cls]['name']}** (`{UNIVERSE[cls]['coverage']}`): {UNIVERSE[cls]['note']}")
    if result["missing"]:
        L.append(f"- No usable data this run for: {', '.join(result['missing'])}.")
    L.append("")
    return "\n".join(L)


def format_metric(m: Dict[str, Any]) -> str:
    v, unit = m["value"], m["unit"]
    if unit == "pct":
        return f"{v:,.2f}%"
    if unit == "bps":
        return f"{v:+,.0f} bps"
    if unit == "pts":
        return f"{v:+,.2f} pts"
    if unit == "usd":
        return f"${v / 1e9:,.1f}B" if abs(v) >= 1e9 else f"${v:,.2f}"
    if unit == "contracts":
        return f"{v:,.0f}"
    if unit == "count":
        return f"{v:+,.1f}"
    return f"{v:,.3f}"


def print_terminal(result: Dict[str, Any]) -> None:
    bar = "=" * 118
    print(bar)
    print(f" ASSET-CLASS WATCH — {result['run_date']} | {result['timestamp_utc']} | "
          f"{len(result['rows'])} instruments, {len(result['metrics'])} metrics, {len(result['alerts'])} alerts")
    print(bar)
    for group in GROUPS:
        members = [s for s in result["summaries"] if s["group"] == group]
        if not members:
            continue
        print(f"\n[{group}]")
        print(f"  {'Asset class':<42}{'cov':<7}{'n':>3}{'1d':>9}{'5d':>9}{'1m':>9}{'YTD':>9}{'1y':>9}{'>50d':>7}  trend")
        for s in members:
            print(
                f"  {s['asset_class']:<42}{s['coverage']:<7}{s['n_instruments']:>3}"
                f"{fmt(s['median_change_1d_pct'], 2, '%', True):>9}{fmt(s['median_change_5d_pct'], 2, '%', True):>9}"
                f"{fmt(s['median_change_1m_pct'], 2, '%', True):>9}{fmt(s['median_change_ytd_pct'], 1, '%', True):>9}"
                f"{fmt(s['median_change_1y_pct'], 1, '%', True):>9}{fmt(s['pct_above_50dma'], 0, '%'):>7}  {s['trend']}"
            )
    print("\n[Class-specific metrics — highlights]")
    for cls in result["classes"]:
        ms = [m for m in result["metrics"] if m["asset_class"] == cls
              and not m["metric_id"].startswith(("sofr_fwd_3m_", "ust_par_", "tracking_gap_", "excess_return_"))]
        if ms:
            print(f"  {UNIVERSE[cls]['name']}")
            for m in ms[:9]:
                print(f"    {m['metric_name']:<62} {format_metric(m):>14}")
            if len(ms) > 9:
                print(f"    … {len(ms) - 9} more in data/asset_class_metrics.csv")
    print(f"\n[Alerts — {len(result['alerts'])}]")
    for a in result["alerts"][:25]:
        print(f"  [{a['severity'].upper():<6}] {a['asset_id']:<14} {a['signal_type']:<26} {a['details'][:150]}")
    if len(result["alerts"]) > 25:
        print(f"  … {len(result['alerts']) - 25} more")
    if result["missing"]:
        print(f"\n[No data] {', '.join(result['missing'])}")
    print(bar)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch fourteen asset classes and instrument families.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print without writing files (default)")
    parser.add_argument("--write-csv", action="store_true", help="Update data/asset_class*.csv")
    parser.add_argument("--write-report", metavar="PATH", help="Write a Markdown brief to PATH")
    parser.add_argument("--classes", nargs="+", choices=CLASS_ORDER, metavar="CLASS",
                        help=f"Subset of classes: {' '.join(CLASS_ORDER)}")
    parser.add_argument("--data-dir", default="data", help="Data directory (default: data)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of the terminal view")
    parser.add_argument("--list", action="store_true", help="List the watched universe and exit")
    args = parser.parse_args()

    if args.list:
        for group in GROUPS:
            print(f"\n{group}")
            for cls in CLASS_ORDER:
                meta = UNIVERSE[cls]
                if meta["group"] == group:
                    print(f"  {cls:<18} {meta['name']} [{meta['coverage']}] — {len(meta['instruments'])} instruments")
                    print("    " + ", ".join(t for t, *_ in meta["instruments"]))
        return 0

    classes = [c for c in CLASS_ORDER if not args.classes or c in args.classes]
    result = run_watch(classes)
    if not result["rows"]:
        print("[ERR] No instrument data could be fetched.", file=sys.stderr)
        return 1

    if args.write_csv:
        write_csvs(result, Path(args.data_dir))
        print(f"[OK] Updated asset-class CSV files in `{args.data_dir}/` for run_date={result['run_date']}.")
    if args.write_report:
        path = Path(args.write_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(result), encoding="utf-8")
        print(f"[OK] Wrote asset-class brief to `{path}`.")

    if args.json:
        printable = {k: v for k, v in result.items() if k != "history"}
        print(json.dumps(printable, indent=2, default=str))
    else:
        print_terminal(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
