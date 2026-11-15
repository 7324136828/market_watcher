#!/usr/bin/env python3
"""
visualize_goods_drugs_housing.py — Visualizer for `scripts/watch_goods_drugs_housing.py`.

Reads the CSV files the watcher writes into `data/`:
  luxury_brands.csv, luxury_models.csv, drug_prices.csv, housing_prices.csv, housing_history.csv,
  goods_drugs_housing_alerts.csv
and produces three views of the latest run of each section:

  1. Terminal dashboard (stdout).
  2. PNG overview (`reports/goods_drugs_housing_overview.png`, needs Pillow): nine panels — luxury brand
     medians and model prices, pharmacy and physician-administered drug costs, and one-year home-price
     movers for US metros, US states, Brazilian cities, Europe / UK and the world.
  3. Self-contained interactive HTML dashboard (`reports/goods_drugs_housing_dashboard.html`): brand
     and model explorer, drug tables, and a housing explorer with movers, a sortable table and a
     price-history chart per market.

A section whose CSV is missing or empty is simply left out.

Usage:
  python3 scripts/visualize_goods_drugs_housing.py
  python3 scripts/visualize_goods_drugs_housing.py --html reports/goods_drugs_housing_dashboard.html \
      --png reports/goods_drugs_housing_overview.png --open
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

# (view id, label, market, ranked levels, benchmark levels) — the housing explorer's views, in display order.
# Benchmarks (a national figure or an aggregate) are shown for context but never ranked as movers.
HOUSING_VIEWS = [
    ("us_metro", "US metros", "United States", ["metro"], ["country"]),
    ("us_state", "US states", "United States", ["state"], ["country"]),
    ("br_city", "Brazil cities", "Brazil", ["city"], ["composite", "country"]),
    ("eu_country", "Europe", "Europe", ["country"], ["aggregate"]),
    ("uk", "UK regions", "Europe", ["uk_region"], ["uk_country"]),
    ("world", "World", "World", ["country"], ["aggregate"]),
]
UNIT_SYMBOL = {"USD": "$", "GBP": "£", "BRL per m²": "R$"}


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with path.open(newline="", encoding="utf-8") as fh:
        return list(csv.DictReader(fh))


def to_float(val: Any) -> Optional[float]:
    try:
        return float(val) if val not in (None, "") else None
    except (TypeError, ValueError):
        return None


def latest(rows: List[Dict[str, str]]) -> Tuple[str, List[Dict[str, Any]]]:
    if not rows:
        return "", []
    date = max(r["date"] for r in rows)
    return date, [dict(r) for r in rows if r["date"] == date]


def numeric(rows: List[Dict[str, Any]], cols: List[str]) -> None:
    for r in rows:
        for c in cols:
            r[c] = to_float(r.get(c))


def load_datasets(data_dir: Path) -> Dict[str, Any]:
    brands_all = load_csv(data_dir / "luxury_brands.csv")
    models_all = load_csv(data_dir / "luxury_models.csv")
    drugs_all = load_csv(data_dir / "drug_prices.csv")
    housing_all = load_csv(data_dir / "housing_prices.csv")

    lux_date, brands = latest(brands_all)
    _, models = latest(models_all)
    drug_date, drugs = latest(drugs_all)
    housing_date, housing = latest(housing_all)
    numeric(brands, ["n_listings", "median_usd", "p25_usd", "p75_usd", "min_usd", "max_usd", "change_vs_prev_pct"])
    numeric(models, ["n_listings", "median_usd", "p25_usd", "p75_usd", "min_usd", "max_usd", "change_vs_prev_pct"])
    numeric(drugs, ["unit_price_usd", "units_per_reference", "reference_cost_usd", "change_prev_pct", "change_ytd_pct", "change_1y_pct"])
    numeric(housing, ["value", "change_prev_pct", "change_1y_pct", "change_5y_pct", "real_change_1y_pct", "rent_value",
                      "rent_change_1y_pct", "rental_yield_pct"])

    # run-over-run histories (one point per run date)
    model_history: Dict[str, List[Tuple[str, float]]] = {}
    for r in models_all:
        v = to_float(r.get("median_usd"))
        if v is not None:
            model_history.setdefault(r["model_id"], []).append((r["date"], v))
    drug_history: Dict[str, List[Tuple[str, float]]] = {}
    for r in drugs_all:
        v = to_float(r.get("reference_cost_usd"))
        if v is not None:
            drug_history.setdefault(r["drug"], []).append((r["date"], v))

    wanted = {h["region_id"] for h in housing}
    housing_history: Dict[str, List[Tuple[str, float]]] = {}
    for r in load_csv(data_dir / "housing_history.csv"):
        v = to_float(r.get("value"))
        if v is not None and r["region_id"] in wanted:
            housing_history.setdefault(r["region_id"], []).append((r["period"], v))

    dates = [d for d in (lux_date, drug_date, housing_date) if d]
    alerts_all = load_csv(data_dir / "goods_drugs_housing_alerts.csv")
    alert_date = max((a["date"] for a in alerts_all), default="")
    alerts = [a for a in alerts_all if a["date"] == alert_date]
    return {
        "latest_date": max(dates) if dates else "",
        "luxury_date": lux_date, "drug_date": drug_date, "housing_date": housing_date,
        "brands": brands, "models": models, "drugs": drugs, "housing": housing,
        "model_history": {k: sorted(v) for k, v in model_history.items()},
        "drug_history": {k: sorted(v) for k, v in drug_history.items()},
        "housing_history": housing_history,
        "alerts": alerts,
    }


# ---------------------------------------------------------------------------
# Shared selections
# ---------------------------------------------------------------------------

def view_rows(ds: Dict[str, Any], view_id: str) -> List[Dict[str, Any]]:
    _, _, market, ranked, bench = next(v for v in HOUSING_VIEWS if v[0] == view_id)
    return [h for h in ds["housing"] if h["market"] == market and h["level"] in ranked + bench]


def housing_movers(ds: Dict[str, Any], view_id: str, n: int) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    levels = next(v for v in HOUSING_VIEWS if v[0] == view_id)[3]
    ranked = sorted((h for h in view_rows(ds, view_id) if h["change_1y_pct"] is not None and h["level"] in levels),
                    key=lambda h: -h["change_1y_pct"])
    k = min(n, len(ranked) // 2)
    return ranked[:k], ranked[::-1][:k]


def brand_bags(ds: Dict[str, Any]) -> List[Dict[str, Any]]:
    rows = [b for b in ds["brands"] if b["store_id"] == "all" and b["category"] == "bags" and b["median_usd"]]
    return sorted(rows, key=lambda b: -b["median_usd"])


def fmt_value(h: Dict[str, Any]) -> str:
    if h["unit"] == "index":
        return f"{h['value']:,.1f}"
    return f"{UNIT_SYMBOL.get(h['unit'], '')}{h['value']:,.0f}" + ("/m2" if "per m" in h["unit"] else "")


def money(v: Optional[float], d: int = 0) -> str:
    return "—" if v is None else f"${v:,.{d}f}"


def signed(v: Optional[float], d: int = 1) -> str:
    return "—" if v is None else f"{v:+.{d}f}%"


# ---------------------------------------------------------------------------
# 1. Terminal dashboard
# ---------------------------------------------------------------------------

def render_terminal(ds: Dict[str, Any]) -> None:
    bar = "=" * 118
    print(bar)
    print(f" GOODS, DRUGS & HOUSING PRICE DASHBOARD — luxury {ds['luxury_date'] or 'n/a'} · drugs {ds['drug_date'] or 'n/a'} · housing {ds['housing_date'] or 'n/a'}")
    print(bar)
    if ds["brands"]:
        print("\n[Luxury — median resale asking price for bags, all stores (USD)]")
        rows = brand_bags(ds)
        top = max(math.log10(r["median_usd"]) for r in rows)
        for r in rows:
            fill = int(max(math.log10(r["median_usd"]) - 2, 0) / max(top - 2, 0.1) * 30)
            print(f"  {r['brand']:<20} {'█' * fill:<30} {money(r['median_usd']):>9}  ({money(r['p25_usd'])} – {money(r['p75_usd'])}, {r['n_listings']:.0f} listings)")
        print("\n[Luxury — iconic models]")
        print(f"  {'Model':<40}{'listings':>9}{'median':>10}{'lowest':>10}{'vs prev run':>13}")
        for m in sorted((m for m in ds["models"] if m["median_usd"]), key=lambda m: -m["median_usd"]):
            print(f"  {(m['brand'] + ' ' + m['model'])[:39]:<40}{m['n_listings']:>9.0f}{money(m['median_usd']):>10}{money(m['min_usd']):>10}{signed(m['change_vs_prev_pct']):>13}")
        unpriced = [f"{m['brand']} {m['model']}" for m in ds["models"] if not m["median_usd"]]
        if unpriced:
            print(f"  Unpriced (fewer than 3 listings): {', '.join(unpriced)}")
    if ds["drugs"]:
        for channel, title in (("retail_pharmacy", "Pharmacy drugs — NADAC, what pharmacies pay"),
                               ("physician_administered", "Physician-administered drugs — Medicare Part B payment limit")):
            rows = sorted((d for d in ds["drugs"] if d["channel"] == channel), key=lambda d: -d["reference_cost_usd"])
            if rows:
                print(f"\n[Drugs — {title}]")
                print(f"  {'Drug':<18}{'Maker':<30}{'Reference cost':>15}{'prev':>9}{'1y':>9}  reference")
                for d in rows:
                    print(f"  {d['drug']:<18}{d['maker'][:29]:<30}{money(d['reference_cost_usd']):>15}{signed(d['change_prev_pct'], 2):>9}"
                          f"{signed(d['change_1y_pct']):>9}  {d['reference'].split(' — ')[-1]}")
    if ds["housing"]:
        print("\n[Housing — one-year change, largest rises and falls per market]")
        for view_id, label, *_ in HOUSING_VIEWS:
            top, bottom = housing_movers(ds, view_id, 4)
            if not top:
                continue
            print(f"  {label:<14} ▲ " + "  ".join(f"{h['region_name']} {signed(h['change_1y_pct'])}" for h in top))
            print(f"  {'':<14} ▼ " + "  ".join(f"{h['region_name']} {signed(h['change_1y_pct'])}" for h in bottom))
        national = [h for h in ds["housing"] if h["region_id"] in ("us-national", "br-fipezap-composite", "uk-united-kingdom", "eu-eu27_2020", "bis-xw")]
        for h in national:
            print(f"  {h['region_name']:<34} {fmt_value(h):>12}  1y {signed(h['change_1y_pct'])}  ({h['period']})")
    print(f"\n[Alerts — {len(ds['alerts'])}]")
    for a in ds["alerts"][:20]:
        print(f"  [{a['severity'].upper():<6}] {a['section']:<8} {a['details'][:140]}")
    print(bar)


# ---------------------------------------------------------------------------
# 2. PNG overview
# ---------------------------------------------------------------------------

FONT_CANDIDATES = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
                "/System/Library/Fonts/Supplemental/Arial.ttf", "C:/Windows/Fonts/arial.ttf"],
}


def load_font(kind: str, size: int) -> Any:
    for path in FONT_CANDIDATES[kind]:
        try:
            return ImageFont.truetype(path, size)
        except OSError:
            continue
    try:
        return ImageFont.load_default(size)
    except TypeError:
        return ImageFont.load_default()


def render_png(ds: Dict[str, Any], out_path: Path) -> bool:
    if not HAS_PIL:
        return False
    W, H = 2400, 2130
    BG, CARD, BORDER = (13, 17, 23), (22, 27, 34), (48, 54, 61)
    TEXT, MUTED = (230, 237, 243), (139, 148, 158)
    GREEN, RED, BLUE = (63, 185, 80), (248, 81, 73), (88, 166, 255)
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_h2 = load_font("bold", 30), load_font("bold", 21)
    f_body, f_small, f_bold = load_font("regular", 16), load_font("regular", 14), load_font("bold", 16)

    def tw(s: str, font: Any) -> float:
        return d.textlength(s, font=font)

    def right(x: float, y: float, s: str, font: Any, fill: Tuple[int, int, int]) -> None:
        d.text((x - tw(s, font), y), s, font=font, fill=fill)

    def clip(s: str, font: Any, width: float) -> str:
        if tw(s, font) <= width:
            return s
        while s and tw(s + "...", font) > width:
            s = s[:-1]
        return s + "..."

    def panel(col: int, row: int, title: str, sub: str) -> Tuple[int, int, int, int]:
        cw, rh = (W - 48 - 40) // 3, (H - 124 - 24 - 40) // 3
        x0, y0 = 24 + col * (cw + 20), 124 + row * (rh + 20)
        d.rounded_rectangle([x0, y0, x0 + cw, y0 + rh], radius=10, fill=CARD, outline=BORDER, width=2)
        d.text((x0 + 20, y0 + 14), title, font=f_h2, fill=TEXT)
        d.text((x0 + 20, y0 + 44), sub, font=f_small, fill=MUTED)
        return x0 + 20, y0 + 76, x0 + cw - 20, y0 + rh - 16

    def empty(box: Tuple[int, int, int, int]) -> None:
        d.text((box[0], box[1]), "No data in this run.", font=f_body, fill=MUTED)

    def table(box: Tuple[int, int, int, int], header: List[Tuple[str, float, str]], rows: List[List[Tuple[str, Tuple[int, int, int]]]]) -> None:
        """header: (label, x fraction of the column's right or left edge, 'l' | 'r')."""
        x0, y0, x1, y1 = box
        if not rows:
            return empty(box)
        width = x1 - x0
        for label, frac, align in header:
            x = x0 + frac * width
            (right if align == "r" else d.text)(*((x, y0, label, f_small, MUTED) if align == "r" else ((x, y0), label)), **({} if align == "r" else {"font": f_small, "fill": MUTED}))
        d.line([x0, y0 + 22, x1, y0 + 22], fill=BORDER, width=1)
        rh = min((y1 - y0 - 28) / len(rows), 30)
        for i, row in enumerate(rows):
            y = y0 + 28 + i * rh
            for (label, frac, align), (text, color) in zip(header, row):
                x = x0 + frac * width
                if align == "r":
                    right(x, y, text, f_body, color)
                else:
                    d.text((x, y), text, font=f_body, fill=color)

    def movers_panel(box: Tuple[int, int, int, int], view_id: str, n: int) -> None:
        top, bottom = housing_movers(ds, view_id, n)
        items = top + bottom[::-1]
        if not items:
            return empty(box)
        x0, y0, x1, y1 = box
        rh = min((y1 - y0) / len(items), 34)
        label_w, value_w = 250, 190
        bx0, bx1 = x0 + label_w, x1 - value_w
        mid = (bx0 + bx1) / 2
        scale = max(abs(h["change_1y_pct"]) for h in items) or 1.0
        d.line([mid, y0, mid, y0 + rh * len(items)], fill=BORDER, width=2)
        for i, h in enumerate(items):
            y, v = y0 + i * rh, h["change_1y_pct"]
            d.text((x0, y + rh / 2 - 10), clip(h["region_name"], f_body, label_w - 10), font=f_body, fill=TEXT)
            length = abs(v) / scale * (bx1 - mid)
            d.rectangle([mid, y + 6, mid + length, y + rh - 6] if v >= 0 else [mid - length, y + 6, mid, y + rh - 6], fill=GREEN if v >= 0 else RED)
            right(x1, y + rh / 2 - 10, f"{v:+.1f}%", f_bold, TEXT)
            right(x1 - 84, y + rh / 2 - 9, fmt_value(h), f_small, MUTED)

    def change_color(v: Optional[float]) -> Tuple[int, int, int]:
        return MUTED if v is None or abs(v) < 0.05 else (GREEN if v > 0 else RED)

    d.rounded_rectangle([24, 20, W - 24, 104], radius=10, fill=CARD, outline=BORDER, width=2)
    d.text((46, 32), f"GOODS, DRUGS & HOUSING PRICE WATCH — {ds['latest_date']}", font=f_title, fill=TEXT)
    d.text((46, 72), f"luxury resale asking prices ({ds['luxury_date'] or 'n/a'})  |  US drug prices ({ds['drug_date'] or 'n/a'})  |  "
           f"home prices by region ({ds['housing_date'] or 'n/a'}, {len(ds['housing'])} regions)", font=f_small, fill=MUTED)

    # Row 1: luxury brands, luxury models, pharmacy drugs
    box = panel(0, 0, "Luxury: median resale asking price, bags", "All stores, USD. Bar length on a log scale; range is the middle half of listings")
    rows = brand_bags(ds)
    if rows:
        x0, y0, x1, y1 = box
        rh = min((y1 - y0) / len(rows), 40)
        lo, hi = 2.5, max(math.log10(r["median_usd"]) for r in rows)
        for i, r in enumerate(rows):
            y = y0 + i * rh
            d.text((x0, y + rh / 2 - 10), r["brand"], font=f_body, fill=TEXT)
            bx0, bx1 = x0 + 190, x1 - 300
            frac = max((math.log10(r["median_usd"]) - lo) / max(hi - lo, 0.1), 0.02)
            d.rectangle([bx0, y + 7, bx0 + (bx1 - bx0) * frac, y + rh - 7], fill=BLUE)
            right(x1 - 190, y + rh / 2 - 10, money(r["median_usd"]), f_bold, TEXT)
            right(x1, y + rh / 2 - 9, f"{money(r['p25_usd'])} - {money(r['p75_usd'])}", f_small, MUTED)
    else:
        empty(box)

    box = panel(1, 0, "Luxury: iconic models", "Median and lowest resale asking price (USD), by number of listings")
    models = sorted((m for m in ds["models"] if m["median_usd"]), key=lambda m: -m["n_listings"])[:18]
    table(box, [("Model", 0, "l"), ("Listings", 0.62, "r"), ("Median", 0.80, "r"), ("Lowest", 1.0, "r")],
          [[(clip(f"{m['brand']} {m['model']}", f_body, 330), TEXT), (f"{m['n_listings']:.0f}", MUTED), (money(m["median_usd"]), TEXT), (money(m["min_usd"]), MUTED)] for m in models])

    box = panel(2, 0, "Pharmacy drugs: reference pack cost", "NADAC: what US pharmacies pay, not list or patient price (USD)")
    retail = sorted((x for x in ds["drugs"] if x["channel"] == "retail_pharmacy"), key=lambda x: -x["reference_cost_usd"])[:18]
    table(box, [("Drug", 0, "l"), ("Pack", 0.24, "l"), ("Cost", 0.80, "r"), ("1 year", 1.0, "r")],
          [[(x["drug"], TEXT), (clip(x["reference"].split(" — ")[-1], f_body, 250), MUTED), (money(x["reference_cost_usd"]), TEXT),
            (signed(x["change_1y_pct"]), change_color(x["change_1y_pct"]))] for x in retail])

    # Row 2: Part B drugs, US metros, US states
    box = panel(0, 1, "Physician-administered drugs: cost per dose", "Medicare Part B payment limit, average sales price + 6% (USD)")
    part_b = sorted((x for x in ds["drugs"] if x["channel"] == "physician_administered"), key=lambda x: -x["reference_cost_usd"])
    table(box, [("Drug", 0, "l"), ("Reference", 0.26, "l"), ("Cost", 0.80, "r"), ("1 year", 1.0, "r")],
          [[(x["drug"], TEXT), (clip(x["reference"].split(" — ")[-1], f_body, 290), MUTED), (money(x["reference_cost_usd"]), TEXT),
            (signed(x["change_1y_pct"]), change_color(x["change_1y_pct"]))] for x in part_b])
    movers_panel(panel(1, 1, "US metros: home value, 1-year change", "Zillow typical home value, 50 largest metros; value shown in USD"), "us_metro", 8)
    movers_panel(panel(2, 1, "US states: home value, 1-year change", "Zillow typical home value; value shown in USD"), "us_state", 8)

    # Row 3: Brazil, Europe + UK, world
    movers_panel(panel(0, 2, "Brazil cities: price per m2, 1-year change", "FipeZap residential asking price, R$ per m2"), "br_city", 8)
    box = panel(1, 2, "Europe and UK: 1-year change", "Eurostat house price index by country (top) and UK average price by region (bottom)")
    x0, y0, x1, y1 = box
    half = (y1 - y0) / 2
    movers_panel((x0, y0, x1, int(y0 + half - 8)), "eu_country", 4)
    d.line([x0, y0 + half, x1, y0 + half], fill=BORDER, width=1)
    movers_panel((x0, int(y0 + half + 8), x1, y1), "uk", 4)
    movers_panel(panel(2, 2, "World: house prices, 1-year change", "BIS residential property price index, nominal, about 60 countries"), "world", 8)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, "PNG", optimize=True)
    return True


