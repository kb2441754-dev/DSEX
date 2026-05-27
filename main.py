"""
DSEX Backend for OpenBB Workspace
==================================
Full Dhaka Stock Exchange Broad Index (DSEX) backend.
Serves live prices, constituents, sector breakdowns,
top gainers/losers, and market stats — all sourced from
dsebd.org (official DSE website).

Run:
    uvicorn main:app --reload --port 5050
"""

import json
import re
import time
from pathlib import Path
from typing import Optional
from functools import lru_cache

import requests
from bs4 import BeautifulSoup
import pandas as pd
import plotly.express as px
import plotly.graph_objects as go

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ─────────────────────────────────────────────
# App Setup
# ─────────────────────────────────────────────

app = FastAPI(
    title="DSEX Backend",
    description="Dhaka Stock Exchange Broad Index backend for OpenBB Workspace",
    version="1.0.0",
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

# ─────────────────────────────────────────────
# Static DSEX Constituents (from CSV)
# ─────────────────────────────────────────────

CONSTITUENTS_DF = pd.read_csv(ROOT_PATH / "DSEX.csv")

# ─────────────────────────────────────────────
# DSE Live Data Scraper
# ─────────────────────────────────────────────

DSE_BASE = "https://www.dsebd.org"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    )
}

_price_cache: dict = {"data": None, "ts": 0}
_CACHE_TTL = 120  # seconds


