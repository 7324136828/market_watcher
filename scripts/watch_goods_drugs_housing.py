#!/usr/bin/env python3
"""
watch_goods_drugs_housing.py — Real-world price watcher for `omni-market-watcher`.

Three sections, each from public, key-free sources:

  luxury   Asking prices for pre-owned luxury goods (Louis Vuitton, Chanel, Gucci, Hermès, Dior, Prada,
           Saint Laurent, Bottega Veneta, Fendi, Celine, Goyard, Balenciaga, Loewe, Burberry, Cartier,
           Van Cleef & Arpels, Tiffany, Rolex) from the public product feeds of resale stores
           (Fashionphile, Dallas Designer Handbags, Madison Avenue Couture), up to 1,000 of the newest
           listings per brand and store. Per brand and category: listing count, median and quartiles. Per iconic model (Neverfull MM, Classic Flap, Birkin 30,
           GG Marmont, Love bracelet, ...): the same, plus the cheapest live listing.
           These are resale ASKING prices, not boutique retail prices and not sold prices.

  drugs    US drug prices. Pharmacy drugs (Ozempic, Wegovy, Mounjaro, Zepbound, Eliquis, Humira, ...)
           from Medicaid's weekly NADAC survey: what retail pharmacies pay per unit, scaled to a
           reference pack. Physician-administered drugs (Keytruda, Opdivo, Eylea, ...) from the quarterly
           Medicare Part B payment-limit file (average sales price + 6%), scaled to a reference dose.
           Neither is the list price, the patient's price, or the net price after rebates.

  housing  Home prices by region. United States: Zillow typical home value and rent for the nation,
           every state and the 50 largest metros. Brazil: FipeZap asking price per m², rent and rental
           yield for 56 cities, plus the central bank's IVG-R index. Europe: Eurostat house price index
           by country and UK Land Registry average prices by region. Other economies: BIS residential
           property price indices (nominal and real) for about 60 countries.
           Values stay in local currency; indices are not comparable in level across countries.

Usage:
  python3 scripts/watch_goods_drugs_housing.py --dry-run
  python3 scripts/watch_goods_drugs_housing.py --write-csv --write-report reports/goods-drugs-housing-YYYY-MM-DD.md
  python3 scripts/watch_goods_drugs_housing.py --section drugs housing --dry-run
  python3 scripts/watch_goods_drugs_housing.py --section luxury --luxury-pages 4 --dry-run
  python3 scripts/watch_goods_drugs_housing.py --dry-run --json
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import io
import json
import math
import re
import statistics
import sys
import threading
import time
import unicodedata
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from zoneinfo import ZoneInfo

NY_TZ = ZoneInfo("America/New_York")
UTC_TZ = dt.timezone.utc
USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)
SECTIONS = ["luxury", "drugs", "housing"]
MIN_COMPS = 3  # claude.md §7.4: fewer than 3 observations is never priced
LOW_SAMPLE = "LOW_LIQUIDITY_UNPRICED"


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def http_get_bytes(url: str, timeout: int = 60, retries: int = 4) -> bytes:
    last_exc: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT, "Accept": "*/*"})
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return resp.read()
        except urllib.error.HTTPError as exc:
            last_exc = exc
            if exc.code not in (429, 500, 502, 503, 504):
                raise
            if exc.code == 429:  # rate limited: wait as long as the server asks, within reason
                wait = to_float(exc.headers.get("Retry-After")) if exc.headers else None
                time.sleep(min(wait if wait is not None else 10.0 * (attempt + 1), 30.0))
                continue
        except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
            last_exc = exc
        time.sleep(2.0 * (attempt + 1))
    raise last_exc if last_exc else RuntimeError(f"GET failed: {url}")


def http_get(url: str, timeout: int = 60, encoding: str = "utf-8") -> str:
    return http_get_bytes(url, timeout).decode(encoding, errors="replace")


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


def pct_change(new: Optional[float], old: Optional[float]) -> Optional[float]:
    if new is None or old is None or old == 0:
        return None
    return (new / old - 1.0) * 100.0


def percentile(sorted_vals: List[float], q: float) -> float:
    """Linear-interpolated percentile of an already sorted list (q in 0..1)."""
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    pos = q * (len(sorted_vals) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (pos - lo)


def normalise(text: str) -> str:
    """Lower-case, accent-free, alphanumeric tokens separated by single spaces (padded with spaces)."""
    plain = unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode("ascii").lower()
    return " " + re.sub(r"[^a-z0-9]+", " ", plain).strip() + " "


def slug(text: str) -> str:
    return normalise(text).strip().replace(" ", "-")


def money(val: Optional[float], symbol: str = "$", decimals: int = 0) -> str:
    return "—" if val is None else f"{symbol}{val:,.{decimals}f}"


def signed(val: Optional[float], decimals: int = 1, suffix: str = "%") -> str:
    return "—" if val is None else f"{val:+,.{decimals}f}{suffix}"


# ---------------------------------------------------------------------------
# 1. Luxury goods — resale asking prices
# ---------------------------------------------------------------------------

# Brand -> collection handle on each store's public Shopify product feed.
LUXURY_STORES: List[Dict[str, Any]] = [
    {
        "id": "fashionphile", "name": "Fashionphile", "base": "https://www.fashionphile.com",
        "handles": {
            "Louis Vuitton": "louis-vuitton", "Chanel": "chanel", "Gucci": "gucci", "Hermès": "hermes",
            "Christian Dior": "christian-dior", "Prada": "prada", "Saint Laurent": "saint-laurent",
            "Bottega Veneta": "bottega-veneta", "Fendi": "fendi", "Celine": "celine", "Goyard": "goyard",
            "Balenciaga": "balenciaga", "Loewe": "loewe", "Burberry": "burberry", "Cartier": "cartier",
            "Van Cleef & Arpels": "van-cleef-arpels", "Tiffany": "tiffany", "Rolex": "rolex",
        },
    },
    {
        "id": "ddh", "name": "Dallas Designer Handbags", "base": "https://www.dallasdesignerhandbags.com",
        "handles": {
            "Louis Vuitton": "louis-vuitton", "Chanel": "chanel", "Gucci": "gucci", "Hermès": "hermes",
            "Christian Dior": "christian-dior", "Prada": "prada", "Saint Laurent": "yves-saint-laurent",
            "Celine": "celine", "Goyard": "goyard",
        },
    },
    {
        "id": "mac", "name": "Madison Avenue Couture", "base": "https://www.madisonavenuecouture.com",
        "handles": {"Hermès": "hermes"},
    },
]

# (brand, model, keyword alternatives, excluded words, category or None for any)
# A listing matches when every keyword of at least one alternative appears as a whole word in its title.
LUXURY_MODELS: List[Tuple[str, str, List[List[str]], List[str], Optional[str]]] = [
    ("Louis Vuitton", "Neverfull MM", [["neverfull", "mm"]], ["pochette", "pouch"], "bags"),
    ("Louis Vuitton", "Speedy 25", [["speedy", "25"]], [], "bags"),
    ("Louis Vuitton", "Speedy 30", [["speedy", "30"]], [], "bags"),
    ("Louis Vuitton", "Alma BB", [["alma", "bb"]], [], "bags"),
    ("Louis Vuitton", "Pochette Métis", [["pochette", "metis"]], [], "bags"),
    ("Louis Vuitton", "OnTheGo MM", [["onthego", "mm"], ["on the go", "mm"]], [], "bags"),
    ("Louis Vuitton", "Keepall 55", [["keepall", "55"]], [], "bags"),
    ("Chanel", "Classic Medium Double Flap", [["medium", "double", "flap"]], [], "bags"),
    ("Chanel", "Classic Jumbo Double Flap", [["jumbo", "double", "flap"]], [], "bags"),
    ("Chanel", "Chanel 19 Flap", [["chanel 19"], ["19", "flap"]], [], "bags"),
    ("Chanel", "Boy Flap", [["boy", "flap"]], [], "bags"),
    ("Chanel", "Wallet on Chain", [["wallet on chain"], ["woc"]], [], None),
    ("Chanel", "Chanel 22", [["chanel 22"], ["22", "hobo"]], [], "bags"),
    ("Chanel", "Mini Rectangular Flap", [["mini", "rectangular", "flap"]], [], "bags"),
    ("Gucci", "GG Marmont", [["marmont"]], ["wallet", "belt", "card"], "bags"),
    ("Gucci", "Dionysus", [["dionysus"]], ["wallet", "card"], "bags"),
    ("Gucci", "Jackie", [["jackie"]], [], "bags"),
    ("Gucci", "Horsebit 1955", [["horsebit", "1955"]], [], "bags"),
    ("Gucci", "Soho Disco", [["soho", "disco"]], [], "bags"),
    ("Gucci", "Ophidia", [["ophidia"]], ["wallet", "card", "belt"], "bags"),
    ("Hermès", "Birkin 25", [["birkin", "25"]], [], "bags"),
    ("Hermès", "Birkin 30", [["birkin", "30"]], [], "bags"),
    ("Hermès", "Birkin 35", [["birkin", "35"]], [], "bags"),
    ("Hermès", "Kelly 25", [["kelly", "25"]], ["wallet", "pochette", "cut", "danse"], "bags"),
    ("Hermès", "Kelly 28", [["kelly", "28"]], ["wallet", "pochette", "cut", "danse"], "bags"),
    ("Hermès", "Constance", [["constance"]], ["wallet", "belt"], "bags"),
    ("Hermès", "Evelyne", [["evelyne"]], [], "bags"),
    ("Hermès", "Picotin", [["picotin"]], [], "bags"),
    ("Hermès", "Lindy", [["lindy"]], [], "bags"),
    ("Christian Dior", "Lady Dior", [["lady dior"]], ["wallet", "card"], "bags"),
    ("Christian Dior", "Book Tote", [["book tote"]], [], "bags"),
    ("Christian Dior", "Saddle", [["saddle"]], ["wallet", "card", "belt"], "bags"),
    ("Prada", "Re-Edition", [["re edition"]], [], "bags"),
    ("Prada", "Galleria", [["galleria"]], [], "bags"),
    ("Saint Laurent", "Loulou", [["loulou"]], [], "bags"),
    ("Saint Laurent", "Kate", [["kate"]], [], "bags"),
    ("Saint Laurent", "Sac de Jour", [["sac de jour"]], [], "bags"),
    ("Bottega Veneta", "Jodie", [["jodie"]], [], "bags"),
    ("Bottega Veneta", "Cassette", [["cassette"]], [], "bags"),
    ("Fendi", "Baguette", [["baguette"]], ["wallet", "card", "charm"], "bags"),
    ("Fendi", "Peekaboo", [["peekaboo"]], [], "bags"),
    ("Celine", "Luggage", [["luggage"]], [], "bags"),
    ("Celine", "Triomphe", [["triomphe"]], ["wallet", "card", "belt"], "bags"),
    ("Goyard", "Saint Louis", [["saint louis"], ["st louis"]], [], "bags"),
    ("Goyard", "Artois", [["artois"]], [], "bags"),
    ("Balenciaga", "City", [["city"]], [], "bags"),
    ("Balenciaga", "Le Cagole", [["cagole"]], [], "bags"),
    ("Balenciaga", "Hourglass", [["hourglass"]], ["wallet", "card"], "bags"),
    ("Loewe", "Puzzle", [["puzzle"]], [], "bags"),
    ("Cartier", "Love Bracelet", [["love", "bracelet"]], [], "jewelry"),
    ("Cartier", "Juste un Clou Bracelet", [["juste un clou", "bracelet"]], [], "jewelry"),
    ("Cartier", "Tank Watch", [["tank"]], [], "watches"),
    ("Cartier", "Santos Watch", [["santos"]], [], "watches"),
    ("Van Cleef & Arpels", "Vintage Alhambra", [["vintage alhambra"]], [], "jewelry"),
    ("Rolex", "Datejust", [["datejust"]], [], "watches"),
    ("Rolex", "Submariner", [["submariner"]], [], "watches"),
    ("Rolex", "Daytona", [["daytona"]], [], "watches"),
]


def listing_category(product_type: str) -> str:
    kind = (product_type or "").lower()
    if "bag" in kind:
        return "bags"
    if "jewel" in kind:
        return "jewelry"
    if "watch" in kind:
        return "watches"
    return "other"


# One request at a time per store with a pause after each (about 30 a minute): the largest feed
# rate-limits anything faster, and a rate-limited run is both slow and incomplete.
STORE_GATES: Dict[str, threading.BoundedSemaphore] = {}
STORE_PAUSE_SECONDS = 1.2


def fetch_brand_listings(store: Dict[str, Any], brand: str, handle: str, max_pages: int) -> Dict[str, Any]:
    """All available listings of one brand on one store (stops at the first short page)."""
    listings: Dict[Any, Dict[str, Any]] = {}
    truncated = False
    incomplete = False
    gate = STORE_GATES.setdefault(store["id"], threading.BoundedSemaphore(1))
    for page in range(1, max_pages + 1):
        url = f"{store['base']}/collections/{handle}/products.json?limit=250&page={page}"
        try:
            with gate:
                body = http_get_bytes(url, retries=4)
                time.sleep(STORE_PAUSE_SECONDS)
            products = json.loads(body.decode("utf-8", errors="replace")).get("products", [])
        except Exception as exc:
            warn(f"{store['name']} / {brand} page {page}: {exc}")
            incomplete = True
            break
        for p in products:
            prices = [to_float(v.get("price")) for v in p.get("variants", []) if v.get("available")]
            prices = [x for x in prices if x and x > 0]
            if not prices:
                continue
            listings[p["id"]] = {
                "store_id": store["id"], "brand": brand, "title": p.get("title", ""),
                "norm": normalise(p.get("title", "")), "category": listing_category(p.get("product_type", "")),
                "price": min(prices), "url": f"{store['base']}/products/{p.get('handle', '')}",
            }
        if len(products) < 250:
            break
        truncated = page == max_pages
    return {"store_id": store["id"], "brand": brand, "listings": list(listings.values()), "truncated": truncated,
            "incomplete": incomplete}


def price_stats(prices: List[float]) -> Dict[str, Any]:
    """Median and spread; nothing is priced on fewer than MIN_COMPS observations."""
    n = len(prices)
    if n < MIN_COMPS:
        return {"n_listings": n, "median_usd": None, "p25_usd": None, "p75_usd": None,
                "min_usd": min(prices) if prices else None, "max_usd": max(prices) if prices else None}
    s = sorted(prices)
    return {"n_listings": n, "median_usd": statistics.median(s), "p25_usd": percentile(s, 0.25),
            "p75_usd": percentile(s, 0.75), "min_usd": s[0], "max_usd": s[-1]}


def model_matches(listing: Dict[str, Any], alternatives: List[List[str]], excluded: List[str], category: Optional[str]) -> bool:
    if category and listing["category"] != category:
        return False
    title = listing["norm"]
    if any(f" {w} " in title for w in excluded):
        return False
    return any(all(f" {kw} " in title for kw in alt) for alt in alternatives)


def watch_luxury(max_pages: int) -> Dict[str, Any]:
    jobs = [(store, brand, handle) for store in LUXURY_STORES for brand, handle in store["handles"].items()]
    with ThreadPoolExecutor(max_workers=6) as pool:  # per-store gates do the real pacing
        fetched = list(pool.map(lambda j: fetch_brand_listings(j[0], j[1], j[2], max_pages), jobs))
    store_names = {s["id"]: s["name"] for s in LUXURY_STORES}
    listings = [item for f in fetched for item in f["listings"]]
    truncated = sorted({f"{store_names[f['store_id']]} / {f['brand']}" for f in fetched if f["truncated"]})
    incomplete = sorted({f"{store_names[f['store_id']]} / {f['brand']}" for f in fetched if f["incomplete"]})

    brand_rows: List[Dict[str, Any]] = []
    brands = list(dict.fromkeys(b for s in LUXURY_STORES for b in s["handles"]))
    for brand in brands:
        for store_id in ["all"] + [s["id"] for s in LUXURY_STORES]:
            pool_ = [x for x in listings if x["brand"] == brand and (store_id == "all" or x["store_id"] == store_id)]
            if not pool_:
                continue
            for category in ("all", "bags", "jewelry", "watches", "other"):
                prices = [x["price"] for x in pool_ if category == "all" or x["category"] == category]
                if len(prices) < MIN_COMPS:
                    continue
                brand_rows.append({
                    "store_id": store_id, "store_name": "All stores" if store_id == "all" else store_names[store_id],
                    "brand": brand, "category": category, **price_stats(prices),
                    "source": "resale store product feeds (asking prices)",
                })

    model_rows: List[Dict[str, Any]] = []
    for brand, model, alternatives, excluded, category in LUXURY_MODELS:
        hits = [x for x in listings if x["brand"] == brand and model_matches(x, alternatives, excluded, category)]
        stats = price_stats([x["price"] for x in hits])
        cheapest = min(hits, key=lambda x: x["price"]) if hits else None
        per_store: Dict[str, int] = {}
        for x in hits:
            per_store[x["store_id"]] = per_store.get(x["store_id"], 0) + 1
        model_rows.append({
            "brand": brand, "model": model, "model_id": slug(f"{brand} {model}"), "category": category or "any", **stats,
            "cheapest_title": cheapest["title"] if cheapest else "", "cheapest_url": cheapest["url"] if cheapest else "",
            "stores": ";".join(f"{k}:{v}" for k, v in sorted(per_store.items())),
            "status": "PRICED" if stats["median_usd"] is not None else LOW_SAMPLE,
        })
    return {"brands": brand_rows, "models": model_rows, "n_listings": len(listings), "truncated": truncated,
            "incomplete": incomplete, "stores": [s["name"] for s in LUXURY_STORES]}


# ---------------------------------------------------------------------------
# 2. Drugs — NADAC (pharmacy) and Medicare Part B payment limits (physician-administered)
# ---------------------------------------------------------------------------

# (brand, generic, maker, area, exact NADAC description, pricing units in the reference pack, pack label)
RETAIL_DRUGS: List[Tuple[str, str, str, str, str, float, str]] = [
    ("Ozempic", "semaglutide", "Novo Nordisk", "Diabetes (GLP-1)", "OZEMPIC 1 MG/DOSE (4 MG/3 ML) PEN", 3, "1 pen, 4 weeks"),
    ("Wegovy", "semaglutide", "Novo Nordisk", "Obesity (GLP-1)", "WEGOVY 2.4 MG/0.75 ML PEN", 3, "4 pens, 4 weeks"),
    ("Rybelsus", "semaglutide (oral)", "Novo Nordisk", "Diabetes (GLP-1)", "RYBELSUS 14 MG TABLET", 30, "30 tablets"),
    ("Mounjaro", "tirzepatide", "Eli Lilly", "Diabetes (GIP/GLP-1)", "MOUNJARO 5 MG/0.5 ML PEN", 2, "4 pens, 4 weeks"),
    ("Zepbound", "tirzepatide", "Eli Lilly", "Obesity (GIP/GLP-1)", "ZEPBOUND 5 MG/0.5 ML PEN", 2, "4 pens, 4 weeks"),
    ("Trulicity", "dulaglutide", "Eli Lilly", "Diabetes (GLP-1)", "TRULICITY 1.5 MG/0.5 ML PEN", 2, "4 pens, 4 weeks"),
    ("Jardiance", "empagliflozin", "Boehringer Ingelheim / Eli Lilly", "Diabetes, heart failure", "JARDIANCE 10 MG TABLET", 30, "30 tablets"),
    ("Farxiga", "dapagliflozin", "AstraZeneca", "Diabetes, heart failure", "FARXIGA 10 MG TABLET", 30, "30 tablets"),
    ("Januvia", "sitagliptin", "Merck", "Diabetes", "JANUVIA 100 MG TABLET", 30, "30 tablets"),
    ("Lantus SoloStar", "insulin glargine", "Sanofi", "Diabetes (insulin)", "LANTUS SOLOSTAR 100 UNIT/ML", 15, "5 pens (15 mL)"),
    ("Humalog KwikPen", "insulin lispro", "Eli Lilly", "Diabetes (insulin)", "HUMALOG 100 UNIT/ML KWIKPEN", 15, "5 pens (15 mL)"),
    ("Eliquis", "apixaban", "Bristol Myers Squibb / Pfizer", "Anticoagulant", "ELIQUIS 5 MG TABLET", 60, "60 tablets"),
    ("Xarelto", "rivaroxaban", "Johnson & Johnson / Bayer", "Anticoagulant", "XARELTO 20 MG TABLET", 30, "30 tablets"),
    ("Entresto", "sacubitril/valsartan", "Novartis", "Heart failure", "ENTRESTO 49 MG-51 MG TABLET", 60, "60 tablets"),
    ("Humira", "adalimumab", "AbbVie", "Immunology", "HUMIRA(CF) PEN 40 MG/0.4 ML", 2, "2 pens, 4 weeks"),
    ("Enbrel", "etanercept", "Amgen", "Immunology", "ENBREL 50 MG/ML SURECLICK", 4, "4 autoinjectors, 4 weeks"),
    ("Dupixent", "dupilumab", "Sanofi / Regeneron", "Immunology", "DUPIXENT 300 MG/2 ML PEN", 4, "2 pens, 4 weeks"),
    ("Stelara", "ustekinumab", "Johnson & Johnson", "Immunology", "STELARA 90 MG/ML SYRINGE", 1, "1 syringe (one dose)"),
    ("Skyrizi", "risankizumab", "AbbVie", "Immunology", "SKYRIZI 150 MG/ML PEN", 1, "1 pen (one dose)"),
    ("Biktarvy", "bictegravir/emtricitabine/TAF", "Gilead", "HIV", "BIKTARVY 50-200-25 MG TABLET", 30, "30 tablets"),
]

# (brand, generic, maker, area, HCPCS code, reference amount in the code's dosage unit, reference label)
PART_B_DRUGS: List[Tuple[str, str, str, str, str, float, str]] = [
    ("Keytruda", "pembrolizumab", "Merck", "Oncology", "J9271", 200, "200 mg dose, every 3 weeks"),
    ("Opdivo", "nivolumab", "Bristol Myers Squibb", "Oncology", "J9299", 480, "480 mg dose, every 4 weeks"),
    ("Tecentriq", "atezolizumab", "Roche", "Oncology", "J9022", 1200, "1,200 mg dose, every 3 weeks"),
    ("Imfinzi", "durvalumab", "AstraZeneca", "Oncology", "J9173", 1500, "1,500 mg dose, every 4 weeks"),
    ("Darzalex Faspro", "daratumumab + hyaluronidase", "Johnson & Johnson", "Oncology", "J9144", 1800, "1,800 mg dose"),
    ("Enhertu", "trastuzumab deruxtecan", "Daiichi Sankyo / AstraZeneca", "Oncology", "J9358", 100, "100 mg vial"),
    ("Eylea", "aflibercept", "Regeneron", "Ophthalmology", "J0178", 2, "2 mg injection"),
    ("Prolia", "denosumab", "Amgen", "Osteoporosis", "J0897", 60, "60 mg injection, every 6 months"),
    ("Ocrevus", "ocrelizumab", "Roche", "Multiple sclerosis", "J2350", 600, "600 mg infusion, every 6 months"),
    ("Entyvio", "vedolizumab", "Takeda", "Immunology", "J3380", 300, "300 mg infusion"),
    ("Botox", "onabotulinumtoxinA", "AbbVie", "Neurology", "J0585", 100, "100-unit vial"),
]


def nadac_dataset_ids() -> Dict[int, str]:
    """{year: dataset id} for the yearly NADAC datasets on data.medicaid.gov."""
    found: Dict[int, str] = {}
    data = json.loads(http_get("https://data.medicaid.gov/api/1/search?fulltext=NADAC&page-size=50"))
    for item in data.get("results", {}).values():
        m = re.match(r"NADAC \(National Average Drug Acquisition Cost\) (\d{4})$", item.get("title", ""))
        if m:
            found[int(m.group(1))] = item["identifier"]
    return found


def nadac_series(dataset_id: str, description: str) -> Dict[str, float]:
    """{as_of_date: mean NADAC per unit across the NDCs of one exact drug description}."""
    by_date: Dict[str, List[float]] = {}
    offset = 0
    while True:
        query = urllib.parse.urlencode({
            "conditions[0][property]": "ndc_description", "conditions[0][operator]": "=",
            "conditions[0][value]": description, "limit": 500, "offset": offset,
        })
        rows = json.loads(http_get(f"https://data.medicaid.gov/api/1/datastore/query/{dataset_id}/0?{query}")).get("results", [])
        for r in rows:
            price = to_float(r.get("nadac_per_unit"))
            if price is not None and r.get("as_of_date"):
                by_date.setdefault(str(r["as_of_date"])[:10], []).append(price)
        if len(rows) < 500:
            break
        offset += 500
    return {d: statistics.fmean(v) for d, v in by_date.items()}


def watch_retail_drugs() -> List[Dict[str, Any]]:
    try:
        datasets = nadac_dataset_ids()
    except Exception as exc:
        warn(f"NADAC dataset lookup failed: {exc}")
        return []
    if not datasets:
        return []
    year = max(datasets)
    wanted = [datasets[y] for y in (year - 1, year) if y in datasets]

    def one(drug: Tuple[str, str, str, str, str, float, str]) -> Optional[Dict[str, Any]]:
        brand, generic, maker, area, description, units, pack = drug
        series: Dict[str, float] = {}
        try:
            for ds in wanted:
                series.update(nadac_series(ds, description))
        except Exception as exc:
            warn(f"NADAC fetch failed for {brand}: {exc}")
        if not series:
            return None
        dates = sorted(series)
        latest = dates[-1]
        year_start = next((d for d in dates if d[:4] == latest[:4]), None)
        cutoff = (dt.date.fromisoformat(latest) - dt.timedelta(days=364)).isoformat()
        year_ago = [d for d in dates if d <= cutoff]
        unit_price = series[latest]
        return {
            "drug": brand, "generic": generic, "maker": maker, "therapeutic_area": area, "channel": "retail_pharmacy",
            "price_basis": "NADAC (pharmacy acquisition cost)", "reference": f"{description} — {pack}",
            "unit_price_usd": unit_price, "units_per_reference": units, "reference_cost_usd": unit_price * units,
            "as_of": latest,
            "change_prev_pct": pct_change(unit_price, series[dates[-2]]) if len(dates) > 1 else None,
            "change_ytd_pct": pct_change(unit_price, series[year_start]) if year_start else None,
            "change_1y_pct": pct_change(unit_price, series[year_ago[-1]]) if year_ago else None,
            "source": f"data.medicaid.gov NADAC {latest[:4]}",
            "note": "what pharmacies pay per unit; not list price, patient price or net price after rebates",
        }

    with ThreadPoolExecutor(max_workers=5) as pool:
        return [r for r in pool.map(one, RETAIL_DRUGS) if r]


def part_b_file_links() -> List[str]:
    """Quarterly Medicare Part B payment-limit zip files, newest first."""
    html = http_get("https://www.cms.gov/medicare/payment/part-b-drugs/asp-pricing-files")
    links = re.findall(r'href="(/files/zip/[^"]*\.zip)"', html)
    keep = [l for l in links if re.search(r"payment-limit-files|asp-pricing", l)]
    return ["https://www.cms.gov" + urllib.parse.quote(l) for l in dict.fromkeys(keep)]


def part_b_limits(url: str) -> Dict[str, Any]:
    """{'period': 'Effective ...', 'codes': {HCPCS: (dosage text, payment limit)}} from one quarterly zip."""
    archive = zipfile.ZipFile(io.BytesIO(http_get_bytes(url, timeout=90)))
    for name in archive.namelist():
        if not name.lower().endswith(".csv"):
            continue
        lines = archive.read(name).decode("latin-1").splitlines()
        header = next((i for i, l in enumerate(lines) if l.startswith("HCPCS Code,")), None)
        if header is None or "Payment Limit" not in lines[header]:
            continue
        period = next((l.strip('", ') for l in lines[:header] if l.strip('", ').lower().startswith("effective")), "")
        codes = {}
        for row in csv.DictReader(lines[header:]):
            limit = to_float(row.get("Payment Limit"))
            if row.get("HCPCS Code") and limit is not None:
                codes[row["HCPCS Code"].strip()] = (row.get("HCPCS Code Dosage", "").strip(), limit)
        if len(codes) > 100:  # the main payment-limit file, not the short "not payable" list
            return {"period": period, "codes": codes}
    return {"period": "", "codes": {}}


def watch_part_b_drugs() -> List[Dict[str, Any]]:
    try:
        links = part_b_file_links()
        files = [part_b_limits(u) for u in links[:1] + links[1:2] + links[4:5]]  # now, last quarter, a year ago
    except Exception as exc:
        warn(f"Medicare Part B payment-limit files failed: {exc}")
        return []
    current = files[0]
    previous = files[1] if len(files) > 1 else {"codes": {}}
    year_ago = files[2] if len(files) > 2 else {"codes": {}}
    m = re.search(r"Effective\s+(\w+ \d+, \d{4})", current["period"], re.I)
    rows = []
    for brand, generic, maker, area, code, amount, label in PART_B_DRUGS:
        if code not in current["codes"]:
            warn(f"Part B: HCPCS {code} ({brand}) not in the current file")
            continue
        dosage, limit = current["codes"][code]
        per_unit = to_float(dosage.split()[0]) or 1.0
        units = amount / per_unit
        rows.append({
            "drug": brand, "generic": generic, "maker": maker, "therapeutic_area": area,
            "channel": "physician_administered", "price_basis": "Medicare Part B payment limit (ASP + 6%)",
            "reference": f"HCPCS {code} per {dosage} — {label}", "unit_price_usd": limit,
            "units_per_reference": units, "reference_cost_usd": limit * units,
            "as_of": m.group(1) if m else current["period"],
            "change_prev_pct": pct_change(limit, previous["codes"].get(code, (None, None))[1]),
            "change_ytd_pct": None,
            "change_1y_pct": pct_change(limit, year_ago["codes"].get(code, (None, None))[1]),
            "source": "cms.gov Medicare Part B payment limit file",
            "note": "what Medicare pays the provider per billing unit; quarterly, based on sales two quarters earlier",
        })
    return rows


def watch_drugs() -> List[Dict[str, Any]]:
    return watch_retail_drugs() + watch_part_b_drugs()


# ---------------------------------------------------------------------------
# 3. Housing — US (Zillow), Brazil (FipeZap, BCB), Europe (Eurostat, UK HPI), world (BIS)
# ---------------------------------------------------------------------------

ZILLOW = "https://files.zillowstatic.com/research/public_csvs"
US_STATES = {
    "Alabama": "AL", "Alaska": "AK", "Arizona": "AZ", "Arkansas": "AR", "California": "CA", "Colorado": "CO",
    "Connecticut": "CT", "Delaware": "DE", "District of Columbia": "DC", "Florida": "FL", "Georgia": "GA",
    "Hawaii": "HI", "Idaho": "ID", "Illinois": "IL", "Indiana": "IN", "Iowa": "IA", "Kansas": "KS",
    "Kentucky": "KY", "Louisiana": "LA", "Maine": "ME", "Maryland": "MD", "Massachusetts": "MA", "Michigan": "MI",
    "Minnesota": "MN", "Mississippi": "MS", "Missouri": "MO", "Montana": "MT", "Nebraska": "NE", "Nevada": "NV",
    "New Hampshire": "NH", "New Jersey": "NJ", "New Mexico": "NM", "New York": "NY", "North Carolina": "NC",
    "North Dakota": "ND", "Ohio": "OH", "Oklahoma": "OK", "Oregon": "OR", "Pennsylvania": "PA",
    "Rhode Island": "RI", "South Carolina": "SC", "South Dakota": "SD", "Tennessee": "TN", "Texas": "TX",
    "Utah": "UT", "Vermont": "VT", "Virginia": "VA", "Washington": "WA", "West Virginia": "WV",
    "Wisconsin": "WI", "Wyoming": "WY",
}
BIS_COUNTRIES = {
    "US": ("United States", "North America"), "CA": ("Canada", "North America"), "MX": ("Mexico", "Latin America"),
    "BR": ("Brazil", "Latin America"), "CL": ("Chile", "Latin America"), "CO": ("Colombia", "Latin America"),
    "PE": ("Peru", "Latin America"), "GB": ("United Kingdom", "Europe"), "DE": ("Germany", "Europe"),
    "FR": ("France", "Europe"), "IT": ("Italy", "Europe"), "ES": ("Spain", "Europe"), "NL": ("Netherlands", "Europe"),
    "BE": ("Belgium", "Europe"), "AT": ("Austria", "Europe"), "CH": ("Switzerland", "Europe"), "SE": ("Sweden", "Europe"),
    "NO": ("Norway", "Europe"), "DK": ("Denmark", "Europe"), "FI": ("Finland", "Europe"), "IE": ("Ireland", "Europe"),
    "PT": ("Portugal", "Europe"), "GR": ("Greece", "Europe"), "PL": ("Poland", "Europe"), "CZ": ("Czechia", "Europe"),
    "HU": ("Hungary", "Europe"), "RO": ("Romania", "Europe"), "BG": ("Bulgaria", "Europe"), "HR": ("Croatia", "Europe"),
    "SI": ("Slovenia", "Europe"), "SK": ("Slovakia", "Europe"), "EE": ("Estonia", "Europe"), "LV": ("Latvia", "Europe"),
    "LT": ("Lithuania", "Europe"), "LU": ("Luxembourg", "Europe"), "MT": ("Malta", "Europe"), "CY": ("Cyprus", "Europe"),
    "IS": ("Iceland", "Europe"), "RS": ("Serbia", "Europe"), "MK": ("North Macedonia", "Europe"), "RU": ("Russia", "Europe"),
    "TR": ("Türkiye", "Middle East & Africa"), "IL": ("Israel", "Middle East & Africa"),
    "ZA": ("South Africa", "Middle East & Africa"), "MA": ("Morocco", "Middle East & Africa"),
    "AE": ("United Arab Emirates", "Middle East & Africa"), "SA": ("Saudi Arabia", "Middle East & Africa"),
    "CN": ("China", "Asia-Pacific"), "JP": ("Japan", "Asia-Pacific"), "KR": ("South Korea", "Asia-Pacific"),
    "IN": ("India", "Asia-Pacific"), "ID": ("Indonesia", "Asia-Pacific"), "MY": ("Malaysia", "Asia-Pacific"),
    "TH": ("Thailand", "Asia-Pacific"), "PH": ("Philippines", "Asia-Pacific"), "SG": ("Singapore", "Asia-Pacific"),
    "HK": ("Hong Kong", "Asia-Pacific"), "AU": ("Australia", "Asia-Pacific"), "NZ": ("New Zealand", "Asia-Pacific"),
    "XM": ("Euro area", "Aggregate"), "4T": ("Emerging markets (BIS aggregate)", "Aggregate"),
    "5R": ("Advanced economies (BIS aggregate)", "Aggregate"), "XW": ("World (BIS aggregate)", "Aggregate"),
}
UK_REGIONS = [
    ("united-kingdom", "United Kingdom", "country"), ("england", "England", "country"), ("wales", "Wales", "country"),
    ("scotland", "Scotland", "country"), ("northern-ireland", "Northern Ireland", "country"),
    ("london", "London", "region"), ("south-east", "South East", "region"), ("south-west", "South West", "region"),
    ("east-of-england", "East of England", "region"), ("east-midlands", "East Midlands", "region"),
    ("west-midlands", "West Midlands", "region"), ("north-west", "North West", "region"),
    ("north-east", "North East", "region"), ("yorkshire-and-the-humber", "Yorkshire and The Humber", "region"),
]
HISTORY_MONTHS = 60
HISTORY_QUARTERS = 24


def housing_row(**kw: Any) -> Dict[str, Any]:
    base = {k: None for k in (
        "market", "level", "region_id", "region_name", "parent", "series", "value", "unit", "period", "frequency",
        "change_prev_pct", "change_1y_pct", "change_5y_pct", "real_change_1y_pct", "rent_value", "rent_unit",
        "rent_change_1y_pct", "rental_yield_pct", "source")}
    base.update(kw)
    return base


def series_changes(values: List[Optional[float]], per_year: int) -> Tuple[Optional[float], Optional[float], Optional[float]]:
    """(change vs previous period, vs a year ago, vs five years ago) for a series ending at the latest value."""
    def back(k: int) -> Optional[float]:
        return pct_change(values[-1], values[-1 - k]) if len(values) > k else None
    return back(1), back(per_year), back(per_year * 5)


def read_zillow(path: str) -> Tuple[List[str], List[Dict[str, Any]]]:
    """(month columns, rows) of a Zillow research CSV; each row keeps its trailing run of values."""
    rows = list(csv.reader(http_get(f"{ZILLOW}/{path}", timeout=120).splitlines()))
    header = rows[0]
    first = next(i for i, h in enumerate(header) if re.match(r"\d{4}-\d{2}-\d{2}$", h))
    out = []
    for r in rows[1:]:
        meta = dict(zip(header[:first], r[:first]))
        meta["values"] = [to_float(v) for v in r[first:]]
        out.append(meta)
    return header[first:], out


def trimmed(months: List[str], values: List[Optional[float]]) -> Tuple[List[str], List[float]]:
    """Drop trailing gaps, then keep the contiguous run of numbers at the end."""
    end = len(values)
    while end and values[end - 1] is None:
        end -= 1
    start = end
    while start and values[start - 1] is not None:
        start -= 1
    return months[start:end], [v for v in values[start:end] if v is not None]


def housing_us(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        months, metros = read_zillow("zhvi/Metro_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv")
        _, states = read_zillow("zhvi/State_zhvi_uc_sfrcondo_tier_0.33_0.67_sm_sa_month.csv")
    except Exception as exc:
        warn(f"Zillow home values failed: {exc}")
        return rows
    rents: Dict[str, Tuple[List[str], List[float]]] = {}
    try:
        rent_months, rent_rows = read_zillow("zori/Metro_zori_uc_sfrcondomfr_sm_month.csv")
        rents = {r["RegionID"]: trimmed(rent_months, r["values"]) for r in rent_rows}
    except Exception as exc:
        warn(f"Zillow rents failed: {exc}")

    def add(meta: Dict[str, Any], level: str, region_id: str, name: str, parent: str) -> None:
        m, v = trimmed(months, meta["values"])
        if len(v) < 13:
            return
        prev, yoy, five = series_changes(v, 12)
        rent_m, rent_v = rents.get(meta.get("RegionID"), ([], []))
        rent = rent_v[-1] if rent_v else None
        rows.append(housing_row(
            market="United States", level=level, region_id=region_id, region_name=name, parent=parent,
            series="Typical home value (Zillow ZHVI, mid-tier, smoothed, seasonally adjusted)", value=v[-1], unit="USD",
            period=m[-1][:7], frequency="monthly", change_prev_pct=prev, change_1y_pct=yoy, change_5y_pct=five,
            rent_value=rent, rent_unit="USD per month" if rent else None,
            rent_change_1y_pct=pct_change(rent, rent_v[-13]) if len(rent_v) > 12 else None,
            rental_yield_pct=rent * 12 / v[-1] * 100.0 if rent else None,
            source="Zillow Research (ZHVI, ZORI)"))
        for month, value in list(zip(m, v))[-HISTORY_MONTHS:]:
            history.append({"region_id": region_id, "period": month[:7], "value": value})

    for meta in metros:
        if meta.get("RegionType") == "country":
            add(meta, "country", "us-national", "United States", "")
    for meta in sorted(states, key=lambda r: r.get("RegionName", "")):
        code = US_STATES.get(meta.get("RegionName", ""))
        if code:
            add(meta, "state", f"us-state-{code.lower()}", meta["RegionName"], code)
    top = sorted((m for m in metros if m.get("RegionType") == "msa"), key=lambda r: int(r.get("SizeRank") or 9999))[:50]
    for meta in top:
        add(meta, "metro", f"us-metro-{slug(meta['RegionName'])}", meta["RegionName"], meta.get("StateName", ""))
    return rows


XLSX_MAIN = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
XLSX_REL = "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}"


def read_xlsx_sheets(blob: bytes) -> Dict[str, List[Dict[str, str]]]:
    """Minimal .xlsx reader (standard library only): {sheet name: [ {column letters: text} per row ]}."""
    archive = zipfile.ZipFile(io.BytesIO(blob))
    strings = ["".join(t.text or "" for t in si.iter(XLSX_MAIN + "t"))
               for si in ET.fromstring(archive.read("xl/sharedStrings.xml"))]
    targets = {r.get("Id"): r.get("Target") for r in ET.fromstring(archive.read("xl/_rels/workbook.xml.rels"))}
    sheets: Dict[str, List[Dict[str, str]]] = {}
    for sheet in ET.fromstring(archive.read("xl/workbook.xml")).find(XLSX_MAIN + "sheets"):
        target = targets[sheet.get(XLSX_REL + "id")]
        path = target.lstrip("/") if target.startswith("/") else "xl/" + target
        rows = []
        for row in ET.fromstring(archive.read(path)).iter(XLSX_MAIN + "row"):
            cells = {}
            for c in row:
                v = c.find(XLSX_MAIN + "v")
                if v is not None and v.text is not None:
                    col = re.match(r"[A-Z]+", c.get("r")).group(0)
                    cells[col] = strings[int(v.text)] if c.get("t") == "s" else v.text
            rows.append(cells)
        sheets[sheet.get("name")] = rows
    return sheets


def housing_brazil(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    # FipeZap city sheets: B = Excel date, R = sale price R$/m2, H = monthly change, M = 12-month change,
    # AL = rent R$/m2, AG = rent 12-month change, AQ = monthly rental yield (fractions, not percent).
    try:
        sheets = read_xlsx_sheets(http_get_bytes(
            "https://downloads.fipe.org.br/indices/fipezap/fipezap-serieshistoricas.xlsx", timeout=180))
    except Exception as exc:
        warn(f"FipeZap workbook failed: {exc}")
        sheets = {}
    for name, sheet in sheets.items():
        if name in ("Resumo", "Aux"):
            continue
        obs = [(int(float(r["B"])), r) for r in sheet if to_float(r.get("B")) and to_float(r.get("R"))]
        if len(obs) < 13:
            continue
        obs.sort(key=lambda o: o[0])
        prices = [float(r["R"]) for _, r in obs]
        periods = [(dt.date(1899, 12, 30) + dt.timedelta(days=d)).strftime("%Y-%m") for d, _ in obs]
        last = obs[-1][1]
        composite = name == "Índice FipeZAP"
        region_id = "br-fipezap-composite" if composite else f"br-city-{slug(name)}"
        monthly_yield = to_float(last.get("AQ"))
        _, _, five = series_changes(prices, 12)
        rows.append(housing_row(
            market="Brazil", level="composite" if composite else "city", region_id=region_id,
            region_name="Brazil — FipeZap composite" if composite else name, parent="Brazil",
            series="Residential asking price per m² (FipeZap)", value=prices[-1], unit="BRL per m²",
            period=periods[-1], frequency="monthly",
            change_prev_pct=to_float(last.get("H")) * 100.0 if to_float(last.get("H")) is not None else pct_change(prices[-1], prices[-2]),
            change_1y_pct=to_float(last.get("M")) * 100.0 if to_float(last.get("M")) is not None else pct_change(prices[-1], prices[-13]),
            change_5y_pct=five, rent_value=to_float(last.get("AL")), rent_unit="BRL per m² per month",
            rent_change_1y_pct=to_float(last.get("AG")) * 100.0 if to_float(last.get("AG")) is not None else None,
            rental_yield_pct=monthly_yield * 12 * 100.0 if monthly_yield is not None else None,
            source="FIPE / ZAP (FipeZap historical series)"))
        for period, value in list(zip(periods, prices))[-HISTORY_MONTHS:]:
            history.append({"region_id": region_id, "period": period, "value": value})
    # Central bank IVG-R: collateral value of financed homes (appraisal based, national)
    try:
        start = (dt.date.today() - dt.timedelta(days=365 * 5 + 150)).strftime("%d/%m/%Y")  # "ultimos" caps at 20 points
        end = dt.date.today().strftime("%d/%m/%Y")
        data = json.loads(http_get("https://api.bcb.gov.br/dados/serie/bcdata.sgs.21340/dados"
                                   f"?formato=json&dataInicial={start}&dataFinal={end}"))[-(HISTORY_MONTHS + 1):]
        values = [float(d["valor"]) for d in data]
        periods = [f"{d['data'][6:10]}-{d['data'][3:5]}" for d in data]
        prev, yoy, five = series_changes(values, 12)
        rows.append(housing_row(
            market="Brazil", level="country", region_id="br-ivgr", region_name="Brazil — IVG-R (central bank)",
            parent="Brazil", series="Collateral value index of financed homes (IVG-R)", value=values[-1], unit="index",
            period=periods[-1], frequency="monthly", change_prev_pct=prev, change_1y_pct=yoy, change_5y_pct=five,
            source="Banco Central do Brasil SGS 21340"))
        for period, value in zip(periods, values):
            history.append({"region_id": "br-ivgr", "period": period, "value": value})
    except Exception as exc:
        warn(f"BCB IVG-R failed: {exc}")
    return rows


def housing_europe(history: List[Dict[str, Any]], today: dt.date) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    try:
        url = ("https://ec.europa.eu/eurostat/api/dissemination/statistics/1.0/data/prc_hpi_q"
               f"?format=JSON&purchase=TOTAL&unit=I15_Q&lastTimePeriod={HISTORY_QUARTERS + 1}")
        data = json.loads(http_get(url, timeout=90))
        geos = sorted(data["dimension"]["geo"]["category"]["index"].items(), key=lambda kv: kv[1])
        labels = data["dimension"]["geo"]["category"]["label"]
        times = [t for t, _ in sorted(data["dimension"]["time"]["category"]["index"].items(), key=lambda kv: kv[1])]
        n_time = len(times)
        fresh = set(times[-3:])  # a country whose series stopped earlier is stale, not a current price
        for code, gi in geos:
            pairs = [(times[ti], data["value"].get(str(gi * n_time + ti))) for ti in range(n_time)]
            periods, values = trimmed([p for p, _ in pairs], [v for _, v in pairs])
            if len(values) < 5 or periods[-1] not in fresh:
                continue
            prev, yoy, five = series_changes(values, 4)
            aggregate = code.startswith(("EU", "EA"))
            region_id = f"eu-{code.lower()}"
            rows.append(housing_row(
                market="Europe", level="aggregate" if aggregate else "country", region_id=region_id,
                region_name=labels.get(code, code).split(" (")[0] if aggregate else labels.get(code, code),
                parent="Europe", series="House price index (Eurostat, 2015 = 100)", value=values[-1], unit="index",
                period=periods[-1], frequency="quarterly", change_prev_pct=prev, change_1y_pct=yoy, change_5y_pct=five,
                source="Eurostat prc_hpi_q"))
            for period, value in zip(periods, values):
                history.append({"region_id": region_id, "period": period, "value": value})
    except Exception as exc:
        warn(f"Eurostat house price index failed: {exc}")

    start = (today - dt.timedelta(days=365 * 5 + 120)).replace(day=1).isoformat()

    def uk(region: Tuple[str, str, str]) -> Optional[Dict[str, Any]]:
        region_slug, name, level = region
        data: List[Dict[str, str]] = []
        failure: Optional[Exception] = None
        for kind in ("region", "country"):  # the registry files nations under either identifier
            location = urllib.parse.quote(f"http://landregistry.data.gov.uk/id/{kind}/{region_slug}", safe="")
            url = (f"https://landregistry.data.gov.uk/app/ukhpi/download/new.csv?from={start}&to={today.isoformat()}"
                   f"&location={location}&thm%5B%5D=property_type&in%5B%5D=avg&in%5B%5D=pac&lang=en")
            try:
                data = [r for r in csv.DictReader(http_get(url).splitlines()) if to_float(r.get("Average price All property types"))]
            except Exception as exc:
                failure = exc
                continue
            if len(data) >= 2:
                break
        if len(data) < 2:
            warn(f"UK HPI returned no data for {name}" + (f": {failure}" if failure else ""))
            return None
        values = [float(r["Average price All property types"]) for r in data]
        prev, yoy, five = series_changes(values, 12)
        published = to_float(data[-1].get("Percentage change (yearly) All property types"))
        return {"row": housing_row(
            market="Europe", level="uk_" + level, region_id=f"uk-{region_slug}", region_name=name,
            parent="United Kingdom", series="Average price, all property types (UK HPI)", value=values[-1], unit="GBP",
            period=data[-1]["Period"], frequency="monthly", change_prev_pct=prev,
            change_1y_pct=published if published is not None else yoy, change_5y_pct=five,
            source="HM Land Registry UK House Price Index"),
            "history": [{"region_id": f"uk-{region_slug}", "period": r["Period"], "value": v}
                        for r, v in list(zip(data, values))[-HISTORY_MONTHS:]]}

    with ThreadPoolExecutor(max_workers=5) as pool:
        for result in pool.map(uk, UK_REGIONS):
            if result:
                rows.append(result["row"])
                history.extend(result["history"])
    return rows


def housing_world(history: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    rows: List[Dict[str, Any]] = []
    base = "https://stats.bis.org/api/v2/data/dataflow/BIS/WS_SPP/1.0"
    try:
        nominal = list(csv.DictReader(http_get(f"{base}/Q..N.628?format=csv&lastNObservations={HISTORY_QUARTERS + 1}", timeout=120).splitlines()))
        real = list(csv.DictReader(http_get(f"{base}/Q..R.771?format=csv&lastNObservations=1", timeout=120).splitlines()))
    except Exception as exc:
        warn(f"BIS property prices failed: {exc}")
        return rows
    real_yoy = {r["REF_AREA"]: to_float(r["OBS_VALUE"]) for r in real}
    by_area: Dict[str, List[Tuple[str, float]]] = {}
    for r in nominal:
        value = to_float(r["OBS_VALUE"])
        if value is not None:
            by_area.setdefault(r["REF_AREA"], []).append((r["TIME_PERIOD"], value))
    for code, series in sorted(by_area.items()):
        series.sort()
        name, group = BIS_COUNTRIES.get(code, (code, "Other"))
        values = [v for _, v in series]
        if len(values) < 5:
            continue
        prev, yoy, five = series_changes(values, 4)
        region_id = f"bis-{code.lower()}"
        rows.append(housing_row(
            market="World", level="aggregate" if group == "Aggregate" else "country", region_id=region_id,
            region_name=name, parent=group, series="Residential property price index (BIS, nominal, 2010 = 100)",
            value=values[-1], unit="index", period=series[-1][0], frequency="quarterly", change_prev_pct=prev,
            change_1y_pct=yoy, change_5y_pct=five, real_change_1y_pct=real_yoy.get(code),
            source="BIS selected residential property prices (WS_SPP)"))
        for period, value in series:
            history.append({"region_id": region_id, "period": period, "value": value})
    return rows


def watch_housing(today: dt.date) -> Dict[str, Any]:
    history: List[Dict[str, Any]] = []
    rows = housing_us(history) + housing_brazil(history) + housing_europe(history, today) + housing_world(history)
    return {"rows": rows, "history": history}


# ---------------------------------------------------------------------------
# Alerts
# ---------------------------------------------------------------------------

def build_alerts(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    alerts: List[Dict[str, Any]] = []

    def emit(severity: str, asset_id: str, section: str, signal: str, value: Optional[float], threshold: str, details: str) -> None:
        alerts.append({"severity": severity, "asset_id": asset_id, "section": section, "signal_type": signal,
                       "value": value, "threshold": threshold, "details": details})

    for m in result.get("luxury", {}).get("models", []):
        change = m.get("change_vs_prev_pct")
        if change is not None and abs(change) >= 10:
            emit("medium", m["model_id"], "luxury", "luxury_model_price_shift", change, "|median change| >= 10% vs previous run",
                 f"{m['brand']} {m['model']}: median asking price {signed(change)} since {m.get('prev_date')} "
                 f"to {money(m['median_usd'])} ({m['n_listings']} listings).")
    for d in result.get("drugs", []):
        for key, label, limit in (("change_prev_pct", "since the previous file", 3.0), ("change_1y_pct", "over one year", 8.0)):
            change = d.get(key)
            if change is not None and abs(change) >= limit:
                emit("medium" if key == "change_prev_pct" else "info", slug(d["drug"]), "drugs",
                     "drug_price_change" if key == "change_prev_pct" else "drug_price_change_1y", change,
                     f"|change| >= {limit:g}% {label}",
                     f"{d['drug']} ({d['price_basis']}): {signed(change)} {label}; reference cost {money(d['reference_cost_usd'])}.")
    seen_regions: set = set()
    for h in result.get("housing", {}).get("rows", []):
        yoy = h.get("change_1y_pct")
        if yoy is None or h["level"] in ("aggregate", "composite") or h["region_name"] in seen_regions:
            continue  # a country covered by both Eurostat and BIS alerts once
        seen_regions.add(h["region_name"])
        if yoy <= -5 or yoy >= 15:
            emit("info", h["region_id"], "housing", "housing_price_fall" if yoy < 0 else "housing_price_surge", yoy,
                 "1y change <= -5% or >= +15%", f"{h['region_name']} ({h['market']}): {signed(yoy)} over one year ({h['period']}).")
    order = {"high": 0, "medium": 1, "info": 2}
    alerts.sort(key=lambda a: (order[a["severity"]], a["section"], -abs(a["value"] or 0)))
    return alerts


# ---------------------------------------------------------------------------
# Orchestration + CSV output
# ---------------------------------------------------------------------------

LUXURY_BRAND_COLS = ["date", "timestamp_utc", "store_id", "store_name", "brand", "category", "n_listings", "median_usd",
                     "p25_usd", "p75_usd", "min_usd", "max_usd", "prev_date", "change_vs_prev_pct", "source"]
LUXURY_MODEL_COLS = ["date", "timestamp_utc", "model_id", "brand", "model", "category", "n_listings", "median_usd",
                     "p25_usd", "p75_usd", "min_usd", "max_usd", "prev_date", "change_vs_prev_pct", "cheapest_title",
                     "cheapest_url", "stores", "status"]
DRUG_COLS = ["date", "timestamp_utc", "drug", "generic", "maker", "therapeutic_area", "channel", "price_basis",
             "reference", "unit_price_usd", "units_per_reference", "reference_cost_usd", "as_of", "change_prev_pct",
             "change_ytd_pct", "change_1y_pct", "source", "note"]
HOUSING_COLS = ["date", "timestamp_utc", "market", "level", "region_id", "region_name", "parent", "series", "value",
                "unit", "period", "frequency", "change_prev_pct", "change_1y_pct", "change_5y_pct", "real_change_1y_pct",
                "rent_value", "rent_unit", "rent_change_1y_pct", "rental_yield_pct", "source"]
HISTORY_COLS = ["region_id", "period", "value"]
ALERT_COLS = ["date", "timestamp_utc", "severity", "asset_id", "section", "signal_type", "value", "threshold", "details"]


def cell(val: Any) -> str:
    if val is None:
        return ""
    if isinstance(val, float):
        return f"{val:.6g}" if abs(val) < 1e6 else f"{val:.2f}"
    return str(val)


def read_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def upsert(path: Path, header: List[str], new_rows: List[Dict[str, Any]], run_date: str) -> None:
    """Replace this run date's rows; earlier dates are never edited."""
    kept = [r for r in read_csv(path) if r.get("date") != run_date]
    with path.open("w", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=header, extrasaction="ignore")
        writer.writeheader()
        for row in kept:
            writer.writerow({k: row.get(k, "") for k in header})
        for row in new_rows:
            writer.writerow({k: cell(row.get(k)) for k in header})