# ---------------------------------------------------------------------------
# 3. Interactive HTML dashboard
# ---------------------------------------------------------------------------

HTML_TEMPLATE = r"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Goods, Drugs & Housing Watch — __DATE__</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --panel-alt:#1c2128; --border:#30363d; --text:#e6edf3; --muted:#8b949e;
    --blue:#58a6ff; --green:#3fb950; --amber:#d29922; --red:#f85149; --purple:#bc8cff;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg:#f6f8fa; --panel:#ffffff; --panel-alt:#f0f3f6; --border:#d0d7de; --text:#1f2328; --muted:#656d76;
      --blue:#0969da; --green:#1a7f37; --amber:#9a6700; --red:#cf222e; --purple:#8250df;
    }
  }
  :root[data-theme="light"] {
    --bg:#f6f8fa; --panel:#ffffff; --panel-alt:#f0f3f6; --border:#d0d7de; --text:#1f2328; --muted:#656d76;
    --blue:#0969da; --green:#1a7f37; --amber:#9a6700; --red:#cf222e; --purple:#8250df;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); line-height:1.45; padding:20px 16px 60px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  main { max-width:1500px; margin:0 auto; }
  header, section { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:18px 20px; margin-bottom:18px; }
  h1 { font-size:1.4rem; letter-spacing:-0.02em; } header p { color:var(--muted); font-size:0.88rem; margin-top:4px; }
  h2 { font-size:1.1rem; margin-bottom:4px; }
  h3 { font-size:0.8rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; margin:18px 0 8px; }
  .sub { color:var(--muted); font-size:0.83rem; margin-bottom:12px; max-width:110ch; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr)); gap:18px; }
  .grid2 > * { min-width:0; }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
  select, button.opt, input { background:var(--panel-alt); color:var(--text); border:1px solid var(--border); border-radius:6px; padding:6px 10px; font:inherit; font-size:0.85rem; }
  button.opt { cursor:pointer; } button.opt.active { border-color:var(--blue); color:var(--blue); }
  .scroll { overflow-x:auto; } .scroll.tall { max-height:560px; overflow-y:auto; }
  .scroll.tall th { position:sticky; top:0; background:var(--panel); }
  table { width:100%; border-collapse:collapse; font-size:0.84rem; font-variant-numeric:tabular-nums; }
  th, td { padding:6px 9px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
  th.l, td.l { text-align:left; } td.wrap { white-space:normal; text-align:left; min-width:180px; }
  th { color:var(--muted); font-weight:600; font-size:0.76rem; text-transform:uppercase; letter-spacing:0.03em; }
  th[data-k] { cursor:pointer; user-select:none; } th[data-k]:hover { color:var(--text); }
  tr.pick { cursor:pointer; } tr.pick.on td:first-child { box-shadow:inset 3px 0 0 var(--blue); }
  .pos { color:var(--green); } .neg { color:var(--red); } .muted { color:var(--muted); }
  a { color:var(--blue); text-decoration:none; } a:hover { text-decoration:underline; }
  .bars { display:grid; grid-template-columns:minmax(110px,max-content) 1fr max-content; gap:5px 10px; align-items:center; font-size:0.84rem; }
  .bars .track { position:relative; height:16px; background:var(--panel-alt); border-radius:3px; min-width:60px; }
  .bars .track b { position:absolute; top:0; bottom:0; border-radius:3px; }
  .bars .track.div::after { content:""; position:absolute; left:50%; top:-2px; bottom:-2px; width:1px; background:var(--border); }
  .bars .v { font-variant-numeric:tabular-nums; font-weight:600; text-align:right; }
  @media (max-width:640px) { .bars { grid-template-columns:minmax(80px,max-content) 1fr; } .bars .v { grid-column:1/-1; text-align:left; margin:-2px 0 6px; } }
  .bars .v small { color:var(--muted); font-weight:400; margin-left:6px; }
  .kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(170px,1fr)); gap:10px; margin-bottom:14px; }
  .kv div { background:var(--panel-alt); border:1px solid var(--border); border-radius:8px; padding:10px 12px; }
  .kv div > span { display:block; color:var(--muted); font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em; }
  .kv b { font-size:1.15rem; font-variant-numeric:tabular-nums; } .kv small { display:block; color:var(--muted); font-size:0.74rem; }
  .chart svg { width:100%; height:auto; display:block; }
  .legend { display:flex; flex-wrap:wrap; gap:6px 14px; margin-top:8px; font-size:0.78rem; }
  .legend i { width:10px; height:10px; border-radius:2px; display:inline-block; margin-right:4px; }
  .chip { font-size:0.68rem; padding:1px 7px; border-radius:999px; border:1px solid var(--border); color:var(--muted); }
  .chip.medium { color:var(--amber); border-color:var(--amber); } .chip.high { color:var(--red); border-color:var(--red); }
  .note { color:var(--muted); font-size:0.8rem; margin-top:10px; max-width:110ch; }
  .empty { color:var(--muted); font-size:0.85rem; padding:8px 0; }
