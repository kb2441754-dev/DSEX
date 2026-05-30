"""
DSEX Backend for OpenBB Workspace  ·  v2.0.0
=============================================
Dhaka Stock Exchange Broad Index (DSEX) — full-featured backend.

NEW in v2.0.0
─────────────
  • TradingView UDF feed  (/udf/config · /udf/time · /udf/search
                          /udf/symbols · /udf/history)
  • Company Profile        (/dsex_ticker_profile)
  • Valuation Metrics      (/dsex_valuation_metrics)
  • Price-Performance      (/dsex_price_performance)
  • Technical Gauge        (/dsex_technical_gauge)

Run:
    uvicorn main:app --reload --port 5050
"""

# ─────────────────────────────────────────────────────────────────────────────
# Standard-library / Third-party imports
# ─────────────────────────────────────────────────────────────────────────────

import json
import re
import time
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional

import requests
from bs4 import BeautifulSoup
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ─────────────────────────────────────────────────────────────────────────────
# FastAPI + CORS setup
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DSEX Backend",
    description="Dhaka Stock Exchange (DSEX) backend for OpenBB Workspace",
    version="2.0.0",
)

origins = [
    "https://pro.openbb.co",
    "https://pro.openbb.dev",
    "http://localhost:1420",
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ROOT_PATH = Path(__file__).parent.resolve()

# ─────────────────────────────────────────────────────────────────────────────
# Static constituent data
# ─────────────────────────────────────────────────────────────────────────────

CONSTITUENTS_DF = pd.read_csv(ROOT_PATH / "DSEX.csv")

# ─────────────────────────────────────────────────────────────────────────────
# Shared constants
# ─────────────────────────────────────────────────────────────────────────────

DSE_BASE = "https://www.dsebd.org"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 1: Existing helpers (unchanged) ─────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

_price_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 120  # seconds


def fetch_live_prices() -> list[dict]:
    """Scrape live prices for all DSE shares from dsebd.org (2-min cache)."""
    now = time.time()
    if _price_cache["data"] and (now - _price_cache["ts"]) < _CACHE_TTL:
        return _price_cache["data"]

    try:
        resp = requests.get(
            f"{DSE_BASE}/latest_share_price_scroll_l.php",
            headers=HEADERS, timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        rows = []
        for row in soup.select("table tr"):
            cols = row.find_all("td")
            if len(cols) >= 7:
                ticker = cols[1].get_text(strip=True)
                if not ticker:
                    continue
                try:
                    last       = float(cols[2].get_text(strip=True).replace(",", "") or 0)
                    high       = float(cols[3].get_text(strip=True).replace(",", "") or 0)
                    low        = float(cols[4].get_text(strip=True).replace(",", "") or 0)
                    close_prev = float(cols[5].get_text(strip=True).replace(",", "") or 0)
                    volume     = float(cols[6].get_text(strip=True).replace(",", "") or 0)
                    change     = round(last - close_prev, 2)
                    pct        = round((change / close_prev * 100), 2) if close_prev else 0.0
                    rows.append({
                        "Ticker": ticker, "Last": last,
                        "High": high, "Low": low,
                        "Prev Close": close_prev,
                        "Change": change, "Change %": pct,
                        "Volume": int(volume),
                    })
                except (ValueError, IndexError):
                    continue

        if rows:
            _price_cache["data"] = rows
            _price_cache["ts"]   = now
            return rows

    except Exception as e:
        print(f"[DSEX] Live price fetch failed: {e}")

    # Fallback — return constituent list with zero prices
    return [
        {"Ticker": r["Ticker"], "Last": 0.0, "High": 0.0, "Low": 0.0,
         "Prev Close": 0.0, "Change": 0.0, "Change %": 0.0, "Volume": 0}
        for _, r in CONSTITUENTS_DF.iterrows()
    ]


def fetch_index_summary() -> dict:
    """Scrape the DSEX index level from dsebd.org."""
    try:
        resp = requests.get(f"{DSE_BASE}/dseX_share.php", headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)
        m = re.search(r'DSEX[^\d]*([\d,]+\.?\d*)', text)
        value = float(m.group(1).replace(",", "")) if m else 0.0
        return {"index": "DSEX", "value": value}
    except Exception as e:
        print(f"[DSEX] Index summary fetch failed: {e}")
        return {"index": "DSEX", "value": 0.0}


def fetch_market_stats() -> dict:
    """Scrape market statistics from dsebd.org."""
    try:
        resp = requests.get(
            f"{DSE_BASE}/market-statistics.php", headers=HEADERS, timeout=10
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        stats = {}
        for row in soup.select("table tr"):
            cols = row.find_all("td")
            if len(cols) >= 2:
                key = cols[0].get_text(strip=True)
                val = cols[1].get_text(strip=True)
                if key and val:
                    stats[key] = val
        return stats
    except Exception as e:
        print(f"[DSEX] Market stats fetch failed: {e}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 2: NEW — bdshare helper layer ───────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

# In-memory cache: key → (DataFrame, epoch_stored)
_hist_cache: dict[str, tuple[pd.DataFrame, float]] = {}
_HIST_TTL = 3_600  # 1 hour — historical prices don't change intra-day


def _get_ohlcv(symbol: str, start: str, end: str) -> Optional[pd.DataFrame]:
    """
    Fetch basic OHLCV from bdshare with in-memory caching.

    Returns a DataFrame with columns: date (str), open, high, low, close, volume
    sorted ascending (oldest row first). Returns None on any error.

    The bdshare date column contains raw strings scraped from DSE's website
    (format varies but pd.to_datetime handles it with dayfirst=True).
    """
    cache_key = f"{symbol.upper()}|{start}|{end}"
    now = time.time()

    if cache_key in _hist_cache:
        df_cached, ts = _hist_cache[cache_key]
        if (now - ts) < _HIST_TTL:
            return df_cached.copy()

    try:
        import bdshare
        df = bdshare.get_basic_historical_data(start=start, end=end, code=symbol.upper())
        if df is None or df.empty:
            return None
        _hist_cache[cache_key] = (df.copy(), now)
        return df
    except Exception as e:
        print(f"[bdshare] {symbol} {start}→{end}: {e}")
        return None


def _df_with_datetime_index(df: pd.DataFrame) -> pd.DataFrame:
    """
    Parse the 'date' column (raw DSE string) into a proper DatetimeIndex.
    DSE uses formats like '01-Jan-2024', '01 Jan 2024', or '2024-01-01'.
    """
    df = df.copy()
    df["_dt"] = pd.to_datetime(df["date"], dayfirst=True, errors="coerce")
    df = df.dropna(subset=["_dt"]).set_index("_dt").sort_index()
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 3: Core endpoints (unchanged) ───────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {
        "info": "DSEX Backend for OpenBB Workspace",
        "version": "2.0.0",
        "endpoints": [
            "/widgets.json", "/apps.json",
            # v1 endpoints
            "/dsex_constituents", "/dsex_live_prices",
            "/dsex_top_gainers", "/dsex_top_losers",
            "/dsex_sector_summary", "/dsex_sector_chart",
            "/dsex_market_overview",
            # v2 endpoints
            "/udf/config", "/udf/time", "/udf/search",
            "/udf/symbols", "/udf/history",
            "/dsex_ticker_profile", "/dsex_valuation_metrics",
            "/dsex_price_performance", "/dsex_technical_gauge",
        ],
    }


@app.get("/widgets.json")
def get_widgets():
    return JSONResponse(content=json.load((ROOT_PATH / "widgets.json").open()))


@app.get("/apps.json")
def get_apps():
    return JSONResponse(content=json.load((ROOT_PATH / "apps.json").open()))


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 4: v1 widget endpoints (unchanged) ──────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dsex_constituents")
def dsex_constituents(
    sector:   Optional[str] = Query(default="All", description="Filter by Sector"),
    industry: Optional[str] = Query(default="All", description="Filter by Industry"),
):
    """Full DSEX constituent list with optional sector/industry filter."""
    df = CONSTITUENTS_DF.copy()
    if sector   and sector   != "All": df = df[df["Sector"]   == sector]
    if industry and industry != "All": df = df[df["Industry"] == industry]
    return df.to_dict(orient="records")


@app.get("/dsex_live_prices")
def dsex_live_prices(
    sector: Optional[str] = Query(default="All", description="Filter by Sector"),
):
    """Live prices merged with constituent metadata (2-min cache)."""
    prices = fetch_live_prices()
    merged = pd.DataFrame(prices).merge(
        CONSTITUENTS_DF[["Ticker", "Name", "Sector", "Industry"]],
        on="Ticker", how="left",
    )
    merged["Sector"]   = merged["Sector"].fillna("Unknown")
    merged["Industry"] = merged["Industry"].fillna("Unknown")
    merged["Name"]     = merged["Name"].fillna(merged["Ticker"])
    if sector and sector != "All":
        merged = merged[merged["Sector"] == sector]
    cols = ["Ticker", "Name", "Sector", "Industry",
            "Last", "Change", "Change %", "High", "Low", "Prev Close", "Volume"]
    return merged[[c for c in cols if c in merged.columns]].to_dict(orient="records")


@app.get("/dsex_top_gainers")
def dsex_top_gainers(
    limit: int = Query(default=20, description="Number of top gainers to show"),
):
    """Top DSEX gainers by % change today."""
    df = pd.DataFrame(fetch_live_prices())
    df = df[df["Change %"] > 0].sort_values("Change %", ascending=False).head(limit)
    merged = df.merge(CONSTITUENTS_DF[["Ticker", "Name", "Sector"]], on="Ticker", how="left")
    merged["Name"]   = merged["Name"].fillna(merged["Ticker"])
    merged["Sector"] = merged["Sector"].fillna("Unknown")
    cols = ["Ticker", "Name", "Sector", "Last", "Change", "Change %", "Volume"]
    return merged[[c for c in cols if c in merged.columns]].to_dict(orient="records")


@app.get("/dsex_top_losers")
def dsex_top_losers(
    limit: int = Query(default=20, description="Number of top losers to show"),
):
    """Top DSEX losers by % change today."""
    df = pd.DataFrame(fetch_live_prices())
    df = df[df["Change %"] < 0].sort_values("Change %", ascending=True).head(limit)
    merged = df.merge(CONSTITUENTS_DF[["Ticker", "Name", "Sector"]], on="Ticker", how="left")
    merged["Name"]   = merged["Name"].fillna(merged["Ticker"])
    merged["Sector"] = merged["Sector"].fillna("Unknown")
    cols = ["Ticker", "Name", "Sector", "Last", "Change", "Change %", "Volume"]
    return merged[[c for c in cols if c in merged.columns]].to_dict(orient="records")


@app.get("/dsex_sector_summary")
def dsex_sector_summary():
    """DSEX sector breakdown — companies and industries per sector."""
    df = CONSTITUENTS_DF.copy()
    sector_counts = (
        df.groupby("Sector")
        .agg(Companies=("Ticker", "count"), Industries=("Industry", "nunique"))
        .reset_index().sort_values("Companies", ascending=False)
    )
    industry_detail = (
        df.groupby(["Sector", "Industry"]).agg(Count=("Ticker", "count"))
        .reset_index().sort_values(["Sector", "Count"], ascending=[True, False])
    )
    return {
        "sector_summary":  sector_counts.to_dict(orient="records"),
        "industry_detail": industry_detail.to_dict(orient="records"),
    }


@app.get("/dsex_sector_chart")
def dsex_sector_chart(
    chart_type: str = Query(default="bar", description="Chart type: bar or pie"),
):
    """Plotly chart — DSEX constituents distribution by sector."""
    df = CONSTITUENTS_DF.copy()
    sector_counts = (
        df.groupby("Sector")["Ticker"].count().reset_index()
        .rename(columns={"Ticker": "Companies"})
        .sort_values("Companies", ascending=False)
    )
    if chart_type == "pie":
        fig = px.pie(
            sector_counts, names="Sector", values="Companies",
            title="DSEX Constituents by Sector",
            color_discrete_sequence=px.colors.qualitative.Set3,
        )
    else:
        fig = px.bar(
            sector_counts, x="Companies", y="Sector", orientation="h",
            title="DSEX Constituents by Sector",
            color="Companies", color_continuous_scale="teal",
            labels={"Companies": "# Listed Securities", "Sector": ""},
        )
        fig.update_layout(yaxis={"categoryorder": "total ascending"},
                          coloraxis_showscale=False)

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#FFFFFF"}, title_font_size=16,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
    )
    return json.loads(fig.to_json())


@app.get("/dsex_market_overview")
def dsex_market_overview():
    """DSEX market overview — index level + market statistics."""
    index = fetch_index_summary()
    stats = fetch_market_stats()
    overview = [
        {"Metric": "Index",       "Value": index.get("index", "DSEX")},
        {"Metric": "Index Value", "Value": index.get("value", "N/A")},
    ]
    for key, val in stats.items():
        if key and val and len(key) < 60:
            overview.append({"Metric": key, "Value": val})
    if len(overview) <= 2:
        overview = [
            {"Metric": "Index",              "Value": "DSEX"},
            {"Metric": "Base Date",          "Value": "27 Jan 2013"},
            {"Metric": "Base Value",         "Value": "1,000"},
            {"Metric": "Total Constituents", "Value": str(len(CONSTITUENTS_DF))},
            {"Metric": "Exchange",           "Value": "Dhaka Stock Exchange (DSE)"},
            {"Metric": "Country",            "Value": "Bangladesh"},
            {"Metric": "Currency",           "Value": "BDT (Bangladeshi Taka ৳)"},
            {"Metric": "Data Source",        "Value": "dsebd.org"},
            {"Metric": "Index Methodology",  "Value": "Float-adjusted market cap (S&P DJI)"},
        ]
    return overview


@app.get("/dsex_sector_options")
def dsex_sector_options():
    """Sector option list for downstream dropdown widgets."""
    sectors = sorted(CONSTITUENTS_DF["Sector"].dropna().unique().tolist())
    return [{"label": "All Sectors", "value": "All"}] + [
        {"label": s, "value": s} for s in sectors
    ]


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 5: v2 — TradingView UDF Protocol ────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/udf/config")
def udf_config():
    """
    UDF configuration endpoint.
    Tells TradingView which resolutions and exchange we support.
    We expose daily, weekly, and monthly since bdshare provides daily OHLCV.
    """
    return {
        "supported_resolutions":   ["D", "W", "M"],
        "supports_group_request":  False,
        "supports_marks":          False,
        "supports_search":         True,
        "supports_timescale_marks": False,
        "supports_time":           True,
        "exchanges": [
            {"value": "",    "name": "All Exchanges",       "desc": ""},
            {"value": "DSE", "name": "Dhaka Stock Exchange", "desc": "DSE — Dhaka, Bangladesh"},
        ],
        "symbols_types": [
            {"name": "All types", "value": ""},
            {"name": "Stocks",    "value": "stock"},
        ],
    }


@app.get("/udf/time")
def udf_time():
    """UDF server-time endpoint — returns current epoch integer for chart sync."""
    return int(time.time())


@app.get("/udf/search")
def udf_search(
    query: str = Query("",  description="Search query (ticker or company name)"),
    limit: int = Query(30,  description="Maximum number of results"),
):
    """
    UDF symbol-search endpoint.
    Filters CONSTITUENTS_DF by ticker prefix or company-name substring.
    """
    q = query.strip().lower()
    results = []
    for _, row in CONSTITUENTS_DF.iterrows():
        ticker = str(row["Ticker"]).strip()
        name   = str(row.get("Name", ticker)).strip()
        if q in ticker.lower() or q in name.lower():
            results.append({
                "symbol":      ticker,
                "full_name":   f"DSE:{ticker}",
                "description": name,
                "exchange":    "DSE",
                "ticker":      ticker,
                "type":        "stock",
            })
            if len(results) >= limit:
                break
    return results


@app.get("/udf/symbols")
def udf_symbols(
    symbol: str = Query(..., description="Ticker symbol (e.g. GP, SQURPHARMA)"),
):
    """
    UDF symbol-info endpoint.
    Returns metadata for a single DSE ticker: timezone, session hours, pricescale.
    """
    # Strip exchange prefix if provided (e.g. "DSE:GP" → "GP")
    clean = symbol.split(":")[-1].upper().strip()

    row  = CONSTITUENTS_DF[CONSTITUENTS_DF["Ticker"] == clean]
    name = str(row["Name"].values[0]) if not row.empty else clean

    return {
        "name":                    clean,
        "description":             name,
        "type":                    "stock",
        "session":                 "1000-1430",          # DSE trading hours (Sun–Thu)
        "timezone":                "Asia/Dhaka",
        "exchange":                "DSE",
        "listed_exchange":         "DSE",
        "minmov":                  1,
        "pricescale":              100,                  # 2 decimal places
        "has_intraday":            False,
        "has_daily":               True,
        "has_weekly_and_monthly":  True,
        "supported_resolutions":   ["D", "W", "M"],
        "volume_precision":        0,
        "has_volume":              True,
    }


@app.get("/udf/history")
def udf_history(
    symbol:    str = Query(..., description="Ticker symbol"),
    resolution: str = Query(..., description="Resolution: D, W, or M"),
    from_time: int = Query(..., alias="from", description="Start epoch (Unix)"),
    to_time:   int = Query(..., alias="to",   description="End epoch (Unix)"),
):
    """
    UDF historical-data endpoint — the core of the TradingView chart feed.

    Converts TradingView's Unix epoch range to YYYY-MM-DD strings for bdshare,
    fetches OHLCV via bdshare.get_basic_historical_data(), re-indexes with a
    proper DatetimeIndex, optionally resamples to W/M, and returns the UDF
    bar format: { s, t, o, h, l, c, v }.
    """
    try:
        clean = symbol.split(":")[-1].upper().strip()

        # ── Convert epoch boundaries to date strings ──────────────────────
        from_dt = datetime.utcfromtimestamp(from_time)
        to_dt   = datetime.utcfromtimestamp(to_time)

        # bdshare hard-limits date ranges to 5 years; clip if needed
        min_allowed = datetime.utcnow() - timedelta(days=365 * 5)
        if from_dt < min_allowed:
            from_dt = min_allowed

        from_str = from_dt.strftime("%Y-%m-%d")
        to_str   = to_dt.strftime("%Y-%m-%d")

        # ── Fetch OHLCV (cached) ──────────────────────────────────────────
        df = _get_ohlcv(clean, from_str, to_str)
        if df is None or df.empty:
            return {"s": "no_data", "nextTime": from_time}

        # ── Attach proper DatetimeIndex for resampling ────────────────────
        df = _df_with_datetime_index(df)
        if df.empty:
            return {"s": "no_data", "nextTime": from_time}

        # ── Resample for weekly / monthly ─────────────────────────────────
        if resolution in ("W", "M"):
            rule = "W-THU" if resolution == "W" else "ME"   # DSE week ends Thu
            try:
                df = (
                    df[["open", "high", "low", "close", "volume"]]
                    .resample(rule)
                    .agg({"open": "first", "high": "max",
                          "low":  "min",   "close": "last",
                          "volume": "sum"})
                    .dropna(subset=["open"])
                )
            except Exception:
                # pandas < 2.2 uses 'M' not 'ME'; fall back gracefully
                rule = "W-THU" if resolution == "W" else "M"
                df = (
                    df[["open", "high", "low", "close", "volume"]]
                    .resample(rule)
                    .agg({"open": "first", "high": "max",
                          "low":  "min",   "close": "last",
                          "volume": "sum"})
                    .dropna(subset=["open"])
                )

        if df.empty:
            return {"s": "no_data", "nextTime": from_time}

        # ── Build UDF response arrays ─────────────────────────────────────
        timestamps = [int(ts.timestamp()) for ts in df.index]
        opens      = [round(float(v), 2) for v in df["open"].tolist()]
        highs      = [round(float(v), 2) for v in df["high"].tolist()]
        lows       = [round(float(v), 2) for v in df["low"].tolist()]
        closes     = [round(float(v), 2) for v in df["close"].tolist()]
        volumes    = [int(v) for v in df["volume"].fillna(0).tolist()]

        return {"s": "ok", "t": timestamps,
                "o": opens, "h": highs, "l": lows,
                "c": closes, "v": volumes}

    except Exception as e:
        print(f"[UDF/history] {symbol}: {e}")
        return {"s": "no_data", "nextTime": from_time}


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 6: v2 — Sandbox-style analysis widgets ─────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

# ── 6a  Company Profile ──────────────────────────────────────────────────────

@app.get("/dsex_ticker_profile")
def dsex_ticker_profile(
    symbol: str = Query(default="GP", description="DSE ticker symbol"),
):
    """
    Key-value company profile card sourced from the static CONSTITUENTS_DF.
    Returns metadata rows: Ticker, Name, Sector, Industry, Region, Exchange.
    """
    try:
        clean = symbol.strip().upper()
        row   = CONSTITUENTS_DF[CONSTITUENTS_DF["Ticker"] == clean]

        if row.empty:
            return [{"Field": "Error", "Value": f"Ticker '{clean}' not found in DSEX."}]

        r = row.iloc[0]
        return [
            {"Field": "Ticker Symbol",   "Value": str(r.get("Ticker",   "N/A"))},
            {"Field": "Company Name",    "Value": str(r.get("Name",     "N/A"))},
            {"Field": "Primary Sector",  "Value": str(r.get("Sector",   "N/A"))},
            {"Field": "Industry Group",  "Value": str(r.get("Industry", "N/A"))},
            {"Field": "Market Region",   "Value": str(r.get("Country",  "Bangladesh"))},
            {"Field": "Trading Venue",   "Value": str(r.get("Exchange", "DSE"))},
        ]
    except Exception as e:
        return [{"Field": "Error", "Value": str(e)}]


# ── 6b  Valuation Metrics (dsebd.org scrape) ────────────────────────────────

def _scrape_valuation_metrics(symbol: str) -> list[dict]:
    """
    Scrape the DSE company-detail page for fundamental metrics.

    Target: https://www.dsebd.org/displayCompany.php?name={symbol}
    Extracts: Market Cap, P/E, EPS, Dividend Yield, Face Value, Authorised Capital.
    Falls back to a skeleton table with N/A values if the page is unavailable.
    """
    fallback = [
        {"Metric": "Market Capitalization", "Value": "N/A", "Unit": "BDT mn"},
        {"Metric": "Moving P/E Ratio",      "Value": "N/A", "Unit": "x"},
        {"Metric": "Basic EPS",             "Value": "N/A", "Unit": "BDT"},
        {"Metric": "Dividend Yield",        "Value": "N/A", "Unit": "%"},
        {"Metric": "Face Value",            "Value": "N/A", "Unit": "BDT"},
        {"Metric": "Authorized Capital",    "Value": "N/A", "Unit": "BDT mn"},
        {"Metric": "Data Source",           "Value": "dsebd.org (scrape failed)", "Unit": ""},
    ]

    try:
        url  = f"{DSE_BASE}/displayCompany.php?name={symbol.upper()}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")
        text = soup.get_text(" ", strip=True)

        def _find(pattern: str, fallback_val: str = "N/A") -> str:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(1).strip().replace(",", "") if m else fallback_val

        # ── Targeted regex patterns for DSE's company page ────────────────
        market_cap = _find(
            r'Market\s+Capitalization[^0-9]*([\d,]+(?:\.\d+)?)'
        )
        pe = _find(
            r'(?:Moving\s+)?P\s*/\s*E\s*Ratio[^0-9]*([+-]?[\d,]+(?:\.\d+)?)'
        )
        eps = _find(
            r'(?:Basic\s+)?EPS[^0-9]*([+-]?[\d,]+(?:\.\d+)?)'
        )
        div_yield_raw = re.search(
            r'Dividend\s+Yield[^0-9]*([+-]?[\d,]+(?:\.\d+)?)\s*%?', text, re.IGNORECASE
        )
        div_yield = (div_yield_raw.group(1).strip() + "%") if div_yield_raw else "N/A"
        face_value = _find(
            r'Face\s+Value[^0-9]*([\d,]+(?:\.\d+)?)'
        )
        auth_cap = _find(
            r'Authorized?\s+Capital[^0-9]*([\d,]+(?:\.\d+)?)'
        )

        return [
            {"Metric": "Market Capitalization", "Value": market_cap, "Unit": "BDT mn"},
            {"Metric": "Moving P/E Ratio",      "Value": pe,         "Unit": "x"},
            {"Metric": "Basic EPS",             "Value": eps,        "Unit": "BDT"},
            {"Metric": "Dividend Yield",        "Value": div_yield,  "Unit": "%"},
            {"Metric": "Face Value",            "Value": face_value, "Unit": "BDT"},
            {"Metric": "Authorized Capital",    "Value": auth_cap,   "Unit": "BDT mn"},
            {"Metric": "Data Source",           "Value": "dsebd.org",  "Unit": ""},
            {"Metric": "URL",                   "Value": url,          "Unit": ""},
        ]

    except Exception as e:
        print(f"[ValuationMetrics] {symbol}: {e}")
        fallback.append({"Metric": "Error", "Value": str(e), "Unit": ""})
        return fallback


@app.get("/dsex_valuation_metrics")
def dsex_valuation_metrics(
    symbol: str = Query(default="GP", description="DSE ticker symbol"),
):
    """
    Fundamental valuation metrics scraped from dsebd.org/displayCompany.php.
    Returns: Market Cap, P/E, EPS, Dividend Yield, Face Value, Authorised Capital.
    """
    return _scrape_valuation_metrics(symbol.strip().upper())


# ── 6c  Historical Price Performance ────────────────────────────────────────

@app.get("/dsex_price_performance")
def dsex_price_performance(
    symbol: str = Query(default="GP", description="DSE ticker symbol"),
):
    """
    Historical price-performance tracker using bdshare.

    Calculates total-return % over 4 standard lookback windows:
      1-Week (5 trading days) · 1-Month (22 td) · 3-Month (66 td) · 1-Year (250 td)

    Pulls the last 400 calendar days of daily OHLCV to cover all windows.
    """
    HORIZONS = [
        ("1-Week  (5 td)",   5),
        ("1-Month (22 td)",  22),
        ("3-Month (66 td)",  66),
        ("1-Year  (250 td)", 250),
    ]

    try:
        clean    = symbol.strip().upper()
        end_str  = datetime.utcnow().strftime("%Y-%m-%d")
        start_str = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")

        df = _get_ohlcv(clean, start_str, end_str)
        if df is None or df.empty:
            return [{"Horizon": "Error", "Return %": "N/A",
                     "Start Price": "N/A", "End Price": "N/A",
                     "Note": "No data from bdshare for this ticker."}]

        df = _df_with_datetime_index(df)
        closes = df["close"].dropna()

        if closes.empty:
            return [{"Horizon": "Error", "Return %": "N/A",
                     "Start Price": "N/A", "End Price": "N/A",
                     "Note": "Close prices are all NaN."}]

        current_price = float(closes.iloc[-1])
        rows = []

        for label, n_days in HORIZONS:
            if len(closes) >= n_days + 1:
                past_price  = float(closes.iloc[-(n_days + 1)])
                ret_pct     = round(((current_price - past_price) / past_price) * 100, 2)
                signal      = "▲" if ret_pct >= 0 else "▼"
                rows.append({
                    "Horizon":     label,
                    "Return %":    f"{signal} {abs(ret_pct):.2f}%",
                    "Start Price": f"৳ {past_price:,.2f}",
                    "End Price":   f"৳ {current_price:,.2f}",
                })
            else:
                rows.append({
                    "Horizon":     label,
                    "Return %":    "Insufficient data",
                    "Start Price": "N/A",
                    "End Price":   f"৳ {current_price:,.2f}",
                })

        rows.append({
            "Horizon":     "Current Price",
            "Return %":    "—",
            "Start Price": "—",
            "End Price":   f"৳ {current_price:,.2f}",
        })
        return rows

    except Exception as e:
        print(f"[PricePerformance] {symbol}: {e}")
        return [{"Horizon": "Error", "Return %": str(e),
                 "Start Price": "N/A", "End Price": "N/A"}]


# ── 6d  Moving Average Technical Gauge ──────────────────────────────────────

@app.get("/dsex_technical_gauge")
def dsex_technical_gauge(
    symbol: str = Query(default="GP", description="DSE ticker symbol"),
):
    """
    Moving-average technical dashboard gauge.

    Calculates the 20-day, 50-day, and 200-day Simple Moving Averages (SMA)
    from the last 400 calendar days of daily close data via bdshare.

    Returns a table per indicator showing:
      Indicator · Current Price · SMA Value · Market Condition
    where condition = 'Bullish (Above SMA)' or 'Bearish (Below SMA)'.
    """
    SMA_PERIODS = [
        ("SMA 20",  20),
        ("SMA 50",  50),
        ("SMA 200", 200),
    ]

    try:
        clean     = symbol.strip().upper()
        end_str   = datetime.utcnow().strftime("%Y-%m-%d")
        start_str = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")

        df = _get_ohlcv(clean, start_str, end_str)
        if df is None or df.empty:
            return [{"Indicator": "Error", "Current Price": "N/A",
                     "SMA Value": "N/A", "Market Condition": "No data from bdshare."}]

        df      = _df_with_datetime_index(df)
        closes  = df["close"].dropna()

        if closes.empty:
            return [{"Indicator": "Error", "Current Price": "N/A",
                     "SMA Value": "N/A", "Market Condition": "Close prices all NaN."}]

        current = float(closes.iloc[-1])
        rows    = []

        for label, period in SMA_PERIODS:
            if len(closes) >= period:
                sma_val   = round(float(closes.rolling(window=period).mean().iloc[-1]), 2)
                condition = "🟢 Bullish (Above SMA)" if current >= sma_val else "🔴 Bearish (Below SMA)"
                rows.append({
                    "Indicator":        label,
                    "Current Price":    f"৳ {current:,.2f}",
                    "SMA Value":        f"৳ {sma_val:,.2f}",
                    "Market Condition": condition,
                })
            else:
                rows.append({
                    "Indicator":        label,
                    "Current Price":    f"৳ {current:,.2f}",
                    "SMA Value":        "Insufficient data",
                    "Market Condition": f"Need ≥ {period} trading days",
                })

        return rows

    except Exception as e:
        print(f"[TechnicalGauge] {symbol}: {e}")
        return [{"Indicator": "Error", "Current Price": "N/A",
                 "SMA Value": "N/A", "Market Condition": str(e)}]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5050)
