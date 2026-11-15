#!/usr/bin/env python3
"""
visualize_asset_classes.py — Visualizer for the asset-class watch (`scripts/watch_asset_classes.py`).

Reads the CSV files that the watcher writes into `data/`:
  asset_classes.csv, asset_class_summary.csv, asset_class_metrics.csv, asset_class_alerts.csv,
  asset_classes_history.csv
and produces three views of the latest run:

  1. Terminal dashboard (stdout): scoreboard with 90-day sparklines per class, return heatmap,
     largest Z-score deviations, rates / options / futures / annuity highlights, alerts.
  2. PNG overview (`reports/asset_classes_overview.png`, needs Pillow): six panels — return heatmap,
     breadth, Z-score deviations, Treasury vs implied swap curve, options, futures curve slopes.
  3. Self-contained interactive HTML dashboard (`reports/asset_classes_dashboard.html`): class cards,
     return heatmap, per-class chart explorer with sortable instrument table, rates & swaps curves,
     derivatives panels, fund scorecards, annuity and cash tables, full metric browser, alerts.

Usage:
  python3 scripts/visualize_asset_classes.py
  python3 scripts/visualize_asset_classes.py --html reports/asset_classes_dashboard.html \
      --png reports/asset_classes_overview.png --open
"""

from __future__ import annotations

import argparse
import csv
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

try:
    from PIL import Image, ImageDraw, ImageFont

    HAS_PIL = True
except ImportError:
    HAS_PIL = False

try:  # class notes live with the watcher's universe definition
    from watch_asset_classes import UNIVERSE
except Exception:
    UNIVERSE = {}

GROUPS = ["Core asset classes", "Investment vehicles", "Broader financial instruments"]
HORIZONS = [("1d", "median_change_1d_pct"), ("5d", "median_change_5d_pct"), ("1m", "median_change_1m_pct"),
            ("3m", "median_change_3m_pct"), ("YTD", "median_change_ytd_pct"), ("1y", "median_change_1y_pct")]
SHORT_NAMES = {
    "stocks": "Stocks", "bonds": "Bonds", "cash": "Cash", "commodities": "Commodities",
    "real_estate": "Real Estate", "alternatives": "Alternatives", "etfs": "ETFs", "index_funds": "Index Funds",
    "mutual_funds": "Mutual Funds", "options": "Options (vol)", "futures_forwards": "Futures & Fwds",
    "swaps": "Swaps", "structured": "Structured/Hybrid", "annuities": "Annuities",
}
NUMERIC_ROW_COLS = [
    "price", "change_1d_abs", "change_1d_pct", "change_5d_pct", "change_1m_pct", "change_3m_pct", "change_ytd_pct",
    "change_1y_pct", "z_score_20d", "volume", "vol_ratio_20d", "realized_vol_30d_pct", "drawdown_52w_pct",
]


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


def latest_rows(rows: List[Dict[str, str]]) -> List[Dict[str, str]]:
    if not rows:
        return []
    latest = max(r["date"] for r in rows)
    return [r for r in rows if r["date"] == latest]


def class_index(tickers: List[str], dates: List[str], series: Dict[str, List[Optional[float]]], window: int = 90) -> List[float]:
    """Equal-weighted index (start = 100) of a class's instruments over the trailing window."""
    start = max(len(dates) - window, 0)
    paths = []
    for t in tickers:
        vals = series.get(t)
        if not vals:
            continue
        filled, last = [], None
        for v in vals[start:]:
            last = v if v is not None else last
            filled.append(last)
        base = next((v for v in filled if v is not None), None)
        if base:
            paths.append([(v / base * 100.0) if v is not None else None for v in filled])
    out = []
    for i in range(len(dates) - start):
        col = [p[i] for p in paths if p[i] is not None]
        if col:
            out.append(sum(col) / len(col))
    return out


def load_datasets(data_dir: Path) -> Dict[str, Any]:
    rows = latest_rows(load_csv(data_dir / "asset_classes.csv"))
    summaries = latest_rows(load_csv(data_dir / "asset_class_summary.csv"))
    metrics = latest_rows(load_csv(data_dir / "asset_class_metrics.csv"))
    all_alerts = load_csv(data_dir / "asset_class_alerts.csv")
    latest_date = max((r["date"] for r in rows), default="")
    alerts = [a for a in all_alerts if a["date"] == latest_date]
    history = load_csv(data_dir / "asset_classes_history.csv")

    for r in rows:
        for col in NUMERIC_ROW_COLS:
            r[col] = to_float(r.get(col))
    for s in summaries:
        for col in list(s.keys()):
            if col.startswith(("median_", "pct_", "best_1d_pct", "worst_1d_pct", "max_abs_z")) and not col.endswith("_id"):
                s[col] = to_float(s[col])
        s["n_instruments"] = int(to_float(s.get("n_instruments")) or 0)
    for m in metrics:
        m["value"] = to_float(m.get("value"))
    for a in alerts:
        a["value"] = to_float(a.get("value"))

    dates = [h["date"] for h in history]
    tickers = [c for c in (history[0].keys() if history else []) if c != "date"]
    series = {t: [to_float(h.get(t)) for h in history] for t in tickers}

    by_class: Dict[str, List[str]] = {}
    for r in rows:
        by_class.setdefault(r["class_id"], []).append(r["asset_id"])
    for s in summaries:
        s["index_90d"] = [round(v, 3) for v in class_index(by_class.get(s["class_id"], []), dates, series)]
        s["note"] = UNIVERSE.get(s["class_id"], {}).get("note", "")
        s["short_name"] = SHORT_NAMES.get(s["class_id"], s["asset_class"])

    return {
        "latest_date": latest_date,
        "timestamp_utc": rows[0]["timestamp_utc"] if rows else "",
        "rows": rows,
        "summaries": summaries,
        "metrics": metrics,
        "metric_by_id": {m["metric_id"]: m for m in metrics},
        "alerts": alerts,
        "dates": dates,
        "series": series,
    }


# ---------------------------------------------------------------------------
# Shared selections used by all three renderers
# ---------------------------------------------------------------------------

def top_deviations(ds: Dict[str, Any], n: int = 12) -> List[Dict[str, Any]]:
    seen, out = set(), []
    for r in sorted((r for r in ds["rows"] if r["z_score_20d"] is not None), key=lambda r: -abs(r["z_score_20d"])):
        if r["asset_id"] not in seen:
            seen.add(r["asset_id"])
            out.append(r)
        if len(out) >= n:
            break
    return out


def metrics_with_prefix(ds: Dict[str, Any], prefix: str) -> List[Dict[str, Any]]:
    return [m for m in ds["metrics"] if m["metric_id"].startswith(prefix) and m["value"] is not None]


def mval(ds: Dict[str, Any], metric_id: str) -> Optional[float]:
    m = ds["metric_by_id"].get(metric_id)
    return m["value"] if m else None


TENOR_YEARS = {"1m": 1 / 12, "2m": 2 / 12, "3m": 0.25, "4m": 4 / 12, "6m": 0.5, "1y": 1, "2y": 2, "3y": 3, "5y": 5,
               "7y": 7, "10y": 10, "20y": 20, "30y": 30}


def curve_points(ds: Dict[str, Any]) -> Tuple[List[Tuple[float, float]], List[Tuple[float, float]], List[Tuple[float, float]]]:
    """(Treasury par curve, implied SOFR swap curve, T-bill yields) as (years, pct) points."""
    treasury = sorted((TENOR_YEARS[m["metric_id"][8:]], m["value"]) for m in metrics_with_prefix(ds, "ust_par_")
                      if m["metric_id"][8:] in TENOR_YEARS)
    swaps = sorted((float(m["metric_id"].split("_")[3].rstrip("y")), m["value"])
                   for m in metrics_with_prefix(ds, "sofr_swap_implied_"))
    bills = sorted((int(m["metric_id"].split("_")[1].rstrip("w")) / 52.0, m["value"])
                   for m in metrics_with_prefix(ds, "tbill_"))
    return treasury, swaps, bills


def fmt_pct(v: Optional[float], d: int = 2) -> str:
    return "—" if v is None else f"{v:+.{d}f}%"


def sparkline(values: List[float], width: int = 24) -> str:
    if len(values) < 2:
        return " " * width
    step = max(len(values) / width, 1.0)
    sampled = [values[min(int(i * step), len(values) - 1)] for i in range(min(width, len(values)))]
    lo, hi = min(sampled), max(sampled)
    blocks = "▁▂▃▄▅▆▇█"
    if hi == lo:
        return blocks[3] * len(sampled)
    return "".join(blocks[int((v - lo) / (hi - lo) * 7)] for v in sampled)


# ---------------------------------------------------------------------------
# 1. Terminal dashboard
# ---------------------------------------------------------------------------