def attach_previous_run(rows: List[Dict[str, Any]], path: Path, key_fields: List[str], run_date: str) -> None:
    """Add `prev_date` / `change_vs_prev_pct` against the most recent earlier run date in the CSV."""
    earlier = [r for r in read_csv(path) if r.get("date", "") < run_date]
    if not earlier:
        return
    prev_date = max(r["date"] for r in earlier)
    lookup = {tuple(r.get(k, "") for k in key_fields): to_float(r.get("median_usd")) for r in earlier if r["date"] == prev_date}
    for row in rows:
        before = lookup.get(tuple(str(row.get(k, "")) for k in key_fields))
        if before is not None and row.get("median_usd") is not None:
            row["prev_date"] = prev_date
            row["change_vs_prev_pct"] = pct_change(row["median_usd"], before)


def run_watch(sections: List[str], luxury_pages: int, data_dir: Path) -> Dict[str, Any]:
    now_utc = dt.datetime.now(UTC_TZ)
    run_date = now_utc.astimezone(NY_TZ).strftime("%Y-%m-%d")
    result: Dict[str, Any] = {"run_date": run_date, "timestamp_utc": now_utc.strftime("%Y-%m-%dT%H:%M:%SZ"), "sections": sections}
    if "luxury" in sections:
        result["luxury"] = watch_luxury(luxury_pages)
        attach_previous_run(result["luxury"]["brands"], data_dir / "luxury_brands.csv", ["store_id", "brand", "category"], run_date)
        attach_previous_run(result["luxury"]["models"], data_dir / "luxury_models.csv", ["model_id"], run_date)
    if "drugs" in sections:
        result["drugs"] = watch_drugs()
    if "housing" in sections:
        result["housing"] = watch_housing(now_utc.date())
    result["alerts"] = build_alerts(result)
    return result