</style>
</head>
<body>
<main>
<header>
  <h1>Goods, Drugs &amp; Housing Price Watch</h1>
  <p id="hdr"></p>
</header>

<section id="secLuxury">
  <h2>Luxury goods: resale asking prices</h2>
  <p class="sub">Live listings of pre-owned items on resale stores, in US dollars. These are asking prices: not boutique retail prices and not sold prices. A figure needs at least three listings.</p>
  <div class="controls">
    <select id="luxStore" aria-label="Store"></select>
    <span id="luxCat"></span>
  </div>
  <div class="bars" id="luxBars"></div>
  <p class="note">Bars show the median asking price; the smaller figure is the middle half of listings (25th to 75th percentile) and the listing count.</p>
  <h3>Iconic models</h3>
  <div class="controls"><select id="modelBrand" aria-label="Brand"></select></div>
  <div class="scroll tall"><table id="modelTable"></table></div>
</section>

<section id="secDrugs">
  <h2>Drug prices: United States</h2>
  <p class="sub">Neither series is the list price, the patient's out-of-pocket price, or the net price after rebates.</p>
  <h3>Pharmacy drugs: what pharmacies pay (NADAC, weekly)</h3>
  <div class="scroll"><table id="retailTable"></table></div>
  <h3>Physician-administered drugs: what Medicare pays (Part B payment limit, quarterly)</h3>
  <div class="scroll"><table id="partbTable"></table></div>
  <p class="note">Reference cost is the per-unit price multiplied by a typical pack or dose, named in the Reference column. The Part B limit is the manufacturer's average sales price plus 6%, based on sales two quarters earlier.</p>