def render_terminal(ds: Dict[str, Any]) -> None:
    bar = "=" * 124
    print(bar)
    print(f" ASSET-CLASS WATCH DASHBOARD — {ds['latest_date']} ({ds['timestamp_utc']}) | "
          f"{len(ds['rows'])} instruments · {len(ds['summaries'])} classes · {len(ds['metrics'])} metrics · {len(ds['alerts'])} alerts")
    print(bar)
    for group in GROUPS:
        members = [s for s in ds["summaries"] if s["group"] == group]
        if not members:
            continue
        print(f"\n[{group}]  median total return of tracked instruments · breadth = share above 50-day average")
        print(f"  {'Asset class':<41}{'cov':<7}{'n':>3}{'1d':>9}{'5d':>9}{'1m':>9}{'3m':>9}{'YTD':>9}{'1y':>9}{'>50d':>6}  {'90d index':<24}  trend")
        for s in members:
            breadth = "—" if s["pct_above_50dma"] is None else f"{s['pct_above_50dma']:.0f}%"
            print(f"  {s['asset_class']:<41}{s['coverage']:<7}{s['n_instruments']:>3}"
                  + "".join(f"{fmt_pct(s[k]):>9}" for _, k in HORIZONS)
                  + f"{breadth:>6}  {sparkline(s['index_90d']):<24}  {s['trend']}")

    print("\n[Largest deviations — |Z| vs 20-day mean]")
    for r in top_deviations(ds, 10):
        z = r["z_score_20d"]
        half = 16
        fill = min(int(abs(z) / 4.0 * half), half)
        gauge = (" " * (half - fill) + "█" * fill + "|" + " " * half) if z < 0 else (" " * half + "|" + "█" * fill + " " * (half - fill))
        print(f"  {r['asset_id']:<10} {r['asset_class'][:26]:<26} {gauge} Z={z:+.2f}  1d {fmt_pct(r['change_1d_pct'])}")

    treasury, swaps, bills = curve_points(ds)
    print("\n[Rates — cash, Treasury par curve, implied SOFR swaps]")
    if bills:
        print("  T-bills (CE yield) : " + "  ".join(f"{int(round(t * 52))}w {y:.2f}%" for t, y in bills))
    if treasury:
        print("  Treasury par curve : " + "  ".join(f"{k.upper()} {mval(ds, 'ust_par_' + k):.2f}" for k in TENOR_YEARS if mval(ds, "ust_par_" + k) is not None))
    if swaps:
        print("  Implied OIS swaps  : " + "  ".join(
            f"{int(t)}Y {y:.2f}% (spread {mval(ds, f'swap_spread_{int(t)}y_bps'):+.0f} bps)" if mval(ds, f"swap_spread_{int(t)}y_bps") is not None
            else f"{int(t)}Y {y:.2f}%" for t, y in swaps))
    line = [f"{label} {mval(ds, mid):.2f}%" for label, mid in (("SOFR", "sofr_pct"), ("EFFR", "effr_pct"), ("30Y mortgage", "mortgage_30y_pct")) if mval(ds, mid) is not None]
    if line:
        print("  Reference rates    : " + "  ".join(line))

    print("\n[Options — implied volatility and positioning]")
    vol = {r["asset_id"]: r["price"] for r in ds["rows"] if r["class_id"] == "options"}
    term = [f"{t.lstrip('^')} {vol[t]:.2f}" for t in ("^VIX9D", "^VIX", "^VIX3M", "^VIX6M") if t in vol]
    if term:
        extra = [f"{label} {mval(ds, mid):.2f}" for label, mid in (("VIX/VIX3M", "vix_term_ratio"), ("1y percentile %", "vix_percentile_1y_pct")) if mval(ds, mid) is not None]
        print("  Term structure     : " + "  ".join(term + extra))
    for m in metrics_with_prefix(ds, "put_call_volume_ratio_"):
        sym = m["metric_id"].rsplit("_", 1)[1]
        parts = [f"P/C vol {m['value']:.2f}"]
        for label, mid, f in (("P/C OI", f"put_call_oi_ratio_{sym}", "{:.2f}"), ("IV30", f"iv30_{sym}_pct", "{:.1f}%"),
                              ("25Δ skew", f"skew_25d_{sym}_pts", "{:+.1f} pts")):
            if mval(ds, mid) is not None:
                parts.append(f"{label} {f.format(mval(ds, mid))}")
        print(f"  {sym.upper():<5}: " + "  ".join(parts))

    print("\n[Futures & forwards — 12-month curve slope (+ contango / − backwardation) and forward premium]")
    for m in metrics_with_prefix(ds, "term_structure_12m_"):
        v = m["value"]
        fill = min(int(abs(v) / 25.0 * 16), 16)
        gauge = (" " * (16 - fill) + "█" * fill + "|" + " " * 16) if v < 0 else (" " * 16 + "|" + "█" * fill + " " * (16 - fill))
        print(f"  {m['metric_name'].split(':')[0]:<14} {gauge} {v:+.2f}%")
    carry = [f"{m['metric_name'].split(':')[0]} {m['value']:+.2f}%" for m in metrics_with_prefix(ds, "implied_carry_")]
    if carry:
        print("  Annualised forward premium: " + "  ".join(carry))

    payouts = metrics_with_prefix(ds, "spia_payout_age")
    if payouts:
        print("\n[Annuities — model life-annuity payout (Treasury curve + Gompertz mortality; not a quote)]")
        print("  " + "  ".join(
            f"age {m['metric_id'][15:17]}: {m['value']:.2f}% (${mval(ds, 'spia_monthly_income_age' + m['metric_id'][15:17] + '_usd'):,.0f}/mo per $100k)"
            for m in payouts))

    print(f"\n[Alerts — {len(ds['alerts'])}]")
    for a in ds["alerts"][:20]:
        print(f"  [{a['severity'].upper():<6}] {a['asset_id']:<26} {a['details'][:140]}")
    if len(ds["alerts"]) > 20:
        print(f"  … {len(ds['alerts']) - 20} more in data/asset_class_alerts.csv")
    print(bar)


# ---------------------------------------------------------------------------
# 2. PNG overview (Pillow)
# ---------------------------------------------------------------------------

