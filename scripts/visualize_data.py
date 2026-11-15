#!/usr/bin/env python3
"""
Omni-Market Watcher — Data Folder Visualizer (`visualize_data.py`)

Reads all CSV datasets in `data/` (`timeseries.csv`, `prices_wide.csv`, `liquid.csv`,
`macro.csv`, `crypto_derivatives.csv`, `prediction.csv`, `illiquid.csv`, `comps.csv`,
`alerts.csv`) and produces three complementary visualizations:

  1. Terminal Visual Dashboard (always printed unless `--quiet`):
     - Dataset inventory (row counts, date ranges, schema coverage)
     - Macro regime status banner + 90-day Unicode sparklines (US10Y, DXY, VIX, BTC, SPY)
     - Horizontal Z-score bar chart across all 27 liquid assets (`±2.5σ` trigger bands)
     - Prediction market probability bars & 24h point deltas
     - Collectibles 30d FMV & verified eBay comp price dispersion (`comps.csv`)
     - GPU cloud rental venue spreads (Vast.ai vs. RunPod)
     - Cross-asset 30-day return correlation matrix

  2. High-Resolution Multi-Panel PNG Chart (`reports/data_overview.png` via Pillow):
     - 6-panel institutional dark-theme analytics graphic rendered directly with Pillow

  3. Self-Contained Interactive HTML Dashboard (`reports/dashboard.html`):
     - Interactive 90-day time-series explorer (normalized % return or raw price/yield)
     - Filterable Z-score & volume ratio scanner
     - Prediction markets & crypto derivatives monitor
     - Collectible sold-comps distribution & GPU rental arbitrage chart
     - Cross-asset correlation heatmap & full alert log

Usage:
  python3 scripts/visualize_data.py
  python3 scripts/visualize_data.py --data-dir data --html reports/dashboard.html --png reports/data_overview.png
  python3 scripts/visualize_data.py --open
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


# ---------------------------------------------------------------------------
# Data Loading & Processing Helpers
# ---------------------------------------------------------------------------

def load_csv(path: Path) -> List[Dict[str, str]]:
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as fp:
        return list(csv.DictReader(fp))


def to_float(val: Any) -> Optional[float]:
    if val is None or val == "":
        return None
    try:
        v = float(val)
        return v if not (math.isnan(v) or math.isinf(v)) else None
    except (ValueError, TypeError):
        return None


def sparkline(values: List[float], width: int = 28) -> str:
    """Render a compact Unicode sparkline for a numeric series."""
    bars = " ▂▃▄▅▆▇█"
    clean = [v for v in values if v is not None]
    if not clean:
        return ""
    if len(clean) > width:
        step = len(clean) / width
        sampled = [clean[min(len(clean) - 1, int(i * step))] for i in range(width)]
    else:
        sampled = clean
    lo, hi = min(sampled), max(sampled)
    span = hi - lo if hi != lo else 1.0
    return "".join(bars[min(len(bars) - 1, max(0, int((v - lo) / span * (len(bars) - 1))))] for v in sampled)


def z_bar(z: float, half_width: int = 16) -> str:
    """Render a centered ASCII/Unicode bar for a Z-score in [-3.5, +3.5]."""
    clamped = max(-3.5, min(3.5, z))
    norm = int(round((clamped / 3.5) * half_width))
    left = [" "] * half_width
    right = [" "] * half_width
    # Mark -2.5 and +2.5 trigger lines
    trig_pos = int(round((2.5 / 3.5) * half_width))
    if 0 < trig_pos <= half_width:
        left[half_width - trig_pos] = "┆"
        right[trig_pos - 1] = "┆"
    if norm < 0:
        for i in range(half_width + norm, half_width):
            left[i] = "█" if abs(z) >= 2.5 else "▓"
    elif norm > 0:
        for i in range(0, norm):
            right[i] = "█" if abs(z) >= 2.5 else "▒"
    return "".join(left) + "│" + "".join(right)


def pearson_corr(x: List[float], y: List[float]) -> Optional[float]:
    n = len(x)
    if n < 3:
        return None
    mx = sum(x) / n
    my = sum(y) / n
    num = sum((a - mx) * (b - my) for a, b in zip(x, y))
    dx = math.sqrt(sum((a - mx) ** 2 for a in x))
    dy = math.sqrt(sum((b - my) ** 2 for b in y))
    if dx == 0 or dy == 0:
        return 0.0
    return num / (dx * dy)


def compute_correlation_matrix(
    wide_rows: List[Dict[str, str]],
    tickers: List[str],
    window: int = 30,
) -> Dict[str, Dict[str, Optional[float]]]:
    """Compute pairwise daily-return Pearson correlation matrix over trailing `window` overlapping bars."""
    matrix: Dict[str, Dict[str, Optional[float]]] = {t: {} for t in tickers}
    for a in tickers:
        for b in tickers:
            if a == b:
                matrix[a][b] = 1.0
                continue
            pairs: List[Tuple[float, float]] = []
            for r in wide_rows:
                va = to_float(r.get(a))
                vb = to_float(r.get(b))
                if va is not None and vb is not None:
                    pairs.append((va, vb))
            recent = pairs[-(window + 1) :] if len(pairs) >= window + 1 else pairs
            if len(recent) < 4:
                matrix[a][b] = None
                continue
            ret_a = [(recent[i][0] / recent[i - 1][0]) - 1.0 for i in range(1, len(recent)) if recent[i - 1][0] != 0]
            ret_b = [(recent[i][1] / recent[i - 1][1]) - 1.0 for i in range(1, len(recent)) if recent[i - 1][1] != 0]
            rho = pearson_corr(ret_a, ret_b) if len(ret_a) == len(ret_b) else None
            matrix[a][b] = round(rho, 2) if rho is not None else None
    return matrix


def load_all_datasets(data_dir: Path) -> Dict[str, Any]:
    """Load and structure all CSV files in `data/` for visualization."""
    csv_files = sorted(data_dir.glob("*.csv"))
    inventory = []
    for f in csv_files:
        rows = load_csv(f)
        dates = sorted(
            {
                r.get("date") or r.get("date_utc") or r.get("date_collected") or ""
                for r in rows
                if (r.get("date") or r.get("date_utc") or r.get("date_collected"))
            }
        )
        with open(f, "r", encoding="utf-8") as fp:
            cols = next(csv.reader(fp), [])
        inventory.append(
            {
                "file": f.name,
                "rows": len(rows),
                "cols": len(cols),
                "date_min": dates[0] if dates else "—",
                "date_max": dates[-1] if dates else "—",
                "size_kb": round(f.stat().st_size / 1024.0, 1),
            }
        )

    liquid_all = load_csv(data_dir / "liquid.csv")
    macro_all = load_csv(data_dir / "macro.csv")
    derivs_all = load_csv(data_dir / "crypto_derivatives.csv")
    pred_all = load_csv(data_dir / "prediction.csv")
    illiquid_all = load_csv(data_dir / "illiquid.csv")
    comps_all = load_csv(data_dir / "comps.csv")
    alerts_all = load_csv(data_dir / "alerts.csv")
    wide_all = load_csv(data_dir / "prices_wide.csv")
    ts_all = load_csv(data_dir / "timeseries.csv")

    latest_date = max((r["date"] for r in liquid_all if r.get("date")), default="")
    liquid_latest = [r for r in liquid_all if r.get("date") == latest_date]
    macro_latest = next((r for r in reversed(macro_all) if r.get("regime_status")), macro_all[-1] if macro_all else {})
    pred_latest_date = max((r["date"] for r in pred_all if r.get("date")), default="")
    pred_latest = [r for r in pred_all if r.get("date") == pred_latest_date]
    illiquid_latest_date = max((r["date"] for r in illiquid_all if r.get("date")), default="")
    illiquid_latest = [r for r in illiquid_all if r.get("date") == illiquid_latest_date]
    derivs_latest_date = max((r["date"] for r in derivs_all if r.get("date")), default="")
    derivs_latest = [r for r in derivs_all if r.get("date") == derivs_latest_date]
    alerts_latest_date = max((r["date"] for r in alerts_all if r.get("date")), default="")
    alerts_latest = [r for r in alerts_all if r.get("date") == alerts_latest_date]

    # Extract GPU rentals & Spot metals from timeseries.csv on latest date
    ts_latest = [r for r in ts_all if r.get("date") == latest_date]
    gpu_rentals = [r for r in ts_latest if r.get("category") == "GPU rental"]
    spot_metals = [r for r in ts_latest if r.get("category") == "Spot metal"]

    # Group comps by item_id
    comps_by_item: Dict[str, List[Dict[str, Any]]] = {}
    for c in comps_all:
        p = to_float(c.get("sale_price_usd"))
        if p is not None:
            comps_by_item.setdefault(c["item_id"], []).append(
                {
                    "sale_date": c.get("sale_date", ""),
                    "price": p,
                    "grade": c.get("grade_condition", ""),
                    "url": c.get("url", ""),
                }
            )

    corr_tickers = ["SPY", "QQQ", "XLK", "BTC-USD", "ETH-USD", "SOL-USD", "GC=F", "CL=F", "DX-Y.NYB", "US10Y", "^VIX"]
    corr_matrix = compute_correlation_matrix(wide_all, corr_tickers, window=30)

    return {
        "latest_date": latest_date,
        "inventory": inventory,
        "liquid_latest": liquid_latest,
        "macro_all": macro_all,
        "macro_latest": macro_latest,
        "derivs_latest": derivs_latest,
        "pred_latest": pred_latest,
        "illiquid_latest": illiquid_latest,
        "comps_by_item": comps_by_item,
        "alerts_latest": alerts_latest,
        "wide_all": wide_all,
        "gpu_rentals": gpu_rentals,
        "spot_metals": spot_metals,
        "corr_tickers": corr_tickers,
        "corr_matrix": corr_matrix,
    }


# ---------------------------------------------------------------------------
# 1. Terminal Visualization (ASCII / Unicode Dashboard)
# ---------------------------------------------------------------------------

def render_terminal_dashboard(ds: Dict[str, Any]) -> None:
    latest_date = ds["latest_date"]
    macro = ds["macro_latest"]
    wide = ds["wide_all"]

    print("\n" + "═" * 96)
    print(f" 📊 OMNI-MARKET WATCHER — DATA FOLDER VISUALIZATION (Snapshot: {latest_date})")
    print("═" * 96)

    # 1. Inventory
    print("\n┌─ 1. DATA FOLDER INVENTORY (`data/*.csv`) " + "─" * 52)
    print(f"│ {'File':<26} {'Rows':>7} {'Cols':>6} {'Start Date':>12} {'End Date':>12} {'Size (KB)':>10}")
    print("├" + "─" * 94)
    for inv in ds["inventory"]:
        print(
            f"│ {inv['file']:<26} {inv['rows']:>7,d} {inv['cols']:>6d} "
            f"{inv['date_min']:>12} {inv['date_max']:>12} {inv['size_kb']:>9.1f}K"
        )
    print("└" + "─" * 94)

    # 2. Macro Regime & 90-Day Sparklines
    print("\n┌─ 2. MACRO & LIQUIDITY REGIME + 90-DAY TRAJECTORIES " + "─" * 42)
    regime = macro.get("regime_status", "UNKNOWN")
    ro_flag = macro.get("risk_off_flag", "")
    print(
        f"│ Regime: [{regime}] (risk_off_flag={ro_flag}) | "
        f"US2Y: {macro.get('us2y_pct')}% | US10Y: {macro.get('us10y_pct')}% "
        f"({macro.get('us10y_change_48h_bps')} bps/48h) | US30Y: {macro.get('us30y_pct')}%"
    )
    print(
        f"│ DXY: {macro.get('dxy')} (30d high {macro.get('dxy_30d_high')}) | "
        f"VIX: {macro.get('vix')} | USD/JPY: {macro.get('usdjpy')} | "
        f"Stablecoins: ${ (to_float(macro.get('stablecoin_mcap_usd')) or 0)/1e9:,.2f}B"
    )
    print("├" + "─" * 94)
    for tk, label in [
        ("SPY", "S&P 500 (SPY)       "),
        ("QQQ", "Nasdaq-100 (QQQ)    "),
        ("BTC-USD", "Bitcoin (BTC-USD)   "),
        ("ETH-USD", "Ethereum (ETH-USD)  "),
        ("US10Y", "US 10Y Par Yield (%)"),
        ("DX-Y.NYB", "US Dollar Idx (DXY) "),
        ("^VIX", "CBOE VIX            "),
        ("GC=F", "Gold Future (GC=F)  "),
    ]:
        series = [to_float(r.get(tk)) for r in wide if to_float(r.get(tk)) is not None]
        if series:
            lo, hi, cur = min(series), max(series), series[-1]
            chg_90d = ((cur / series[0]) - 1.0) * 100.0 if series[0] else 0.0
            sp = sparkline(series, width=30)
            print(
                f"│ {label} │ {sp:<30} │ Now: {cur:>10,.2f} (90d: {chg_90d:>+6.1f}% | Lo:{lo:,.1f} Hi:{hi:,.1f})"
            )
    print("└" + "─" * 94)

    # 3. Liquid Z-Score Scanner
    print("\n┌─ 3. LIQUID MARKET 20-DAY Z-SCORE & VOLUME BREAKOUT SCANNER (`┆` = ±2.5σ trigger) " + "─" * 12)
    print(f"│ {'Asset':<10} {'Category':<12} {'Price':>11} {'24h %':>8} {'5d %':>8} {'Z-Score':>8} {'VolRatio':>8}  {'-3.5σ       -2.5σ │ +2.5σ       +3.5σ'}")
    print("├" + "─" * 94)
    liq_sorted = sorted(
        ds["liquid_latest"],
        key=lambda r: abs(to_float(r.get("z_score_20d")) or 0.0),
        reverse=True,
    )
    for r in liq_sorted:
        p = to_float(r.get("price")) or 0.0
        c24 = to_float(r.get("change_24h_pct")) or 0.0
        c5d = to_float(r.get("change_5d_pct")) or 0.0
        z = to_float(r.get("z_score_20d")) or 0.0
        vr = to_float(r.get("vol_ratio_20d"))
        vr_str = f"{vr:.2f}x" if vr is not None else "   — "
        bar = z_bar(z, half_width=16)
        print(
            f"│ {r['asset_id']:<10} {r['category']:<12} {p:>11,.2f} {c24:>+7.2f}% {c5d:>+7.2f}% "
            f"{z:>+8.2f} {vr_str:>8}  {bar}"
        )
    print("└" + "─" * 94)

    # 4. Prediction Markets
    print("\n┌─ 4. PREDICTION MARKETS (`data/prediction.csv`) " + "─" * 46)
    print(f"│ {'Venue':<10} {'Prob':>6} {'24h Δ':>9} {'24h Vol ($)':>12}  {'Prob Bar':<20} {'Contract Question'}")
    print("├" + "─" * 94)
    pred_sorted = sorted(
        ds["pred_latest"],
        key=lambda r: abs(to_float(r.get("change_24h_pts")) or 0.0),
        reverse=True,
    )
    for p in pred_sorted[:12]:
        prob = (to_float(p.get("probability")) or 0.0) * 100.0
        chg = to_float(p.get("change_24h_pts")) or 0.0
        vol = to_float(p.get("volume_usd")) or 0.0
        filled = int(round(prob / 5.0))
        pbar = "█" * filled + "░" * (20 - filled)
        q_text = p.get("question", "")[:35]
        print(f"│ {p['venue']:<10} {prob:>5.1f}% {chg:>+8.1f}p ${vol:>11,.0f}  {pbar} {q_text}")
    print("└" + "─" * 94)

    # 5. Collectibles & Verified Sold Comps
    print("\n┌─ 5. ILLIQUID COLLECTIBLES FMV & VERIFIED EBAY SOLD COMPS (`illiquid.csv` + `comps.csv`) " + "─" * 5)
    print(f"│ {'Item ID':<41} {'Cat':<10} {'30d FMV':>10} {'Comps':>5} {'Comp Range (Min–Max)':>22} {'Deal Line':>10}")
    print("├" + "─" * 94)
    for il in ds["illiquid_latest"]:
        iid = il["item_id"]
        fmv = to_float(il.get("fmv_30d_usd"))
        n_c = int(to_float(il.get("n_comps")) or 0)
        item_comps = [c["price"] for c in ds["comps_by_item"].get(iid, [])]
        if fmv is not None:
            c_min = min(item_comps) if item_comps else fmv
            c_max = max(item_comps) if item_comps else fmv
            deal = fmv * 0.78
            print(
                f"│ {iid[:41]:<41} {il['category']:<10} ${fmv:>9,.2f} {n_c:>5d} "
                f"${c_min:>8,.2f} – ${c_max:>8,.2f} <=${deal:>7,.0f}"
            )
        else:
            print(f"│ {iid[:41]:<41} {il['category']:<10} {'UNPRICED':>10} {n_c:>5d} {'(source 403 / n<3)':>22} {'—':>10}")
    print("└" + "─" * 94)

    # 6. GPU Rentals & Crypto Derivatives
    print("\n┌─ 6. AI GPU CLOUD SPOT RENTALS & CRYPTO PERPETUAL SWAPS " + "─" * 38)
    for d in ds["derivs_latest"]:
        fr = to_float(d.get("funding_rate_pct")) or 0.0
        oi = (to_float(d.get("open_interest_usd")) or 0.0) / 1e6
        print(f"│ [Perp] {d['asset_id']:<8} {d['venue']:<26} Funding: {fr:>+9.6f}%/8h | Open Interest: ${oi:>8,.1f}M")
    if ds["gpu_rentals"]:
        print("├" + "─" * 94)
        gpu_sorted = sorted(ds["gpu_rentals"], key=lambda g: g["asset_id"])
        for i in range(0, len(gpu_sorted), 2):
            left_g = gpu_sorted[i]
            l_str = f"{left_g['asset_id']:<35} ${to_float(left_g['value']) or 0:>5.2f}/hr"
            if i + 1 < len(gpu_sorted):
                right_g = gpu_sorted[i + 1]
                r_str = f"{right_g['asset_id']:<35} ${to_float(right_g['value']) or 0:>5.2f}/hr"
            else:
                r_str = ""
            print(f"│ {l_str} │ {r_str}")
    print("└" + "─" * 94)

    # 7. Active Alerts
    print("\n┌─ 7. ACTIVE QUANTITATIVE ALERTS (`data/alerts.csv`) " + "─" * 42)
    for a in ds["alerts_latest"]:
        sev = a.get("severity", "info").upper()
        print(f"│ [{sev:<6}] {a['asset_id']:<20} {a['signal_type']:<30} val={a.get('value') or '—'}")
    print("└" + "─" * 94 + "\n")


# ---------------------------------------------------------------------------
# 2. High-Resolution Multi-Panel PNG Dashboard (via Pillow)
# ---------------------------------------------------------------------------

def render_png_overview(ds: Dict[str, Any], out_path: Path) -> bool:
    """Render a 6-panel 2200x1500 dark-mode PNG visual summary using Pillow."""
    if not HAS_PIL:
        return False

    W, H = 2200, 1500
    bg_color = (13, 17, 23)
    card_bg = (22, 27, 34)
    border_col = (48, 54, 61)
    text_main = (230, 237, 243)
    text_muted = (139, 148, 158)
    accent_red = (248, 81, 73)
    accent_green = (46, 160, 67)
    accent_blue = (88, 166, 255)
    accent_amber = (210, 153, 34)
    accent_purple = (188, 140, 255)
    accent_cyan = (57, 197, 207)

    img = Image.new("RGB", (W, H), bg_color)
    draw = ImageDraw.Draw(img)

    try:
        font_title = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 28)
        font_h2 = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial Bold.ttf", 20)
        font_body = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 15)
        font_small = ImageFont.truetype("/System/Library/Fonts/Menlo.ttc", 13)
    except Exception:
        font_title = ImageFont.load_default()
        font_h2 = ImageFont.load_default()
        font_body = ImageFont.load_default()
        font_small = ImageFont.load_default()

    # Header Banner
    macro = ds["macro_latest"]
    draw.rectangle([25, 20, W - 25, 105], fill=card_bg, outline=border_col, width=2)
    draw.text(
        (45, 32),
        f"OMNI-MARKET WATCHER — MULTI-ASSET DATA OVERVIEW ({ds['latest_date']})",
        fill=text_main,
        font=font_title,
    )
    regime = macro.get("regime_status", "NEUTRAL")
    badge_col = accent_red if "RISK-OFF" in regime else accent_green
    draw.rectangle([45, 68, 220, 94], fill=badge_col)
    draw.text((58, 72), f"REGIME: {regime}", fill=(255, 255, 255), font=font_body)
    header_metrics = (
        f"US2Y: {macro.get('us2y_pct')}%   |   US10Y: {macro.get('us10y_pct')}% (+{macro.get('us10y_change_48h_bps')} bps/48h)   |   "
        f"US30Y: {macro.get('us30y_pct')}%   |   DXY: {macro.get('dxy')}   |   VIX: {macro.get('vix')}   |   "
        f"Stablecoins: ${(to_float(macro.get('stablecoin_mcap_usd')) or 0)/1e9:.2f}B"
    )
    draw.text((240, 72), header_metrics, fill=text_muted, font=font_body)

    # Helper to draw panel frame
    def draw_panel(x0: int, y0: int, x1: int, y1: int, title: str) -> Tuple[int, int, int, int]:
        draw.rectangle([x0, y0, x1, y1], fill=card_bg, outline=border_col, width=2)
        draw.text((x0 + 18, y0 + 14), title, fill=text_main, font=font_h2)
        draw.line([x0 + 12, y0 + 44, x1 - 12, y0 + 44], fill=border_col, width=1)
        return (x0 + 20, y0 + 56, x1 - 20, y1 - 20)

    wide = ds["wide_all"]

    # Panel 1 (Top-Left): Normalized 90-Day Cross-Asset Trajectories (Indexed = 100)
    px0, py0, px1, py1 = draw_panel(25, 120, 1095, 560, "1. 90-Day Cross-Asset Normalized Performance (Base = 100)")
    chart_x0, chart_y0, chart_x1, chart_y1 = px0 + 45, py0 + 25, px1 - 15, py1 - 25
    series_specs = [
        ("SPY", accent_blue),
        ("QQQ", accent_cyan),
        ("BTC-USD", accent_amber),
        ("ETH-USD", accent_purple),
        ("GC=F", accent_green),
        ("DX-Y.NYB", accent_red),
    ]
    norm_series: Dict[str, List[Tuple[int, float]]] = {}
    all_norm_vals = [100.0]
    for tk, _ in series_specs:
        raw_pts = [(i, to_float(r.get(tk))) for i, r in enumerate(wide)]
        valid = [(i, v) for i, v in raw_pts if v is not None]
        if valid and valid[0][1] != 0:
            base = valid[0][1]
            norm_pts = [(i, (v / base) * 100.0) for i, v in valid]
            norm_series[tk] = norm_pts
            all_norm_vals.extend(v for _, v in norm_pts)

    lo_v, hi_v = min(all_norm_vals) * 0.98, max(all_norm_vals) * 1.02
    for grid_i in range(5):
        gy = chart_y0 + int((chart_y1 - chart_y0) * grid_i / 4)
        gval = hi_v - (hi_v - lo_v) * grid_i / 4
        draw.line([chart_x0, gy, chart_x1, gy], fill=(35, 41, 49), width=1)
        draw.text((px0, gy - 7), f"{gval:>5.0f}", fill=text_muted, font=font_small)

    max_idx = max(1, len(wide) - 1)
    leg_x = chart_x0
    for tk, col in series_specs:
        pts = norm_series.get(tk, [])
        if len(pts) >= 2:
            xy = [
                (
                    chart_x0 + int((i / max_idx) * (chart_x1 - chart_x0)),
                    chart_y1 - int(((v - lo_v) / (hi_v - lo_v)) * (chart_y1 - chart_y0)),
                )
                for i, v in pts
            ]
            draw.line(xy, fill=col, width=3)
            last_v = pts[-1][1] - 100.0
            draw.text((leg_x, py0), f"■ {tk} ({last_v:+.1f}%)", fill=col, font=font_small)
            leg_x += 160

    # Panel 2 (Top-Right): U.S. Treasury Yield Curve (2Y / 10Y / 30Y) Over 90 Days
    px0, py0, px1, py1 = draw_panel(1115, 120, W - 25, 560, "2. U.S. Treasury Par Yield Curve (2Y / 10Y / 30Y %)")
    chart_x0, chart_y0, chart_x1, chart_y1 = px0 + 50, py0 + 25, px1 - 15, py1 - 25
    y_specs = [("US2Y", accent_cyan), ("US10Y", accent_amber), ("US30Y", accent_red)]
    y_vals = []
    y_pts_map: Dict[str, List[Tuple[int, float]]] = {}
    for tk, _ in y_specs:
        pts = [(i, to_float(r.get(tk))) for i, r in enumerate(wide)]
        valid = [(i, v) for i, v in pts if v is not None]
        y_pts_map[tk] = valid
        y_vals.extend(v for _, v in valid)
    ylo, yhi = (min(y_vals) - 0.1, max(y_vals) + 0.1) if y_vals else (4.0, 5.6)
    for grid_i in range(5):
        gy = chart_y0 + int((chart_y1 - chart_y0) * grid_i / 4)
        gval = yhi - (yhi - ylo) * grid_i / 4
        draw.line([chart_x0, gy, chart_x1, gy], fill=(35, 41, 49), width=1)
        draw.text((px0, gy - 7), f"{gval:>4.2f}%", fill=text_muted, font=font_small)
    leg_x = chart_x0
    for tk, col in y_specs:
        pts = y_pts_map.get(tk, [])
        if len(pts) >= 2:
            xy = [
                (
                    chart_x0 + int((i / max_idx) * (chart_x1 - chart_x0)),
                    chart_y1 - int(((v - ylo) / (yhi - ylo)) * (chart_y1 - chart_y0)),
                )
                for i, v in pts
            ]
            draw.line(xy, fill=col, width=3)
            draw.text((leg_x, py0), f"■ {tk}: {pts[-1][1]:.2f}%", fill=col, font=font_small)
            leg_x += 200

    # Panel 3 (Middle-Left): Liquid Assets 20-Day Z-Score Ranking
    px0, py0, px1, py1 = draw_panel(25, 580, 1095, 1030, "3. Liquid Markets 20-Day Z-Score Breakout Scanner (±2.5σ Trigger)")
    liq_sorted = sorted(
        ds["liquid_latest"],
        key=lambda r: abs(to_float(r.get("z_score_20d")) or 0.0),
        reverse=True,
    )[:14]
    mid_x = (px0 + 180 + px1 - 80) // 2
    scale_w = (px1 - 80 - (px0 + 180)) // 2
    # Zero and ±2.5σ vertical lines
    draw.line([mid_x, py0 + 5, mid_x, py1 - 5], fill=text_muted, width=1)
    for sign in (-1, 1):
        tx = mid_x + int(sign * (2.5 / 3.5) * scale_w)
        draw.line([tx, py0 + 5, tx, py1 - 5], fill=accent_red, width=1)
    row_h = (py1 - py0 - 10) // max(1, len(liq_sorted))
    for idx, r in enumerate(liq_sorted):
        ry = py0 + 6 + idx * row_h
        z = to_float(r.get("z_score_20d")) or 0.0
        vr = to_float(r.get("vol_ratio_20d"))
        draw.text((px0, ry), f"{r['asset_id']:<9} ({r['category'][:6]})", fill=text_main, font=font_small)
        bar_w = int(max(-3.5, min(3.5, z)) / 3.5 * scale_w)
        col = accent_red if abs(z) >= 2.5 else (accent_amber if abs(z) >= 1.8 else accent_blue)
        x_start, x_end = (mid_x + bar_w, mid_x) if bar_w < 0 else (mid_x, mid_x + bar_w)
        draw.rectangle([x_start, ry + 2, max(x_start + 2, x_end), ry + row_h - 4], fill=col)
        vr_txt = f"{vr:.1f}x" if vr is not None else "—"
        draw.text((px1 - 115, ry), f"Z={z:+.2f} ({vr_txt})", fill=text_muted, font=font_small)

    # Panel 4 (Middle-Right): Prediction Market Probabilities & 24h Deltas
    px0, py0, px1, py1 = draw_panel(1115, 580, W - 25, 1030, "4. Event Prediction Markets — Top Contracts & 24h Odds Shift")
    pred_top = sorted(
        ds["pred_latest"],
        key=lambda r: abs(to_float(r.get("change_24h_pts")) or 0.0),
        reverse=True,
    )[:12]
    row_h = (py1 - py0 - 10) // max(1, len(pred_top))
    bar_x0, bar_x1 = px0 + 440, px1 - 140
    for idx, p in enumerate(pred_top):
        ry = py0 + 6 + idx * row_h
        prob = (to_float(p.get("probability")) or 0.0) * 100.0
        chg = to_float(p.get("change_24h_pts")) or 0.0
        q_short = p.get("question", "")[:46]
        draw.text((px0, ry), f"[{p['venue'][:4]}] {q_short}", fill=text_main, font=font_small)
        draw.rectangle([bar_x0, ry + 3, bar_x1, ry + row_h - 5], fill=(35, 41, 49))
        fill_w = int((prob / 100.0) * (bar_x1 - bar_x0))
        b_col = accent_purple if p["venue"] == "Polymarket" else accent_cyan
        if fill_w > 0:
            draw.rectangle([bar_x0, ry + 3, bar_x0 + fill_w, ry + row_h - 5], fill=b_col)
        chg_col = accent_green if chg > 0 else (accent_red if chg < 0 else text_muted)
        draw.text((bar_x1 + 10, ry), f"{prob:>5.1f}% ({chg:+.1f}p)", fill=chg_col, font=font_small)

    # Panel 5 (Bottom-Left): Collectibles 30d FMV & Verified Comps Range
    px0, py0, px1, py1 = draw_panel(25, 1050, 1095, H - 25, "5. Illiquid Collectibles — 30d Verified FMV & Sold Comps Dispersion")
    priced_items = [il for il in ds["illiquid_latest"] if to_float(il.get("fmv_30d_usd")) is not None]
    row_h = (py1 - py0 - 20) // max(1, len(priced_items))
    for idx, il in enumerate(priced_items):
        ry = py0 + 10 + idx * row_h
        iid = il["item_id"]
        fmv = to_float(il.get("fmv_30d_usd")) or 0.0
        n_c = int(to_float(il.get("n_comps")) or 0)
        comps = [c["price"] for c in ds["comps_by_item"].get(iid, [])]
        c_min = min(comps) if comps else fmv
        c_max = max(comps) if comps else fmv
        deal = fmv * 0.78
        draw.text((px0, ry), f"{iid[:36]:<36}", fill=text_main, font=font_body)
        draw.text(
            (px0 + 360, ry),
            f"FMV: ${fmv:>8,.2f} (n={n_c:>2})  |  Comps: ${c_min:,.0f}–${c_max:,.0f}  |  Deal ≤ ${deal:,.0f}",
            fill=accent_green,
            font=font_body,
        )

    # Panel 6 (Bottom-Right): GPU Cloud Rental Arbitrage & Active Alerts
    px0, py0, px1, py1 = draw_panel(1115, 1050, W - 25, H - 25, "6. GPU Cloud Spot Rental Rates ($/GPU-hr) & High-Priority Alerts")
    gpu_focus = [
        ("B200 (Vast / RunPod Comm / Sec)", ["gpu-rental-b200-vast", "gpu-rental-b200-runpod-community", "gpu-rental-b200-runpod-secure"]),
        ("H100 SXM (Vast / Ver / RP Sec)", ["gpu-rental-h100-sxm-vast", "gpu-rental-h100-sxm-vast-verified", "gpu-rental-h100-sxm-runpod-secure"]),
        ("H100 PCIe (Vast / RP Comm / Sec)", ["gpu-rental-h100-pcie-vast", "gpu-rental-h100-pcie-runpod-community", "gpu-rental-h100-pcie-runpod-secure"]),
        ("RTX 5090 (Vast / RP Comm / Sec)", ["gpu-rental-rtx-5090-vast", "gpu-rental-rtx-5090-runpod-community", "gpu-rental-rtx-5090-runpod-secure"]),
        ("RTX 4090 (Vast / Ver / RP Comm)", ["gpu-rental-rtx-4090-vast", "gpu-rental-rtx-4090-vast-verified", "gpu-rental-rtx-4090-runpod-community"]),
    ]
    gpu_lookup = {g["asset_id"]: to_float(g["value"]) for g in ds["gpu_rentals"]}
    ry = py0 + 8
    for label, slugs in gpu_focus:
        vals_str = "   vs  ".join(f"${gpu_lookup.get(s, 0):.3f}/hr" for s in slugs if gpu_lookup.get(s) is not None)
        draw.text((px0, ry), f"{label:<34}: {vals_str}", fill=accent_cyan, font=font_body)
        ry += 32

    draw.line([px0, ry + 6, px1, ry + 6], fill=border_col, width=1)
    ry += 16
    draw.text((px0, ry), "Active Triggers & Risk Warnings:", fill=accent_amber, font=font_body)
    ry += 26
    for a in ds["alerts_latest"][:5]:
        sev = a.get("severity", "").upper()
        col = accent_red if sev == "HIGH" else accent_amber
        draw.text((px0, ry), f"[{sev}] {a['asset_id']} — {a['signal_type']} ({a.get('value')})", fill=col, font=font_small)
        ry += 22

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG")
    return True


# ---------------------------------------------------------------------------
# 3. Interactive Self-Contained HTML Dashboard (`reports/dashboard.html`)
# ---------------------------------------------------------------------------

def render_html_dashboard(ds: Dict[str, Any], out_path: Path) -> None:
    """Generate a rich, interactive self-contained HTML dashboard from `data/*.csv`."""
    # Prepare clean JSON payload for client-side interactive charts & tables
    wide_dates = [r["date"] for r in ds["wide_all"]]
    wide_cols = [c for c in (ds["wide_all"][0].keys() if ds["wide_all"] else []) if c != "date"]
    wide_series: Dict[str, List[Optional[float]]] = {
        col: [to_float(r.get(col)) for r in ds["wide_all"]] for col in wide_cols
    }

    payload_json = json.dumps(
        {
            "latest_date": ds["latest_date"],
            "inventory": ds["inventory"],
            "macro_latest": ds["macro_latest"],
            "macro_all": ds["macro_all"],
            "liquid_latest": ds["liquid_latest"],
            "derivs_latest": ds["derivs_latest"],
            "pred_latest": ds["pred_latest"],
            "illiquid_latest": ds["illiquid_latest"],
            "comps_by_item": ds["comps_by_item"],
            "gpu_rentals": ds["gpu_rentals"],
            "spot_metals": ds["spot_metals"],
            "alerts_latest": ds["alerts_latest"],
            "wide_dates": wide_dates,
            "wide_series": wide_series,
            "corr_tickers": ds["corr_tickers"],
            "corr_matrix": ds["corr_matrix"],
        }
    )

    html_content = f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1.0" />
  <title>Omni-Market Watcher — Data Folder Interactive Visualizer ({ds['latest_date']})</title>
  <style>
    :root {{
      --bg: #0d1117;
      --panel: #161b22;
      --panel-alt: #1c2128;
      --border: #30363d;
      --text: #e6edf3;
      --muted: #8b949e;
      --blue: #58a6ff;
      --cyan: #39c5cf;
      --green: #3fb950;
      --amber: #d29922;
      --red: #f85149;
      --purple: #bc8cff;
    }}
    * {{ box-sizing: border-box; margin: 0; padding: 0; }}
    body {{
      background: var(--bg);
      color: var(--text);
      font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Helvetica, Arial, sans-serif;
      line-height: 1.5;
      padding: 20px 28px 60px;
    }}
    header {{
      display: flex;
      flex-wrap: wrap;
      justify-content: space-between;
      align-items: center;
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 24px;
      margin-bottom: 20px;
      gap: 16px;
    }}
    .title-group h1 {{ font-size: 1.4rem; font-weight: 700; letter-spacing: -0.02em; }}
    .title-group p {{ color: var(--muted); font-size: 0.86rem; margin-top: 2px; }}
    .regime-badge {{
      padding: 8px 16px;
      border-radius: 999px;
      font-weight: 700;
      font-size: 0.85rem;
      letter-spacing: 0.04em;
      background: rgba(248, 81, 73, 0.16);
      color: var(--red);
      border: 1px solid var(--red);
    }}
    .regime-badge.neutral {{
      background: rgba(88, 166, 255, 0.16);
      color: var(--blue);
      border-color: var(--blue);
    }}
    .kpi-strip {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(165px, 1fr));
      gap: 12px;
      margin-bottom: 20px;
    }}
    .kpi-card {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 8px;
      padding: 14px 16px;
    }}
    .kpi-label {{ font-size: 0.75rem; text-transform: uppercase; color: var(--muted); letter-spacing: 0.05em; }}
    .kpi-val {{ font-size: 1.35rem; font-weight: 700; margin-top: 4px; font-variant-numeric: tabular-nums; }}
    .kpi-sub {{ font-size: 0.78rem; color: var(--muted); margin-top: 2px; }}
    .grid-2 {{
      display: grid;
      grid-template-columns: repeat(auto-fit, minmax(540px, 1fr));
      gap: 18px;
      margin-bottom: 18px;
    }}
    .panel {{
      background: var(--panel);
      border: 1px solid var(--border);
      border-radius: 10px;
      padding: 18px 20px;
      margin-bottom: 18px;
      overflow-x: auto;
    }}
    .panel-header {{
      display: flex;
      justify-content: space-between;
      align-items: center;
      flex-wrap: wrap;
      gap: 10px;
      margin-bottom: 14px;
      padding-bottom: 10px;
      border-bottom: 1px solid var(--border);
    }}
    .panel-header h2 {{ font-size: 1.02rem; font-weight: 600; }}
    .controls {{ display: flex; gap: 8px; align-items: center; flex-wrap: wrap; }}
    select, input, button {{
      background: var(--panel-alt);
      color: var(--text);
      border: 1px solid var(--border);
      border-radius: 6px;
      padding: 6px 10px;
      font-size: 0.82rem;
      cursor: pointer;
    }}
    button.active {{
      background: var(--blue);
      color: #0d1117;
      border-color: var(--blue);
      font-weight: 600;
    }}
    table {{
      width: 100%;
      border-collapse: collapse;
      font-size: 0.83rem;
      font-variant-numeric: tabular-nums;
    }}
    th, td {{
      padding: 8px 10px;
      text-align: left;
      border-bottom: 1px solid rgba(48, 54, 61, 0.65);
    }}
    th {{
      color: var(--muted);
      font-weight: 600;
      font-size: 0.76rem;
      text-transform: uppercase;
      letter-spacing: 0.04em;
      cursor: pointer;
      user-select: none;
    }}
    tr:hover td {{ background: rgba(255, 255, 255, 0.025); }}
    .num {{ text-align: right; font-family: "SFMono-Regular", Consolas, Menlo, monospace; }}
    .pos {{ color: var(--green); }}
    .neg {{ color: var(--red); }}
    .warn {{ color: var(--amber); }}
    .pill {{
      display: inline-block;
      padding: 2px 8px;
      border-radius: 4px;
      font-size: 0.73rem;
      font-weight: 600;
    }}
    .pill.high {{ background: rgba(248, 81, 73, 0.2); color: var(--red); }}
    .pill.medium {{ background: rgba(210, 153, 34, 0.2); color: var(--amber); }}
    .pill.info {{ background: rgba(88, 166, 255, 0.2); color: var(--blue); }}
    .bar-wrap {{
      width: 130px;
      height: 8px;
      background: #21262d;
      border-radius: 4px;
      overflow: hidden;
      display: inline-block;
      vertical-align: middle;
    }}
    .bar-fill {{ height: 100%; border-radius: 4px; }}
    svg.chart-svg {{
      width: 100%;
      height: 310px;
      background: #0f141b;
      border-radius: 8px;
      border: 1px solid #21262d;
    }}
    .corr-cell {{
      text-align: center;
      font-family: "SFMono-Regular", Consolas, Menlo, monospace;
      font-size: 0.77rem;
      padding: 6px;
      border-radius: 3px;
    }}
  </style>
</head>
<body>
  <header>
    <div class="title-group">
      <h1>🌐 Omni-Market Watcher — Data Folder Explorer</h1>
      <p>Interactive visualization of <code>data/*.csv</code> • Latest snapshot: <strong id="hdr-date"></strong></p>
    </div>
    <div id="hdr-regime" class="regime-badge">REGIME</div>
  </header>

  <section class="kpi-strip" id="kpi-strip"></section>

  <!-- Interactive 90-Day Time Series & Yield Curve -->
  <div class="grid-2">
    <div class="panel">
      <div class="panel-header">
        <h2>📈 90-Day Price & FMV History (<code>data/prices_wide.csv</code>)</h2>
        <div class="controls">
          <select id="ts-select-1"></select>
          <select id="ts-select-2"></select>
          <button id="btn-norm" class="active" onclick="setTsMode('norm')">Indexed (100)</button>
          <button id="btn-raw" onclick="setTsMode('raw')">Raw Level</button>
        </div>
      </div>
      <svg id="svg-timeseries" class="chart-svg" viewBox="0 0 760 300"></svg>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2>🏛️ U.S. Treasury Par Yield Curve & Stablecoin Liquidity (<code>data/macro.csv</code>)</h2>
        <div class="controls">
          <button id="btn-yields" class="active" onclick="setMacroMode('yields')">2Y / 10Y / 30Y Yields</button>
          <button id="btn-stable" onclick="setMacroMode('stable')">Stablecoin Supply ($B)</button>
        </div>
      </div>
      <svg id="svg-macro" class="chart-svg" viewBox="0 0 760 300"></svg>
    </div>
  </div>

  <!-- Liquid Z-Score Scanner -->
  <div class="panel">
    <div class="panel-header">
      <h2>⚡ Liquid Markets 20-Day Z-Score & Volume Breakout Scanner (<code>data/liquid.csv</code>)</h2>
      <div class="controls">
        <input type="text" id="liq-filter" placeholder="Filter ticker or category..." oninput="renderLiquidTable()" />
        <select id="liq-cat" onchange="renderLiquidTable()">
          <option value="ALL">All Categories</option>
        </select>
      </div>
    </div>
    <table id="tbl-liquid">
      <thead>
        <tr>
          <th onclick="sortLiquid('asset_id')">Ticker ▾</th>
          <th onclick="sortLiquid('category')">Category</th>
          <th class="num" onclick="sortLiquid('price')">Price</th>
          <th class="num" onclick="sortLiquid('change_24h_pct')">24h Δ</th>
          <th class="num" onclick="sortLiquid('change_5d_pct')">5d Δ</th>
          <th class="num" onclick="sortLiquid('z_score_20d')">20d Z-Score</th>
          <th>Z-Score Visual (±2.5σ)</th>
          <th class="num" onclick="sortLiquid('vol_ratio_20d')">20d Vol Ratio</th>
          <th>Source</th>
        </tr>
      </thead>
      <tbody></tbody>
    </table>
  </div>

  <!-- Prediction Markets & Collectibles -->
  <div class="grid-2">
    <div class="panel">
      <div class="panel-header">
        <h2>🎯 Prediction Markets (<code>data/prediction.csv</code>)</h2>
        <div class="controls">
          <select id="pred-venue" onchange="renderPredictionTable()">
            <option value="ALL">All Venues</option>
            <option value="Polymarket">Polymarket</option>
            <option value="Kalshi">Kalshi</option>
          </select>
        </div>
      </div>
      <table id="tbl-pred">
        <thead>
          <tr>
            <th>Venue</th>
            <th>Question</th>
            <th class="num">Prob</th>
            <th>Odds Bar</th>
            <th class="num">24h Shift</th>
            <th class="num">24h Vol</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2>💎 Illiquid Collectibles & Comps Explorer (<code>illiquid.csv</code> + <code>comps.csv</code>)</h2>
        <div class="controls">
          <select id="comp-item-select" onchange="renderCompsChart()"></select>
        </div>
      </div>
      <svg id="svg-comps" class="chart-svg" style="height:195px; margin-bottom:12px;" viewBox="0 0 760 195"></svg>
      <table id="tbl-illiquid">
        <thead>
          <tr>
            <th>Item ID</th>
            <th>Category</th>
            <th>Condition</th>
            <th class="num">30d FMV</th>
            <th class="num">Comps</th>
            <th class="num">Deal Target (≤78%)</th>
            <th>Status</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <!-- GPU Rentals, Crypto Derivatives & Correlation Matrix -->
  <div class="grid-2">
    <div class="panel">
      <div class="panel-header">
        <h2>🖥️ AI GPU Cloud Rental Rates & Crypto Derivatives</h2>
      </div>
      <table id="tbl-gpu" style="margin-bottom: 16px;">
        <thead>
          <tr>
            <th>GPU Tier Slug</th>
            <th>Description</th>
            <th class="num">Rate ($/GPU-hr)</th>
            <th>Source</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
      <table id="tbl-derivs">
        <thead>
          <tr>
            <th>Asset</th>
            <th>Perpetual Venue</th>
            <th class="num">8h Funding Rate</th>
            <th class="num">Open Interest (USD)</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2>🔗 30-Day Cross-Asset Daily Return Correlation Matrix</h2>
      </div>
      <div id="corr-container"></div>
    </div>
  </div>

  <!-- Alerts & Data Folder Inventory -->
  <div class="grid-2">
    <div class="panel">
      <div class="panel-header">
        <h2>🚨 Active Quantitative Alerts (<code>data/alerts.csv</code>)</h2>
      </div>
      <table id="tbl-alerts">
        <thead>
          <tr>
            <th>Severity</th>
            <th>Asset</th>
            <th>Signal Type</th>
            <th class="num">Value</th>
            <th>Details</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>

    <div class="panel">
      <div class="panel-header">
        <h2>🗂️ Data Folder CSV Inventory (<code>data/*.csv</code>)</h2>
      </div>
      <table id="tbl-inv">
        <thead>
          <tr>
            <th>CSV File</th>
            <th class="num">Rows</th>
            <th class="num">Columns</th>
            <th>Start Date</th>
            <th>End Date</th>
            <th class="num">Size</th>
          </tr>
        </thead>
        <tbody></tbody>
      </table>
    </div>
  </div>

  <script>
    const DATA = {payload_json};
    let tsMode = 'norm';
    let macroMode = 'yields';
    let liqSortCol = 'z_score_20d';
    let liqSortAsc = false;

    function fmtNum(v, d = 2) {{
      if (v === null || v === undefined || v === '' || isNaN(Number(v))) return '—';
      return Number(v).toLocaleString(undefined, {{ minimumFractionDigits: d, maximumFractionDigits: d }});
    }}

    function initDashboard() {{
      document.getElementById('hdr-date').textContent = DATA.latest_date;
      const m = DATA.macro_latest || {{}};
      const regEl = document.getElementById('hdr-regime');
      regEl.textContent = `REGIME: ${{m.regime_status || 'NEUTRAL'}}`;
      if ((m.regime_status || '').indexOf('RISK-OFF') === -1) regEl.classList.add('neutral');

      // KPI Strip
      const kpis = [
        {{ label: 'US 10Y Par Yield', val: `${{m.us10y_pct || '—'}}%`, sub: `48h Δ: +${{m.us10y_change_48h_bps || 0}} bps (2Y: ${{m.us2y_pct}}%)` }},
        {{ label: 'US Dollar Index (DXY)', val: m.dxy || '—', sub: `Prior 30d High: ${{m.dxy_30d_high || '—'}}` }},
        {{ label: 'CBOE VIX', val: m.vix || '—', sub: `USD/JPY: ${{m.usdjpy || '—'}}` }},
        {{ label: 'Stablecoin Supply', val: `$${{(Number(m.stablecoin_mcap_usd || 0) / 1e9).toFixed(2)}}B`, sub: `BTC 8h Funding: ${{m.btc_funding_pct || '—'}}%` }},
        {{ label: 'Tracked Liquid Assets', val: DATA.liquid_latest.length, sub: `Across 90 daily bars` }},
        {{ label: 'Verified Sold Comps', val: Object.values(DATA.comps_by_item).reduce((a, b) => a + b.length, 0), sub: `Across ${{DATA.illiquid_latest.length}} watchlist items` }},
      ];
      document.getElementById('kpi-strip').innerHTML = kpis.map(k => `
        <div class="kpi-card">
          <div class="kpi-label">${{k.label}}</div>
          <div class="kpi-val">${{k.val}}</div>
          <div class="kpi-sub">${{k.sub}}</div>
        </div>
      `).join('');

      // Populate timeseries selectors
      const cols = Object.keys(DATA.wide_series).sort();
      const s1 = document.getElementById('ts-select-1');
      const s2 = document.getElementById('ts-select-2');
      s1.innerHTML = cols.map(c => `<option value="${{c}}" ${{c === 'BTC-USD' ? 'selected' : ''}}>${{c}}</option>`).join('');
      s2.innerHTML = `<option value="">(None — Single Series)</option>` + cols.map(c => `<option value="${{c}}" ${{c === 'SPY' ? 'selected' : ''}}>${{c}}</option>`).join('');
      s1.onchange = renderTimeseriesChart;
      s2.onchange = renderTimeseriesChart;

      // Populate Liquid Categories
      const cats = Array.from(new Set(DATA.liquid_latest.map(r => r.category))).sort();
      const catSel = document.getElementById('liq-cat');
      cats.forEach(c => {{
        const opt = document.createElement('option');
        opt.value = c;
        opt.textContent = c;
        catSel.appendChild(opt);
      }});

      // Populate Comps selector
      const compItems = Object.keys(DATA.comps_by_item);
      const compSel = document.getElementById('comp-item-select');
      compSel.innerHTML = compItems.map(id => `<option value="${{id}}">${{id}} (n=${{DATA.comps_by_item[id].length}})</option>`).join('');

      renderTimeseriesChart();
      renderMacroChart();
      renderLiquidTable();
      renderPredictionTable();
      renderIlliquidTable();
      renderCompsChart();
      renderGpuAndDerivs();
      renderCorrelationMatrix();
      renderAlertsAndInventory();
    }}

    function setTsMode(mode) {{
      tsMode = mode;
      document.getElementById('btn-norm').classList.toggle('active', mode === 'norm');
      document.getElementById('btn-raw').classList.toggle('active', mode === 'raw');
      renderTimeseriesChart();
    }}

    function setMacroMode(mode) {{
      macroMode = mode;
      document.getElementById('btn-yields').classList.toggle('active', mode === 'yields');
      document.getElementById('btn-stable').classList.toggle('active', mode === 'stable');
      renderMacroChart();
    }}

    function drawLineSvg(svgId, dates, seriesList) {{
      const svg = document.getElementById(svgId);
      const W = 760, H = 300, padL = 58, padR = 24, padT = 28, padB = 34;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      let allVals = [];
      seriesList.forEach(s => s.pts.forEach(p => allVals.push(p.v)));
      if (!allVals.length) {{
        svg.innerHTML = `<text x="380" y="150" fill="#8b949e" text-anchor="middle">No data points available</text>`;
        return;
      }}
      let minV = Math.min(...allVals), maxV = Math.max(...allVals);
      if (minV === maxV) {{ minV -= 1; maxV += 1; }}
      const span = maxV - minV;
      minV -= span * 0.05;
      maxV += span * 0.05;

      let out = '';
      for (let i = 0; i <= 4; i++) {{
        const y = padT + (plotH * i) / 4;
        const val = maxV - ((maxV - minV) * i) / 4;
        out += `<line x1="${{padL}}" y1="${{y}}" x2="${{W - padR}}" y2="${{y}}" stroke="#21262d" stroke-width="1"/>`;
        out += `<text x="${{padL - 8}}" y="${{y + 4}}" fill="#8b949e" font-size="11" text-anchor="end">${{val.toFixed(val > 100 ? 0 : 2)}}</text>`;
      }}

      const maxIdx = Math.max(1, dates.length - 1);
      [0, Math.floor(maxIdx / 2), maxIdx].forEach(idx => {{
        const x = padL + (idx / maxIdx) * plotW;
        out += `<text x="${{x}}" y="${{H - 10}}" fill="#8b949e" font-size="11" text-anchor="middle">${{dates[idx] || ''}}</text>`;
      }});

      let legX = padL;
      seriesList.forEach(s => {{
        if (s.pts.length < 2) return;
        const dPath = s.pts.map((p, idx) => {{
          const x = (padL + (p.i / maxIdx) * plotW).toFixed(1);
          const y = (padT + plotH - ((p.v - minV) / (maxV - minV)) * plotH).toFixed(1);
          return `${{idx === 0 ? 'M' : 'L'}}${{x}},${{y}}`;
        }}).join(' ');
        out += `<path d="${{dPath}}" fill="none" stroke="${{s.color}}" stroke-width="2.4"/>`;
        const lastVal = s.pts[s.pts.length - 1].v;
        out += `<text x="${{legX}}" y="18" fill="${{s.color}}" font-size="12" font-weight="600">● ${{s.label}} (${{lastVal.toFixed(2)}})</text>`;
        legX += 210;
      }});
      svg.innerHTML = out;
    }}

    function renderTimeseriesChart() {{
      const c1 = document.getElementById('ts-select-1').value;
      const c2 = document.getElementById('ts-select-2').value;
      const chosen = [
        {{ key: c1, color: '#58a6ff' }},
        ...(c2 ? [{{ key: c2, color: '#d29922' }}] : [])
      ];
      const seriesList = chosen.map(item => {{
        const raw = DATA.wide_series[item.key] || [];
        const valid = [];
        raw.forEach((v, i) => {{ if (v !== null && v !== undefined) valid.push({{ i, v: Number(v) }}); }});
        if (tsMode === 'norm' && valid.length > 0 && valid[0].v !== 0) {{
          const base = valid[0].v;
          return {{ label: `${{item.key}} (Idx)`, color: item.color, pts: valid.map(p => ({{ i: p.i, v: (p.v / base) * 100 }})) }};
        }}
        return {{ label: item.key, color: item.color, pts: valid }};
      }});
      drawLineSvg('svg-timeseries', DATA.wide_dates, seriesList);
    }}

    function renderMacroChart() {{
      const dates = DATA.macro_all.map(r => r.date);
      if (macroMode === 'yields') {{
        const specs = [
          {{ k: 'us2y_pct', label: 'US2Y (%)', color: '#39c5cf' }},
          {{ k: 'us10y_pct', label: 'US10Y (%)', color: '#d29922' }},
          {{ k: 'us30y_pct', label: 'US30Y (%)', color: '#f85149' }},
        ];
        const series = specs.map(s => ({{
          label: s.label,
          color: s.color,
          pts: DATA.macro_all.map((r, i) => ({{ i, v: r[s.k] ? Number(r[s.k]) : null }})).filter(p => p.v !== null)
        }}));
        drawLineSvg('svg-macro', dates, series);
      }} else {{
        const pts = DATA.macro_all
          .map((r, i) => ({{ i, v: r.stablecoin_mcap_usd ? Number(r.stablecoin_mcap_usd) / 1e9 : null }}))
          .filter(p => p.v !== null);
        drawLineSvg('svg-macro', dates, [{{ label: 'Stablecoin Supply ($B)', color: '#3fb950', pts }}]);
      }}
    }}

    function sortLiquid(col) {{
      if (liqSortCol === col) liqSortAsc = !liqSortAsc;
      else {{ liqSortCol = col; liqSortAsc = false; }}
      renderLiquidTable();
    }}

    function renderLiquidTable() {{
      const q = document.getElementById('liq-filter').value.toLowerCase();
      const cat = document.getElementById('liq-cat').value;
      let rows = DATA.liquid_latest.filter(r => {{
        if (cat !== 'ALL' && r.category !== cat) return false;
        if (q && !(r.asset_id.toLowerCase().includes(q) || r.category.toLowerCase().includes(q))) return false;
        return true;
      }});
      rows.sort((a, b) => {{
        const va = a[liqSortCol], vb = b[liqSortCol];
        const na = Number(va), nb = Number(vb);
        if (!isNaN(na) && !isNaN(nb) && va !== '' && vb !== '') {{
          const cmp = liqSortCol === 'z_score_20d' ? Math.abs(na) - Math.abs(nb) : na - nb;
          return liqSortAsc ? cmp : -cmp;
        }}
        return liqSortAsc ? String(va).localeCompare(String(vb)) : String(vb).localeCompare(String(va));
      }});

      const tbody = document.querySelector('#tbl-liquid tbody');
      tbody.innerHTML = rows.map(r => {{
        const c24 = Number(r.change_24h_pct || 0);
        const c5d = Number(r.change_5d_pct || 0);
        const z = Number(r.z_score_20d || 0);
        const vr = r.vol_ratio_20d ? `${{Number(r.vol_ratio_20d).toFixed(2)}}x` : '—';
        const pctWidth = Math.min(100, Math.round((Math.abs(z) / 3.5) * 100));
        const barColor = Math.abs(z) >= 2.5 ? '#f85149' : (Math.abs(z) >= 1.8 ? '#d29922' : '#58a6ff');
        return `
          <tr>
            <td><strong>${{r.asset_id}}</strong></td>
            <td>${{r.category}}</td>
            <td class="num">${{fmtNum(r.price, r.price < 10 ? 4 : 2)}}</td>
            <td class="num ${{c24 >= 0 ? 'pos' : 'neg'}}">${{c24 >= 0 ? '+' : ''}}${{c24.toFixed(2)}}%</td>
            <td class="num ${{c5d >= 0 ? 'pos' : 'neg'}}">${{c5d >= 0 ? '+' : ''}}${{c5d.toFixed(2)}}%</td>
            <td class="num ${{Math.abs(z) >= 2.5 ? 'neg' : ''}}"><strong>${{z >= 0 ? '+' : ''}}${{z.toFixed(2)}}σ</strong></td>
            <td>
              <div class="bar-wrap"><div class="bar-fill" style="width:${{pctWidth}}%; background:${{barColor}};"></div></div>
            </td>
            <td class="num">${{vr}}</td>
            <td style="color:var(--muted); font-size:0.75rem;">${{r.source}}</td>
          </tr>
        `;
      }}).join('');
    }}

    function renderPredictionTable() {{
      const venue = document.getElementById('pred-venue').value;
      const rows = DATA.pred_latest
        .filter(r => venue === 'ALL' || r.venue === venue)
        .sort((a, b) => Math.abs(Number(b.change_24h_pts || 0)) - Math.abs(Number(a.change_24h_pts || 0)));
      document.querySelector('#tbl-pred tbody').innerHTML = rows.map(r => {{
        const prob = Number(r.probability || 0) * 100;
        const chg = Number(r.change_24h_pts || 0);
        return `
          <tr>
            <td><span class="pill info">${{r.venue}}</span></td>
            <td><a href="${{r.url}}" target="_blank" style="color:var(--text); text-decoration:none;">${{r.question}}</a></td>
            <td class="num"><strong>${{prob.toFixed(1)}}%</strong></td>
            <td><div class="bar-wrap" style="width:85px;"><div class="bar-fill" style="width:${{Math.min(100, prob)}}%; background:#bc8cff;"></div></div></td>
            <td class="num ${{chg > 0 ? 'pos' : (chg < 0 ? 'neg' : '')}}">${{chg >= 0 ? '+' : ''}}${{chg.toFixed(1)}} pts</td>
            <td class="num">$${{fmtNum(r.volume_usd, 0)}}</td>
          </tr>
        `;
      }}).join('');
    }}

    function renderIlliquidTable() {{
      document.querySelector('#tbl-illiquid tbody').innerHTML = DATA.illiquid_latest.map(r => {{
        const fmv = r.fmv_30d_usd ? Number(r.fmv_30d_usd) : null;
        const deal = fmv ? `$${{(fmv * 0.78).toFixed(0)}}` : '—';
        const pillCls = r.status === 'PRICED' ? 'pos' : 'warn';
        return `
          <tr>
            <td><strong>${{r.item_id}}</strong></td>
            <td>${{r.category}}</td>
            <td>${{r.grade_condition}}</td>
            <td class="num">${{fmv ? '$' + fmtNum(fmv, 2) : '—'}}</td>
            <td class="num">${{r.n_comps}}</td>
            <td class="num">${{deal}}</td>
            <td><span class="${{pillCls}}">${{r.status}}</span></td>
          </tr>
        `;
      }}).join('');
    }}

    function renderCompsChart() {{
      const itemId = document.getElementById('comp-item-select').value;
      const comps = (DATA.comps_by_item[itemId] || []).slice().sort((a, b) => a.sale_date.localeCompare(b.sale_date));
      const svg = document.getElementById('svg-comps');
      if (!comps.length) {{
        svg.innerHTML = `<text x="380" y="95" fill="#8b949e" text-anchor="middle">No verified comps</text>`;
        return;
      }}
      const W = 760, H = 195, padL = 58, padR = 24, padT = 24, padB = 28;
      const plotW = W - padL - padR, plotH = H - padT - padB;
      const prices = comps.map(c => c.price);
      let minP = Math.min(...prices) * 0.94, maxP = Math.max(...prices) * 1.06;
      const ilRow = DATA.illiquid_latest.find(r => r.item_id === itemId);
      const fmv = ilRow && ilRow.fmv_30d_usd ? Number(ilRow.fmv_30d_usd) : null;

      let out = '';
      for (let i = 0; i <= 3; i++) {{
        const y = padT + (plotH * i) / 3;
        const val = maxP - ((maxP - minP) * i) / 3;
        out += `<line x1="${{padL}}" y1="${{y}}" x2="${{W - padR}}" y2="${{y}}" stroke="#21262d"/>`;
        out += `<text x="${{padL - 8}}" y="${{y + 4}}" fill="#8b949e" font-size="11" text-anchor="end">$${{val.toFixed(0)}}</text>`;
      }}
      if (fmv) {{
        const fy = padT + plotH - ((fmv - minP) / (maxP - minP)) * plotH;
        out += `<line x1="${{padL}}" y1="${{fy}}" x2="${{W - padR}}" y2="${{fy}}" stroke="#3fb950" stroke-dasharray="5,4" stroke-width="1.8"/>`;
        out += `<text x="${{W - padR - 6}}" y="${{fy - 6}}" fill="#3fb950" font-size="11" text-anchor="end">30d Trimmed Median FMV: $${{fmv.toFixed(2)}}</text>`;
      }}
      const maxIdx = Math.max(1, comps.length - 1);
      comps.forEach((c, idx) => {{
        const x = padL + (idx / maxIdx) * plotW;
        const y = padT + plotH - ((c.price - minP) / (maxP - minP)) * plotH;
        out += `<circle cx="${{x.toFixed(1)}}" cy="${{y.toFixed(1)}}" r="4.5" fill="#58a6ff" stroke="#0d1117" stroke-width="1.2">
          <title>${{c.sale_date}}: $${{c.price}} (${{c.grade}})</title>
        </circle>`;
      }});
      out += `<text x="${{padL}}" y="${{H - 6}}" fill="#8b949e" font-size="11">${{comps[0].sale_date}}</text>`;
      out += `<text x="${{W - padR}}" y="${{H - 6}}" fill="#8b949e" font-size="11" text-anchor="end">${{comps[comps.length - 1].sale_date}} (${{comps.length}} eBay sales)</text>`;
      svg.innerHTML = out;
    }}

    function renderGpuAndDerivs() {{
      document.querySelector('#tbl-gpu tbody').innerHTML = DATA.gpu_rentals.map(g => `
        <tr>
          <td><code>${{g.asset_id}}</code></td>
          <td>${{g.asset_name}}</td>
          <td class="num pos"><strong>$${{Number(g.value).toFixed(3)}}/hr</strong></td>
          <td style="color:var(--muted); font-size:0.75rem;">${{g.source}}</td>
        </tr>
      `).join('');

      document.querySelector('#tbl-derivs tbody').innerHTML = DATA.derivs_latest.map(d => {{
        const fr = Number(d.funding_rate_pct || 0);
        return `
          <tr>
            <td><strong>${{d.asset_id}}</strong></td>
            <td>${{d.venue}}</td>
            <td class="num ${{fr < 0 ? 'neg' : 'pos'}}">${{fr >= 0 ? '+' : ''}}${{fr.toFixed(6)}}%</td>
            <td class="num">$${{fmtNum(d.open_interest_usd, 0)}}</td>
          </tr>
        `;
      }}).join('');
    }}

    function renderCorrelationMatrix() {{
      const tks = DATA.corr_tickers;
      const mat = DATA.corr_matrix;
      let html = `<table><thead><tr><th>Asset</th>` + tks.map(t => `<th style="text-align:center;">${{t}}</th>`).join('') + `</tr></thead><tbody>`;
      tks.forEach(rTk => {{
        html += `<tr><td><strong>${{rTk}}</strong></td>`;
        tks.forEach(cTk => {{
          const v = mat[rTk] ? mat[rTk][cTk] : null;
          let bg = 'transparent';
          if (v !== null && rTk !== cTk) {{
            const alpha = Math.min(0.65, Math.abs(v) * 0.75).toFixed(2);
            bg = v > 0 ? `rgba(63, 185, 80, ${{alpha}})` : `rgba(248, 81, 73, ${{alpha}})`;
          }} else if (rTk === cTk) {{
            bg = 'rgba(88, 166, 255, 0.15)';
          }}
          html += `<td class="corr-cell" style="background:${{bg}};">${{v !== null ? v.toFixed(2) : '—'}}</td>`;
        }});
        html += `</tr>`;
      }});
      html += `</tbody></table>`;
      document.getElementById('corr-container').innerHTML = html;
    }}

    function renderAlertsAndInventory() {{
      document.querySelector('#tbl-alerts tbody').innerHTML = DATA.alerts_latest.map(a => `
        <tr>
          <td><span class="pill ${{a.severity}}">${{a.severity.toUpperCase()}}</span></td>
          <td><strong>${{a.asset_id}}</strong></td>
          <td><code>${{a.signal_type}}</code></td>
          <td class="num">${{a.value || '—'}}</td>
          <td style="font-size:0.78rem; color:var(--muted);">${{a.details}}</td>
        </tr>
      `).join('');

      document.querySelector('#tbl-inv tbody').innerHTML = DATA.inventory.map(inv => `
        <tr>
          <td><code>data/${{inv.file}}</code></td>
          <td class="num">${{inv.rows.toLocaleString()}}</td>
          <td class="num">${{inv.cols}}</td>
          <td>${{inv.date_min}}</td>
          <td>${{inv.date_max}}</td>
          <td class="num">${{inv.size_kb}} KB</td>
        </tr>
      `).join('');
    }}

    initDashboard();
  </script>
</body>
</html>
"""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(html_content, encoding="utf-8")