</section>

<section id="secHousing">
  <h2>Home prices by region</h2>
  <p class="sub" id="houseSub"></p>
  <div class="controls" id="houseViews"></div>
  <div class="kv" id="houseKv"></div>
  <div class="grid2">
    <div><h3 style="margin-top:0">Largest one-year rises</h3><div class="bars" id="houseTop"></div></div>
    <div><h3 style="margin-top:0">Largest one-year falls or smallest rises</h3><div class="bars" id="houseBottom"></div></div>
  </div>
  <h3>Price history</h3>
  <p class="sub">Indexed to 100 at the start of the window. Select rows in the table below to add or remove lines.</p>
  <div class="chart" id="houseChart"></div>
  <div class="legend" id="houseLegend"></div>
  <h3>All regions in this view</h3>
  <div class="controls"><input id="houseQ" placeholder="Filter by name" aria-label="Filter regions by name" /></div>
  <div class="scroll tall"><table id="houseTable"></table></div>
  <p class="note">Values are in local currency. Index levels use different base years and cannot be compared across sources; compare the changes instead.</p>
</section>

<section id="secAlerts">
  <h2>Alerts</h2>
  <div class="scroll"><table id="alertTable"></table></div>
</section>
</main>

<script>
const D = __PAYLOAD__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (v, d = 0) => v == null ? "—" : v.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const usd = (v, d = 0) => v == null ? "—" : "$" + num(v, d);
const sgn = (v, d = 1) => v == null ? '<span class="muted">—</span>' : `<span class="${v > 0.049 ? "pos" : v < -0.049 ? "neg" : "muted"}">${v > 0 ? "+" : ""}${num(v, d)}%</span>`;
const SYM = {"USD": "$", "GBP": "£", "BRL per m²": "R$"};
const val = h => h.unit === "index" ? num(h.value, 1) : (SYM[h.unit] || "") + num(h.value, 0) + (h.unit.includes("per m") ? "/m²" : "");
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
$("hdr").textContent = `Luxury ${D.luxury_date || "not collected"} · drugs ${D.drug_date || "not collected"} · housing ${D.housing_date || "not collected"} (${D.housing.length} regions)`;