def fetch_live_prices() -> list[dict]:
    """
    Scrape live prices for all DSEX constituents from dsebd.org.
    Results are cached for 2 minutes to avoid hammering the server.
    """
    now = time.time()
    if _price_cache["data"] and (now - _price_cache["ts"]) < _CACHE_TTL:
        return _price_cache["data"]

    try:
        resp = requests.get(
            f"{DSE_BASE}/latest_share_price_scroll_l.php",
            headers=HEADERS,
            timeout=10,
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
                    last = float(cols[2].get_text(strip=True).replace(",", "") or 0)
                    high = float(cols[3].get_text(strip=True).replace(",", "") or 0)
                    low = float(cols[4].get_text(strip=True).replace(",", "") or 0)
                    close_prev = float(cols[5].get_text(strip=True).replace(",", "") or 0)
                    volume = float(cols[6].get_text(strip=True).replace(",", "") or 0)
                    change = round(last - close_prev, 2)
                    pct = round((change / close_prev * 100), 2) if close_prev else 0.0
                    rows.append({
                        "Ticker": ticker,
                        "Last": last,
                        "High": high,
                        "Low": low,
                        "Prev Close": close_prev,
                        "Change": change,
                        "Change %": pct,
                        "Volume": int(volume),
                    })
                except (ValueError, IndexError):
                    continue

        if rows:
            _price_cache["data"] = rows
            _price_cache["ts"] = now
            return rows

    except Exception as e:
        print(f"[DSEX] Live price fetch failed: {e}")

    # Fallback: return constituent list with zeros
    fallback = []
    for _, r in CONSTITUENTS_DF.iterrows():
        fallback.append({
            "Ticker": r["Ticker"],
            "Last": 0.0,
            "High": 0.0,
            "Low": 0.0,
            "Prev Close": 0.0,
            "Change": 0.0,
            "Change %": 0.0,
            "Volume": 0,
        })
    return fallback


def fetch_index_summary() -> dict:
    """Scrape the DSEX index level and change from dsebd.org."""
    try:
        resp = requests.get(
            f"{DSE_BASE}/dseX_share.php",
            headers=HEADERS,
            timeout=10,
        )
        resp.raise_for_status()
        soup = BeautifulSoup(resp.text, "html.parser")

        # Look for index value in the page
        text = soup.get_text(" ", strip=True)
        # DSE page typically shows "DSEX: 5,316.18 ▲ 17.59"
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
            f"{DSE_BASE}/market-statistics.php",
            headers=HEADERS,
            timeout=10,
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


# ─────────────────────────────────────────────
# Core Endpoints
# ─────────────────────────────────────────────

@app.get("/")
def read_root():
    return {
        "info": "DSEX Backend for OpenBB Workspace",
        "version": "1.0.0",
        "endpoints": [
            "/widgets.json",
            "/apps.json",
            "/dsex_constituents",
            "/dsex_live_prices",
            "/dsex_top_gainers",
            "/dsex_top_losers",
            "/dsex_sector_summary",
            "/dsex_sector_chart",
            "/dsex_market_overview",
        ],
    }


@app.get("/widgets.json")
def get_widgets():
    return JSONResponse(
        content=json.load((ROOT_PATH / "widgets.json").open())
    )


@app.get("/apps.json")
def get_apps():
    return JSONResponse(
        content=json.load((ROOT_PATH / "apps.json").open())
    )


# ─────────────────────────────────────────────
# Widget Endpoints
# ─────────────────────────────────────────────

@app.get("/dsex_constituents")
def dsex_constituents(
    sector: Optional[str] = Query(default="All", description="Filter by Sector"),
    industry: Optional[str] = Query(default="All", description="Filter by Industry"),
):
    """
    Full DSEX constituent list with optional sector/industry filter.
    Returns 389 listed securities with name, sector, industry metadata.
    """
    df = CONSTITUENTS_DF.copy()

    if sector and sector != "All":
        df = df[df["Sector"] == sector]

    if industry and industry != "All":
        df = df[df["Industry"] == industry]

    return df.to_dict(orient="records")


@app.get("/dsex_live_prices")
def dsex_live_prices(
    sector: Optional[str] = Query(default="All", description="Filter by Sector"),
):
    """
    Live DSEX prices scraped from dsebd.org.
    Merged with static constituent metadata (sector, industry).
    Cached for 2 minutes.
    """
    prices = fetch_live_prices()

    # Merge with constituent metadata
    price_df = pd.DataFrame(prices)
    merged = price_df.merge(
        CONSTITUENTS_DF[["Ticker", "Name", "Sector", "Industry"]],
        on="Ticker",
        how="left",
    )

    # Fill missing metadata for non-DSEX tickers
    merged["Sector"] = merged["Sector"].fillna("Unknown")
    merged["Industry"] = merged["Industry"].fillna("Unknown")
    merged["Name"] = merged["Name"].fillna(merged["Ticker"])

    if sector and sector != "All":
        merged = merged[merged["Sector"] == sector]

    # Reorder columns
    cols = ["Ticker", "Name", "Sector", "Industry", "Last", "Change", "Change %", "High", "Low", "Prev Close", "Volume"]
    merged = merged[[c for c in cols if c in merged.columns]]

    return merged.to_dict(orient="records")


@app.get("/dsex_top_gainers")
def dsex_top_gainers(
    limit: int = Query(default=20, description="Number of top gainers to show"),
):
    """Top DSEX gainers by % change (live prices)."""
    prices = fetch_live_prices()
    df = pd.DataFrame(prices)
    df = df[df["Change %"] > 0].sort_values("Change %", ascending=False).head(limit)

    merged = df.merge(
        CONSTITUENTS_DF[["Ticker", "Name", "Sector"]],
        on="Ticker", how="left"
    )
    merged["Name"] = merged["Name"].fillna(merged["Ticker"])
    merged["Sector"] = merged["Sector"].fillna("Unknown")

    cols = ["Ticker", "Name", "Sector", "Last", "Change", "Change %", "Volume"]
    merged = merged[[c for c in cols if c in merged.columns]]
    return merged.to_dict(orient="records")


@app.get("/dsex_top_losers")
def dsex_top_losers(
    limit: int = Query(default=20, description="Number of top losers to show"),
):
    """Top DSEX losers by % change (live prices)."""
    prices = fetch_live_prices()
    df = pd.DataFrame(prices)
    df = df[df["Change %"] < 0].sort_values("Change %", ascending=True).head(limit)

    merged = df.merge(
        CONSTITUENTS_DF[["Ticker", "Name", "Sector"]],
        on="Ticker", how="left"
    )
    merged["Name"] = merged["Name"].fillna(merged["Ticker"])
    merged["Sector"] = merged["Sector"].fillna("Unknown")

    cols = ["Ticker", "Name", "Sector", "Last", "Change", "Change %", "Volume"]
    merged = merged[[c for c in cols if c in merged.columns]]
    return merged.to_dict(orient="records")


@app.get("/dsex_sector_summary")
def dsex_sector_summary():
    """
    DSEX sector breakdown — number of listed companies per sector,
    computed from the full 389-constituent list.
    """
    df = CONSTITUENTS_DF.copy()

    # Count by sector
    sector_counts = (
        df.groupby("Sector")
        .agg(
            Companies=("Ticker", "count"),
            Industries=("Industry", "nunique"),
        )
        .reset_index()
        .sort_values("Companies", ascending=False)
    )

    # Also add industry-level detail
    industry_detail = (
        df.groupby(["Sector", "Industry"])
        .agg(Count=("Ticker", "count"))
        .reset_index()
        .sort_values(["Sector", "Count"], ascending=[True, False])
    )

    return {
        "sector_summary": sector_counts.to_dict(orient="records"),
        "industry_detail": industry_detail.to_dict(orient="records"),
    }


@app.get("/dsex_sector_chart")
def dsex_sector_chart(
    chart_type: str = Query(default="bar", description="Chart type: bar or pie"),
):
    """
    Plotly chart showing DSEX constituents distribution by sector.
    Returns a Plotly JSON figure.
    """
    df = CONSTITUENTS_DF.copy()
    sector_counts = (
        df.groupby("Sector")["Ticker"]
        .count()
        .reset_index()
        .rename(columns={"Ticker": "Companies"})
        .sort_values("Companies", ascending=False)
    )

    if chart_type == "pie":
        fig = px.pie(
            sector_counts,
            names="Sector",
            values="Companies",
            title="DSEX Constituents by Sector",
            color_discrete_sequence=px.colors.qualitative.Set3,
        )
    else:
        fig = px.bar(
            sector_counts,
            x="Companies",
            y="Sector",
            orientation="h",
            title="DSEX Constituents by Sector",
            color="Companies",
            color_continuous_scale="teal",
            labels={"Companies": "# Listed Securities", "Sector": ""},
        )
        fig.update_layout(
            yaxis={"categoryorder": "total ascending"},
            coloraxis_showscale=False,
        )

    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)",
        plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#FFFFFF"},
        title_font_size=16,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
    )

    return json.loads(fig.to_json())