def write_csvs(result: Dict[str, Any], data_dir: Path) -> None:
    data_dir.mkdir(parents=True, exist_ok=True)
    stamp = {"date": result["run_date"], "timestamp_utc": result["timestamp_utc"]}
    run_date = result["run_date"]
    if "luxury" in result:
        upsert(data_dir / "luxury_brands.csv", LUXURY_BRAND_COLS, [{**stamp, **r} for r in result["luxury"]["brands"]], run_date)
        upsert(data_dir / "luxury_models.csv", LUXURY_MODEL_COLS, [{**stamp, **r} for r in result["luxury"]["models"]], run_date)
    if "drugs" in result:
        upsert(data_dir / "drug_prices.csv", DRUG_COLS, [{**stamp, **r} for r in result["drugs"]], run_date)
    if "housing" in result:
        upsert(data_dir / "housing_prices.csv", HOUSING_COLS, [{**stamp, **r} for r in result["housing"]["rows"]], run_date)
        with (data_dir / "housing_history.csv").open("w", newline="", encoding="utf-8") as fh:  # regenerated each run
            writer = csv.DictWriter(fh, fieldnames=HISTORY_COLS)
            writer.writeheader()
            for row in result["housing"]["history"]:
                writer.writerow({k: cell(row[k]) for k in HISTORY_COLS})
    if set(result["sections"]) == set(SECTIONS):  # alerts span sections, so only a full run replaces the day's set
        upsert(data_dir / "goods_drugs_housing_alerts.csv", ALERT_COLS, [{**stamp, **a} for a in result["alerts"]], run_date)


