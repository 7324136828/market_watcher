#!/usr/bin/env python3
"""
build_market_update.py — weekly "Market Update" briefing as a PDF (and its Markdown source).

Builds a numbered, outline-style briefing on the week in markets — every region, not only the
United States — in three steps:

  1. Market data   Weekly, month-to-date and year-to-date moves for about 80 instruments: US, European,
                   Asia-Pacific, Latin American and Middle East / Africa equity indices, US sectors and
                   styles, Treasury yields, commodities, currencies, crypto and volatility (Yahoo Finance
                   daily bars and the treasury.gov par curve). If the other watchers have run, their CSVs
                   in `data/` add stock movers, the Fed path, mortgage rates, prediction markets, drug,
                   housing and luxury prices.
  2. News          Headlines of the week from public RSS feeds (Google News searches by theme and
                   region, BBC, Financial Times, Nikkei Asia, South China Morning Post, Economic Times,
                   The Guardian, DW, France 24 and WSJ Markets). Only established outlets are kept. Headlines
                   about the same story are grouped; a story reported by more outlets ranks higher. Every
                   headline is quoted as published and cited with a link to its publisher.
  3. Rendering     The briefing is written as Markdown (`reports/market-update-YYYY-MM-DD.md`) and typeset
                   to PDF (`reports/market-update-YYYY-MM-DD.pdf`) by a small built-in PDF writer — no
                   third-party packages.

The statements about prices are computed; the news items are publishers' headlines, not analysis. To add
commentary, edit the Markdown and render it again with `--from-markdown`.

The news feeds only reach back about a week, so an issue for an older week (`--week-ending`) has full
market data but few or no headlines.

Usage:
  python3 scripts/build_market_update.py                          # last completed week -> reports/*.md + *.pdf
  python3 scripts/build_market_update.py --week-ending 2026-11-13
  python3 scripts/build_market_update.py --site czpzc.com --out-dir reports
  python3 scripts/build_market_update.py --no-news                # market data only
  python3 scripts/build_market_update.py --from-markdown reports/market-update-2026-11-16.md   # re-render after editing
  python3 scripts/build_market_update.py --dry-run                # print the Markdown, write nothing
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import email.utils
import html
import math
import re
import sys
import unicodedata
import urllib.parse
import xml.etree.ElementTree as ET
import zlib
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from watch_asset_classes import fetch_many, http_get  # shared Yahoo / HTTP helpers

UTC_TZ = dt.timezone.utc
DEFAULT_SITE = "czpzc.com"

# ---------------------------------------------------------------------------
# Market universe: (ticker, display name, group, unit)   unit: pct change | yield (bps change)
# ---------------------------------------------------------------------------
MARKETS: List[Tuple[str, str, str, str]] = [
    ("^GSPC", "S&P 500", "us", "pct"), ("^IXIC", "Nasdaq", "us", "pct"), ("^DJI", "DJIA", "us", "pct"),
    ("^RUT", "Russell 2000", "us", "pct"),
    ("RSP", "equal-weighted S&P 500", "style", "pct"), ("RPV", "S&P 500 Pure Value", "style", "pct"),
    ("RPG", "S&P 500 Pure Growth", "style", "pct"),
    ("^GSPTSE", "Canada TSX", "americas", "pct"), ("^BVSP", "Brazil Bovespa", "americas", "pct"),
    ("^MXX", "Mexico IPC", "americas", "pct"), ("^MERV", "Argentina Merval", "americas", "pct"),
    ("^IPSA", "Chile IPSA", "americas", "pct"),
    ("^STOXX", "STOXX Europe 600", "europe", "pct"), ("^STOXX50E", "Euro Stoxx 50", "europe", "pct"),
    ("^FTSE", "UK FTSE 100", "europe", "pct"), ("^GDAXI", "Germany DAX", "europe", "pct"),
    ("^FCHI", "France CAC 40", "europe", "pct"), ("^IBEX", "Spain IBEX 35", "europe", "pct"),
    ("FTSEMIB.MI", "Italy FTSE MIB", "europe", "pct"), ("^SSMI", "Switzerland SMI", "europe", "pct"),
    ("^N225", "Japan Nikkei 225", "asia", "pct"), ("^HSI", "Hong Kong Hang Seng", "asia", "pct"),
    ("000001.SS", "Shanghai Composite", "asia", "pct"), ("^KS11", "South Korea KOSPI", "asia", "pct"),
    ("^TWII", "Taiwan TAIEX", "asia", "pct"), ("^BSESN", "India Sensex", "asia", "pct"),
    ("^AXJO", "Australia ASX 200", "asia", "pct"), ("^STI", "Singapore STI", "asia", "pct"),
    ("^JKSE", "Indonesia JCI", "asia", "pct"),
    ("^TASI.SR", "Saudi Tadawul", "mea", "pct"), ("^TA125.TA", "Israel TA-125", "mea", "pct"),
    ("EZA", "South Africa (EZA, USD)", "mea", "pct"), ("TUR", "Türkiye (TUR, USD)", "mea", "pct"),
    ("ACWI", "MSCI All-Country World (ACWI)", "global", "pct"), ("EFA", "developed markets ex-US (EFA)", "global", "pct"),
    ("EEM", "emerging markets (EEM)", "global", "pct"),
    ("XLK", "Technology", "sector", "pct"), ("XLC", "Communication Services", "sector", "pct"),
    ("XLY", "Consumer Discretionary", "sector", "pct"), ("XLP", "Consumer Staples", "sector", "pct"),
    ("XLF", "Financials", "sector", "pct"), ("XLV", "Health Care", "sector", "pct"),
    ("XLI", "Industrials", "sector", "pct"), ("XLE", "Energy", "sector", "pct"),
    ("XLB", "Materials", "sector", "pct"), ("XLU", "Utilities", "sector", "pct"),
    ("XLRE", "Real Estate", "sector", "pct"),
    ("^IRX", "3-month T-bill", "rates", "yield"), ("^FVX", "5-year Treasury", "rates", "yield"),
    ("^TNX", "10-year Treasury", "rates", "yield"), ("^TYX", "30-year Treasury", "rates", "yield"),
    ("AGG", "US aggregate bonds (AGG)", "credit", "pct"), ("LQD", "investment-grade corporates (LQD)", "credit", "pct"),
    ("HYG", "high-yield corporates (HYG)", "credit", "pct"), ("EMB", "emerging-market dollar debt (EMB)", "credit", "pct"),
    ("BNDX", "international bonds, hedged (BNDX)", "credit", "pct"),
    ("CL=F", "WTI crude", "commodities", "pct"), ("BZ=F", "Brent crude", "commodities", "pct"),
    ("NG=F", "US natural gas", "commodities", "pct"), ("GC=F", "gold", "commodities", "pct"),
    ("SI=F", "silver", "commodities", "pct"), ("HG=F", "copper", "commodities", "pct"),
    ("ZW=F", "wheat", "commodities", "pct"), ("ZS=F", "soybeans", "commodities", "pct"),
    ("DX-Y.NYB", "dollar index", "fx", "pct"), ("EURUSD=X", "EUR/USD", "fx", "pct"), ("USDJPY=X", "USD/JPY", "fx", "pct"),
    ("GBPUSD=X", "GBP/USD", "fx", "pct"), ("USDCNY=X", "USD/CNY", "fx", "pct"), ("USDINR=X", "USD/INR", "fx", "pct"),
    ("USDBRL=X", "USD/BRL", "fx", "pct"), ("USDMXN=X", "USD/MXN", "fx", "pct"), ("AUDUSD=X", "AUD/USD", "fx", "pct"),
    ("BTC-USD", "Bitcoin", "crypto", "pct"), ("ETH-USD", "Ether", "crypto", "pct"), ("SOL-USD", "Solana", "crypto", "pct"),
    ("^VIX", "VIX", "vol", "pct"),
]
REGION_LABELS = {"europe": "Europe", "asia": "Asia-Pacific", "americas": "Americas outside the US", "mea": "Middle East and Africa"}


# ---------------------------------------------------------------------------
# 1. Market data
# ---------------------------------------------------------------------------

def last_completed_week(today: dt.date) -> dt.date:
    """The most recent Friday strictly before `today` (a Saturday or Sunday run uses the Friday just gone)."""
    day = today - dt.timedelta(days=1)
    while day.weekday() != 4:
        day -= dt.timedelta(days=1)
    return day


def value_on_or_before(dates: List[str], values: List[float], day: dt.date, max_age_days: int = 6) -> Optional[Tuple[str, float]]:
    """Latest observation dated on or before `day`, unless it is older than `max_age_days`."""
    limit = day.isoformat()
    floor = (day - dt.timedelta(days=max_age_days)).isoformat()
    for d, v in zip(reversed(dates), reversed(values)):
        if d <= limit:
            return (d, v) if d >= floor else None
    return None


def collect_markets(week_end: dt.date) -> Dict[str, Dict[str, Any]]:
    """Per ticker: level at the week's end and the change over the week, month to date and year to date."""
    history = fetch_many([m[0] for m in MARKETS], "1y")
    prev_end = week_end - dt.timedelta(days=7)
    month_ref = week_end.replace(day=1) - dt.timedelta(days=1)
    year_ref = dt.date(week_end.year - 1, 12, 31)
    out: Dict[str, Dict[str, Any]] = {}
    for ticker, name, group, unit in MARKETS:
        h = history.get(ticker)
        if not h:
            continue
        now = value_on_or_before(h["dates"], h["close"], week_end)
        before = value_on_or_before(h["dates"], h["close"], prev_end)
        if not now or not before or now[0] == before[0]:
            continue

        def change(ref_day: dt.date) -> Optional[float]:
            ref = value_on_or_before(h["dates"], h["close"], ref_day, max_age_days=10)
            if not ref:
                return None
            return (now[1] - ref[1]) * 100.0 if unit == "yield" else (now[1] / ref[1] - 1.0) * 100.0

        out[ticker] = {"ticker": ticker, "name": name, "group": group, "unit": unit, "level": now[1], "as_of": now[0],
                       "week": (now[1] - before[1]) * 100.0 if unit == "yield" else (now[1] / before[1] - 1.0) * 100.0,
                       "mtd": change(month_ref), "ytd": change(year_ref)}
    return out


