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

# The run also saves the movers collection (up to 100 top and 100 bottom per class and horizon, plus an
# all-classes ranking) to data/asset_class_movers.csv; --movers-limit N keeps fewer

# Only some classes, or list the watched universe
python3 scripts/watch_asset_classes.py --classes options swaps annuities --dry-run
python3 scripts/watch_asset_classes.py --list
```

### 4. Visualize the Asset-Class Watch (`scripts/visualize_asset_classes.py`)
```bash
# Terminal dashboard + reports/asset_classes_dashboard.html & reports/asset_classes_overview.png
python3 scripts/visualize_asset_classes.py

# Top and bottom movers per class over another horizon (1d 5d 1m 3m ytd 1y), 1-100 per side in the terminal
python3 scripts/visualize_asset_classes.py --movers-horizon 1m --movers-count 25
```
The PNG needs Pillow (`pip install pillow`); the terminal view and HTML dashboard need only the standard library.

### 5. Watch Luxury Goods, Drug and Home Prices (`scripts/watch_goods_drugs_housing.py`)
Resale asking prices for 18 luxury brands and 57 iconic models, US prices for 31 drugs (Ozempic, Wegovy,
Keytruda, ...), and home prices for 269 regions in the US, Brazil, Europe and about 60 other economies.
```bash
# Preview in the terminal (about 4 minutes; the luxury feeds are fetched slowly on purpose)
python3 scripts/watch_goods_drugs_housing.py --dry-run

# Update the CSVs in data/ and write a Markdown brief
python3 scripts/watch_goods_drugs_housing.py --write-csv --write-report reports/goods-drugs-housing-YYYY-MM-DD.md

# Only some sections: luxury, drugs, housing
python3 scripts/watch_goods_drugs_housing.py --section drugs housing --dry-run
```

### 6. Visualize Them (`scripts/visualize_goods_drugs_housing.py`)
```bash
# Terminal dashboard + reports/goods_drugs_housing_dashboard.html & reports/goods_drugs_housing_overview.png
python3 scripts/visualize_goods_drugs_housing.py
```