# ---------------------------------------------------------------------------
# Markdown brief + terminal output
# ---------------------------------------------------------------------------

UNIT_SYMBOL = {"USD": "$", "GBP": "£", "BRL per m²": "R$"}


def fmt_value(row: Dict[str, Any]) -> str:
    if row["unit"] == "index":
        return f"{row['value']:,.1f}"
    suffix = "/m²" if "per m²" in row["unit"] else ""
    return f"{UNIT_SYMBOL.get(row['unit'], '')}{row['value']:,.0f}{suffix}"


def movers(rows: List[Dict[str, Any]], key: str, n: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    ranked = sorted((r for r in rows if r.get(key) is not None), key=lambda r: -r[key])
    k = min(n, len(ranked) // 2)
    return ranked[:k], ranked[::-1][:k]


def render_markdown(result: Dict[str, Any]) -> str:
    L: List[str] = []
    stamp = result["timestamp_utc"].replace("T", " ").replace("Z", " UTC")
    L.append(f"## 🛍️ Goods, Drugs & Housing Price Brief — [{stamp}]")
    L.append("")
    L.append(f"> Run date `{result['run_date']}` (`America/New_York`). Generated by `scripts/watch_goods_drugs_housing.py`.")
    L.append("")
    if "luxury" in result:
        lux = result["luxury"]
        L.append("### 1. Luxury Goods — Resale Asking Prices")
        L.append(f"{lux['n_listings']:,} live listings from {', '.join(lux['stores'])}. Asking prices for pre-owned items in USD, "
                 "not boutique retail prices and not sold prices.")
        L.append("")
        L.append("| Brand | Bag listings | Median bag price | Middle half | All listings | Median, all categories |")
        L.append("| :--- | ---: | ---: | :--- | ---: | ---: |")
        everything = {r["brand"]: r for r in lux["brands"] if r["store_id"] == "all" and r["category"] == "all"}
        bags = {r["brand"]: r for r in lux["brands"] if r["store_id"] == "all" and r["category"] == "bags"}
        for brand, row in sorted(everything.items(), key=lambda kv: -(bags.get(kv[0], kv[1])["median_usd"] or 0)):
            b = bags.get(brand)
            L.append(f"| {brand} | {b['n_listings'] if b else '—'} | {money(b['median_usd']) if b else '—'} | "
                     f"{money(b['p25_usd']) + ' – ' + money(b['p75_usd']) if b else '—'} | {row['n_listings']} | {money(row['median_usd'])} |")
        L.append("")
        L.append("| Model | Listings | Median | Middle half | Lowest | Since previous run |")
        L.append("| :--- | ---: | ---: | :--- | ---: | ---: |")
        for m in lux["models"]:
            if m["status"] != "PRICED":
                L.append(f"| {m['brand']} {m['model']} | {m['n_listings']} | `{LOW_SAMPLE}` | — | {money(m['min_usd'])} | — |")
                continue
            L.append(f"| {m['brand']} {m['model']} | {m['n_listings']} | {money(m['median_usd'])} | {money(m['p25_usd'])} – {money(m['p75_usd'])} | "
                     f"[{money(m['min_usd'])}]({m['cheapest_url']}) | {signed(m.get('change_vs_prev_pct'))} |")
        if lux["truncated"]:
            L.append("")
            L.append(f"- Page cap reached (sample, not every listing) for: {', '.join(lux['truncated'])}.")
        if lux["incomplete"]:
            L.append("")
            L.append(f"- Feed error part-way through (figures rest on a partial listing set) for: {', '.join(lux['incomplete'])}.")
        L.append("")
    if "drugs" in result:
        L.append("### 2. Drug Prices — United States")
        for channel, title, prev_label in (
            ("retail_pharmacy", "Pharmacy drugs — NADAC, what pharmacies pay (weekly)", "1 week"),
            ("physician_administered", "Physician-administered drugs — Medicare Part B payment limit (quarterly)", "1 quarter"),
        ):
            rows = [d for d in result["drugs"] if d["channel"] == channel]
            if not rows:
                continue
            L.append("")
            L.append(f"**{title}**")
            L.append("")
            L.append(f"| Drug | Maker | Reference | Per unit | Reference cost | {prev_label} | 1 year | As of |")
            L.append("| :--- | :--- | :--- | ---: | ---: | ---: | ---: | :--- |")
            for d in sorted(rows, key=lambda d: -d["reference_cost_usd"]):
                L.append(f"| {d['drug']} ({d['generic']}) | {d['maker']} | {d['reference']} | {money(d['unit_price_usd'], decimals=2)} | "
                         f"{money(d['reference_cost_usd'])} | {signed(d['change_prev_pct'], 2)} | {signed(d['change_1y_pct'])} | {d['as_of']} |")
        L.append("")
        L.append("- Neither series is the list price, the patient's out-of-pocket price, or the net price after rebates.")
        L.append("")
    if "housing" in result:
        rows = result["housing"]["rows"]
        L.append("### 3. Home Prices by Region")
        for market, levels, label in (
            ("United States", ("metro",), "US metros (50 largest)"), ("United States", ("state",), "US states"),
            ("Brazil", ("city",), "Brazilian cities"), ("Europe", ("country",), "European countries"),
            ("Europe", ("uk_region", "uk_country"), "UK nations and regions"), ("World", ("country",), "World (BIS)"),
        ):
            pool_ = [r for r in rows if r["market"] == market and r["level"] in levels]
            top, bottom = movers(pool_, "change_1y_pct", 5)
            if not top:
                continue
            L.append("")
            L.append(f"**{label}** — {len(pool_)} regions, largest one-year rises and falls")
            L.append("")
            L.append("| Rising | Level | 1 year | Falling / weakest | Level | 1 year |")
            L.append("| :--- | ---: | ---: | :--- | ---: | ---: |")
            for a, b in zip(top, bottom):
                L.append(f"| {a['region_name']} | {fmt_value(a)} | {signed(a['change_1y_pct'])} | {b['region_name']} | {fmt_value(b)} | {signed(b['change_1y_pct'])} |")
        L.append("")
        L.append("- Values are in local currency. Index levels use different base years and are not comparable across sources.")
        L.append("")
    L.append("### 4. Alerts")
    L.append("")
    if result["alerts"]:
        L.append("| Severity | Section | Item | Signal | Details |")
        L.append("| :--- | :--- | :--- | :--- | :--- |")
        for a in result["alerts"]:
            L.append(f"| `{a['severity'].upper()}` | {a['section']} | `{a['asset_id']}` | `{a['signal_type']}` | {a['details']} |")
    else:
        L.append("No triggers fired.")
    L.append("")
    return "\n".join(L)


def print_terminal(result: Dict[str, Any]) -> None:
    bar = "=" * 116
    print(bar)
    print(f" GOODS, DRUGS & HOUSING PRICE WATCH — {result['run_date']} | {result['timestamp_utc']}")
    print(bar)
    if "luxury" in result:
        lux = result["luxury"]
        print(f"\n[Luxury resale asking prices — {lux['n_listings']:,} live listings, USD]")
        bags = sorted((r for r in lux["brands"] if r["store_id"] == "all" and r["category"] == "bags"), key=lambda r: -r["median_usd"])
        print(f"  {'Brand (bags)':<22}{'n':>6}{'median':>10}{'p25':>10}{'p75':>10}")
        for r in bags:
            print(f"  {r['brand']:<22}{r['n_listings']:>6}{money(r['median_usd']):>10}{money(r['p25_usd']):>10}{money(r['p75_usd']):>10}")
        print(f"\n  {'Model':<42}{'n':>5}{'median':>10}{'lowest':>10}  vs prev run")
        for m in lux["models"]:
            median = money(m["median_usd"]) if m["status"] == "PRICED" else "unpriced"
            print(f"  {(m['brand'] + ' ' + m['model'])[:41]:<42}{m['n_listings']:>5}{median:>10}{money(m['min_usd']):>10}  {signed(m.get('change_vs_prev_pct'))}")
        for label, items in (("Page cap reached", lux["truncated"]), ("Feed error, partial data", lux["incomplete"])):
            if items:
                print(f"  [{label}] {', '.join(items)}")
    if "drugs" in result:
        print("\n[US drug prices — reference pack or dose]")
        print(f"  {'Drug':<18}{'Basis':<12}{'Reference cost':>15}{'prev':>9}{'1y':>9}  as of")
        for d in sorted(result["drugs"], key=lambda d: (d["channel"], -d["reference_cost_usd"])):
            basis = "NADAC" if d["channel"] == "retail_pharmacy" else "Part B"
            print(f"  {d['drug']:<18}{basis:<12}{money(d['reference_cost_usd']):>15}{signed(d['change_prev_pct'], 2):>9}{signed(d['change_1y_pct']):>9}  {d['as_of']}")
    if "housing" in result:
        rows = result["housing"]["rows"]
        print(f"\n[Home prices — {len(rows)} regions]")
        for market, levels, label in (("United States", ("metro",), "US metros"), ("United States", ("state",), "US states"),
                                      ("Brazil", ("city",), "Brazil cities"), ("Europe", ("country",), "Europe"),
                                      ("Europe", ("uk_region", "uk_country"), "UK"), ("World", ("country",), "World (BIS)")):
            pool_ = [r for r in rows if r["market"] == market and r["level"] in levels]
            top, bottom = movers(pool_, "change_1y_pct", 3)
            if top:
                print(f"  {label:<14} ▲ " + "  ".join(f"{r['region_name']} {signed(r['change_1y_pct'])}" for r in top))
                print(f"  {'':<14} ▼ " + "  ".join(f"{r['region_name']} {signed(r['change_1y_pct'])}" for r in bottom))
    print(f"\n[Alerts — {len(result['alerts'])}]")
    for a in result["alerts"][:20]:
        print(f"  [{a['severity'].upper():<6}] {a['section']:<8} {a['details'][:140]}")
    if len(result["alerts"]) > 20:
        print(f"  … {len(result['alerts']) - 20} more")
    print(bar)


def main() -> int:
    parser = argparse.ArgumentParser(description="Watch luxury resale prices, US drug prices and home prices by region.")
    parser.add_argument("--dry-run", action="store_true", help="Fetch and print without writing files (default)")
    parser.add_argument("--write-csv", action="store_true", help="Update the luxury / drug / housing CSV files in data/")
    parser.add_argument("--write-report", metavar="PATH", help="Write a Markdown brief to PATH")
    parser.add_argument("--section", nargs="+", choices=SECTIONS, help="Subset of sections (default: all)")
    parser.add_argument("--luxury-pages", type=int, default=4, metavar="N",
                        help="Pages of 250 listings read per brand and store, newest first (default 4 = up to 1,000 listings; "
                             "more pages cover older listings but take longer and risk rate limiting)")
    parser.add_argument("--data-dir", default="data", help="Data directory (default: data)")
    parser.add_argument("--json", action="store_true", help="Print machine-readable JSON instead of the terminal view")
    args = parser.parse_args()
    if args.luxury_pages < 1:
        parser.error("--luxury-pages must be at least 1")

    sections = [s for s in SECTIONS if not args.section or s in args.section]
    result = run_watch(sections, args.luxury_pages, Path(args.data_dir))
    empty = [s for s in sections if not (result[s]["brands"] if s == "luxury" else result[s]["rows"] if s == "housing" else result[s])]
    for s in empty:
        warn(f"section `{s}` returned no data")
    if len(empty) == len(sections):
        print("[ERR] No data could be fetched.", file=sys.stderr)
        return 1

    if args.write_csv:
        write_csvs(result, Path(args.data_dir))
        print(f"[OK] Updated luxury / drug / housing CSV files in `{args.data_dir}/` for run_date={result['run_date']}.")
    if args.write_report:
        path = Path(args.write_report)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(render_markdown(result), encoding="utf-8")
        print(f"[OK] Wrote brief to `{path}`.")
    if args.json:
        printable = {k: v for k, v in result.items() if k != "housing"}
        if "housing" in result:
            printable["housing"] = result["housing"]["rows"]
        print(json.dumps(printable, indent=2, default=str))
    else:
        print_terminal(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