def collect_treasury_curve(week_end: dt.date) -> Dict[str, Tuple[float, Optional[float]]]:
    """{tenor: (yield at week end, change over the week in bps)} from the treasury.gov daily par curve."""
    rows: List[Dict[str, str]] = []
    for year in sorted({week_end.year, (week_end - dt.timedelta(days=10)).year}, reverse=True):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{year}/all?type=daily_treasury_yield_curve&field_tdr_date_value={year}&page&_format=csv")
        try:
            rows += list(csv.DictReader(http_get(url).splitlines()))
        except Exception as exc:
            print(f"[WARN] Treasury par curve {year}: {exc}", file=sys.stderr)
    dated = []
    for r in rows:
        try:
            dated.append((dt.datetime.strptime(r["Date"], "%m/%d/%Y").date(), r))
        except (KeyError, ValueError):
            continue
    dated.sort(key=lambda x: x[0])

    def row_at(day: dt.date) -> Optional[Dict[str, str]]:
        hits = [r for d, r in dated if day - dt.timedelta(days=6) <= d <= day]
        return hits[-1] if hits else None

    now, before = row_at(week_end), row_at(week_end - dt.timedelta(days=7))
    out: Dict[str, Tuple[float, Optional[float]]] = {}
    if not now:
        return out
    for col, tenor in (("3 Mo", "3-month"), ("2 Yr", "2-year"), ("5 Yr", "5-year"), ("10 Yr", "10-year"), ("30 Yr", "30-year")):
        try:
            y = float(now[col])
        except (KeyError, ValueError):
            continue
        try:
            delta: Optional[float] = (y - float(before[col])) * 100.0 if before else None
        except (KeyError, ValueError):
            delta = None
        out[tenor] = (y, delta)
    return out


