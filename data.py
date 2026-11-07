"""
Market data:
- weekly prices (adjusted for dividends and splits) of 15 DAX companies from Alpha Vantage
  (XETRA, free tier: 25 requests a day, so every response is cached in data/raw/)
- the ECB main refinancing rate from the Deutsche Bundesbank API, as the risk-free rate

    python data.py              # fetch whatever is missing or older than a week
"""
import csv
import io
import json
import os
import time
import urllib.request
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd

RAW = Path("data/raw")
API = "https://www.alphavantage.co/query?function=TIME_SERIES_WEEKLY_ADJUSTED&symbol={symbol}.DEX&apikey={key}"
BUNDESBANK = "https://api.statistiken.bundesbank.de/rest/data/BBIN1/M.D0.ECB.ECBMIN.EUR.ME"

# 15 large DAX companies with history back to 2005, across sectors
STOCKS = {
    "SAP": ("SAP", "Software"), "SIE": ("Siemens", "Industrials"), "ALV": ("Allianz", "Insurance"),
    "DTE": ("Deutsche Telekom", "Telecom"), "MBG": ("Mercedes-Benz", "Autos"), "BMW": ("BMW", "Autos"),
    "BAS": ("BASF", "Chemicals"), "BAYN": ("Bayer", "Pharma"), "MUV2": ("Munich Re", "Insurance"),
    "DBK": ("Deutsche Bank", "Banks"), "ADS": ("Adidas", "Consumer"), "IFX": ("Infineon", "Semiconductors"),
    "RWE": ("RWE", "Utilities"), "EOAN": ("E.ON", "Utilities"), "HEN3": ("Henkel", "Consumer"),
}


def api_key() -> str:
    key = os.environ.get("ALPHAVANTAGE_API_KEY")
    if not key and Path(".env").exists():
        key = next((line.split("=", 1)[1].strip() for line in Path(".env").read_text().splitlines()
                    if line.startswith("ALPHAVANTAGE_API_KEY=")), "")
    if not key:
        raise SystemExit("Set ALPHAVANTAGE_API_KEY (free key: https://www.alphavantage.co/support/#api-key)")
    return key


def stale(path: Path, days: int = 6) -> bool:
    return not path.exists() or datetime.now() - datetime.fromtimestamp(path.stat().st_mtime) > timedelta(days=days)


def fetch_prices() -> None:
    RAW.mkdir(parents=True, exist_ok=True)
    key = None
    for symbol in STOCKS:
        path = RAW / f"{symbol}.json"
        if not stale(path):
            continue
        key = key or api_key()
        time.sleep(2)                                   # free tier: at most one request per second
        with urllib.request.urlopen(API.format(symbol=symbol, key=key), timeout=60) as response:
            data = json.load(response)
        if "Weekly Adjusted Time Series" not in data:
            raise RuntimeError(f"{symbol}: {str(data)[:200]}")
        path.write_text(json.dumps(data))
        print(f"{symbol}: {len(data['Weekly Adjusted Time Series'])} weeks")


def fetch_rate() -> None:
    path = RAW / "ecb_rate.csv"
    if stale(path):
        request = urllib.request.Request(BUNDESBANK, headers={"Accept": "text/csv"})
        with urllib.request.urlopen(request, timeout=60) as response:
            path.write_bytes(response.read())


def parse_weekly(data: dict) -> pd.Series:
    series = data["Weekly Adjusted Time Series"]
    prices = pd.Series({pd.Timestamp(day): float(row["5. adjusted close"]) for day, row in series.items()})
    return prices.sort_index()


def prices() -> pd.DataFrame:
    """Adjusted weekly closes, one column per stock. Weeks where any stock is missing are dropped."""
    table = pd.DataFrame({s: parse_weekly(json.loads((RAW / f"{s}.json").read_text())) for s in STOCKS})
    return table.dropna()


def returns() -> pd.DataFrame:
    """Weekly simple returns."""
    return prices().pct_change().dropna()


def portfolio_returns(stock_returns: pd.DataFrame) -> pd.Series:
    """Equal-weight portfolio, rebalanced every week."""
    return stock_returns.mean(axis=1).rename("portfolio")


def ecb_rate() -> pd.Series:
    """Monthly ECB main refinancing rate in % p.a., from the Bundesbank's statistics API."""
    rows = list(csv.reader(io.StringIO((RAW / "ecb_rate.csv").read_text(encoding="utf-8")), delimiter=";"))
    values = {}
    for row in rows:
        if len(row) > 8 and row[0].startswith("BBK:") and row[8] not in ("", "."):
            values[pd.Period(row[7], "M")] = float(row[8])
    return pd.Series(values).sort_index()


def weekly_risk_free(index: pd.DatetimeIndex) -> pd.Series:
    """The ECB rate in force each week, as a weekly return."""
    rate = ecb_rate()
    monthly = rate.reindex(pd.period_range(rate.index.min(), index.max().to_period("M"), freq="M")).ffill()
    return pd.Series(np.asarray(monthly.reindex(index.to_period("M")).fillna(0)) / 100 / 52, index=index)


if __name__ == "__main__":
    fetch_prices()
    fetch_rate()
    table = prices()
    print(f"{len(table)} complete weeks, {table.index.min().date()} to {table.index.max().date()}")
