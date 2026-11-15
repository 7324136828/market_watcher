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