def load_csv_latest(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        rows = list(csv.DictReader(fh))
    if not rows or "date" not in rows[0]:
        return rows
    latest = max(r["date"] for r in rows)
    return [r for r in rows if r["date"] == latest]


def num(value: Any) -> Optional[float]:
    try:
        return float(value) if value not in (None, "") else None
    except (TypeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# 2. News
# ---------------------------------------------------------------------------

def google_news(query: str) -> str:
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(query + " when:7d") + "&hl=en-US&gl=US&ceid=US:en"


# (feed label, url, fixed source name or None when each item names its own, weight)
FEEDS: List[Tuple[str, str, Optional[str], float]] = [
    ("Top business", "https://news.google.com/rss/headlines/section/topic/BUSINESS?hl=en-US&gl=US&ceid=US:en", None, 2.0),
    ("Markets", google_news('"stock market" OR "global markets" OR "Wall Street" stocks'), None, 1.0),
    ("Central banks", google_news('"Federal Reserve" OR ECB OR "Bank of Japan" OR "Bank of England" OR "central bank" rates'), None, 1.0),
    ("Bonds", google_news('"bond yields" OR Treasuries OR gilts OR bunds OR "credit spreads"'), None, 1.0),
    ("Inflation and economy", google_news("inflation OR GDP OR jobs report OR unemployment economy"), None, 1.0),
    ("Earnings", google_news("earnings results shares quarter"), None, 1.0),
    ("AI and tech", google_news("AI stocks OR Nvidia OR chipmakers OR semiconductors shares"), None, 1.0),
    ("Oil and commodities", google_news('"oil prices" OR OPEC OR "gold price" OR copper OR "natural gas" prices'), None, 1.0),
    ("Deals", google_news("IPO OR merger OR acquisition OR takeover"), None, 1.0),
    ("Currencies", google_news("dollar OR yen OR euro OR yuan currency markets"), None, 1.0),
    ("Crypto", google_news("bitcoin OR crypto market"), None, 1.0),
    ("Europe", google_news('"European stocks" OR "European shares" OR STOXX OR DAX OR FTSE'), None, 1.0),
    ("Asia", google_news('"Asian markets" OR Nikkei OR "Hang Seng" OR "Asian shares"'), None, 1.0),
    ("China", google_news("China economy OR Chinese stocks OR yuan OR PBOC"), None, 1.0),
    ("India", google_news("Sensex OR Nifty OR RBI OR rupee India markets"), None, 1.0),
    ("Japan", google_news('"Bank of Japan" OR yen OR Nikkei Japan economy'), None, 1.0),
    ("Latin America", google_news('Brazil OR Mexico OR Argentina markets OR Ibovespa OR "Latin America" economy'), None, 1.0),
    ("Emerging markets", google_news('"emerging markets" stocks OR bonds OR currencies'), None, 1.0),
    ("Middle East and Africa", google_news('"Saudi" OR "Gulf markets" OR "South Africa" OR Nigeria OR Egypt OR Turkey economy markets'), None, 1.0),
    ("BBC", "https://feeds.bbci.co.uk/news/business/rss.xml", "BBC", 1.0),
    ("FT", "https://www.ft.com/markets?format=rss", "FT", 1.5),
    ("WSJ", "https://feeds.content.dowjones.io/public/rss/RSSMarketsMain", "WSJ", 1.5),
    ("Nikkei Asia", "https://asia.nikkei.com/rss/feed/nar", "Nikkei Asia", 1.0),
    ("SCMP", "https://www.scmp.com/rss/92/feed", "SCMP", 1.0),
    ("Economic Times", "https://economictimes.indiatimes.com/markets/rssfeeds/1977021501.cms", "Economic Times", 1.0),
    ("Guardian", "https://www.theguardian.com/business/rss", "Guardian", 1.0),
    ("DW", "https://rss.dw.com/rdf/rss-en-bus", "DW", 1.0),
    ("France 24", "https://www.france24.com/en/business-tech/rss", "France 24", 1.0),
]
SOURCE_SHORT = {
    "The Wall Street Journal": "WSJ", "Wall Street Journal": "WSJ", "Financial Times": "FT", "The Economist": "Economist",
    "The New York Times": "NYT", "The Washington Post": "Washington Post", "The Guardian": "Guardian",
    "South China Morning Post": "SCMP", "The Economic Times": "Economic Times", "Associated Press": "AP", "AP News": "AP",
    "Bloomberg.com": "Bloomberg", "Yahoo Finance": "Yahoo Finance", "Investor's Business Daily": "IBD",
    "The Globe and Mail": "Globe and Mail", "The Times of India": "Times of India", "Al Jazeera": "Al Jazeera",
    "Nikkei Asia": "Nikkei Asia", "Barron's": "Barron's", "Yahoo Finance UK": "Yahoo Finance", "Yahoo! Finance Canada": "Yahoo Finance",
    "Yahoo Finance Australia": "Yahoo Finance", "Yahoo Finance Singapore": "Yahoo Finance", "Yahoo": "Yahoo Finance",
    "Barron’s": "Barron's", "The Hindu": "The Hindu BusinessLine", "Caixin": "Caixin Global", "Yonhap": "Yonhap News Agency",
    "The Australian Financial Review": "Australian Financial Review", "MarketWatch": "MarketWatch", "CNBC": "CNBC", "Reuters": "Reuters",
    "BBC": "BBC", "Business Insider": "Business Insider", "Fortune": "Fortune", "Forbes": "Forbes", "Axios": "Axios",
    "The Japan Times": "Japan Times", "The Straits Times": "Straits Times", "Mint": "Mint", "Livemint": "Mint",
}
PRIORITY_SOURCES = ["Reuters", "Bloomberg", "WSJ", "FT", "AP", "Economist", "Barron's", "CNBC", "BBC", "MarketWatch",
                    "Nikkei Asia", "NYT", "Guardian", "SCMP", "Economic Times", "Yahoo Finance"]
# Only headlines from these outlets are used (short names, after SOURCE_SHORT). Extend the set to admit more.
ALLOWED_SOURCES = {
    "Reuters", "Bloomberg", "WSJ", "FT", "AP", "Economist", "Barron's", "CNBC", "BBC", "MarketWatch", "NYT", "Washington Post",
    "Guardian", "Nikkei Asia", "SCMP", "Economic Times", "Yahoo Finance", "Fortune", "Forbes", "Axios", "Business Insider",
    "Politico", "The Hill", "Semafor", "NPR", "CNN", "CNN Business", "Fox Business", "Financial Post", "Globe and Mail", "CBC",
    "Japan Times", "Straits Times", "Mint", "Times of India", "Business Standard", "Moneycontrol", "The Hindu BusinessLine",
    "Al Jazeera", "DW", "France 24", "Euronews", "Sky News", "The Telegraph", "The Times", "Handelsblatt", "Caixin Global",
    "Korea Herald", "Yonhap News Agency", "Bangkok Post", "Australian Financial Review", "The Sydney Morning Herald",
    "Buenos Aires Times", "Arab News", "The National", "Gulf News", "AGBI", "Africanews", "Business Day", "CoinDesk", "The Block",
    "TechCrunch", "The Information", "The Verge", "IBD", "Morningstar", "S&P Global", "IMF", "BIS",
}
EXCLUDED_PATTERNS = re.compile(
    r"stocks? to (buy|watch|sell)|should you|how to |here'?s (how|why|what)|is the stock market open|best .* (stocks|etfs|funds)"
    r"|price prediction|horoscope|live updates|live:|as it happened|what to (know|watch|expect)|\bquiz\b|\bpodcast\b"
    r"|my (wife|husband|mother|father|son|daughter|cousin|brother|sister)|\bi'm \d\d\b|\bi am \d\d\b|dear |\bopinion\b|\bvideo:"
    r"|stock market today|charts, data and news|reports (first|second|third|fourth)[- ]quarter|^history of|^letters?:|^review:"
    r"|\bnewsletter\b|^watch:|^listen:|\bexplained\b|^the week in|week ahead|morning bid|^factbox|current unemployment rate"
    r"|how is the economy doing|crypto markets? today|market today,|^watch |editor'?s choice|stocks i |buy the dip|investing expert"
    r"|investors are watching|this week on the stock market|weekly review|contribution limits|401\(k\)|social security|\bretire"
    r"|mortgage rates today|credit cards?\b|fuel economy|stock market news,|indicators to watch|means for your portfolio|market talk|tax brackets|roundup:",
    re.I)
# A daily market wrap ("European shares end higher ..."): kept once per region, never ranked as a story.
WRAP_PATTERN = re.compile(
    r"^(?:[\w.'’&%-]+\s){0,3}(shares|stocks|markets?|bourses|equities|sensex|nifty|nikkei|kospi|wall st|wall street|stoxx|ftse|dax|futures)\b"
    r"(?! of).*\b(rise|rises|rose|fall|falls|fell|slip|slips|gain|gains|drop|drops|end|ends|close|closes|retreat|retreats|rally|rallies"
    r"|steady|mixed|lower|higher|slump|slumps|jump|jumps|climb|climbs|tumble|tumbles|record|notch|notches|pull back|rebound|rebounds"
    r"|slide|slides|sink|sinks|open|opens|edge|edges|near|hit|hits|extend|extends|snap|snaps|lead|leads|weigh|weighs|brace|braces"
    r"|waver|wavers|struggle|struggles|wobble|wobbles|surge|surges|soar|soars|plunge|plunges|dip|dips|advance|advances|decline"
    r"|declines|tread|hold|holds|pare|pares|eke|start|starts|finish|finishes|head|heads|down|up|reverse|reverses)\b|markets wrap", re.I)
TITLE_PREFIX = re.compile(r"^(exclusive|analysis|explainer|breakingviews|insight|focus|update \d+|column|scoop)\s*[:\-–|]\s*", re.I)
STOPWORDS = set("""a an and are as at be but by for from has have he her his how in into is it its more new not of on or over s
says say said she than that the their they this to up us was were what when where which who why will with after amid before
could would may might can about against all also among around because been being between both down during each few first just
last latest like most much near next no now off one only other out own per same so some such them then there these those through
too under until very via week weeks year years month day days today monday tuesday wednesday thursday friday saturday sunday
news report reports update market markets stock stocks shares share again maybe despite ahead still back here make makes made
take takes get gets keep keeps set sets see sees look looks put puts move moves come comes going want wants need needs
big bigger biggest high higher highest low lower lowest best worst top key major global world says wall street""".split())

# Too vague to be the subject of a story.
GENERIC_SUBJECTS = set("""price prices investor investors bank banks company companies record trade deal plan plans data rise
fall sale sales billion million trillion firm firms group business economy economic rate rates growth profit earnings quarter
quarterly result results forecast outlook demand supply cost costs risk risks concern concerns fear fears hope hopes surge drop
jump slump rally selloff sell-off gain gains loss losses shock boost hit crisis call calls warn warns warning expect expected
analyst analysts trader traders official officials ceo chief boss fund funds index government state states country nation
power future month quarter time people money cash value target bet bets push pressure question questions""".split())

# theme id -> (section title, part, keywords). A phrase with a space must appear as a phrase.
THEMES: Dict[str, Tuple[str, str, List[str]]] = {
    "ai": ("AI and technology", "themes", ["ai", "nvidia", "anthropic", "openai", "chip", "chips", "chipmaker", "chipmakers",
           "semiconductor", "semiconductors", "data center", "data centers", "tsmc", "huang", "microsoft", "alphabet", "google",
           "meta", "amazon", "apple", "tesla", "software", "tech", "coreweave", "broadcom", "amd", "intel", "samsung"]),
    "earnings": ("Earnings and companies", "themes", ["earnings", "profit", "profits", "revenue", "guidance", "quarterly", "results",
                 "forecast", "outlook", "sales", "dividend", "buyback", "layoffs", "ceo"]),
    "politics": ("Politics, trade and geopolitics", "themes", ["war", "iran", "tariff", "tariffs", "sanction", "sanctions", "election",
                 "elections", "trade deal", "trump", "congress", "democrats", "republicans", "ukraine", "russia", "strait",
                 "ceasefire", "shutdown", "midterm", "midterms", "white house", "blockade", "houthi"]),
    "economy": ("The economy", "themes", ["jobs", "payrolls", "unemployment", "gdp", "recession", "consumer", "consumers", "retail sales",
                "housing", "factory", "manufacturing", "pmi", "economy", "economic", "wages", "spending", "growth", "productivity"]),
    "deals": ("Deals, credit and capital markets", "themes", ["ipo", "merger", "acquisition", "takeover", "buyout", "stake", "listing",
              "rights issue", "bond sale", "debt", "credit", "default", "bankruptcy", "restructuring", "private equity", "spinoff"]),
    "central": ("Central banks, inflation and rates", "rates", ["fed", "fomc", "ecb", "boj", "boe", "pboc", "rba", "rbi", "central bank",
                "central banks", "rate hike", "rate cut", "interest rate", "interest rates", "yield", "yields", "treasury", "treasuries",
                "bond", "bonds", "inflation", "cpi", "warsh", "lagarde", "gilts", "bund", "bunds", "federal reserve"]),
    "commodities": ("Energy and commodities", "macro", ["oil", "crude", "opec", "brent", "gas", "lng", "gold", "copper", "silver",
                    "commodity", "commodities", "wheat", "iron ore", "lithium", "coal", "uranium", "energy"]),
    "fx": ("Currencies", "macro", ["dollar index", "us dollar", "greenback", "against dollar", "against the dollar", "dollar gains",
           "dollar rises", "dollar falls", "dollar slips", "dollar hits", "strong dollar", "weak dollar", "yen", "euro", "yuan",
           "renminbi", "rupee", "peso", "currency", "currencies", "forex", "sterling", "franc", "lira", "rand", "intervention"]),
    "crypto": ("Crypto", "macro", ["bitcoin", "crypto", "cryptocurrency", "ether", "ethereum", "stablecoin", "stablecoins", "solana"]),
}
# When a headline matches several themes equally, the more specific theme (earlier here) wins.
THEME_PRIORITY = ["central", "commodities", "crypto", "fx", "ai", "politics", "deals", "economy", "earnings"]
REGIONS: Dict[str, Tuple[str, List[str]]] = {
    "europe": ("Europe", ["europe", "european", "eu", "eurozone", "germany", "german", "france", "french", "uk", "britain", "british",
               "italy", "italian", "spain", "spanish", "stoxx", "ftse", "london", "swiss", "switzerland", "netherlands", "bank of england",
               "dutch", "sweden", "poland", "brussels", "ecb", "brexit", "eurozone", "sterling", "gilts", "bund", "bunds", "boe"]),
    "asia": ("Asia-Pacific", ["china", "chinese", "japan", "japanese", "india", "indian", "hong kong", "korea", "korean", "taiwan",
             "asia", "asian", "nikkei", "sensex", "nifty", "hang seng", "shanghai", "singapore", "australia", "australian",
             "indonesia", "vietnam", "thailand", "malaysia", "philippines", "beijing", "tokyo", "pboc", "boj", "rbi", "rupee", "yen",
             "yuan", "renminbi", "won", "bank of japan"]),
    "latam": ("Latin America", ["brazil", "brazilian", "mexico", "mexican", "argentina", "argentine", "chile", "colombia",
              "latin america", "ibovespa", "bovespa", "peru", "venezuela", "milei", "lula"]),
    "mea": ("Middle East and Africa", ["saudi", "uae", "dubai", "abu dhabi", "gulf", "africa", "african", "nigeria", "egypt", "turkey",
            "turkish", "israel", "qatar", "south africa", "kenya", "aramco", "kuwait", "rand", "lira", "riyadh"]),
}


def parse_feed(label: str, url: str, fixed_source: Optional[str], weight: float) -> List[Dict[str, Any]]:
    """Items of an RSS 2.0, RDF or Atom feed as {title, source, url, published, feed, rank, weight}."""
    try:
        root = ET.fromstring(http_get(url, timeout=30).encode("utf-8"))
    except Exception as exc:
        print(f"[WARN] news feed {label}: {exc}", file=sys.stderr)
        return []
    items = []
    for rank, node in enumerate(n for n in root.iter() if n.tag.rsplit("}", 1)[-1] in ("item", "entry")):
        fields: Dict[str, str] = {}
        for child in node:
            tag = child.tag.rsplit("}", 1)[-1]
            text = (child.text or "").strip()
            if tag == "link" and not text:
                text = child.get("href", "")
            if tag not in fields and text:
                fields[tag] = text
        title = re.sub(r"\s+", " ", html.unescape(fields.get("title", ""))).strip()
        source = fixed_source or fields.get("source", "")
        if not fixed_source and source and title.endswith(" - " + source):
            title = title[: -len(" - " + source)].strip()
        elif not fixed_source and " - " in title:  # Google News titles end with the publisher
            title, _, tail = title.rpartition(" - ")
            source = source or tail
        title = TITLE_PREFIX.sub("", title)
        if not title or not source or not fields.get("link"):
            continue
        stamp = fields.get("pubDate") or fields.get("published") or fields.get("updated") or fields.get("date") or ""
        published: Optional[dt.datetime] = None
        try:
            published = email.utils.parsedate_to_datetime(stamp)
        except (TypeError, ValueError):
            try:
                published = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00").replace(" ", "T", 1))
            except ValueError:
                published = None
        if published and published.tzinfo is None:
            published = published.replace(tzinfo=UTC_TZ)
        items.append({"title": title, "source": SOURCE_SHORT.get(source, source), "raw_source": source, "url": fields["link"],
                      "published": published, "feed": label, "rank": rank, "weight": weight})
    return items


def keywords(title: str) -> set:
    plain = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode("ascii").lower()
    words = re.findall(r"[a-z0-9][a-z0-9&'.-]*", plain)
    out = set()
    for w in words:
        w = w.strip(".'-").replace("'s", "")
        if len(w) >= 3 and w not in STOPWORDS:
            out.add(w[:-1] if len(w) > 4 and w.endswith("s") and not w.endswith("ss") else w)
    return out


def matches(title_norm: str, terms: List[str]) -> int:
    return sum(1 for t in terms if f" {t} " in title_norm)


def collect_news(week_end: dt.date) -> List[Dict[str, Any]]:
    """Story clusters for the week, most widely reported first."""
    with ThreadPoolExecutor(max_workers=8) as pool:
        batches = list(pool.map(lambda f: parse_feed(*f), FEEDS))
    start = dt.datetime.combine(week_end - dt.timedelta(days=6), dt.time.min, UTC_TZ)
    end = dt.datetime.combine(week_end + dt.timedelta(days=3), dt.time.max, UTC_TZ)
    seen_titles = set()
    stories = []
    for item in (i for batch in batches for i in batch[:100]):
        if item["source"] not in ALLOWED_SOURCES or EXCLUDED_PATTERNS.search(item["title"]) or len(item["title"]) < 25:
            continue
        if item["published"] and not start <= item["published"] <= end:
            continue
        key = re.sub(r"[^a-z0-9]+", " ", item["title"].lower()).strip()
        if (key, item["source"]) in seen_titles:
            continue
        seen_titles.add((key, item["source"]))
        item["keys"] = keywords(item["title"])
        item["wrap"] = bool(WRAP_PATTERN.search(item["title"]))
        if len(item["keys"]) >= 3:
            stories.append(item)

    # Rare words identify a story; common ones ("trump", "rate", "war") must not glue unrelated headlines together.
    doc_freq: Dict[str, int] = {}
    for story in stories:
        for k in story["keys"]:
            doc_freq[k] = doc_freq.get(k, 0) + 1
    total = max(len(stories), 1)
    weight = {k: math.log(1.0 + total / n) for k, n in doc_freq.items()}

    def mass(keys: set) -> float:
        return sum(weight[k] for k in keys)

    # Words capitalised mid-headline by sentence-case outlets are names (Nvidia, Anthropic, Iran): the best story subjects.
    proper_nouns: set = set()
    for story in stories:
        words = story["title"].split()
        if len(words) > 4 and sum(w[:1].isupper() for w in words) / len(words) < 0.6:
            proper_nouns |= keywords(" ".join(w for w in words[1:] if w[:1].isupper()))

    clusters: List[Dict[str, Any]] = []
    for story in sorted(stories, key=lambda s: (-s["weight"], s["rank"])):
        best, best_score = None, 0.0
        for c in clusters:
            if c["wrap"] != story["wrap"]:
                continue
            shared = story["keys"] & c["keys"]
            score = mass(shared) / min(mass(story["keys"]), mass(c["keys"])) if shared else 0.0
            if len(shared) >= 3 and score >= 0.55 and score > best_score:
                best, best_score = c, score
        if best:
            best["items"].append(story)
        else:
            clusters.append({"keys": set(story["keys"]), "items": [story], "wrap": story["wrap"]})

    def source_rank(item: Dict[str, Any]) -> int:
        return PRIORITY_SOURCES.index(item["source"]) if item["source"] in PRIORITY_SOURCES else len(PRIORITY_SOURCES)

    floor = dt.datetime.min.replace(tzinfo=UTC_TZ)
    for c in clusters:
        c["items"].sort(key=lambda i: (source_rank(i), len(i["title"])))
        lead = c["items"][0]
        text = " " + re.sub(r"[^a-z0-9]+", " ", " ".join(i["title"] for i in c["items"][:4]).lower()) + " "
        c["title"], c["lead"] = lead["title"], lead
        c["sources"] = list(dict.fromkeys(i["source"] for i in c["items"]))
        c["latest"] = max((i["published"] or floor) for i in c["items"])
        c["score"] = (len(c["sources"]) + max(i["weight"] for i in c["items"])
                      + (1.0 if any(i["feed"] == "Top business" and i["rank"] < 12 for i in c["items"]) else 0.0)
                      + (0.5 if lead["source"] in PRIORITY_SOURCES[:8] else 0.0))
        theme_hits = {tid: matches(text, THEMES[tid][2]) for tid in THEME_PRIORITY}
        lead_text = " " + re.sub(r"[^a-z0-9]+", " ", lead["title"].lower()) + " "
        region_hits = {rid: matches(lead_text, kws) for rid, (_, kws) in REGIONS.items()}
        c["theme"] = max(THEME_PRIORITY, key=lambda t: theme_hits[t]) if max(theme_hits.values()) else None
        c["region"] = max(region_hits, key=region_hits.get) if max(region_hits.values()) else None
        # the story's subject: its most widely used keyword this week (e.g. "nvidia", "anthropic", "oil")
        common = [k for k in lead["keys"] if doc_freq.get(k, 0) >= 4 and k not in GENERIC_SUBJECTS]
        named = [k for k in common if k in proper_nouns]
        c["subject"] = max(named or common, key=lambda k: doc_freq[k]) if common else None
        c["names"] = lead["keys"] & proper_nouns
    clusters.sort(key=lambda c: -c["score"])
    return clusters

# ---------------------------------------------------------------------------
# 3. Outline (Markdown)
# ---------------------------------------------------------------------------

def pct(value: Optional[float], decimals: int = 2) -> str:
    return "n/a" if value is None else f"{value:+.{decimals}f}%"


def level(value: float) -> str:
    if abs(value) >= 1000:
        return f"{value:,.0f}"
    if abs(value) >= 100:
        return f"{value:,.1f}"
    return f"{value:,.2f}" if abs(value) >= 10 else f"{value:,.4f}"


def verb(value: float, up: str = "rose", down: str = "fell") -> str:
    return up if value > 0.005 else down if value < -0.005 else "was flat"


def md_escape(text: str) -> str:
    return text.replace("[", "(").replace("]", ")").replace("*", "").replace("_", " ").strip()


def cites(cluster: Dict[str, Any], limit: int = 4) -> str:
    seen, parts = set(), []
    for item in cluster["items"]:
        if item["source"] not in seen:
            seen.add(item["source"])
            parts.append(f"[({md_escape(item['source'])})]({item['url']})")
        if len(parts) >= limit:
            break
    return " ".join(parts)


def group_rows(markets: Dict[str, Dict[str, Any]], group: str) -> List[Dict[str, Any]]:
    return sorted((m for m in markets.values() if m["group"] == group), key=lambda m: -m["week"])


def joined(rows: List[Dict[str, Any]], key: str = "week") -> str:
    return "; ".join(f"{m['name']} {pct(m[key])}" for m in rows if m.get(key) is not None)


def read_extras(data_dir: Path) -> Dict[str, List[Dict[str, str]]]:
    names = ["asset_class_movers", "asset_class_metrics", "housing_prices", "drug_prices", "luxury_brands", "luxury_models"]
    return {n: load_csv_latest(data_dir / f"{n}.csv") for n in names}


def build_outline(markets: Dict[str, Dict[str, Any]], curve: Dict[str, Tuple[float, Optional[float]]],
                  clusters: List[Dict[str, Any]], extras: Dict[str, List[Dict[str, str]]], week_end: dt.date,
                  generated: dt.datetime) -> List[str]:
    issue = week_end + dt.timedelta(days=3)
    L: List[str] = [f"# Market Update – {issue.strftime('%B')} {issue.day}, {issue.year}", ""]
    counter = [0]
    used: set = set()

    def section(text: str) -> None:
        counter[0] += 1
        L.extend(["", f"### #{counter[0]}. {text}"])

    def reliable(c: Dict[str, Any], outlets: int) -> bool:
        return len(c["sources"]) >= 2 or c["lead"]["source"] in PRIORITY_SOURCES[:outlets]

    def related(a: Dict[str, Any], b: Dict[str, Any]) -> bool:
        """Same story: three shared keywords, or two shared names (Nvidia + Huang, Anthropic + IPO)."""
        shared = a["lead"]["keys"] & b["lead"]["keys"]
        return len(shared) >= 3 or len(shared & a["names"] & b["names"]) >= 2

    def take(predicate: Any, limit: int, per_subject: int = 2, outlets: int = 10, consume: bool = True) -> List[List[Dict[str, Any]]]:
        """
        Up to `limit` stories matching the predicate, most widely covered first. A story is a lead headline (the
        one carried by most outlets) plus related headlines on the same subject. At most `per_subject` stories
        share a subject. A single-outlet headline only counts if it comes from one of the first `outlets`
        priority sources.
        """
        by_subject: Dict[Any, List[Dict[str, Any]]] = {}
        for c in clusters:
            if id(c) not in used and not c["wrap"] and predicate(c) and reliable(c, outlets):
                by_subject.setdefault(c["subject"] or id(c), []).append(c)
        groups: List[List[Dict[str, Any]]] = []
        for members in by_subject.values():
            remaining = sorted(members, key=lambda c: (-len(c["sources"]), -c["score"]))
            for _ in range(per_subject):
                if not remaining:
                    break
                lead, rest = remaining[0], remaining[1:]
                kin = [c for c in rest if related(lead, c)]
                groups.append([lead] + kin)
                remaining = [c for c in rest if not any(c is k for k in kin)]
        groups.sort(key=lambda g: -(len({s for c in g for s in c["sources"]}) + g[0]["score"] / 10.0))
        groups = groups[:limit]
        if consume:
            used.update(id(c) for g in groups for c in g)
        return groups

    def emit(group: List[Dict[str, Any]], depth: int = 0, subs: int = 3) -> None:
        """Lead headline with its citations, then up to `subs` related headlines from other outlets."""
        pad = "   " * depth
        lead = group[0]
        L.append(f"{pad}1. {md_escape(lead['title'])} {cites(lead)}")
        shown = {lead["lead"]["source"]}
        candidates = [i for i in lead["items"][1:] if len(i["keys"] - lead["lead"]["keys"]) >= 3] + [c["lead"] for c in group[1:]]
        for item in candidates:
            if subs <= 0:
                break
            if item["source"] in shown:
                continue
            shown.add(item["source"])
            L.append(f"{pad}   1. {md_escape(item['title'])} [({md_escape(item['source'])})]({item['url']})")
            subs -= 1

    def latest_wrap(region: Optional[str]) -> Optional[Dict[str, Any]]:
        """One market wrap for the region: a week-in-review headline if there is one, else the most recent."""
        wraps = [c for c in clusters if c["wrap"] and c["region"] == region and reliable(c, len(PRIORITY_SOURCES))]
        return max(wraps, key=lambda c: ("week" in c["title"].lower(), c["latest"], c["score"])) if wraps else None

    def m(ticker: str) -> Optional[Dict[str, Any]]:
        return markets.get(ticker)

    # ---- Part 1 ---------------------------------------------------------------------------------
    L.append("## Part 1: Global Stock Market Summary")
    us = group_rows(markets, "us")
    if us:
        tone = ("Stocks rose for the week" if all(x["week"] > 0 for x in us) else
                "Stocks fell for the week" if all(x["week"] < 0 for x in us) else "It was a mixed week for stocks")
        text = f"{tone}: {joined(us)}."
        if m("RSP"):
            text += f" The equal-weighted S&P 500 {verb(m('RSP')['week'])} {abs(m('RSP')['week']):.2f}%."
        if m("RPV") and m("RPG") and m("RPV")["mtd"] is not None and m("RPG")["mtd"] is not None:
            text += (f" For the month to date, S&P 500 Pure Value is {pct(m('RPV')['mtd'])} and Pure Growth is {pct(m('RPG')['mtd'])}.")
        section(text)
    else:
        section("US equity index data was unavailable for this week.")
    ten = curve.get("10-year") or ((m("^TNX")["level"], m("^TNX")["week"]) if m("^TNX") else None)
    two = curve.get("2-year")
    if ten:
        line = f"1. The 10-year Treasury yield {verb(ten[1] or 0)} {abs(ten[1] or 0):.0f} bps to {ten[0]:.2f}%"
        line += f" and the 2-year {verb(two[1] or 0)} {abs(two[1] or 0):.0f} bps to {two[0]:.2f}%." if two else "."
        L.append(line)
    if m("CL=F"):
        line = f"1. WTI crude finished the week at ${m('CL=F')['level']:.2f} ({pct(m('CL=F')['week'])})"
        line += f"; Brent at ${m('BZ=F')['level']:.2f} ({pct(m('BZ=F')['week'])})." if m("BZ=F") else "."
        L.append(line)
    bits = []
    if m("GC=F"):
        bits.append(f"gold {verb(m('GC=F')['week'])} {abs(m('GC=F')['week']):.2f}% to ${m('GC=F')['level']:,.0f}")
    if m("DX-Y.NYB"):
        bits.append(f"the dollar index {verb(m('DX-Y.NYB')['week'])} {abs(m('DX-Y.NYB')['week']):.2f}% to {m('DX-Y.NYB')['level']:.2f}")
    if m("BTC-USD"):
        bits.append(f"Bitcoin {verb(m('BTC-USD')['week'])} {abs(m('BTC-USD')['week']):.2f}% to ${m('BTC-USD')['level']:,.0f}")
    if m("^VIX"):
        bits.append(f"the VIX closed at {m('^VIX')['level']:.2f} ({pct(m('^VIX')['week'], 1)} on the week)")
    if bits:
        text = "; ".join(bits) + "."
        L.append("1. " + text[0].upper() + text[1:])
    big: List[List[Dict[str, Any]]] = []
    for group in take(lambda c: True, 14, per_subject=1, consume=False):
        # two subjects can be one story (Nvidia and Huang): skip a lead that shares two names with one already listed
        if not any(len(group[0]["names"] & g[0]["names"]) >= 2 for g in big):
            big.append(group)
    big = big[:6]
    if big:
        L.extend(["", "The most widely reported stories of the week included:"])
        for group in big:
            merged = {"items": [i for c in group for i in c["items"]]}
            L.append(f"1. {md_escape(group[0]['title'])} {cites(merged)}")

    foreign = [x for g in ("europe", "asia", "americas", "mea") for x in group_rows(markets, g)]
    if foreign:
        ranked = sorted(foreign, key=lambda x: -x["week"])
        ups = sum(1 for x in foreign if x["week"] > 0)
        section(f"Outside the US, {ups} of {len(foreign)} major indices rose in local-currency terms. "
                f"{ranked[0]['name']} led ({pct(ranked[0]['week'])}) and {ranked[-1]['name']} lagged ({pct(ranked[-1]['week'])}).")
        for g in ("europe", "asia", "americas", "mea"):
            rows = group_rows(markets, g)
            if rows:
                L.append(f"1. **{REGION_LABELS[g]}:** {joined(rows)}.")
        world = group_rows(markets, "global")
        if world:
            L.append(f"1. **In US-dollar terms:** {joined(world)}.")
            ytd = [x for x in world if x["ytd"] is not None]
            if ytd:
                L.append(f"   1. Year to date: {joined(ytd, 'ytd')}." + (f" The S&P 500 is {pct(m('^GSPC')['ytd'])}." if m("^GSPC") and m("^GSPC")["ytd"] is not None else ""))

    sectors = group_rows(markets, "sector")
    if sectors:
        section(f"Under the surface in the US: {sectors[0]['name']} led the sectors ({pct(sectors[0]['week'])}) and "
                f"{sectors[-1]['name']} lagged ({pct(sectors[-1]['week'])}).")
        L.append(f"1. All eleven sectors: {joined(sectors)}.")
        if m("^RUT") and m("^GSPC"):
            gap = m("^RUT")["week"] - m("^GSPC")["week"]
            L.append(f"1. Small caps {'beat' if gap > 0 else 'trailed'} large caps by {abs(gap):.2f} points (Russell 2000 {pct(m('^RUT')['week'])} vs S&P 500 {pct(m('^GSPC')['week'])}).")
        if m("RPV") and m("RPG"):
            L.append(f"1. Pure Value {pct(m('RPV')['week'])} vs Pure Growth {pct(m('RPG')['week'])} for the week.")
        stock_movers = [r for r in extras["asset_class_movers"] if r.get("class_id") == "stocks" and r.get("horizon") == "5d"
                        and not r.get("asset_id", "").startswith("^")]
        if stock_movers:
            as_of = stock_movers[0].get("last_bar_date") or stock_movers[0].get("date", "")
            for side, label in (("top", "Largest gains"), ("bottom", "Largest losses")):
                rows = sorted((r for r in stock_movers if r["side"] == side), key=lambda r: int(r["rank"]))[:6]
                L.append(f"1. {label} among about 100 large US stocks over the five sessions to {as_of}: "
                         + "; ".join(f"{r['asset_name']} {pct(num(r['change_pct']))}" for r in rows) + ".")

    # ---- Part 2 ---------------------------------------------------------------------------------
    local_themes = {"economy", "earnings", "deals", None}
    regional_themes = local_themes | {"central"}  # a regional central bank story belongs with its region
    theme_ids = [t for t, (_, part, _) in THEMES.items() if part == "themes"]
    theme_blocks = []
    for tid in theme_ids:
        picked = take(lambda c, tid=tid: c["theme"] == tid and not (c["region"] and tid in local_themes), 6)
        if picked:
            theme_blocks.append((sum(len({s for c in g for s in c["sources"]}) for g in picked[:3]), tid, picked))
    if theme_blocks:
        L.extend(["", "## Part 2: The Week's Stories"])
        for _, tid, picked in sorted(theme_blocks, key=lambda b: -b[0]):
            section(THEMES[tid][0])
            for group in picked:
                emit(group)

    # ---- Part 3 ---------------------------------------------------------------------------------
    L.extend(["", "## Part 3: Rates, Credit and Central Banks"])
    if curve:
        ten_d = curve.get("10-year", (None, None))[1] or 0.0
        section(f"Rates moved {'higher' if ten_d > 0 else 'lower' if ten_d < 0 else 'sideways'} this week.")
        L.append("1. Treasury par yields: " + "; ".join(
            f"{tenor} {y:.2f}%" + (f" ({d:+.0f} bps)" if d is not None else "") for tenor, (y, d) in curve.items()) + ".")
        if "2-year" in curve and "10-year" in curve:
            slope = (curve["10-year"][0] - curve["2-year"][0]) * 100
            d2, d10 = curve["2-year"][1], curve["10-year"][1]
            change = f", {'steeper' if d10 - d2 > 0 else 'flatter'} by {abs(d10 - d2):.0f} bps on the week" if d2 is not None and d10 is not None else ""
            L.append(f"1. The 2s10s curve is {slope:+.0f} bps{change}.")
    else:
        section("Rates")
    credit = group_rows(markets, "credit")
    if credit:
        L.append(f"1. Bond funds (price change): {joined(credit)}.")
        if m("HYG") and m("AGG"):
            gap = m("HYG")["week"] - m("AGG")["week"]
            L.append(f"   1. High yield {'outperformed' if gap > 0 else 'underperformed'} core bonds by {abs(gap):.2f} points, a rough read on credit appetite.")
    metrics = {r["metric_id"]: r for r in extras["asset_class_metrics"]}
    notes = []
    if num(metrics.get("fed_cuts_priced_12m", {}).get("value")) is not None:
        n = num(metrics["fed_cuts_priced_12m"]["value"])
        notes.append(f"Fed funds futures price {abs(n):.1f} quarter-point {'cuts' if n > 0 else 'hikes'} over the next 12 months")
    if num(metrics.get("sofr_swap_implied_2y_pct", {}).get("value")) is not None:
        notes.append(f"the 2-year swap rate implied by SOFR futures is {num(metrics['sofr_swap_implied_2y_pct']['value']):.2f}%")
    if num(metrics.get("mortgage_30y_pct", {}).get("value")) is not None:
        notes.append(f"the average US 30-year mortgage rate is {num(metrics['mortgage_30y_pct']['value']):.2f}%")
    if notes:
        text = "; ".join(notes) + f" (as of {metrics[next(iter(metrics))]['date']})."
        L.append("1. " + text[0].upper() + text[1:])
    central = take(lambda c: c["theme"] == "central" and not c["region"], 7, per_subject=3)
    if central:
        section(THEMES["central"][0])
        for group in central:
            emit(group, subs=2)

    # ---- Part 4 ---------------------------------------------------------------------------------
    L.extend(["", "## Part 4: Commodities, Currencies and Crypto"])
    commodities = group_rows(markets, "commodities")
    if commodities and m("CL=F"):
        section(f"WTI crude {verb(m('CL=F')['week'])} {abs(m('CL=F')['week']):.2f}% for the week to ${m('CL=F')['level']:.2f}.")
        L.append(f"1. Commodity futures: {joined(commodities)}.")
        ytd = [x for x in commodities if x["ytd"] is not None]
        if ytd:
            L.append(f"   1. Year to date: {joined(sorted(ytd, key=lambda x: -x['ytd']), 'ytd')}.")
    else:
        section(THEMES["commodities"][0])
    for group in take(lambda c: c["theme"] == "commodities", 5):
        emit(group, subs=2)
    fx = group_rows(markets, "fx")
    if fx and m("DX-Y.NYB"):
        section(f"The dollar index {verb(m('DX-Y.NYB')['week'])} {abs(m('DX-Y.NYB')['week']):.2f}% to {m('DX-Y.NYB')['level']:.2f}.")
        L.append("1. Exchange rates: " + "; ".join(f"{x['name']} {level(x['level'])} ({pct(x['week'])})" for x in fx if x["ticker"] != "DX-Y.NYB") + ".")
        L.append("   1. A pair quoted as USD/XXX rising means the dollar strengthened against that currency.")
        for group in take(lambda c: c["theme"] == "fx", 4):
            emit(group, subs=2)
    crypto = group_rows(markets, "crypto")
    if crypto and m("BTC-USD"):
        section(f"Bitcoin {verb(m('BTC-USD')['week'])} {abs(m('BTC-USD')['week']):.2f}% for the week to ${m('BTC-USD')['level']:,.0f}.")
        L.append("1. " + "; ".join(f"{x['name']} ${level(x['level'])} ({pct(x['week'])} on the week, {pct(x['ytd'], 1)} year to date)" for x in crypto) + ".")
        for group in take(lambda c: c["theme"] == "crypto", 4):
            emit(group, subs=2)

    # ---- Part 5 ---------------------------------------------------------------------------------
    region_groups = {"europe": "europe", "asia": "asia", "latam": "americas", "mea": "mea"}
    region_blocks = []
    for rid, (label, _) in REGIONS.items():
        picked = take(lambda c, rid=rid: c["region"] == rid and c["theme"] in regional_themes, 6, per_subject=3, outlets=len(PRIORITY_SOURCES))
        wrap = latest_wrap(rid)
        if wrap:
            picked.insert(0, [wrap])
        rows = group_rows(markets, region_groups[rid])
        if rid == "latam":
            rows = [x for x in rows if "Canada" not in x["name"]]
        if picked or rows:
            region_blocks.append((label, rows, picked))
    if region_blocks:
        L.extend(["", "## Part 5: Around the World"])
        for label, rows, picked in region_blocks:
            section(f"{label}: {rows[0]['name']} {pct(rows[0]['week'])}; {rows[-1]['name']} {pct(rows[-1]['week'])}." if len(rows) > 1 else label)
            if rows:
                ytd = sorted((x for x in rows if x["ytd"] is not None), key=lambda x: -x["ytd"])
                if ytd:
                    L.append(f"1. Year to date: {joined(ytd, 'ytd')}.")
            for group in picked:
                emit(group, subs=2)

    # ---- Part 6 ---------------------------------------------------------------------------------
    other: List[str] = []
    housing = [r for r in extras["housing_prices"] if num(r.get("change_1y_pct")) is not None]
    if housing:
        def movers_of(market: str, levels: Tuple[str, ...]) -> Optional[str]:
            pool_ = sorted((r for r in housing if r["market"] == market and r["level"] in levels), key=lambda r: -num(r["change_1y_pct"]))
            if len(pool_) < 4:
                return None
            return (f"strongest {pool_[0]['region_name']} {pct(num(pool_[0]['change_1y_pct']), 1)} and {pool_[1]['region_name']} {pct(num(pool_[1]['change_1y_pct']), 1)}; "
                    f"weakest {pool_[-1]['region_name']} {pct(num(pool_[-1]['change_1y_pct']), 1)} and {pool_[-2]['region_name']} {pct(num(pool_[-2]['change_1y_pct']), 1)}")
        block = [f"### Home prices over one year (latest published month or quarter; collected {housing[0]['date']})"]
        for label, market, levels in (("US metros", "United States", ("metro",)), ("Brazil cities", "Brazil", ("city",)),
                                      ("Europe", "Europe", ("country",)), ("UK regions", "Europe", ("uk_region",)),
                                      ("World", "World", ("country",))):
            text = movers_of(market, levels)
            if text:
                block.append(f"1. **{label}:** {text}.")
        other.append("\n".join(block))
    drugs = [r for r in extras["drug_prices"] if num(r.get("reference_cost_usd")) is not None]
    if drugs:
        block = [f"### US drug prices (pharmacy acquisition cost or Medicare payment limit, not list or patient prices; collected {drugs[0]['date']})"]
        watch = [d for d in drugs if d["drug"] in ("Keytruda", "Wegovy", "Ozempic", "Mounjaro", "Zepbound", "Humira", "Eliquis")]
        block.append("1. " + "; ".join(f"{d['drug']} ${num(d['reference_cost_usd']):,.0f} ({pct(num(d['change_1y_pct']), 1)} over one year)" for d in watch) + ".")
        movers = sorted((d for d in drugs if num(d.get("change_1y_pct")) is not None), key=lambda d: num(d["change_1y_pct"]))
        if movers:
            block.append(f"1. Largest one-year changes: {movers[0]['drug']} {pct(num(movers[0]['change_1y_pct']), 1)}; {movers[-1]['drug']} {pct(num(movers[-1]['change_1y_pct']), 1)}.")
        other.append("\n".join(block))
    bags = sorted((r for r in extras["luxury_brands"] if r.get("store_id") == "all" and r.get("category") == "bags" and num(r.get("median_usd"))),
                  key=lambda r: -num(r["median_usd"]))
    if bags:
        block = [f"### Luxury resale asking prices for bags (median of live listings, not retail or sold prices; collected {bags[0]['date']})"]
        block.append("1. " + "; ".join(f"{r['brand']} ${num(r['median_usd']):,.0f}" for r in bags[:8]) + ".")
        shifts = sorted((r for r in extras["luxury_models"] if num(r.get("change_vs_prev_pct")) is not None), key=lambda r: -abs(num(r["change_vs_prev_pct"])))[:4]
        if shifts:
            block.append("1. Largest model moves since the previous run: " + "; ".join(f"{r['brand']} {r['model']} {pct(num(r['change_vs_prev_pct']), 1)}" for r in shifts) + ".")
        other.append("\n".join(block))
    if other:
        L.extend(["", "## Part 6: Other Prices We Track"])
        for block in other:
            head, *rest = block.split("\n")
            section(head[4:])
            L.extend(rest)

    L.extend(["", f"*About this update: compiled automatically on {generated.strftime('%B %d, %Y at %H:%M UTC')} from public market data "
              f"(week ending {week_end.strftime('%B %d, %Y')}) and public news feeds. Index moves are in local currency unless stated. "
              "News items are headlines as published by the cited outlets, grouped by story and ordered by how many outlets covered "
              "them; they are not analysis or verified by us. Nothing here is investment advice.*"])
    return L


# ---------------------------------------------------------------------------
# 4. Markdown -> blocks
# ---------------------------------------------------------------------------

INLINE_RE = re.compile(r"(\*\*[^*]+\*\*|\*[^*]+\*|\[[^\]]+\]\([^)\s]+\))")


def parse_inline(text: str) -> List[Dict[str, Any]]:
    """Runs of {text, bold, italic, url} from the small inline subset: **bold**, *italic*, [label](url)."""
    runs = []
    for part in INLINE_RE.split(text):
        if not part:
            continue
        if part.startswith("**") and part.endswith("**"):
            runs.append({"text": part[2:-2], "bold": True, "italic": False, "url": None})
        elif part.startswith("[") and part.endswith(")") and "](" in part:
            label, url = part[1:-1].split("](", 1)
            runs.append({"text": label, "bold": False, "italic": False, "url": url})
        elif part.startswith("*") and part.endswith("*") and len(part) > 2:
            runs.append({"text": part[1:-1], "bold": False, "italic": True, "url": None})
        else:
            runs.append({"text": part, "bold": False, "italic": False, "url": None})
    return runs


def parse_markdown(lines: List[str]) -> List[Dict[str, Any]]:
    """
    Blocks from the briefing's Markdown subset:
      `# ` document title · `## ` part · `### ` numbered section heading · paragraphs ·
      list items (`1. ` ordered or `- ` bullet), nested by three spaces of indentation per level.
    """
    blocks: List[Dict[str, Any]] = []
    for raw in lines:
        line = raw.rstrip()
        if not line.strip():
            continue
        item = re.match(r"^(\s*)(\d+\.|[-*•])\s+(.*)$", line)
        if line.startswith("### "):
            blocks.append({"type": "heading", "runs": parse_inline(line[4:])})
        elif line.startswith("## "):
            blocks.append({"type": "part", "runs": parse_inline(line[3:])})
        elif line.startswith("# "):
            blocks.append({"type": "title", "runs": parse_inline(line[2:])})
        elif item:
            blocks.append({"type": "item", "depth": min(len(item.group(1).replace("\t", "   ")) // 3, 4),
                           "ordered": item.group(2)[0].isdigit(), "runs": parse_inline(item.group(3))})
        else:
            blocks.append({"type": "para", "runs": parse_inline(line.strip())})
    return blocks


def roman(n: int) -> str:
    out = ""
    for value, numeral in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"), (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
                           (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        while n >= value:
            out += numeral
            n -= value
    return out


def list_label(depth: int, index: int) -> str:
    """Outline numbering by depth: 1. / a. / i. / 1. / a."""
    style = depth % 3
    if style == 0:
        return f"{index}."
    if style == 1:
        letters = ""
        n = index
        while n > 0:
            n, rem = divmod(n - 1, 26)
            letters = chr(97 + rem) + letters
        return f"{letters}."
    return f"{roman(index)}."


# ---------------------------------------------------------------------------
# 5. PDF writer (standard Helvetica fonts, WinAnsi encoding, no dependencies)
# ---------------------------------------------------------------------------

# Advance widths (1/1000 em) of the standard Helvetica faces for WinAnsi codes 32-255.
FONT_WIDTHS: Dict[str, List[int]] = {
    "regular": [
        278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
        500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
        556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
        278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350, 556, 350, 222, 556, 333, 1000, 556, 556, 333,
        1000, 667, 333, 1000, 350, 611, 350, 350, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350,
        500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333,
        333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722,
        667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722,
        667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556,
        556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500
    ],
    "bold": [
        278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278,
        556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584,
        556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556,
        333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350, 556, 350, 278, 556, 500, 1000, 556, 556, 333,
        1000, 667, 333, 1000, 350, 611, 350, 350, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 350,
        500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333,
        333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722, 1000, 722,
        667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722,
        667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611,
        611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556
    ],
    "italic": [
        278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556, 1015, 667, 667, 722, 722, 667, 611, 778, 722, 278,
        500, 667, 556, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469,
        556, 333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556, 556, 556, 333, 500,
        278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584, 350, 556, 350, 222, 556, 333, 1000, 556, 556, 333,
        1000, 667, 333, 1000, 350, 611, 350, 350, 222, 222, 333, 333, 350, 556, 1000, 333, 1000, 500, 333, 944, 350,
        500, 667, 278, 333, 556, 556, 556, 556, 260, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333,
        333, 333, 556, 537, 278, 333, 333, 365, 556, 834, 834, 834, 611, 667, 667, 667, 667, 667, 667, 1000, 722,
        667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722,
        667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 500, 556, 556, 556, 556, 278, 278, 278, 278, 556, 556,
        556, 556, 556, 556, 556, 584, 611, 556, 556, 556, 556, 500, 556, 500
    ],
    "bolditalic": [
        278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278, 556, 556, 556, 556, 556,
        556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611, 975, 722, 722, 722, 722, 667, 611, 778, 722, 278,
        556, 722, 611, 833, 722, 778, 667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584,
        556, 333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611, 611, 611, 389, 556,
        333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584, 350, 556, 350, 278, 556, 500, 1000, 556, 556, 333,
        1000, 667, 333, 1000, 350, 611, 350, 350, 278, 278, 500, 500, 350, 556, 1000, 333, 1000, 556, 333, 944, 350,
        500, 667, 278, 333, 556, 556, 556, 556, 280, 556, 333, 737, 370, 556, 584, 333, 737, 333, 400, 584, 333,
        333, 333, 611, 556, 278, 333, 333, 365, 556, 834, 834, 834, 611, 722, 722, 722, 722, 722, 722, 1000, 722,
        667, 667, 667, 667, 278, 278, 278, 278, 722, 722, 778, 778, 778, 778, 778, 584, 778, 722, 722, 722, 722,
        667, 667, 611, 556, 556, 556, 556, 556, 556, 889, 556, 556, 556, 556, 556, 278, 278, 278, 278, 611, 611,
        611, 611, 611, 611, 611, 584, 611, 611, 611, 611, 611, 556, 611, 556
    ],
}
FONT_NAMES = {"regular": ("F1", "Helvetica"), "bold": ("F2", "Helvetica-Bold"), "italic": ("F3", "Helvetica-Oblique"),
              "bolditalic": ("F4", "Helvetica-BoldOblique")}
REPLACEMENTS = {"→": "->", "←": "<-", "≥": ">=", "≤": "<=", "−": "-", " ": " ", "‑": "-",
                " ": " ", " ": " ", "​": "", "▲": "", "▼": "", "≈": "~", "﻿": ""}


def to_winansi(text: str) -> bytes:
    """Encode for the standard fonts; characters they lack are transliterated or dropped."""
    out = bytearray()
    for ch in text:
        ch = REPLACEMENTS.get(ch, ch)
        for c in ch:
            try:
                out += c.encode("cp1252")
            except UnicodeEncodeError:
                out += unicodedata.normalize("NFKD", c).encode("ascii", "ignore") or b"?"
    return bytes(out)


def text_width(data: bytes, font: str, size: float) -> float:
    widths = FONT_WIDTHS[font]
    return sum(widths[b - 32] for b in data if b >= 32) * size / 1000.0


def pdf_string(data: bytes) -> bytes:
    out = bytearray(b"(")
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out += b"\\" + bytes([b])
        elif 32 <= b < 127:
            out.append(b)
        else:
            out += b"\\%03o" % b
    return bytes(out + b")")


def pdf_text_string(text: str) -> bytes:
    """A document-information string: plain ASCII, or UTF-16 with a byte-order mark when it needs more."""
    try:
        return pdf_string(text.encode("ascii"))
    except UnicodeEncodeError:
        return pdf_string(b"\xfe\xff" + text.encode("utf-16-be"))


class PdfDocument:
    """A minimal multi-page PDF: text in four Helvetica faces, lines, colours and link annotations."""

    WIDTH, HEIGHT = 612.0, 792.0  # US Letter, points

    def __init__(self, title: str, author: str) -> None:
        self.title, self.author = title, author
        self.pages: List[Dict[str, Any]] = []

    def add_page(self) -> None:
        self.pages.append({"ops": [], "links": []})

    def text(self, x: float, y: float, data: bytes, font: str, size: float, color: Tuple[float, float, float]) -> None:
        r, g, b = color
        self.pages[-1]["ops"].append(
            b"BT /%s %.2f Tf %.3f %.3f %.3f rg %.2f %.2f Td %s Tj ET" % (FONT_NAMES[font][0].encode(), size, r, g, b, x, y, pdf_string(data)))

    def rule(self, x0: float, y: float, x1: float, color: Tuple[float, float, float], width: float = 0.6) -> None:
        r, g, b = color
        self.pages[-1]["ops"].append(b"%.3f %.3f %.3f RG %.2f w %.2f %.2f m %.2f %.2f l S" % (r, g, b, width, x0, y, x1, y))

    def link(self, x0: float, y0: float, x1: float, y1: float, url: str) -> None:
        self.pages[-1]["links"].append((x0, y0, x1, y1, url))

    def save(self, path: Path) -> None:
        objects: List[bytes] = []

        def add(body: bytes) -> int:
            objects.append(body)
            return len(objects)

        catalog, pages_id = add(b""), add(b"")
        fonts = {key: add(b"<< /Type /Font /Subtype /Type1 /BaseFont /%s /Encoding /WinAnsiEncoding >>" % base.encode())
                 for key, (_, base) in FONT_NAMES.items()}
        resources = b"<< /Font << " + b" ".join(b"/%s %d 0 R" % (FONT_NAMES[k][0].encode(), i) for k, i in fonts.items()) + b" >> >>"
        page_ids = []
        for page in self.pages:
            stream = zlib.compress(b"\n".join(page["ops"]))
            content = add(b"<< /Length %d /Filter /FlateDecode >>\nstream\n" % len(stream) + stream + b"\nendstream")
            annots = []
            for x0, y0, x1, y1, url in page["links"]:
                safe = urllib.parse.quote(url, safe=":/?#[]@!$&'*+,;=%~-._").encode("ascii")
                annots.append(add(b"<< /Type /Annot /Subtype /Link /Rect [%.2f %.2f %.2f %.2f] /Border [0 0 0] "
                                  b"/A << /S /URI /URI %s >> >>" % (x0, y0, x1, y1, pdf_string(safe))))
            page_ids.append(add(b"<< /Type /Page /Parent %d 0 R /MediaBox [0 0 %d %d] /Resources %s /Contents %d 0 R /Annots [%s] >>"
                                % (pages_id, self.WIDTH, self.HEIGHT, resources, content, b" ".join(b"%d 0 R" % a for a in annots))))
        objects[catalog - 1] = b"<< /Type /Catalog /Pages %d 0 R >>" % pages_id
        objects[pages_id - 1] = b"<< /Type /Pages /Count %d /Kids [%s] >>" % (len(page_ids), b" ".join(b"%d 0 R" % p for p in page_ids))
        stamp = dt.datetime.now(UTC_TZ).strftime("D:%Y%m%d%H%M%SZ").encode()
        info = add(b"<< /Title %s /Author %s /Creator (build_market_update.py) /CreationDate (%s) >>"
                   % (pdf_text_string(self.title), pdf_text_string(self.author), stamp))
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for number, body in enumerate(objects, start=1):
            offsets.append(len(out))
            out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
        xref = len(out)
        out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
        for offset in offsets:
            out += b"%010d 00000 n \n" % offset
        out += b"trailer\n<< /Size %d /Root %d 0 R /Info %d 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, catalog, info, xref)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bytes(out))


# ---------------------------------------------------------------------------
# 6. Layout
# ---------------------------------------------------------------------------

INK, MUTED, ACCENT, LINK = (0.10, 0.10, 0.12), (0.42, 0.44, 0.48), (0.05, 0.22, 0.45), (0.04, 0.33, 0.68)
MARGIN_X, TOP, BOTTOM = 58.0, 70.0, 54.0
BODY_SIZE, LEADING = 10.2, 13.6


def render_pdf(blocks: List[Dict[str, Any]], path: Path, site: str) -> int:
    """Typeset the blocks; returns the page count."""
    title_runs = next((b["runs"] for b in blocks if b["type"] == "title"), [{"text": "Market Update", "bold": False, "italic": False, "url": None}])
    title = "".join(r["text"] for r in title_runs)
    doc = PdfDocument(title, site)
    right = PdfDocument.WIDTH - MARGIN_X
    state = {"y": 0.0}

    def new_page() -> None:
        doc.add_page()
        number = str(len(doc.pages)).encode()
        top = PdfDocument.HEIGHT - 40
        doc.text(MARGIN_X, top, to_winansi(site), "regular", 9, MUTED)
        doc.link(MARGIN_X, top - 2, MARGIN_X + text_width(to_winansi(site), "regular", 9), top + 8, f"https://{site}")
        doc.text(right - text_width(number, "regular", 9), top, number, "regular", 9, MUTED)
        doc.rule(MARGIN_X, top - 6, right, (0.80, 0.82, 0.85))
        state["y"] = PdfDocument.HEIGHT - TOP

    def ensure(space: float) -> None:
        if state["y"] - space < BOTTOM:
            new_page()

    def font_of(run: Dict[str, Any], base_bold: bool, base_italic: bool) -> str:
        bold, italic = base_bold or run["bold"], base_italic or run["italic"]
        return "bolditalic" if bold and italic else "bold" if bold else "italic" if italic else "regular"

    def flow(runs: List[Dict[str, Any]], x: float, width: float, size: float, leading: float, color: Tuple[float, float, float],
             bold: bool = False, italic: bool = False, first_prefix: Optional[Tuple[float, bytes]] = None) -> None:
        """Word-wrap styled runs into the column starting at x; `first_prefix` draws a list label on line one."""
        words: List[Tuple[bytes, str, Optional[str], bool]] = []  # (text, font, url, space before)
        for run in runs:
            font = font_of(run, bold, italic)
            pieces = re.split(r"(\s+)", run["text"])
            gap = False
            for piece in pieces:
                if not piece:
                    continue
                if piece.isspace():
                    gap = True
                    continue
                words.append((to_winansi(piece), font, run["url"], gap or (bool(words) and run["text"].startswith(piece) is False and False)))
                gap = False
            if run["text"][-1:].isspace() and words:
                words.append((b"", font, None, True))  # carry a trailing space to the next run
        lines: List[List[Tuple[bytes, str, Optional[str], float]]] = [[]]
        cursor, pending_space = 0.0, False
        for data, font, url, space_before in words:
            if not data:
                pending_space = True
                continue
            space = text_width(b" ", font, size) if (space_before or pending_space) and lines[-1] else 0.0
            pending_space = False
            w = text_width(data, font, size)
            if lines[-1] and cursor + space + w > width:
                lines.append([])
                cursor, space = 0.0, 0.0
            lines[-1].append((data, font, url, cursor + space))
            cursor += space + w
        for i, line in enumerate(lines):
            ensure(leading)
            state["y"] -= leading
            if i == 0 and first_prefix:
                doc.text(first_prefix[0], state["y"], first_prefix[1], "bold" if bold else "regular", size, color)
            for data, font, url, offset in line:
                doc.text(x + offset, state["y"], data, font, size, LINK if url else color)
                if url:
                    doc.link(x + offset, state["y"] - 2, x + offset + text_width(data, font, size), state["y"] + size * 0.8, url)

    new_page()
    # masthead
    state["y"] -= 26
    doc.text(MARGIN_X, state["y"], b"MARKET UPDATE", "bold", 27, ACCENT)
    state["y"] -= 17
    sub = to_winansi(f"subscribe at {site}")
    doc.text(MARGIN_X, state["y"], sub, "regular", 10.5, LINK)
    doc.link(MARGIN_X, state["y"] - 2, MARGIN_X + text_width(sub, "regular", 10.5), state["y"] + 9, f"https://{site}")
    state["y"] -= 10
    doc.rule(MARGIN_X, state["y"], right, ACCENT, 1.2)
    state["y"] -= 6
    flow(title_runs, MARGIN_X, right - MARGIN_X, 14.5, 19, INK, bold=True)
    state["y"] -= 4

    counters: Dict[int, int] = {}
    for index, block in enumerate(blocks):
        kind = block["type"]
        if kind == "title":
            continue
        if kind != "item":
            counters.clear()
        if kind == "part":
            ensure(LEADING * 5)
            state["y"] -= 12
            flow(block["runs"], MARGIN_X, right - MARGIN_X, 13, 16, ACCENT, bold=True)
            state["y"] -= 3
            doc.rule(MARGIN_X, state["y"], right, (0.80, 0.82, 0.85))
            state["y"] -= 2
        elif kind == "heading":
            ensure(LEADING * 4)
            state["y"] -= 9
            flow(block["runs"], MARGIN_X, right - MARGIN_X, 11, 14.4, INK, bold=True)
            state["y"] -= 2
        elif kind == "para":
            only_italic = all(r["italic"] for r in block["runs"])
            state["y"] -= 12 if only_italic else 4
            flow(block["runs"], MARGIN_X, right - MARGIN_X, 8.6 if only_italic else BODY_SIZE, 11.4 if only_italic else LEADING,
                 MUTED if only_italic else INK)
            state["y"] -= 1
        else:
            depth = block["depth"]
            for deeper in [d for d in counters if d > depth]:
                del counters[deeper]
            counters[depth] = counters.get(depth, 0) + 1
            label = list_label(depth, counters[depth]) if block["ordered"] else "•"
            label_x = MARGIN_X + 10 + depth * 20
            text_x = label_x + 20
            state["y"] -= 1.5
            flow(block["runs"], text_x, right - text_x, BODY_SIZE, LEADING, INK, first_prefix=(label_x, to_winansi(label)))
    doc.save(path)
    return len(doc.pages)


# ---------------------------------------------------------------------------
# 7. Command line
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(description='Build the weekly "Market Update" briefing as Markdown and PDF.')
    parser.add_argument("--week-ending", metavar="YYYY-MM-DD", help="Friday that ends the week covered (default: the last completed week)")
    parser.add_argument("--site", default=DEFAULT_SITE, help=f"Site shown in the masthead, page header and subscribe line (default: {DEFAULT_SITE})")
    parser.add_argument("--out-dir", default="reports", help="Directory for the .md and .pdf files (default: reports)")
    parser.add_argument("--data-dir", default="data", help="Directory with the other watchers' CSV files (default: data)")
    parser.add_argument("--no-news", action="store_true", help="Skip the news feeds; market data only")
    parser.add_argument("--from-markdown", metavar="FILE", help="Render an existing (edited) Markdown briefing to PDF without fetching anything")
    parser.add_argument("--dry-run", action="store_true", help="Print the Markdown and write nothing")
    args = parser.parse_args()

    if args.from_markdown:
        source = Path(args.from_markdown)
        if not source.exists():
            print(f"[ERR] No such file: {source}", file=sys.stderr)
            return 1
        pdf_path = Path(args.out_dir) / (source.stem + ".pdf")
        pages = render_pdf(parse_markdown(source.read_text(encoding="utf-8").splitlines()), pdf_path, args.site)
        print(f"[OK] Rendered {source} -> {pdf_path} ({pages} pages)")
        return 0

    today = dt.datetime.now(UTC_TZ).date()
    try:
        week_end = dt.date.fromisoformat(args.week_ending) if args.week_ending else last_completed_week(today)
    except ValueError:
        parser.error("--week-ending must be a date like 2026-11-13")
    markets = collect_markets(week_end)
    if not markets:
        print("[ERR] No market data could be fetched for that week.", file=sys.stderr)
        return 1
    curve = collect_treasury_curve(week_end)
    clusters = [] if args.no_news else collect_news(week_end)
    lines = build_outline(markets, curve, clusters, read_extras(Path(args.data_dir)), week_end, dt.datetime.now(UTC_TZ))
    if args.dry_run:
        print("\n".join(lines))
        return 0

    issue = week_end + dt.timedelta(days=3)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    md_path = out_dir / f"market-update-{issue.isoformat()}.md"
    pdf_path = out_dir / f"market-update-{issue.isoformat()}.pdf"
    md_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    pages = render_pdf(parse_markdown(lines), pdf_path, args.site)
    stories = sum(1 for line in lines if "](http" in line)
    print(f"[OK] Week ending {week_end}: {len(markets)} instruments, {len(clusters)} story clusters, {stories} cited items.")
    print(f"[OK] Wrote {md_path}")
    print(f"[OK] Wrote {pdf_path} ({pages} pages)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