FONT_CANDIDATES = {
    "bold": ["/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
             "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
             "/System/Library/Fonts/Supplemental/Arial Bold.ttf", "C:/Windows/Fonts/arialbd.ttf"],
    "regular": ["/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
                "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
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
    except TypeError:  # Pillow < 10.1
        return ImageFont.load_default()


def render_png(ds: Dict[str, Any], out_path: Path) -> bool:
    if not HAS_PIL:
        return False
    W, H = 2400, 1560
    BG, CARD, BORDER = (13, 17, 23), (22, 27, 34), (48, 54, 61)
    TEXT, MUTED = (230, 237, 243), (139, 148, 158)
    GREEN, RED, BLUE, AMBER, PURPLE = (63, 185, 80), (248, 81, 73), (88, 166, 255), (210, 153, 34), (188, 140, 255)

    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)
    f_title, f_h2 = load_font("bold", 30), load_font("bold", 21)
    f_body, f_small, f_bold = load_font("regular", 17), load_font("regular", 14), load_font("bold", 17)

    def text_w(s: str, font: Any) -> float:
        return d.textlength(s, font=font)

    def right(x: float, y: float, s: str, font: Any, fill: Tuple[int, int, int]) -> None:
        d.text((x - text_w(s, font), y), s, font=font, fill=fill)

    def panel(x0: int, y0: int, x1: int, y1: int, title: str, sub: str = "") -> Tuple[int, int, int, int]:
        d.rounded_rectangle([x0, y0, x1, y1], radius=10, fill=CARD, outline=BORDER, width=2)
        d.text((x0 + 20, y0 + 14), title, font=f_h2, fill=TEXT)
        if sub:
            d.text((x0 + 20, y0 + 44), sub, font=f_small, fill=MUTED)
        return x0 + 20, y0 + (74 if sub else 54), x1 - 20, y1 - 18

    def blend(color: Tuple[int, int, int], strength: float) -> Tuple[int, int, int]:
        s = max(0.0, min(strength, 1.0))
        return tuple(int(CARD[i] + (color[i] - CARD[i]) * s) for i in range(3))  # type: ignore[return-value]

    def diverging_bars(box: Tuple[int, int, int, int], items: List[Tuple[str, float, str]], label_w: int, scale: Optional[float] = None) -> None:
        x0, y0, x1, y1 = box
        if not items:
            d.text((x0, y0), "No data in this run.", font=f_body, fill=MUTED)
            return
        row_h = min((y1 - y0) / len(items), 44)
        value_w = 110
        bx0, bx1 = x0 + label_w, x1 - value_w
        mid = (bx0 + bx1) / 2
        scale = scale or max(abs(v) for _, v, _ in items) or 1.0
        d.line([mid, y0, mid, y0 + row_h * len(items)], fill=BORDER, width=2)
        for i, (label, v, shown) in enumerate(items):
            y = y0 + i * row_h
            d.text((x0, y + row_h / 2 - 10), label, font=f_body, fill=TEXT)
            length = min(abs(v) / scale, 1.0) * (bx1 - mid)
            col = GREEN if v >= 0 else RED
            if v >= 0:
                d.rectangle([mid, y + 6, mid + length, y + row_h - 6], fill=col)
            else:
                d.rectangle([mid - length, y + 6, mid, y + row_h - 6], fill=col)
            right(x1, y + row_h / 2 - 10, shown, f_bold, TEXT)

    # Header
    d.rounded_rectangle([24, 20, W - 24, 104], radius=10, fill=CARD, outline=BORDER, width=2)
    d.text((46, 32), f"ASSET-CLASS WATCH — {ds['latest_date']}", font=f_title, fill=TEXT)
    d.text((46, 72), f"{len(ds['rows'])} instruments across {len(ds['summaries'])} asset classes  |  {len(ds['metrics'])} derived metrics  |  "
           f"{len(ds['alerts'])} alerts  |  collected {ds['timestamp_utc']}", font=f_small, fill=MUTED)

    col_w = (W - 24 * 2 - 20 * 2) // 3
    xs = [24, 24 + col_w + 20, 24 + 2 * (col_w + 20)]
    row1 = (124, 850)
    row2 = (870, H - 24)

    # Panel A: return heatmap
    x0, y0, x1, y1 = panel(xs[0], row1[0], xs[0] + col_w, row1[1], "Median return by asset class", "Median total return of each class's tracked instruments (%)")
    sums = ds["summaries"]
    label_w = 190
    cw = (x1 - x0 - label_w) / len(HORIZONS)
    rh = (y1 - y0 - 28) / max(len(sums), 1)
    for j, (h, _) in enumerate(HORIZONS):
        right(x0 + label_w + (j + 1) * cw - 12, y0, h, f_bold, MUTED)
    for j, (_, key) in enumerate(HORIZONS):
        col_max = max((abs(s[key]) for s in sums if s[key] is not None), default=1.0) or 1.0
        for i, s in enumerate(sums):
            y = y0 + 28 + i * rh
            if j == 0:
                d.text((x0, y + rh / 2 - 10), s["short_name"], font=f_body, fill=TEXT)
            v = s[key]
            if v is None:
                continue
            cell = [x0 + label_w + j * cw + 2, y + 2, x0 + label_w + (j + 1) * cw - 2, y + rh - 2]
            d.rectangle(cell, fill=blend(GREEN if v >= 0 else RED, 0.15 + 0.7 * abs(v) / col_max))
            right(cell[2] - 10, y + rh / 2 - 10, f"{v:+.1f}", f_body, TEXT)

    # Panel B: breadth
    x0, y0, x1, y1 = panel(xs[1], row1[0], xs[1] + col_w, row1[1], "Breadth by asset class", "Share of instruments above their 50-day average (%)")
    rh = (y1 - y0) / max(len(sums), 1)
    for i, s in enumerate(sums):
        y = y0 + i * rh
        d.text((x0, y + rh / 2 - 10), s["short_name"], font=f_body, fill=TEXT)
        bx0, bx1 = x0 + 190, x1 - 250
        d.rectangle([bx0, y + 8, bx1, y + rh - 8], fill=blend(MUTED, 0.18))
        v = s["pct_above_50dma"]
        if v is not None:
            col = GREEN if v >= 60 else (RED if v <= 40 else AMBER)
            d.rectangle([bx0, y + 8, bx0 + (bx1 - bx0) * v / 100.0, y + rh - 8], fill=col)
            d.text((bx1 + 12, y + rh / 2 - 10), f"{v:.0f}%", font=f_bold, fill=TEXT)
        right(x1, y + rh / 2 - 9, s["trend"], f_small, MUTED)

    # Panel C: Z-score deviations
    box = panel(xs[2], row1[0], xs[2] + col_w, row1[1], "Largest deviations", "Z-score of the latest close against its 20-day mean")
    devs = top_deviations(ds, 14)
    diverging_bars(box, [(f"{r['asset_id']}  ·  {SHORT_NAMES.get(r['class_id'], '')}", r["z_score_20d"], f"{r['z_score_20d']:+.2f}") for r in devs], 290, scale=4.0)

    # Panel D: rates curves
    x0, y0, x1, y1 = panel(xs[0], row2[0], xs[0] + col_w, row2[1], "Rates: Treasury curve vs implied swaps", "Yield (%) by maturity, square-root time axis")
    treasury, swaps, bills = curve_points(ds)
    all_pts = treasury + swaps + bills
    if all_pts:
        px0, px1, py0, py1 = x0 + 56, x1 - 16, y0 + 10, y1 - 70
        lo, hi = min(y for _, y in all_pts) - 0.15, max(y for _, y in all_pts) + 0.15

        def sx(t: float) -> float:
            return px0 + (t ** 0.5) / (30 ** 0.5) * (px1 - px0)

        def sy(y: float) -> float:
            return py1 - (y - lo) / (hi - lo) * (py1 - py0)

        steps = 5
        for k in range(steps + 1):
            yv = lo + (hi - lo) * k / steps
            d.line([px0, sy(yv), px1, sy(yv)], fill=BORDER, width=1)
            right(px0 - 8, sy(yv) - 9, f"{yv:.2f}", f_small, MUTED)
        for t, lab in ((0.25, "3M"), (1, "1Y"), (2, "2Y"), (5, "5Y"), (10, "10Y"), (20, "20Y"), (30, "30Y")):
            d.text((sx(t) - 12, py1 + 8), lab, font=f_small, fill=MUTED)
        for pts, col, name in ((treasury, BLUE, "Treasury par curve"), (swaps, AMBER, "Implied SOFR OIS swaps (model)"), (bills, PURPLE, "T-bill coupon-equivalent")):
            if not pts:
                continue
            xy = [(sx(t), sy(y)) for t, y in pts]
            if len(xy) > 1:
                d.line(xy, fill=col, width=4, joint="curve")
            for x, y in xy:
                d.ellipse([x - 6, y - 6, x + 6, y + 6], fill=col, outline=CARD, width=2)
        lx = px0
        for col, name, pts in ((BLUE, "Treasury par curve", treasury), (AMBER, "Implied SOFR swaps (model)", swaps), (PURPLE, "T-bills", bills)):
            if pts:
                d.rectangle([lx, y1 - 26, lx + 18, y1 - 12], fill=col)
                d.text((lx + 26, y1 - 30), name, font=f_small, fill=TEXT)
                lx += 44 + int(text_w(name, f_small))
    else:
        d.text((x0, y0), "No rates data in this run.", font=f_body, fill=MUTED)

    # Panel E: options
    x0, y0, x1, y1 = panel(xs[1], row2[0], xs[1] + col_w, row2[1], "Options: implied volatility and positioning", "Volatility indices (level) and Cboe chain statistics")
    vol_rows = [r for r in ds["rows"] if r["class_id"] == "options"]
    order = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M", "^VXN", "^VXD", "^OVX", "^GVZ"]
    vol_rows = sorted((r for r in vol_rows if r["asset_id"] in order), key=lambda r: order.index(r["asset_id"]))
    if vol_rows:
        bw = (x1 - x0) / len(vol_rows)
        top, base = y0 + 30, y0 + 250
        vmax = max(r["price"] for r in vol_rows)
        for i, r in enumerate(vol_rows):
            bh = (base - top) * r["price"] / vmax
            bx = x0 + i * bw
            d.rectangle([bx + 14, base - bh, bx + bw - 14, base], fill=BLUE)
            lab = f"{r['price']:.1f}"
            d.text((bx + bw / 2 - text_w(lab, f_bold) / 2, base - bh - 24), lab, font=f_bold, fill=TEXT)
            name = r["asset_id"].lstrip("^")
            d.text((bx + bw / 2 - text_w(name, f_small) / 2, base + 8), name, font=f_small, fill=MUTED)
    ty = y0 + 300
    cols = [("Underlying", 0), ("P/C volume", 150), ("P/C open int.", 290), ("IV30", 440), ("ATM IV", 530), ("25d skew", 630)]
    for name, off in cols:
        d.text((x0 + off, ty), name, font=f_bold, fill=MUTED)
    d.line([x0, ty + 28, x1, ty + 28], fill=BORDER, width=1)
    for i, m in enumerate(metrics_with_prefix(ds, "put_call_volume_ratio_")):
        sym = m["metric_id"].rsplit("_", 1)[1]
        y = ty + 40 + i * 34
        cells = [sym.upper(), f"{m['value']:.2f}"]
        for mid, f in ((f"put_call_oi_ratio_{sym}", "{:.2f}"), (f"iv30_{sym}_pct", "{:.1f}%"), (f"atm_iv_{sym}_pct", "{:.1f}%"), (f"skew_25d_{sym}_pts", "{:+.1f}")):
            v = mval(ds, mid)
            cells.append("-" if v is None else f.format(v))
        for (name, off), c in zip(cols, cells):
            d.text((x0 + off, y), c, font=f_body, fill=TEXT)
    foot = [f"{label}: {mval(ds, mid):.2f}" for label, mid in (("VIX / VIX3M", "vix_term_ratio"), ("VIX 1y percentile (%)", "vix_percentile_1y_pct")) if mval(ds, mid) is not None]
    if foot:
        d.text((x0, y1 - 24), "   |   ".join(foot), font=f_small, fill=MUTED)

    # Panel F: futures curve slopes + annuity payouts
    x0, y0, x1, y1 = panel(xs[2], row2[0], xs[2] + col_w, row2[1], "Futures curves and annuity payouts", "12-month futures curve slope: + contango, - backwardation (%)")
    slopes = [(m["metric_name"].split(":")[0], m["value"], f"{m['value']:+.1f}%") for m in metrics_with_prefix(ds, "term_structure_12m_")]
    diverging_bars((x0, y0, x1, y0 + 44 * max(len(slopes), 1)), slopes, 170)
    ay = y0 + 44 * max(len(slopes), 1) + 30
    d.line([x0, ay - 14, x1, ay - 14], fill=BORDER, width=1)
    d.text((x0, ay), "Model life-annuity payout (Treasury curve + Gompertz mortality, before insurer loads)", font=f_small, fill=MUTED)
    payouts = metrics_with_prefix(ds, "spia_payout_age")
    if payouts:
        cwid = (x1 - x0) / len(payouts)
        for i, m in enumerate(payouts):
            age = m["metric_id"][15:17]
            income = mval(ds, f"spia_monthly_income_age{age}_usd")
            cx = x0 + i * cwid
            d.text((cx, ay + 32), f"Age {age}", font=f_bold, fill=MUTED)
            d.text((cx, ay + 58), f"{m['value']:.2f}%", font=f_title, fill=TEXT)
            if income is not None:
                d.text((cx, ay + 100), f"${income:,.0f}/mo per $100k", font=f_small, fill=MUTED)
    carry = [f"{m['metric_name'].split(':')[0]} {m['value']:+.1f}%" for m in metrics_with_prefix(ds, "implied_carry_")]
    if carry:
        d.text((x0, y1 - 46), "Annualised forward premium (dated future vs spot):", font=f_small, fill=MUTED)
        d.text((x0, y1 - 24), "   ".join(carry), font=f_small, fill=TEXT)

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
<title>Asset-Class Watch — __DATE__</title>
<style>
  :root {
    --bg:#0d1117; --panel:#161b22; --panel-alt:#1c2128; --border:#30363d; --text:#e6edf3; --muted:#8b949e;
    --blue:#58a6ff; --green:#3fb950; --amber:#d29922; --red:#f85149; --purple:#bc8cff;
    --green-rgb:63,185,80; --red-rgb:248,81,73;
  }
  @media (prefers-color-scheme: light) {
    :root:not([data-theme="dark"]) {
      --bg:#f6f8fa; --panel:#ffffff; --panel-alt:#f0f3f6; --border:#d0d7de; --text:#1f2328; --muted:#656d76;
      --blue:#0969da; --green:#1a7f37; --amber:#9a6700; --red:#cf222e; --purple:#8250df;
      --green-rgb:26,127,55; --red-rgb:207,34,46;
    }
  }
  :root[data-theme="light"] {
    --bg:#f6f8fa; --panel:#ffffff; --panel-alt:#f0f3f6; --border:#d0d7de; --text:#1f2328; --muted:#656d76;
    --blue:#0969da; --green:#1a7f37; --amber:#9a6700; --red:#cf222e; --purple:#8250df;
    --green-rgb:26,127,55; --red-rgb:207,34,46;
  }
  * { box-sizing:border-box; margin:0; padding:0; }
  body { background:var(--bg); color:var(--text); line-height:1.45; padding:20px 16px 60px;
    font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,Helvetica,Arial,sans-serif; }
  main { max-width:1500px; margin:0 auto; }
  header { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:18px 22px; margin-bottom:18px; }
  h1 { font-size:1.4rem; letter-spacing:-0.02em; }
  header p { color:var(--muted); font-size:0.88rem; margin-top:4px; }
  h2 { font-size:1.05rem; margin-bottom:4px; }
  h3 { font-size:0.8rem; color:var(--muted); text-transform:uppercase; letter-spacing:0.06em; margin:18px 0 8px; }
  .sub { color:var(--muted); font-size:0.83rem; margin-bottom:12px; }
  section { background:var(--panel); border:1px solid var(--border); border-radius:10px; padding:18px 20px; margin-bottom:18px; }
  .grid2 { display:grid; grid-template-columns:repeat(auto-fit,minmax(min(100%,430px),1fr)); gap:18px; margin-bottom:18px; }
  .grid2 > section { margin-bottom:0; min-width:0; }
  .cards { display:grid; grid-template-columns:repeat(auto-fill,minmax(230px,1fr)); gap:12px; }
  .card { background:var(--panel-alt); border:1px solid var(--border); border-radius:8px; padding:12px 14px; cursor:pointer; text-align:left;
    color:inherit; font:inherit; }
  .card:hover, .card.active { border-color:var(--blue); }
  .card .name { font-weight:650; font-size:0.93rem; display:flex; justify-content:space-between; gap:8px; align-items:flex-start; }
  .card .nums { display:flex; gap:14px; margin-top:8px; font-variant-numeric:tabular-nums; }
  .card .nums div { font-size:0.95rem; font-weight:600; }
  .card .nums span { display:block; font-size:0.68rem; color:var(--muted); font-weight:400; text-transform:uppercase; }
  .card .foot { display:flex; justify-content:space-between; align-items:center; margin-top:8px; font-size:0.74rem; color:var(--muted); }
  .chip { font-size:0.66rem; padding:1px 7px; border-radius:999px; border:1px solid var(--border); color:var(--muted); white-space:nowrap; font-weight:500; }
  .chip.proxy { color:var(--amber); border-color:var(--amber); } .chip.model { color:var(--purple); border-color:var(--purple); }
  .chip.high { color:var(--red); border-color:var(--red); } .chip.medium { color:var(--amber); border-color:var(--amber); }
  .pos { color:var(--green); } .neg { color:var(--red); } .muted { color:var(--muted); }
  .scroll { overflow-x:auto; }
  .scroll.tall { max-height:520px; overflow-y:auto; }
  .scroll.tall th { position:sticky; top:0; background:var(--panel); }
  table { width:100%; border-collapse:collapse; font-size:0.84rem; font-variant-numeric:tabular-nums; }
  th, td { padding:6px 9px; border-bottom:1px solid var(--border); text-align:right; white-space:nowrap; }
  th:first-child, td:first-child, th.l, td.l { text-align:left; }
  td.wrap { white-space:normal; min-width:220px; }
  th { color:var(--muted); font-weight:600; font-size:0.76rem; text-transform:uppercase; letter-spacing:0.03em; }
  th[data-k] { cursor:pointer; user-select:none; } th[data-k]:hover { color:var(--text); }
  .controls { display:flex; flex-wrap:wrap; gap:8px; align-items:center; margin-bottom:12px; }
  select, button.opt, input { background:var(--panel-alt); color:var(--text); border:1px solid var(--border); border-radius:6px;
    padding:6px 10px; font:inherit; font-size:0.85rem; }
  button.opt { cursor:pointer; } button.opt.active { border-color:var(--blue); color:var(--blue); }
  .legend { display:flex; flex-wrap:wrap; gap:6px 12px; margin-top:8px; font-size:0.78rem; }
  .legend button { background:none; border:none; color:var(--text); cursor:pointer; font:inherit; display:flex; align-items:center; gap:5px; padding:1px 0; }
  .legend button.off { opacity:0.35; }
  .legend i { width:10px; height:10px; border-radius:2px; display:inline-block; }
  .chart { position:relative; width:100%; }
  .chart svg { width:100%; height:auto; display:block; }
  .tip { position:absolute; pointer-events:none; background:var(--panel-alt); border:1px solid var(--border); border-radius:6px; padding:6px 9px;
    font-size:0.76rem; display:none; z-index:5; max-width:240px; font-variant-numeric:tabular-nums; }
  .bars { display:grid; grid-template-columns:minmax(90px,max-content) 1fr max-content; gap:5px 10px; align-items:center; font-size:0.84rem; }
  .bars .track { position:relative; height:16px; background:var(--panel-alt); border-radius:3px; min-width:80px; }
  .bars .track b { position:absolute; top:0; bottom:0; border-radius:3px; }
  .bars .track::after { content:""; position:absolute; left:50%; top:-2px; bottom:-2px; width:1px; background:var(--border); }
  .bars .track.zero::after { left:0; }
  .bars .v { font-variant-numeric:tabular-nums; font-weight:600; text-align:right; }
  .kv { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:10px; }
  .kv div { background:var(--panel-alt); border:1px solid var(--border); border-radius:8px; padding:10px 12px; }
  .kv div > span { display:block; color:var(--muted); font-size:0.72rem; text-transform:uppercase; letter-spacing:0.04em; }
  .kv b { font-size:1.15rem; font-variant-numeric:tabular-nums; } .kv small { display:block; color:var(--muted); font-size:0.74rem; }
  .note { color:var(--muted); font-size:0.8rem; margin-top:10px; }
  ul.notes { list-style:none; font-size:0.84rem; } ul.notes li { padding:6px 0; border-bottom:1px solid var(--border); }
  .empty { color:var(--muted); font-size:0.85rem; padding:8px 0; }
</style>
</head>
<body>
<main>
<header>
  <h1>Asset-Class Watch</h1>
  <p id="hdr"></p>
</header>

<section>
  <h2>Scoreboard</h2>
  <p class="sub">Median total return of each class's tracked instruments. Breadth is the share trading above their 50-day average; the line is an equal-weighted 90-day index. Select a card to open it in the explorer.</p>
  <div id="groups"></div>
</section>

<section>
  <h2>Return heatmap</h2>
  <p class="sub">Median total return (%) by horizon. Colour intensity is scaled within each column.</p>
  <div class="scroll"><table id="heat"></table></div>
</section>

<section id="explorer">
  <h2>Class explorer</h2>
  <p class="sub" id="classNote"></p>
  <div class="controls">
    <select id="classSel" aria-label="Asset class"></select>
    <span id="winBtns"></span>
    <button class="opt" id="modeBtn"></button>
  </div>
  <div class="chart" id="lineChart"><div class="tip" id="tip"></div></div>
  <div class="legend" id="legend"></div>
  <h3>Instruments</h3>
  <div class="scroll"><table id="instTable"></table></div>
</section>

<div class="grid2">
  <section>
    <h2>Rates: Treasury curve vs implied swaps</h2>
    <p class="sub">Treasury par yields, T-bill coupon-equivalent yields and par OIS swap rates implied from the SOFR futures strip (model, no convexity adjustment).</p>
    <div class="chart" id="curveChart"></div>
    <div class="legend" id="curveLegend"></div>
    <h3>Swap spreads and policy path</h3>
    <div class="kv" id="swapKv"></div>
  </section>
  <section>
    <h2>Forward rates</h2>
    <p class="sub">Three-month SOFR implied by each dated futures contract, by start of the reference quarter.</p>
    <div class="chart" id="fwdChart"></div>
    <h3>Cash yields</h3>
    <div class="bars" id="cashBars"></div>
  </section>
</div>

<div class="grid2">
  <section>
    <h2>Options</h2>
    <p class="sub">Implied-volatility term structure and positioning from Cboe delayed option chains.</p>
    <div class="bars" id="volBars"></div>
    <div class="kv" id="volKv" style="margin-top:12px"></div>
    <h3>Chain statistics</h3>
    <div class="scroll"><table id="optTable"></table></div>
    <p class="note">25-delta skew is put IV minus call IV; positive means downside protection costs more.</p>
  </section>
  <section>
    <h2>Futures &amp; forwards</h2>
    <p class="sub">12-month futures curve slope: positive is contango (far contract above near), negative is backwardation.</p>
    <div class="bars" id="termBars"></div>
    <h3>Dated future vs spot</h3>
    <div class="scroll"><table id="basisTable"></table></div>
    <p class="note">The annualised premium is the futures-implied financing rate net of income; for FX it is the interest-rate differential. Closes are not time-synchronised.</p>
  </section>
</div>

<div class="grid2">
  <section>
    <h2>Index funds: tracking gap</h2>
    <p class="sub">One-year total return of each index mutual fund minus the ETF on the same benchmark (percentage points).</p>
    <div class="bars" id="trackBars"></div>
  </section>
  <section>
    <h2>Mutual funds: excess return</h2>
    <p class="sub">One-year total return of each active fund minus its passive benchmark ETF (percentage points).</p>
    <div class="bars" id="excessBars"></div>
    <div class="kv" id="mfKv" style="margin-top:12px"></div>
  </section>
</div>

<div class="grid2">
  <section>
    <h2>Annuities</h2>
    <p class="sub">Model estimate: actuarially fair life-only payout from the Treasury curve and Gompertz mortality. Real quotes are lower after insurer loads.</p>
    <div class="scroll"><table id="annTable"></table></div>
    <div class="kv" id="annKv" style="margin-top:12px"></div>
  </section>
  <section>
    <h2>Real estate, commodities, alternatives and hybrids</h2>
    <p class="sub">Class-specific ratios and relative returns.</p>
    <div class="scroll"><table id="miscTable"></table></div>
  </section>
</div>

<section>
  <h2>Alerts</h2>
  <p class="sub" id="alertSub"></p>
  <div class="scroll"><table id="alertTable"></table></div>
</section>

<section>
  <h2>All derived metrics</h2>
  <div class="controls"><select id="metricSel" aria-label="Filter metrics by class"></select><input id="metricQ" placeholder="Filter by name" aria-label="Filter metrics by name" /></div>
  <div class="scroll tall"><table id="metricTable"></table></div>
</section>

<section>
  <h2>Coverage</h2>
  <p class="sub"><b>direct</b> = quoted instruments of the class itself · <b>proxy</b> = listed stand-ins for an OTC or private market · <b>model</b> = computed estimate, not a quote.</p>
  <ul class="notes" id="notes"></ul>
</section>
</main>

<script>
const D = __PAYLOAD__;
const $ = id => document.getElementById(id);
const esc = s => String(s ?? "").replace(/[&<>"]/g, c => ({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]));
const num = (v, d = 2) => v == null ? "—" : v.toLocaleString("en-US", {minimumFractionDigits: d, maximumFractionDigits: d});
const signed = (v, d = 2, suf = "%") => v == null ? '<span class="muted">—</span>' :
  `<span class="${v > 0 ? "pos" : v < 0 ? "neg" : ""}">${v > 0 ? "+" : ""}${num(v, d)}${suf}</span>`;
const price = v => v == null ? "—" : Math.abs(v) >= 1000 ? num(v, 0) : Math.abs(v) >= 10 ? num(v, 2) : num(v, 4);
const M = Object.fromEntries(D.metrics.map(m => [m.metric_id, m]));
const mv = id => M[id] ? M[id].value : null;
const withPrefix = p => D.metrics.filter(m => m.metric_id.startsWith(p) && m.value != null);
const css = v => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function fmtMetric(m) {
  const v = m.value; if (v == null) return "—";
  switch (m.unit) {
    case "pct": return num(v, 2) + "%";
    case "bps": return (v > 0 ? "+" : "") + num(v, 0) + " bps";
    case "pts": return (v > 0 ? "+" : "") + num(v, 2) + " pts";
    case "usd": return Math.abs(v) >= 1e9 ? "$" + num(v / 1e9, 1) + "B" : "$" + num(v, 2);
    case "contracts": return num(v, 0);
    case "count": return (v > 0 ? "+" : "") + num(v, 1);
    default: return num(v, 3);
  }
}

$("hdr").textContent = `Run date ${D.latest_date} · collected ${D.timestamp_utc} · ${D.rows.length} instruments across ${D.summaries.length} asset classes · ${D.metrics.length} derived metrics · ${D.alerts.length} alerts`;

/* ---------- generic renderers ---------- */
function spark(values, w = 96, h = 26) {
  if (!values || values.length < 2) return "";
  const lo = Math.min(...values), hi = Math.max(...values), r = hi - lo || 1;
  const pts = values.map((v, i) => `${(i / (values.length - 1) * w).toFixed(1)},${(h - 2 - (v - lo) / r * (h - 4)).toFixed(1)}`).join(" ");
  const up = values[values.length - 1] >= values[0];
  return `<svg width="${w}" height="${h}" viewBox="0 0 ${w} ${h}" aria-hidden="true"><polyline points="${pts}" fill="none" stroke="var(--${up ? "green" : "red"})" stroke-width="1.5"/></svg>`;
}
/* items: [{label, value, text}] ; diverging around zero unless zeroBased */
function bars(el, items, opts = {}) {
  if (!items.length) { el.innerHTML = '<div class="empty" style="grid-column:1/-1">No data in this run.</div>'; return; }
  const scale = opts.scale || Math.max(...items.map(i => Math.abs(i.value))) || 1;
  el.innerHTML = items.map(i => {
    const frac = Math.min(Math.abs(i.value) / scale, 1);
    let style, color = opts.color || (i.value >= 0 ? "var(--green)" : "var(--red)");
    if (opts.zeroBased) style = `left:0;width:${(frac * 100).toFixed(1)}%`;
    else style = i.value >= 0 ? `left:50%;width:${(frac * 50).toFixed(1)}%` : `right:50%;width:${(frac * 50).toFixed(1)}%`;
    return `<div title="${esc(i.title || "")}">${esc(i.label)}</div><div class="track${opts.zeroBased ? " zero" : ""}"><b style="${style};background:${color}"></b></div><div class="v">${i.text}</div>`;
  }).join("");
}
function kv(el, items) {
  el.innerHTML = items.filter(i => i.value != null).map(i => `<div><span>${esc(i.label)}</span><b>${i.text}</b>${i.small ? `<small>${esc(i.small)}</small>` : ""}</div>`).join("");
}
/* series: [{name,color,pts:[[x,y],...]}] ; xTicks: [[x,label]] ; xMap optional transform */
function lineChart(el, series, opts = {}) {
  const W = opts.width || 900, H = opts.height || 360, m = {l: 52, r: 14, t: 10, b: 28};
  const xm = opts.xMap || (x => x);
  const all = series.flatMap(s => s.pts);
  el.querySelectorAll("svg").forEach(s => s.remove());
  if (!all.length) { el.insertAdjacentHTML("beforeend", '<svg viewBox="0 0 900 60"><text x="10" y="30" fill="currentColor" font-size="14">No data in this run.</text></svg>'); return null; }
  let x0 = Math.min(...all.map(p => xm(p[0]))), x1 = Math.max(...all.map(p => xm(p[0])));
  if (opts.xDomain) { x0 = xm(opts.xDomain[0]); x1 = xm(opts.xDomain[1]); }
  let y0 = Math.min(...all.map(p => p[1])), y1 = Math.max(...all.map(p => p[1]));
  const pad = (y1 - y0) * 0.06 || 0.5; y0 -= pad; y1 += pad;
  const sx = x => m.l + (xm(x) - x0) / ((x1 - x0) || 1) * (W - m.l - m.r);
  const sy = y => H - m.b - (y - y0) / (y1 - y0) * (H - m.t - m.b);
  let g = "";
  for (let k = 0; k <= 5; k++) {
    const yv = y0 + (y1 - y0) * k / 5;
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(yv)}" y2="${sy(yv)}" stroke="var(--border)" stroke-width="1"/>`;
    g += `<text x="${m.l - 6}" y="${sy(yv) + 4}" text-anchor="end" font-size="11" fill="var(--muted)">${(opts.yFmt || (v => v.toFixed(1)))(yv)}</text>`;
  }
  if (opts.baseline != null && opts.baseline > y0 && opts.baseline < y1)
    g += `<line x1="${m.l}" x2="${W - m.r}" y1="${sy(opts.baseline)}" y2="${sy(opts.baseline)}" stroke="var(--muted)" stroke-dasharray="4 4"/>`;
  (opts.xTicks || []).forEach(([x, lab]) => { g += `<text x="${sx(x)}" y="${H - 8}" text-anchor="middle" font-size="11" fill="var(--muted)">${esc(lab)}</text>`; });
  series.forEach(s => {
    const d = s.pts.map((p, i) => `${i ? "L" : "M"}${sx(p[0]).toFixed(1)},${sy(p[1]).toFixed(1)}`).join("");
    g += `<path d="${d}" fill="none" stroke="${s.color}" stroke-width="${opts.strokeWidth || 1.6}" stroke-linejoin="round"/>`;
    if (opts.dots) s.pts.forEach(p => { g += `<circle cx="${sx(p[0]).toFixed(1)}" cy="${sy(p[1]).toFixed(1)}" r="3.5" fill="${s.color}"><title>${esc(s.name)}: ${p[1].toFixed(2)}${opts.unit || ""}${p[2] ? " (" + esc(p[2]) + ")" : ""}</title></circle>`; });
  });
  g += `<line id="${el.id}-cross" x1="0" x2="0" y1="${m.t}" y2="${H - m.b}" stroke="var(--muted)" stroke-width="1" visibility="hidden"/>`;
  el.insertAdjacentHTML("beforeend", `<svg viewBox="0 0 ${W} ${H}" role="img" aria-label="${esc(opts.label || "chart")}">${g}</svg>`);
  return {W, H, m, sx, x0, x1};
}

/* ---------- scoreboard ---------- */
const GROUPS = __GROUPS__;
$("groups").innerHTML = GROUPS.map(g => {
  const ms = D.summaries.filter(s => s.group === g);
  if (!ms.length) return "";
  return `<h3>${esc(g)}</h3><div class="cards">` + ms.map(s => `
    <button class="card" data-c="${s.class_id}">
      <div class="name"><span>${esc(s.asset_class)}</span><span class="chip ${s.coverage}">${s.coverage}</span></div>
      <div class="nums"><div><span>1d</span>${signed(s.median_change_1d_pct)}</div><div><span>1m</span>${signed(s.median_change_1m_pct)}</div><div><span>YTD</span>${signed(s.median_change_ytd_pct, 1)}</div></div>
      <div class="foot"><span>${s.n_instruments} tracked · ${s.pct_above_50dma == null ? "—" : num(s.pct_above_50dma, 0) + "%"} above 50d · ${esc(s.trend)}</span>${spark(s.index_90d)}</div>
    </button>`).join("") + "</div>";
}).join("");
document.querySelectorAll(".card").forEach(c => c.addEventListener("click", () => { selectClass(c.dataset.c); $("explorer").scrollIntoView({behavior: "smooth"}); }));

/* ---------- heatmap ---------- */
const HZ = __HORIZONS__;
(function () {
  const maxes = HZ.map(([, k]) => Math.max(...D.summaries.map(s => Math.abs(s[k] ?? 0))) || 1);
  let h = `<tr><th>Asset class</th><th class="l">Group</th>${HZ.map(([l]) => `<th>${l}</th>`).join("")}<th>Above 50d</th><th>Best 1d</th><th>Worst 1d</th></tr>`;
  D.summaries.forEach(s => {
    h += `<tr><td>${esc(s.asset_class)}</td><td class="l muted">${esc(s.group)}</td>` + HZ.map(([, k], j) => {
      const v = s[k]; if (v == null) return "<td>—</td>";
      const a = (0.12 + 0.6 * Math.abs(v) / maxes[j]).toFixed(2);
      return `<td style="background:rgba(var(--${v >= 0 ? "green" : "red"}-rgb),${a})">${v > 0 ? "+" : ""}${num(v, 2)}</td>`;
    }).join("") + `<td>${s.pct_above_50dma == null ? "—" : num(s.pct_above_50dma, 0) + "%"}</td>
      <td>${esc(s.best_1d_id)} ${signed(s.best_1d_pct)}</td><td>${esc(s.worst_1d_id)} ${signed(s.worst_1d_pct)}</td></tr>`;
  });
  $("heat").innerHTML = h;
})();

/* ---------- class explorer ---------- */
const state = {cls: D.summaries.length ? D.summaries[0].class_id : null, win: 126, indexed: true, hidden: new Set(), sortK: "z_score_20d", sortDir: -1};
const WINDOWS = [["1M", 21], ["3M", 63], ["6M", 126], ["1Y", 262]];
$("classSel").innerHTML = D.summaries.map(s => `<option value="${s.class_id}">${esc(s.asset_class)}</option>`).join("");
$("classSel").addEventListener("change", e => selectClass(e.target.value));
$("winBtns").innerHTML = WINDOWS.map(([l, n]) => `<button class="opt" data-n="${n}">${l}</button>`).join(" ");
$("winBtns").querySelectorAll("button").forEach(b => b.addEventListener("click", () => { state.win = +b.dataset.n; drawExplorer(); }));
$("modeBtn").addEventListener("click", () => { state.indexed = !state.indexed; drawExplorer(); });
function selectClass(c) { state.cls = c; state.hidden = new Set(); $("classSel").value = c; drawExplorer(); }
const colorFor = (i, n) => `hsl(${Math.round(i * 360 / Math.max(n, 1))},65%,${matchMedia("(prefers-color-scheme: light)").matches ? 40 : 62}%)`;
let chartGeom = null, chartSeries = [], chartDates = [];
function drawExplorer() {
  if (!state.cls) return;
  const s = D.summaries.find(x => x.class_id === state.cls);
  $("classNote").textContent = s.note || "";
  document.querySelectorAll(".card").forEach(c => c.classList.toggle("active", c.dataset.c === state.cls));
  $("winBtns").querySelectorAll("button").forEach(b => b.classList.toggle("active", +b.dataset.n === state.win));
  $("modeBtn").textContent = state.indexed ? "Showing: indexed to 100" : "Showing: raw level";
  const rows = D.rows.filter(r => r.class_id === state.cls);
  const start = Math.max(D.dates.length - state.win, 0);
  chartDates = D.dates.slice(start);
  chartSeries = rows.map((r, i) => {
    const vals = (D.series[r.asset_id] || []).slice(start);
    const base = vals.find(v => v != null);
    const pts = [];
    vals.forEach((v, j) => { if (v != null) pts.push([j, state.indexed ? v / base * 100 : v]); });
    return {id: r.asset_id, name: `${r.asset_id} · ${r.asset_name}`, color: colorFor(i, rows.length), pts};
  }).filter(x => x.pts.length > 1);
  const shown = chartSeries.filter(x => !state.hidden.has(x.id));
  const ticks = []; const step = Math.max(Math.floor(chartDates.length / 6), 1);
  for (let j = 0; j < chartDates.length; j += step) ticks.push([j, chartDates[j].slice(2)]);
  chartGeom = lineChart($("lineChart"), shown, {xTicks: ticks, xDomain: [0, Math.max(chartDates.length - 1, 1)], baseline: state.indexed ? 100 : null,
    yFmt: v => Math.abs(v) >= 1000 ? v.toFixed(0) : v.toFixed(1), label: `${s.asset_class} price history`});
  $("legend").innerHTML = chartSeries.map(x => `<button data-id="${esc(x.id)}" class="${state.hidden.has(x.id) ? "off" : ""}"><i style="background:${x.color}"></i>${esc(x.id)}</button>`).join("");
  $("legend").querySelectorAll("button").forEach(b => b.addEventListener("click", () => {
    state.hidden.has(b.dataset.id) ? state.hidden.delete(b.dataset.id) : state.hidden.add(b.dataset.id); drawExplorer(); }));
  drawInstTable(rows);
}
$("lineChart").addEventListener("mousemove", e => {
  const svg = $("lineChart").querySelector("svg"); if (!svg || !chartGeom) return;
  const box = svg.getBoundingClientRect(), g = chartGeom;
  const vx = (e.clientX - box.left) / box.width * g.W;
  const j = Math.round((vx - g.m.l) / (g.W - g.m.l - g.m.r) * (chartDates.length - 1));
  const cross = $("lineChart-cross"), tip = $("tip");
  if (j < 0 || j >= chartDates.length) { tip.style.display = "none"; if (cross) cross.setAttribute("visibility", "hidden"); return; }
  if (cross) { cross.setAttribute("x1", g.sx(j)); cross.setAttribute("x2", g.sx(j)); cross.setAttribute("visibility", "visible"); }
  const vals = chartSeries.filter(x => !state.hidden.has(x.id)).map(x => { const p = x.pts.find(p => p[0] === j); return p ? [x, p[1]] : null; })
    .filter(Boolean).sort((a, b) => b[1] - a[1]);
  const top = vals.length > 10 ? vals.slice(0, 5).concat(vals.slice(-5)) : vals;
  tip.innerHTML = `<b>${chartDates[j]}</b><br>` + top.map(([x, v]) => `<span style="color:${x.color}">■</span> ${esc(x.id)} ${num(v, 2)}`).join("<br>")
    + (vals.length > 10 ? `<br><span class="muted">top and bottom 5 of ${vals.length}</span>` : "");
  tip.style.display = "block";
  const left = e.clientX - box.left + 14;
  tip.style.left = Math.min(left, box.width - 190) + "px"; tip.style.top = "8px";
});
$("lineChart").addEventListener("mouseleave", () => { $("tip").style.display = "none"; const c = $("lineChart-cross"); if (c) c.setAttribute("visibility", "hidden"); });
const COLS = [["asset_id", "Ticker"], ["asset_name", "Name"], ["sub_class", "Type"], ["price", "Price"], ["change_1d_pct", "1d"], ["change_5d_pct", "5d"],
  ["change_1m_pct", "1m"], ["change_3m_pct", "3m"], ["change_ytd_pct", "YTD"], ["change_1y_pct", "1y"], ["z_score_20d", "Z 20d"],
  ["vol_ratio_20d", "Vol ×"], ["realized_vol_30d_pct", "Vol 30d"], ["drawdown_52w_pct", "From high"]];
function drawInstTable(rows) {
  const k = state.sortK, dir = state.sortDir;
  const sorted = rows.slice().sort((a, b) => {
    const x = a[k], y = b[k];
    if (x == null) return 1; if (y == null) return -1;
    if (typeof x === "string") return x.localeCompare(y) * dir;
    return (k === "z_score_20d" ? Math.abs(x) - Math.abs(y) : x - y) * dir;
  });
  const start = Math.max(D.dates.length - 90, 0);
  let h = "<tr>" + COLS.map(([key, l], i) => `<th data-k="${key}" class="${i < 3 ? "l" : ""}">${l}${key === k ? (dir < 0 ? " ▾" : " ▴") : ""}</th>`).join("") + "<th>90d</th></tr>";
  sorted.forEach(r => {
    const z = r.z_score_20d;
    h += `<tr><td><b>${esc(r.asset_id)}</b></td><td class="l">${esc(r.asset_name)}</td><td class="l muted">${esc(r.sub_class)}</td>
      <td>${price(r.price)}${r.unit === "pct" ? "%" : ""}</td><td>${signed(r.change_1d_pct)}</td><td>${signed(r.change_5d_pct)}</td><td>${signed(r.change_1m_pct)}</td>
      <td>${signed(r.change_3m_pct)}</td><td>${signed(r.change_ytd_pct, 1)}</td><td>${signed(r.change_1y_pct, 1)}</td>
      <td style="${z != null && Math.abs(z) >= 2.5 ? "font-weight:700" : ""}">${signed(z, 2, "")}</td>
      <td>${r.vol_ratio_20d == null ? "—" : num(r.vol_ratio_20d, 2) + "×"}</td><td>${r.realized_vol_30d_pct == null ? "—" : num(r.realized_vol_30d_pct, 1) + "%"}</td>
      <td>${r.drawdown_52w_pct == null ? "—" : num(r.drawdown_52w_pct, 1) + "%"}</td>
      <td>${spark((D.series[r.asset_id] || []).slice(start).filter(v => v != null), 84, 22)}</td></tr>`;
  });
  $("instTable").innerHTML = h;
  $("instTable").querySelectorAll("th[data-k]").forEach(th => th.addEventListener("click", () => {
    if (state.sortK === th.dataset.k) state.sortDir *= -1; else { state.sortK = th.dataset.k; state.sortDir = -1; }
    drawInstTable(rows);
  }));
}
drawExplorer();

/* ---------- rates & swaps ---------- */
(function () {
  const TEN = {"1m": 1 / 12, "2m": 2 / 12, "3m": .25, "4m": 4 / 12, "6m": .5, "1y": 1, "2y": 2, "3y": 3, "5y": 5, "7y": 7, "10y": 10, "20y": 20, "30y": 30};
  const tsy = withPrefix("ust_par_").map(m => [TEN[m.metric_id.slice(8)], m.value, m.metric_id.slice(8).toUpperCase()]).filter(p => p[0]).sort((a, b) => a[0] - b[0]);
  const swp = withPrefix("sofr_swap_implied_").map(m => { const t = parseFloat(m.metric_id.split("_")[3]); return [t, m.value, t + "Y"]; }).sort((a, b) => a[0] - b[0]);
  const bil = withPrefix("tbill_").map(m => { const w = parseInt(m.metric_id.split("_")[1]); return [w / 52, m.value, w + "W"]; }).sort((a, b) => a[0] - b[0]);
  const series = [{name: "Treasury par curve", color: css("--blue"), pts: tsy}, {name: "Implied SOFR OIS swaps (model)", color: css("--amber"), pts: swp},
    {name: "T-bill coupon-equivalent", color: css("--purple"), pts: bil}].filter(s => s.pts.length);
  lineChart($("curveChart"), series, {width: 620, height: 330, xMap: Math.sqrt, dots: true, unit: "%", strokeWidth: 2.2, yFmt: v => v.toFixed(2) + "%",
    xTicks: [[.25, "3M"], [1, "1Y"], [2, "2Y"], [5, "5Y"], [10, "10Y"], [20, "20Y"], [30, "30Y"]], label: "Treasury and swap curves"});
  $("curveLegend").innerHTML = series.map(s => `<span><i style="background:${s.color}"></i> ${esc(s.name)}</span>`).join("");
  const items = [];
  [1, 2, 3, 5].forEach(t => { if (mv(`sofr_swap_implied_${t}y_pct`) != null) items.push({label: `${t}Y implied swap`, value: mv(`sofr_swap_implied_${t}y_pct`),
    text: num(mv(`sofr_swap_implied_${t}y_pct`), 2) + "%", small: mv(`swap_spread_${t}y_bps`) == null ? "" : `spread to Treasury ${mv(`swap_spread_${t}y_bps`) > 0 ? "+" : ""}${num(mv(`swap_spread_${t}y_bps`), 0)} bps`}); });
  [["2s10s slope", "curve_2s10s_bps"], ["3m10y slope", "curve_3m10y_bps"]].forEach(([l, id]) => { if (mv(id) != null) items.push({label: l, value: mv(id), text: (mv(id) > 0 ? "+" : "") + num(mv(id), 0) + " bps"}); });
  if (mv("fed_cuts_priced_12m") != null) { const n = mv("fed_cuts_priced_12m"); items.push({label: "Fed path, 12 months", value: n, text: `${num(Math.abs(n), 1)} ${n > 0 ? "cuts" : "hikes"}`, small: "25 bp moves priced by Fed funds futures"}); }
  kv($("swapKv"), items);

  const fwd = withPrefix("sofr_fwd_3m_").map((m, i) => [i, m.value, m.metric_name.replace("Implied 3-month SOFR starting ", "")]);
  const ticks = fwd.filter((p, i) => i % 4 === 0).map(p => [p[0], p[2]]);
  lineChart($("fwdChart"), fwd.length ? [{name: "Implied 3-month SOFR", color: css("--blue"), pts: fwd}] : [], {dots: true, unit: "%", strokeWidth: 2.2, width: 620, height: 280,
    yFmt: v => v.toFixed(2) + "%", xTicks: ticks, baseline: mv("sofr_pct"), label: "Implied forward SOFR"});
  const cash = [];
  [["SOFR", "sofr_pct"], ["Fed funds (EFFR)", "effr_pct"], ["4-week bill", "tbill_4w_yield_pct"], ["8-week bill", "tbill_8w_yield_pct"], ["13-week bill", "tbill_13w_yield_pct"],
   ["26-week bill", "tbill_26w_yield_pct"], ["52-week bill", "tbill_52w_yield_pct"]].forEach(([l, id]) => { if (mv(id) != null) cash.push({label: l, value: mv(id), text: num(mv(id), 2) + "%", title: M[id].source}); });
  bars($("cashBars"), cash, {zeroBased: true, color: "var(--blue)"});
})();

/* ---------- options ---------- */
(function () {
  const vol = D.rows.filter(r => r.class_id === "options");
  const order = ["^VIX9D", "^VIX", "^VIX3M", "^VIX6M", "^VXN", "^VXD", "^OVX", "^GVZ", "^MOVE", "^VVIX", "^SKEW"];
  const items = vol.filter(r => !["^MOVE", "^VVIX", "^SKEW"].includes(r.asset_id)).sort((a, b) => order.indexOf(a.asset_id) - order.indexOf(b.asset_id))
    .map(r => ({label: r.asset_id.replace("^", ""), value: r.price, text: `${num(r.price, 2)} ${signed(r.change_1d_pct)}`, title: r.asset_name}));
  bars($("volBars"), items, {zeroBased: true, color: "var(--blue)"});
  const k = [];
  if (mv("vix_term_ratio") != null) k.push({label: "VIX / VIX3M", value: 1, text: num(mv("vix_term_ratio"), 3), small: mv("vix_term_ratio") > 1 ? "backwardation: near-term stress" : "contango: normal"});
  if (mv("vix_percentile_1y_pct") != null) k.push({label: "VIX 1-year percentile", value: 1, text: num(mv("vix_percentile_1y_pct"), 0) + "%", small: "IV rank"});
  ["^VVIX", "^SKEW", "^MOVE"].forEach(t => { const r = vol.find(x => x.asset_id === t); if (r) k.push({label: t.replace("^", ""), value: 1, text: num(r.price, 1), small: r.asset_name}); });
  kv($("volKv"), k);
  const syms = withPrefix("put_call_volume_ratio_").map(m => m.metric_id.split("_").pop());
  let h = "<tr><th>Underlying</th><th>P/C volume</th><th>P/C open interest</th><th>IV30</th><th>ATM IV</th><th>25Δ skew</th><th>Contracts</th></tr>";
  syms.forEach(s => {
    const f = (id, d, suf = "") => mv(id) == null ? "—" : num(mv(id), d) + suf;
    h += `<tr><td><b>${s.toUpperCase()}</b></td><td>${f("put_call_volume_ratio_" + s, 2)}</td><td>${f("put_call_oi_ratio_" + s, 2)}</td><td>${f(`iv30_${s}_pct`, 1, "%")}</td>
      <td>${f(`atm_iv_${s}_pct`, 1, "%")}</td><td>${mv(`skew_25d_${s}_pts`) == null ? "—" : signed(mv(`skew_25d_${s}_pts`), 1, " pts")}</td><td>${f("option_volume_" + s, 0)}</td></tr>`;
  });
  $("optTable").innerHTML = syms.length ? h : '<tr><td class="empty">No option-chain data in this run.</td></tr>';
})();

/* ---------- futures & forwards ---------- */
(function () {
  bars($("termBars"), withPrefix("term_structure_12m_").map(m => ({label: m.metric_name.split(":")[0], value: m.value, text: signed(m.value, 2), title: m.source})));
  const basis = withPrefix("basis_");
  let h = "<tr><th>Market</th><th>Future over spot</th><th>Annualised premium</th><th class=\"l\">Contracts</th></tr>";
  basis.forEach(m => { const root = m.metric_id.split("_")[1];
    h += `<tr><td>${esc(m.metric_name.split(":")[0])}</td><td>${signed(m.value, 3)}</td><td>${signed(mv(`implied_carry_${root}_pct`), 2)}</td><td class="l muted">${esc(m.source.replace("yahoo ", ""))}</td></tr>`; });
  $("basisTable").innerHTML = basis.length ? h : '<tr><td class="empty">No dated-futures data in this run.</td></tr>';
})();

/* ---------- funds ---------- */
(function () {
  const name = id => { const r = D.rows.find(x => x.asset_id === id); return r ? r.asset_name : ""; };
  const mk = p => withPrefix(p).map(m => { const [fund, , bench] = m.metric_name.split(" "); return {label: fund, value: m.value, text: signed(m.value, 2, " pts"), title: `${name(fund)} vs ${bench.replace(",", "")}`}; })
    .sort((a, b) => b.value - a.value);
  bars($("trackBars"), mk("tracking_gap_1y_"));
  bars($("excessBars"), mk("excess_return_1y_"));
  const k = [];
  if (mv("funds_beating_benchmark_1y_pct") != null) k.push({label: "Funds ahead of benchmark", value: 1, text: num(mv("funds_beating_benchmark_1y_pct"), 0) + "%", small: M["funds_beating_benchmark_1y_pct"].note});
  if (mv("median_excess_return_1y_pts") != null) k.push({label: "Median excess return", value: 1, text: signed(mv("median_excess_return_1y_pts"), 2, " pts")});
  kv($("mfKv"), k);
})();

/* ---------- annuities + misc ---------- */
(function () {
  const ages = withPrefix("spia_payout_age").map(m => m.metric_id.slice(15, 17));
  let h = "<tr><th>Purchase age</th><th>Payout rate</th><th>Monthly income per $100,000</th></tr>";
  ages.forEach(a => { h += `<tr><td>${a}</td><td>${num(mv(`spia_payout_age${a}_pct`), 2)}%</td><td>$${num(mv(`spia_monthly_income_age${a}_usd`), 0)}</td></tr>`; });
  $("annTable").innerHTML = ages.length ? h : '<tr><td class="empty">No Treasury curve in this run, so no estimate.</td></tr>';
  const s = D.summaries.find(x => x.class_id === "annuities"), k = [];
  if (mv("ust_5y_reference_pct") != null) k.push({label: "5Y Treasury", value: 1, text: num(mv("ust_5y_reference_pct"), 2) + "%", small: "fixed-annuity rate reference"});
  if (s) k.push({label: "Insurer stocks, 1m", value: 1, text: signed(s.median_change_1m_pct), small: `${s.n_instruments} life/annuity insurers and ETFs`});
  if (mv("insurers_vs_sp500_1m_pts") != null) k.push({label: "Insurers vs S&P 500, 1m", value: 1, text: signed(mv("insurers_vs_sp500_1m_pts"), 2, " pts")});
  kv($("annKv"), k);
  const misc = D.metrics.filter(m => ["real_estate", "commodities", "alternatives", "structured", "stocks", "etfs"].includes(m.class_id) && m.value != null);
  $("miscTable").innerHTML = "<tr><th>Metric</th><th class=\"l\">Class</th><th>Value</th></tr>" +
    misc.map(m => `<tr><td class="wrap" title="${esc(m.note)}">${esc(m.metric_name)}</td><td class="l muted">${esc(m.asset_class)}</td><td>${fmtMetric(m)}</td></tr>`).join("");
})();

/* ---------- alerts, metrics, notes ---------- */
(function () {
  const n = {high: 0, medium: 0, info: 0}; D.alerts.forEach(a => n[a.severity] = (n[a.severity] || 0) + 1);
  $("alertSub").textContent = D.alerts.length ? `${n.high} high · ${n.medium} medium · ${n.info} informational` : "No triggers fired in this run.";
  $("alertTable").innerHTML = D.alerts.length ? "<tr><th>Severity</th><th class=\"l\">Asset</th><th class=\"l\">Class</th><th class=\"l\">Signal</th><th class=\"l\">Details</th></tr>" +
    D.alerts.map(a => `<tr><td><span class="chip ${a.severity}">${a.severity}</span></td><td class="l"><b>${esc(a.asset_id)}</b></td><td class="l muted">${esc(a.asset_class)}</td>
      <td class="l">${esc(a.signal_type)}</td><td class="l wrap">${esc(a.details)}</td></tr>`).join("") : "";
  const classes = [...new Map(D.metrics.map(m => [m.class_id, m.asset_class])).entries()];
  $("metricSel").innerHTML = '<option value="">All classes</option>' + classes.map(([id, nm]) => `<option value="${id}">${esc(nm)}</option>`).join("");
  function draw() {
    const c = $("metricSel").value, q = $("metricQ").value.toLowerCase();
    const ms = D.metrics.filter(m => (!c || m.class_id === c) && (!q || m.metric_name.toLowerCase().includes(q)));
    $("metricTable").innerHTML = "<tr><th>Metric</th><th class=\"l\">Class</th><th>Value</th><th class=\"l\">Source</th><th class=\"l\">Note</th></tr>" +
      ms.map(m => `<tr><td>${esc(m.metric_name)}</td><td class="l muted">${esc(m.asset_class)}</td><td>${fmtMetric(m)}</td><td class="l muted">${esc(m.source)}</td><td class="l wrap muted">${esc(m.note)}</td></tr>`).join("");
  }
  $("metricSel").addEventListener("change", draw); $("metricQ").addEventListener("input", draw); draw();
  $("notes").innerHTML = D.summaries.map(s => `<li><b>${esc(s.asset_class)}</b> <span class="chip ${s.coverage}">${s.coverage}</span><br><span class="muted">${esc(s.note)}</span></li>`).join("");
})();
</script>
</body>
</html>
"""


def render_html(ds: Dict[str, Any], out_path: Path) -> None:
    payload = {
        "latest_date": ds["latest_date"],
        "timestamp_utc": ds["timestamp_utc"],
        "rows": ds["rows"],
        "summaries": ds["summaries"],
        "metrics": ds["metrics"],
        "alerts": ds["alerts"],
        "dates": ds["dates"],
        "series": ds["series"],
    }
    blob = json.dumps(payload, separators=(",", ":")).replace("</", "<\\/")
    html = (
        HTML_TEMPLATE.replace("__PAYLOAD__", blob)
        .replace("__GROUPS__", json.dumps(GROUPS))
        .replace("__HORIZONS__", json.dumps(HORIZONS))
        .replace("__DATE__", ds["latest_date"])
    )
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html, encoding="utf-8")


def main() -> int:
    parser = argparse.ArgumentParser(description="Visualize the asset-class watch CSVs in `data/`.")
    parser.add_argument("--data-dir", default="data", help="Data directory (default: data)")
    parser.add_argument("--html", default="reports/asset_classes_dashboard.html", help="Output path for the HTML dashboard")
    parser.add_argument("--png", default="reports/asset_classes_overview.png", help="Output path for the PNG overview")
    parser.add_argument("--quiet", action="store_true", help="Skip the terminal dashboard")
    parser.add_argument("--open", action="store_true", help="Open the HTML dashboard in the default browser")
    args = parser.parse_args()

    data_dir = Path(args.data_dir)
    ds = load_datasets(data_dir)
    if not ds["rows"]:
        print(f"[ERR] No asset-class data in `{data_dir}/asset_classes.csv`. "
              "Run `python3 scripts/watch_asset_classes.py --write-csv` first.", file=sys.stderr)
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
