# market_watcher

Multi-Asset Market & Alternative Price Watcher (`omni-market-watcher`). See [`claude.md`](claude.md) for the full agent specification and [`data/README.md`](data/README.md) for the CSV data dictionary.

## Quick Start

### 1. Fetch Live Market Data Directly (`scripts/fetch_market_data.py`)
```bash
# Preview live data across all asset classes (Equities, Yields, Crypto, Prediction, Collectibles, Metals, GPUs)
python3 scripts/fetch_market_data.py --dry-run

# Fetch live data and update all CSVs in data/ + regenerate data/prices_wide.csv
python3 scripts/fetch_market_data.py --write-csv

# Fetch specific sections or tickers only
python3 scripts/fetch_market_data.py --section liquid macro crypto --dry-run
python3 scripts/fetch_market_data.py --section liquid --tickers SPY QQQ BTC-USD ^VIX --dry-run

# Compute rolling cross-asset Pearson correlation from data/prices_wide.csv
python3 scripts/fetch_market_data.py --correlate BTC-USD SPY --window 30 --lag 0
```

### 2. Visualize the `data/` Folder (`scripts/visualize_data.py`)
```bash
# Print terminal dashboard + generate reports/dashboard.html & reports/data_overview.png
python3 scripts/visualize_data.py

# Open the interactive HTML dashboard in your default browser
python3 scripts/visualize_data.py --open
```

### 3. Watch Fourteen Asset Classes (`scripts/watch_asset_classes.py`)
Stocks, bonds, cash, commodities, real estate, alternatives, ETFs, index funds, mutual funds, options,
futures & forwards, swaps, structured products & hybrids, and annuities.
```bash
# Preview in the terminal
python3 scripts/watch_asset_classes.py --dry-run

# Update data/asset_class*.csv and write a Markdown brief
python3 scripts/watch_asset_classes.py --write-csv --write-report reports/asset-classes-YYYY-MM-DD.md

# Only some classes, or list the watched universe
python3 scripts/watch_asset_classes.py --classes options swaps annuities --dry-run
python3 scripts/watch_asset_classes.py --list
```

### 4. Visualize the Asset-Class Watch (`scripts/visualize_asset_classes.py`)
```bash
# Terminal dashboard + reports/asset_classes_dashboard.html & reports/asset_classes_overview.png
python3 scripts/visualize_asset_classes.py
```
The PNG needs Pillow (`pip install pillow`); the terminal view and HTML dashboard need only the standard library.