/* ---------- luxury ---------- */
(function () {
  if (!D.brands.length) { $("secLuxury").style.display = "none"; return; }
  const state = {store: "all", cat: "bags", brand: "", sortK: "median_usd", dir: -1};
  const stores = [...new Map(D.brands.map(b => [b.store_id, b.store_name])).entries()];
  $("luxStore").innerHTML = stores.map(([id, n]) => `<option value="${id}">${esc(n)}</option>`).join("");
  const cats = [["bags", "Bags"], ["jewelry", "Jewelry"], ["watches", "Watches"], ["all", "All categories"]];
  $("luxCat").innerHTML = cats.map(([id, l]) => `<button class="opt" data-c="${id}">${l}</button>`).join(" ");
  function drawBars() {
    $("luxCat").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.c === state.cat));
    const rows = D.brands.filter(b => b.store_id === state.store && b.category === state.cat && b.median_usd != null).sort((a, b) => b.median_usd - a.median_usd);
    if (!rows.length) { $("luxBars").innerHTML = '<div class="empty" style="grid-column:1/-1">No brand has three or more listings in this view.</div>'; return; }
    const lo = Math.log10(Math.min(...rows.map(r => r.median_usd))) - 0.3, hi = Math.log10(rows[0].median_usd);
    $("luxBars").innerHTML = rows.map(r => {
      const w = Math.max((Math.log10(r.median_usd) - lo) / ((hi - lo) || 1) * 100, 2).toFixed(1);
      return `<div>${esc(r.brand)}</div><div class="track" title="log scale"><b style="left:0;width:${w}%;background:var(--blue)"></b></div>
        <div class="v">${usd(r.median_usd)}<small>${usd(r.p25_usd)} – ${usd(r.p75_usd)} · ${num(r.n_listings)} listings${r.change_vs_prev_pct == null ? "" : " · " + sgn(r.change_vs_prev_pct) + " vs " + esc(r.prev_date)}</small></div>`;
    }).join("");
  }
  $("luxStore").addEventListener("change", e => { state.store = e.target.value; drawBars(); });
  $("luxCat").querySelectorAll("button").forEach(b => b.addEventListener("click", () => { state.cat = b.dataset.c; drawBars(); }));
  const brands = [...new Set(D.models.map(m => m.brand))];
  $("modelBrand").innerHTML = '<option value="">All brands</option>' + brands.map(b => `<option>${esc(b)}</option>`).join("");
  const COLS = [["brand", "Brand", "l"], ["model", "Model", "l"], ["n_listings", "Listings", ""], ["median_usd", "Median", ""], ["p25_usd", "Middle half", ""],
    ["min_usd", "Lowest", ""], ["change_vs_prev_pct", "Since previous run", ""], ["stores", "Stores", "l"]];
  function drawModels() {
    const rows = D.models.filter(m => !state.brand || m.brand === state.brand).sort((a, b) => {
      const x = a[state.sortK], y = b[state.sortK];
      if (x == null) return 1; if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * state.dir;
    });
    $("modelTable").innerHTML = "<tr>" + COLS.map(([k, l, c]) => `<th data-k="${k}" class="${c}">${l}${k === state.sortK ? (state.dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("") + "</tr>" +
      rows.map(m => `<tr><td class="l">${esc(m.brand)}</td><td class="l"><b>${esc(m.model)}</b></td><td>${num(m.n_listings)}</td>
        <td>${m.median_usd == null ? '<span class="muted">unpriced (under 3 listings)</span>' : usd(m.median_usd)}</td>
        <td>${m.p25_usd == null ? "—" : usd(m.p25_usd) + " – " + usd(m.p75_usd)}</td>
        <td>${m.min_usd == null ? "—" : (m.cheapest_url ? `<a href="${esc(m.cheapest_url)}" target="_blank" rel="noopener" title="${esc(m.cheapest_title)}">${usd(m.min_usd)}</a>` : usd(m.min_usd))}</td>
        <td>${sgn(m.change_vs_prev_pct)}${m.prev_date ? ` <span class="muted">vs ${esc(m.prev_date)}</span>` : ""}</td><td class="l muted">${esc((m.stores || "").replace(/;/g, " · "))}</td></tr>`).join("");
    $("modelTable").querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
      if (state.sortK === th.dataset.k) state.dir *= -1; else { state.sortK = th.dataset.k; state.dir = -1; } drawModels(); }));
  }
  $("modelBrand").addEventListener("change", e => { state.brand = e.target.value; drawModels(); });
  drawBars(); drawModels();
})();

/* ---------- drugs ---------- */
(function () {
  if (!D.drugs.length) { $("secDrugs").style.display = "none"; return; }
  function draw(id, channel, prevLabel, showYtd) {
    const rows = D.drugs.filter(d => d.channel === channel).sort((a, b) => b.reference_cost_usd - a.reference_cost_usd);
    if (!rows.length) { $(id).innerHTML = '<tr><td class="empty">No data in this run.</td></tr>'; return; }
    const max = Math.log10(rows[0].reference_cost_usd), min = Math.log10(rows[rows.length - 1].reference_cost_usd) - 0.3;
    $(id).innerHTML = `<tr><th class="l">Drug</th><th class="l">Maker</th><th class="l">Area</th><th class="l">Reference</th><th>Per unit</th><th>Reference cost</th><th class="l" style="min-width:120px"></th><th>${prevLabel}</th>${showYtd ? "<th>Year to date</th>" : ""}<th>1 year</th><th class="l">As of</th></tr>` +
      rows.map(d => `<tr><td class="l"><b>${esc(d.drug)}</b><br><span class="muted">${esc(d.generic)}</span></td><td class="wrap">${esc(d.maker)}</td><td class="l muted">${esc(d.therapeutic_area)}</td>
        <td class="wrap muted">${esc(d.reference)}</td><td>${usd(d.unit_price_usd, 2)}</td><td><b>${usd(d.reference_cost_usd)}</b></td>
        <td class="l"><div class="bars" style="display:block"><div class="track" title="log scale"><b style="left:0;width:${Math.max((Math.log10(d.reference_cost_usd) - min) / ((max - min) || 1) * 100, 2).toFixed(1)}%;background:var(--blue)"></b></div></div></td>
        <td>${sgn(d.change_prev_pct, 2)}</td>${showYtd ? `<td>${sgn(d.change_ytd_pct)}</td>` : ""}<td>${sgn(d.change_1y_pct)}</td><td class="l muted">${esc(d.as_of)}</td></tr>`).join("");
  }
  draw("retailTable", "retail_pharmacy", "1 week", true);
  draw("partbTable", "physician_administered", "1 quarter", false);
})();

/* ---------- housing ---------- */
(function () {
  if (!D.housing.length) { $("secHousing").style.display = "none"; return; }
  const VIEWS = __VIEWS__;
  const PALETTE = ["#58a6ff", "#3fb950", "#f85149", "#d29922", "#bc8cff", "#39c5cf", "#f778ba", "#f0883e", "#8b949e", "#a5d6ff"];
  const state = {view: VIEWS.find(v => rowsOf(v).length)[0], picked: new Set(), sortK: "change_1y_pct", dir: -1, q: ""};
  function rowsOf(v) { return D.housing.filter(h => h.market === v[2] && (v[3].includes(h.level) || v[4].includes(h.level))); }
  const isBench = h => cur()[4].includes(h.level);
  const cur = () => VIEWS.find(v => v[0] === state.view);
  $("houseViews").innerHTML = VIEWS.filter(v => rowsOf(v).length).map(v => `<button class="opt" data-v="${v[0]}">${esc(v[1])}</button>`).join(" ");
  $("houseViews").querySelectorAll("button").forEach(b => b.addEventListener("click", () => setView(b.dataset.v)));
  function setView(v) {
    state.view = v; state.q = ""; $("houseQ").value = "";
    const ranked = rowsOf(cur()).filter(h => h.change_1y_pct != null && !isBench(h)).sort((a, b) => b.change_1y_pct - a.change_1y_pct);
    state.picked = new Set(ranked.slice(0, 3).concat(ranked.slice(-3)).map(h => h.region_id));
    rowsOf(cur()).filter(h => isBench(h)).slice(0, 1).forEach(h => state.picked.add(h.region_id));
    draw();
  }
  function bars(el, items) {
    if (!items.length) { el.innerHTML = '<div class="empty" style="grid-column:1/-1">Too few regions to rank.</div>'; return; }
    const scale = Math.max(...items.map(h => Math.abs(h.change_1y_pct))) || 1;
    el.innerHTML = items.map(h => { const v = h.change_1y_pct, w = (Math.abs(v) / scale * 50).toFixed(1);
      return `<div title="${esc(h.series)}">${esc(h.region_name)}</div><div class="track div"><b style="${v >= 0 ? "left:50%" : "right:50%"};width:${w}%;background:var(--${v >= 0 ? "green" : "red"})"></b></div><div class="v">${sgn(v)}<small>${val(h)}</small></div>`; }).join("");
  }
  const COLS = [["region_name", "Region", "l"], ["value", "Level", ""], ["period", "Period", "l"], ["change_prev_pct", "Previous period", ""], ["change_1y_pct", "1 year", ""],
    ["change_5y_pct", "5 years", ""], ["real_change_1y_pct", "1 year, after inflation", ""], ["rent_value", "Rent", ""], ["rent_change_1y_pct", "Rent, 1 year", ""], ["rental_yield_pct", "Gross rental yield", ""]];
  function draw() {
    const v = cur(), rows = rowsOf(v);
    $("houseViews").querySelectorAll("button").forEach(b => b.classList.toggle("active", b.dataset.v === state.view));
    const series = [...new Set(rows.map(h => h.series))], sources = [...new Set(rows.map(h => h.source))];
    $("houseSub").textContent = `${series.join("; ")}. Source: ${sources.join("; ")}.`;
    const ranked = rows.filter(h => h.change_1y_pct != null && !isBench(h)).sort((a, b) => b.change_1y_pct - a.change_1y_pct);
    const k = Math.min(8, Math.floor(ranked.length / 2));
    bars($("houseTop"), ranked.slice(0, k)); bars($("houseBottom"), ranked.slice(-k).reverse());
    const med = ranked.length ? ranked[Math.floor(ranked.length / 2)].change_1y_pct : null;
    const bench = rows.filter(h => isBench(h));
    $("houseKv").innerHTML = [`<div><span>Regions ranked</span><b>${ranked.length}</b><small>${ranked.filter(h => h.change_1y_pct < 0).length} falling over one year</small></div>`,
      `<div><span>Typical 1-year change</span><b>${sgn(med)}</b><small>median region</small></div>`]
      .concat(bench.slice(0, 3).map(h => `<div><span>${esc(h.region_name)}</span><b>${val(h)}</b><small>${h.change_1y_pct == null ? "" : (h.change_1y_pct > 0 ? "+" : "") + num(h.change_1y_pct, 1) + "% over one year · "}${esc(h.period)}</small></div>`)).join("");
    const cols = COLS.filter(([key]) => key === "region_name" || rows.some(h => h[key] != null));
    const shown = rows.filter(h => !state.q || h.region_name.toLowerCase().includes(state.q)).sort((a, b) => {
      const x = a[state.sortK], y = b[state.sortK];
      if (x == null) return 1; if (y == null) return -1;
      return (typeof x === "string" ? x.localeCompare(y) : x - y) * state.dir; });
    const cellOf = (h, key) => key === "region_name" ? `<td class="l"><b>${esc(h.region_name)}</b>${h.parent && h.parent !== h.region_name && v[0] !== "br_city" ? ` <span class="muted">${esc(h.parent)}</span>` : ""}</td>`
      : key === "value" ? `<td>${val(h)}</td>` : key === "period" ? `<td class="l muted">${esc(h.period)}</td>`
      : key === "rent_value" ? `<td>${h.rent_value == null ? "—" : (h.rent_unit.startsWith("BRL") ? "R$" + num(h.rent_value, 1) + "/m²" : "$" + num(h.rent_value, 0))}</td>`
      : key === "rental_yield_pct" ? `<td>${h.rental_yield_pct == null ? "—" : num(h.rental_yield_pct, 1) + "%"}</td>` : `<td>${sgn(h[key])}</td>`;
    $("houseTable").innerHTML = "<tr>" + cols.map(([key, l, c]) => `<th data-k="${key}" class="${c}">${l}${key === state.sortK ? (state.dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("") + "</tr>" +
      shown.map(h => `<tr class="pick${state.picked.has(h.region_id) ? " on" : ""}" data-id="${esc(h.region_id)}">${cols.map(([key]) => cellOf(h, key)).join("")}</tr>`).join("");
    $("houseTable").querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
      if (state.sortK === th.dataset.k) state.dir *= -1; else { state.sortK = th.dataset.k; state.dir = th.dataset.k === "region_name" ? 1 : -1; } draw(); }));
    $("houseTable").querySelectorAll("tr.pick").forEach(tr => tr.addEventListener("click", () => {
      state.picked.has(tr.dataset.id) ? state.picked.delete(tr.dataset.id) : state.picked.add(tr.dataset.id); draw(); }));
    chart(rows.filter(h => state.picked.has(h.region_id)));
  }
  function chart(rows) {
    const el = $("houseChart"); el.innerHTML = "";
    const lines = rows.map((h, i) => ({h, color: i < PALETTE.length ? PALETTE[i] : `hsl(${(i * 67) % 360},60%,55%)`, pts: D.housing_history[h.region_id] || []})).filter(l => l.pts.length > 1);
    if (!lines.length) { el.innerHTML = '<div class="empty">Select a region in the table to chart its history.</div>'; $("houseLegend").innerHTML = ""; return; }
    const periods = [...new Set(lines.flatMap(l => l.pts.map(p => p[0])))].sort(), pos = Object.fromEntries(periods.map((p, i) => [p, i]));
    const W = 900, H = 340, m = {l: 48, r: 14, t: 10, b: 26};
    const idx = lines.map(l => l.pts.map(p => [pos[p[0]], p[1] / l.pts[0][1] * 100]));
    const all = idx.flat().map(p => p[1]); let y0 = Math.min(...all), y1 = Math.max(...all); const pad = (y1 - y0) * 0.06 || 1; y0 -= pad; y1 += pad;
    const sx = i => m.l + i / Math.max(periods.length - 1, 1) * (W - m.l - m.r), sy = y => H - m.b - (y - y0) / (y1 - y0) * (H - m.t - m.b);
    let g = "";
    for (let k = 0; k <= 5; k++) { const yv = y0 + (y1 - y0) * k / 5;
      g += `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(yv)}" y2="${sy(yv)}" stroke="var(--border)"/><text x="${m.l - 6}" y="${sy(yv) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${yv.toFixed(0)}</text>`; }
    if (100 > y0 && 100 < y1) g += `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(100)}" y2="${sy(100)}" stroke="var(--muted)" stroke-dasharray="4 4"/>`;
    const step = Math.max(Math.floor(periods.length / 6), 1);
    for (let i = 0; i < periods.length; i += step) g += `<text x="${sx(i)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(periods[i])}</text>`;
    lines.forEach((l, j) => { g += `<path d="${idx[j].map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("")}" fill="none" stroke="${l.color}" stroke-width="2" stroke-linejoin="round"><title>${esc(l.h.region_name)}</title></path>`; });
    el.innerHTML = `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="Home price history, indexed">${g}</svg>`;
    $("houseLegend").innerHTML = lines.map((l, j) => `<span><i style="background:${l.color}"></i>${esc(l.h.region_name)} ${sgn(idx[j][idx[j].length - 1][1] - 100)}</span>`).join("");
  }
  $("houseQ").addEventListener("input", e => { state.q = e.target.value.toLowerCase(); draw(); });
  setView(state.view);
})();