@app.get("/dsex_market_overview")
def dsex_market_overview():
    """
    DSEX market overview: index level + market statistics.
    Data scraped from dsebd.org market statistics page.
    """
    index = fetch_index_summary()
    stats = fetch_market_stats()

    # Build a clean response
    overview = [
        {"Metric": "Index", "Value": index.get("index", "DSEX")},
        {"Metric": "Index Value", "Value": index.get("value", "N/A")},
    ]

    for key, val in stats.items():
        if key and val and len(key) < 60:
            overview.append({"Metric": key, "Value": val})

    # Fallback if scraping failed
    if len(overview) <= 2:
        overview = [
            {"Metric": "Index", "Value": "DSEX"},
            {"Metric": "Base Date", "Value": "27 Jan 2013"},
            {"Metric": "Base Value", "Value": "1,000"},
            {"Metric": "Total Constituents", "Value": str(len(CONSTITUENTS_DF))},
            {"Metric": "Exchange", "Value": "Dhaka Stock Exchange (DSE)"},
            {"Metric": "Country", "Value": "Bangladesh"},
            {"Metric": "Currency", "Value": "BDT (Bangladeshi Taka ৳)"},
            {"Metric": "Data Source", "Value": "dsebd.org"},
            {"Metric": "Index Methodology", "Value": "Float-adjusted market cap (S&P DJI)"},
        ]

    return overview


@app.get("/dsex_sector_options")
def dsex_sector_options():
    """Returns the list of sectors for use as dropdown options in other widgets."""
    sectors = sorted(CONSTITUENTS_DF["Sector"].dropna().unique().tolist())
    return [{"label": "All Sectors", "value": "All"}] + [
        {"label": s, "value": s} for s in sectors
    ]


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5050)