# ---------------------------------------------------------------------------
# CLI Entrypoint
# ---------------------------------------------------------------------------

def main() -> int:
    parser = argparse.ArgumentParser(
        description="Visualize all CSV datasets in the market_watcher `data/` folder."
    )
    parser.add_argument(
        "--data-dir",
        default="data",
        help="Path to the data directory (default: data)",
    )
    parser.add_argument(
        "--html",
        default="reports/dashboard.html",
        help="Output path for the self-contained interactive HTML dashboard (default: reports/dashboard.html)",
    )
    parser.add_argument(
        "--png",
        default="reports/data_overview.png",
        help="Output path for the 6-panel high-resolution PNG chart (default: reports/data_overview.png)",
    )
    parser.add_argument(
        "--quiet",
        action="store_true",
        help="Suppress terminal dashboard printing",
    )
    parser.add_argument(
        "--open",
        action="store_true",
        help="Open the generated HTML dashboard in the default browser (macOS `open`)",
    )

    args = parser.parse_args()
    data_dir = Path(args.data_dir)
    if not data_dir.exists():
        print(f"[ERR] Data directory not found: {data_dir}", file=sys.stderr)
        return 1

    ds = load_all_datasets(data_dir)

    if not args.quiet:
        render_terminal_dashboard(ds)

    html_path = Path(args.html)
    render_html_dashboard(ds, html_path)
    print(f"[OK] Generated interactive HTML dashboard -> {html_path}")

    png_path = Path(args.png)
    if render_png_overview(ds, png_path):
        print(f"[OK] Generated 6-panel PNG overview chart -> {png_path}")

    if args.open and sys.platform == "darwin":
        subprocess.run(["open", str(html_path)], check=False)

    return 0


if __name__ == "__main__":
    sys.exit(main())