/* ---------- alerts ---------- */
(function () {
  if (!D.alerts.length) { $("alertTable").innerHTML = '<tr><td class="empty">No triggers fired in the latest full run.</td></tr>'; return; }
  $("alertTable").innerHTML = '<tr><th class="l">Severity</th><th class="l">Section</th><th class="l">Signal</th><th class="l">Details</th></tr>' +
    D.alerts.map(a => `<tr><td class="l"><span class="chip ${a.severity}">${esc(a.severity)}</span></td><td class="l">${esc(a.section)}</td><td class="l muted">${esc(a.signal_type)}</td><td class="wrap">${esc(a.details)}</td></tr>`).join("");
})();
</script>
</body>
</html>
"""


def render_html(ds: Dict[str, Any], out_path: Path) -> None:
    payload = {k: ds[k] for k in ("latest_date", "luxury_date", "drug_date", "housing_date", "brands", "models", "drugs",
                                  "housing", "housing_history", "alerts")}
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = (HTML_TEMPLATE.replace("__PAYLOAD__", blob)
            .replace("__VIEWS__", json.dumps([list(v) for v in HOUSING_VIEWS]))
            .replace("__DATE__", ds["latest_date"]))
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Visualize the luxury / drug / housing price CSVs in `data/`.")
    parser.add_argument("--data-dir", default="data", help="Data directory (default: data)")
    parser.add_argument("--html", default="reports/goods_drugs_housing_dashboard.html", help="Output path for the HTML dashboard")
    parser.add_argument("--png", default="reports/goods_drugs_housing_overview.png", help="Output path for the PNG overview")
    parser.add_argument("--quiet", action="store_true", help="Skip the terminal dashboard")
    parser.add_argument("--open", action="store_true", help="Open the HTML dashboard in the default browser")
    args = parser.parse_args()

    ds = load_datasets(Path(args.data_dir))
    if not (ds["brands"] or ds["drugs"] or ds["housing"]):
        print(f"[ERR] No luxury, drug or housing data in `{args.data_dir}/`. "
              "Run `python3 scripts/watch_goods_drugs_housing.py --write-csv` first.", file=sys.stderr)
        return 1
    if not args.quiet:
        render_terminal(ds)
    html_path = Path(args.html)
    render_html(ds, html_path)
    print(f"[OK] Generated interactive HTML dashboard -> {html_path}")
    png_path = Path(args.png)
    if render_png(ds, png_path):
        print(f"[OK] Generated PNG overview -> {png_path}")
    else:
        print("[WARN] Pillow is not installed; skipped the PNG overview (`pip install pillow`).")
    if args.open:
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        try:
            subprocess.run([opener, str(html_path)], check=False)
        except OSError:
            print(f"[WARN] Could not launch `{opener}`; open {html_path} manually.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
