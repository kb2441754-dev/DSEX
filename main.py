"""
DSEX Backend for OpenBB Workspace  ·  v3.0.0
=============================================
Dhaka Stock Exchange Broad Index (DSEX) — full-featured backend.

v3.0.0 — bd-stock-api integration
───────────────────────────────────
• Replaced bdshare with direct DSE scraping, mirroring the
  DsePriceService.ts architecture from bd-stock-api-main:
    - Universal _dse_scrape() mirrors parseTableRows()
    - Retry-with-backoff mirrors axiosRetry config (3 retries, exponential)
    - All 4 DSE URLs sourced from env.ts
    - Historical date format confirmed YYYY-MM-DD (sample_history.json)
    - Header-based field mapping replaces brittle column-index parsing
• Switched live DSEX prices to dseX_share.php (index-specific page)
• NEW: /dsex_top30  — DSE30 index live prices (dse30_share.php)
• Removed bdshare dependency entirely

Run:
    uvicorn main:app --reload --port 5050
"""

# ─────────────────────────────────────────────────────────────────────────────
# Imports
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

from fastapi import FastAPI, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

# ─────────────────────────────────────────────────────────────────────────────
# App setup
# ─────────────────────────────────────────────────────────────────────────────

app = FastAPI(
    title="DSEX Backend",
    description="Dhaka Stock Exchange (DSEX) backend for OpenBB Workspace — v3.1.0",
    version="3.1.0",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=[
        "https://pro.openbb.co",
        "https://pro.openbb.dev",
        "http://localhost:1420",
    ],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

ROOT_PATH = Path(__file__).parent.resolve()

# ─────────────────────────────────────────────────────────────────────────────
# Static data
# ─────────────────────────────────────────────────────────────────────────────

CONSTITUENTS_DF = pd.read_csv(ROOT_PATH / "DSEX.csv")

# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 1: DSE URL registry (sourced from bd-stock-api env.ts) ──────────
# ─────────────────────────────────────────────────────────────────────────────

DSE_BASE = "https://dsebd.org"

DSE_URLS = {
    # All DSE shares — latest_share_price_scroll_l.php
    "LATEST":     f"{DSE_BASE}/latest_share_price_scroll_l.php",
    # DSEX index constituents only — dseX_share.php
    "DSEX":       f"{DSE_BASE}/dseX_share.php",
    # DSE30 index — dse30_share.php
    "TOP30":      f"{DSE_BASE}/dse30_share.php",
    # Daily OHLCV archive — day_end_archive.php
    "HISTORICAL": f"{DSE_BASE}/day_end_archive.php",
    # Market statistics page
    "MARKET":     f"{DSE_BASE}/market-statistics.php",
    # Company detail page (fundamental metrics)
    "COMPANY":    f"{DSE_BASE}/displayCompany.php",
}

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 2: Universal DSE scraper (mirrors DsePriceService.ts) ───────────
# ─────────────────────────────────────────────────────────────────────────────

def _dse_scrape(
    url: str,
    params: dict | None = None,
    use_tbody: bool = False,
) -> list[dict]:
    """
    Universal DSE HTML-table → list[dict] scraper.

    Mirrors DsePriceService.parseTableRows() from bd-stock-api:
      • Headers sourced from table.table.table-bordered first-row <th> elements
        (mirrors getCurrentTradingCodes)
      • use_tbody=False  → live pages: table.table-bordered tr, skip row 0
        (mirrors parseTableRows($, "table.table-bordered tr", skipFirstRow=true))
      • use_tbody=True   → historical: table.table-bordered tbody tr, no skip
        (mirrors parseTableRows($, "table.table-bordered tbody tr", false))

    Retry logic: 3 attempts with exponential back-off
    (mirrors axiosRetry: { retries:3, retryDelay: exponentialDelay })
    """
    resp = None
    last_exc: Exception | None = None

    for attempt in range(3):
        try:
            resp = requests.get(url, params=params, headers=HEADERS, timeout=15)
            resp.raise_for_status()
            last_exc = None
            break
        except requests.RequestException as exc:
            last_exc = exc
            if attempt < 2:
                time.sleep(0.5 * (2 ** attempt))   # 0.5 s → 1 s

    if last_exc:
        raise last_exc

    soup = BeautifulSoup(resp.text, "html.parser")

    # ── Step 1: extract headers (mirrors getCurrentTradingCodes) ─────────────
    # Prefer table with BOTH classes "table" and "table-bordered"
    tbl = (
        soup.select_one("table.table.table-bordered")
        or soup.select_one("table.table-bordered")
    )
    if not tbl:
        return []

    first_row = tbl.find("tr")
    if not first_row:
        return []

    headers = [th.get_text(strip=True) for th in first_row.find_all("th")]
    if not headers:
        return []

    # ── Step 2: select data rows ──────────────────────────────────────────────
    if use_tbody:
        # Historical page: explicit <tbody>
        data_rows = tbl.select("tbody tr")
    else:
        # Live price pages: all <tr>, skip first (header) row
        data_rows = tbl.select("tr")[1:]

    # ── Step 3: map columns → dict ────────────────────────────────────────────
    records: list[dict] = []
    for tr in data_rows:
        tds = tr.find_all("td")
        if not tds:
            continue
        row: dict = {}
        for idx, header in enumerate(headers):
            if idx < len(tds):
                row[header] = tds[idx].get_text(strip=True).replace(",", "")
        if any(v.strip() for v in row.values()):
            records.append(row)

    return records


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 3: Field normalizers ────────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

def _flt(record: dict, *keys: str, default: float = 0.0) -> float:
    """
    Try multiple key variants and return the first parseable float.
    Handles DSE-style signed strings: '+3.50', '-2.10', '1,234.56'.
    Python's float() natively accepts leading '+'/'-', so no extra stripping needed.
    """
    for k in keys:
        raw = record.get(k, "").strip().replace(",", "")
        if raw:
            try:
                return float(raw)
            except ValueError:
                pass
    return default


def _normalize_live_records(records: list[dict]) -> list[dict]:
    """
    Map raw DSE live-price table rows into clean standardised dicts.

    DSE live-page column layout (dseX_share.php / dse30_share.php):
    #  |  TRADING CODE  |  LTP*  |  HIGH  |  LOW  |  CLOSEP*  |
    YCP*  |  CHANGE  |  TRADE  |  VALUE (mn)  |  VOLUME

    ── Change & % Change formula (exact DSE rules) ──────────────────
    DSE uses the pre-computed CHANGE column which already accounts
    for whether YCP or OAP was used:

      When YCP* is available (> 0):
        CHANGE   = LTP − YCP
        %CHANGE  = (CHANGE × 100) / YCP

      Otherwise (corporate action → OAP used as reference):
        CHANGE   = LTP − OAP      ← DSE provides this in CHANGE column
        %CHANGE  = (CHANGE × 100) / OAP
        OAP is back-calculated as: OAP = LTP − CHANGE

    We always read CHANGE from the DSE table first (it is already
    correct for both cases). We only recompute it as a fallback when
    the column is absent or zero on a traded stock.
    """
    out: list[dict] = []

    for r in records:
        ticker = r.get("TRADING CODE", "").strip()
        if not ticker or ticker in ("#", "TRADING CODE"):
            continue

        ltp    = _flt(r, "LTP*",    "LTP")
        high   = _flt(r, "HIGH")
        low    = _flt(r, "LOW")
        closep = _flt(r, "CLOSEP*", "CLOSEP", "CLOSE")
        ycp    = _flt(r, "YCP*",    "YCP")
        vol    = int(_flt(r, "VOLUME", default=0))
        trade  = int(_flt(r, "TRADE",  default=0))

        # ── Step 1: absolute CHANGE ──────────────────────────────────
        # Prefer DSE's pre-computed CHANGE column — it correctly uses
        # YCP or OAP depending on whether a corporate action occurred.
        dse_change = _flt(r, "CHANGE")

        if dse_change != 0:
            # DSE column is available and non-zero — trust it directly
            change = round(dse_change, 2)
        elif ycp > 0:
            # DSE column absent/zero but YCP is valid — compute ourselves
            change = round(ltp - ycp, 2)
        else:
            change = 0.0

        # ── Step 2: %CHANGE ──────────────────────────────────────────
        # YCP available → use it as divisor (standard case)
        if ycp > 0:
            pct = round((change / ycp) * 100, 2)

        # YCP = 0 → corporate action day; OAP was used by DSE.
        # Back-calculate OAP = LTP − CHANGE (rearranging DSE's formula).
        elif change != 0 and ltp > 0:
            oap = ltp - change
            pct = round((change / oap) * 100, 2) if oap > 0 else 0.0

        else:
            pct = 0.0

        out.append({
            "Ticker":        ticker,
            "Last":          ltp,
            "High":          high,
            "Low":           low,
            "Close":         closep,
            "Prev Close":    ycp if ycp > 0 else None,   # None signals OAP day
            "Change":        change,
            "Change %":      pct,
            "Volume":        vol,
            "Trades":        trade,
        })

    return out


def _normalize_hist_records(records: list[dict]) -> pd.DataFrame:
    """
    Convert raw DSE historical table rows into a clean OHLCV DataFrame.

    DSE historical page headers (day_end_archive.php):
    #  |  DATE  |  TRADING CODE  |  LTP  |  HIGH  |  LOW  |
    OPENP  |  CLOSEP  |  YCP  |  TRADE  |  VALUE (mn)  |  VOLUME

    Date format: YYYY-MM-DD (confirmed from sample_history.json in bd-stock-api).
    Returns DataFrame sorted ascending (oldest row first) with columns:
    date (datetime64), open, high, low, close, volume
    """
    if not records:
        return pd.DataFrame()

    normalized: list[dict] = []
    for r in records:
        # Strip asterisks & normalise to uppercase for robust key matching
        cleaned = {k.upper().replace("*", "").strip(): v for k, v in r.items()}

        def _g(*variants: str) -> float | None:
            for v in variants:
                raw = cleaned.get(v, "").strip().replace(",", "")
                if raw:
                    try:
                        return float(raw)
                    except ValueError:
                        pass
            return None

        date_raw = cleaned.get("DATE", "").strip()
        if not date_raw:
            continue

        normalized.append({
            "date":   date_raw,
            "open":   _g("OPENP", "OPEN", "LTP"),
            "high":   _g("HIGH"),
            "low":    _g("LOW"),
            "close":  _g("CLOSEP", "CLOSE", "LTP"),
            "volume": _g("VOLUME") or 0.0,
        })

    if not normalized:
        return pd.DataFrame()

    df = pd.DataFrame(normalized)
    # date is YYYY-MM-DD from DSE (sample_history.json confirms this)
    df["date"] = pd.to_datetime(df["date"], format="%Y-%m-%d", errors="coerce")
    df = df.dropna(subset=["date", "close"]).sort_values("date").reset_index(drop=True)
    return df


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 4: Cached data-fetch helpers ────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

_LIVE_TTL = 120     # 2 minutes  — live prices
_HIST_TTL = 3_600   # 60 minutes — historical OHLCV

# Live caches  (url_key → {data, ts})
_live_cache: dict[str, dict] = {}

# Historical cache  (symbol|start|end → (DataFrame, ts))
_hist_cache: dict[str, tuple[pd.DataFrame, float]] = {}


def _fetch_live(url_key: str) -> list[dict]:
    """
    Fetch & cache live price records from a DSE page.
    url_key must be one of 'DSEX', 'TOP30', or 'LATEST'.
    """
    cache = _live_cache.setdefault(url_key, {"data": None, "ts": 0.0})
    now   = time.time()

    if cache["data"] and (now - cache["ts"]) < _LIVE_TTL:
        return cache["data"]

    try:
        raw      = _dse_scrape(DSE_URLS[url_key], use_tbody=False)
        records  = _normalize_live_records(raw)
        if records:
            cache["data"] = records
            cache["ts"]   = now
            return records
    except Exception as exc:
        print(f"[live/{url_key}] {exc}")

    # Fallback: return last known good data (stale) or empty list
    return cache["data"] or []


def _get_ohlcv(symbol: str, start: str, end: str) -> pd.DataFrame | None:
    """
    Fetch & cache OHLCV data from DSE's day_end_archive.php.

    Directly mirrors DsePriceService.getHistData() from bd-stock-api:
      URL:    dsebd.org/day_end_archive.php
      Params: startDate, endDate, inst, archive=data
      Table:  table.table-bordered tbody tr  (use_tbody=True)

    Results cached for 60 minutes.
    """
    key = f"{symbol.upper()}|{start}|{end}"
    now = time.time()

    if key in _hist_cache:
        df_c, ts = _hist_cache[key]
        if (now - ts) < _HIST_TTL:
            return df_c.copy()

    try:
        raw = _dse_scrape(
            DSE_URLS["HISTORICAL"],
            params={
                "startDate": start,
                "endDate":   end,
                "inst":      symbol.upper(),
                "archive":   "data",
            },
            use_tbody=True,
        )
        df = _normalize_hist_records(raw)
        if df.empty:
            return None
        _hist_cache[key] = (df.copy(), now)
        return df
    except Exception as exc:
        print(f"[OHLCV] {symbol} {start}→{end}: {exc}")
        return None


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 5: Market-level helpers (index summary + stats) ─────────────────
# ─────────────────────────────────────────────────────────────────────────────

def fetch_index_summary() -> dict:
    """Scrape the DSEX index level from dseX_share.php."""
    try:
        resp = requests.get(DSE_URLS["DSEX"], headers=HEADERS, timeout=10)
        resp.raise_for_status()
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)
        m    = re.search(r'DSEX[^\d]*([\d,]+\.?\d*)', text)
        return {"index": "DSEX", "value": float(m.group(1).replace(",", "")) if m else 0.0}
    except Exception as exc:
        print(f"[index_summary] {exc}")
        return {"index": "DSEX", "value": 0.0}


def fetch_market_stats() -> dict:
    """Scrape market statistics from market-statistics.php."""
    try:
        resp = requests.get(DSE_URLS["MARKET"], headers=HEADERS, timeout=10)
        resp.raise_for_status()
        soup  = BeautifulSoup(resp.text, "html.parser")
        stats = {}
        for row in soup.select("table tr"):
            cols = row.find_all("td")
            if len(cols) >= 2:
                k = cols[0].get_text(strip=True)
                v = cols[1].get_text(strip=True)
                if k and v:
                    stats[k] = v
        return stats
    except Exception as exc:
        print(f"[market_stats] {exc}")
        return {}


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 6: Core FastAPI routes ──────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/")
def read_root():
    return {
        "name":    "DSEX Backend for OpenBB Workspace",
        "version": "3.0.0",
        "data_layer": "Direct DSE scraping (bd-stock-api architecture, no bdshare)",
        "endpoints": [
            "/widgets.json", "/apps.json",
            # v1 — live market
            "/dsex_market_overview", "/dsex_live_prices",
            "/dsex_top_gainers",     "/dsex_top_losers",
            "/dsex_top30",
            # v1 — static / sector
            "/dsex_constituents",   "/dsex_sector_summary",
            "/dsex_sector_chart",
            # v2 — TradingView UDF
            "/udf/config",   "/udf/time",
            "/udf/search",   "/udf/symbols",  "/udf/history",
            # v2 — company analysis
            "/dsex_ticker_profile",    "/dsex_valuation_metrics",
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
# ─── SECTION 7: Live market widgets ──────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dsex_market_overview")
def dsex_market_overview():
    """DSEX index level + full market statistics. Scraped from dsebd.org."""
    index = fetch_index_summary()
    stats = fetch_market_stats()

    overview = [
        {"Metric": "Index",       "Value": index.get("index", "DSEX")},
        {"Metric": "Index Value", "Value": index.get("value", "N/A")},
    ]
    for k, v in stats.items():
        if k and v and len(k) < 60:
            overview.append({"Metric": k, "Value": v})

    if len(overview) <= 2:
        overview = [
            {"Metric": "Index",              "Value": "DSEX"},
            {"Metric": "Base Date",          "Value": "27 Jan 2013"},
            {"Metric": "Base Value",         "Value": "1,000"},
            {"Metric": "Total Constituents", "Value": str(len(CONSTITUENTS_DF))},
            {"Metric": "Exchange",           "Value": "Dhaka Stock Exchange (DSE)"},
            {"Metric": "Country",            "Value": "Bangladesh"},
            {"Metric": "Currency",           "Value": "BDT (Bangladeshi Taka ৳)"},
            {"Metric": "Trading Hours",      "Value": "10:00–14:30 BST (Sun–Thu)"},
            {"Metric": "Data Source",        "Value": "dsebd.org"},
            {"Metric": "Index Methodology",  "Value": "Float-adjusted market cap (S&P DJI)"},
        ]
    return overview


@app.get("/dsex_live_prices")
def dsex_live_prices(
    sector: Optional[str] = Query(default="All", description="Filter by Sector"),
    index:  Optional[str] = Query(default="All", description="Filter: All / DSEX / Non-DSEX"),
):
    """
    Live prices for ALL DSE-listed companies.
    Source: latest_share_price_scroll_l.php — full DSE market scroll.

    Switched from dseX_share.php (DSEX index only) so that non-DSEX
    companies (SME board, Z-category, newly listed, etc.) are included.
    DSEX index membership is flagged in the 'DSEX Index' column.
    """
    records = _fetch_live("LATEST")
    if not records:
        return []

    df = pd.DataFrame(records)

    # Merge DSEX metadata; non-DSEX tickers get NaN → filled below
    df = df.merge(
        CONSTITUENTS_DF[["Ticker", "Name", "Sector", "Industry"]],
        on="Ticker", how="left"
    )

    # DSEX index membership flag
    dsex_set = set(CONSTITUENTS_DF["Ticker"].str.strip())
    df["DSEX Index"] = df["Ticker"].apply(lambda t: "✓" if t in dsex_set else "")

    df["Sector"]   = df["Sector"].fillna("—")
    df["Industry"] = df["Industry"].fillna("—")
    df["Name"]     = df["Name"].fillna(df["Ticker"])

    if sector and sector != "All":
        df = df[df["Sector"] == sector]
    if index == "DSEX":
        df = df[df["DSEX Index"] == "✓"]
    elif index == "Non-DSEX":
        df = df[df["DSEX Index"] == ""]

    cols = ["Ticker", "Name", "DSEX Index", "Sector", "Industry",
            "Last", "Change", "Change %", "High", "Low",
            "Close", "Prev Close", "Volume", "Trades"]
    return df[[c for c in cols if c in df.columns]].to_dict(orient="records")


@app.get("/dsex_top30")
def dsex_top30():
    """
    DSE30 index live prices.
    Source: dse30_share.php  — the 30 largest blue-chip DSE stocks.
    New in v3.0.0 — powered by bd-stock-api URL registry.
    """
    records = _fetch_live("TOP30")

    if not records:
        return []

    merged = (
        pd.DataFrame(records)
        .merge(CONSTITUENTS_DF[["Ticker", "Name", "Sector", "Industry"]],
               on="Ticker", how="left")
    )
    merged["Sector"]   = merged["Sector"].fillna("DSE30")
    merged["Industry"] = merged["Industry"].fillna("—")
    merged["Name"]     = merged["Name"].fillna(merged["Ticker"])

    cols = ["Ticker", "Name", "Sector", "Industry",
            "Last", "Change", "Change %", "High", "Low",
            "Close", "Prev Close", "Volume", "Trades"]
    return merged[[c for c in cols if c in merged.columns]].to_dict(orient="records")


@app.get("/dsex_top_gainers")
def dsex_top_gainers(
    limit: int = Query(default=20, description="Number of results"),
):
    """Top gainers across ALL DSE-listed companies (from latest_share_price_scroll_l.php)."""
    df = pd.DataFrame(_fetch_live("LATEST"))
    if df.empty:
        return []
    df = df[df["Change %"] > 0].sort_values("Change %", ascending=False).head(limit)
    merged = df.merge(CONSTITUENTS_DF[["Ticker", "Name", "Sector"]], on="Ticker", how="left")
    merged["Name"]   = merged["Name"].fillna(merged["Ticker"])
    merged["Sector"] = merged["Sector"].fillna("Unknown")
    cols = ["Ticker", "Name", "Sector", "Last", "Change", "Change %", "Volume"]
    return merged[[c for c in cols if c in merged.columns]].to_dict(orient="records")


@app.get("/dsex_top_losers")
def dsex_top_losers(
    limit: int = Query(default=20, description="Number of results"),
):
    """Top losers across ALL DSE-listed companies (from latest_share_price_scroll_l.php)."""
    df = pd.DataFrame(_fetch_live("LATEST"))
    if df.empty:
        return []
    df = df[df["Change %"] < 0].sort_values("Change %", ascending=True).head(limit)
    merged = df.merge(CONSTITUENTS_DF[["Ticker", "Name", "Sector"]], on="Ticker", how="left")
    merged["Name"]   = merged["Name"].fillna(merged["Ticker"])
    merged["Sector"] = merged["Sector"].fillna("Unknown")
    cols = ["Ticker", "Name", "Sector", "Last", "Change", "Change %", "Volume"]
    return merged[[c for c in cols if c in merged.columns]].to_dict(orient="records")


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 8: Static / sector widgets ──────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dsex_constituents")
def dsex_constituents(
    sector:   Optional[str] = Query(default="All"),
    industry: Optional[str] = Query(default="All"),
):
    """Full 389-constituent list with optional sector/industry filter."""
    df = CONSTITUENTS_DF.copy()
    if sector   and sector   != "All": df = df[df["Sector"]   == sector]
    if industry and industry != "All": df = df[df["Industry"] == industry]
    return df.to_dict(orient="records")


@app.get("/dsex_sector_summary")
def dsex_sector_summary():
    """Companies and unique industries per sector."""
    df     = CONSTITUENTS_DF.copy()
    sector = (
        df.groupby("Sector")
        .agg(Companies=("Ticker", "count"), Industries=("Industry", "nunique"))
        .reset_index().sort_values("Companies", ascending=False)
    )
    detail = (
        df.groupby(["Sector", "Industry"]).agg(Count=("Ticker", "count"))
        .reset_index().sort_values(["Sector", "Count"], ascending=[True, False])
    )
    return {"sector_summary": sector.to_dict(orient="records"),
            "industry_detail": detail.to_dict(orient="records")}


@app.get("/dsex_sector_chart")
def dsex_sector_chart(
    chart_type: str = Query(default="bar", description="bar or pie"),
):
    """Plotly chart — constituent distribution by sector."""
    counts = (
        CONSTITUENTS_DF.groupby("Sector")["Ticker"].count()
        .reset_index().rename(columns={"Ticker": "Companies"})
        .sort_values("Companies", ascending=False)
    )
    if chart_type == "pie":
        fig = px.pie(counts, names="Sector", values="Companies",
                     title="DSEX Constituents by Sector",
                     color_discrete_sequence=px.colors.qualitative.Set3)
    else:
        fig = px.bar(counts, x="Companies", y="Sector", orientation="h",
                     title="DSEX Constituents by Sector",
                     color="Companies", color_continuous_scale="teal",
                     labels={"Companies": "# Listed Securities", "Sector": ""})
        fig.update_layout(yaxis={"categoryorder": "total ascending"},
                          coloraxis_showscale=False)
    fig.update_layout(
        paper_bgcolor="rgba(0,0,0,0)", plot_bgcolor="rgba(0,0,0,0)",
        font={"color": "#FFFFFF"}, title_font_size=16,
        margin={"l": 10, "r": 10, "t": 40, "b": 10},
    )
    return json.loads(fig.to_json())


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 9: TradingView UDF feed ─────────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/udf/config")
def udf_config():
    """UDF config — D/W/M resolutions, DSE exchange info."""
    return {
        "supported_resolutions":    ["D", "W", "M"],
        "supports_group_request":   False,
        "supports_marks":           False,
        "supports_search":          True,
        "supports_timescale_marks": False,
        "supports_time":            True,
        "exchanges": [
            {"value": "",    "name": "All Exchanges",        "desc": ""},
            {"value": "DSE", "name": "Dhaka Stock Exchange", "desc": "DSE — Dhaka, Bangladesh"},
        ],
        "symbols_types": [
            {"name": "All types", "value": ""},
            {"name": "Stocks",    "value": "stock"},
        ],
    }


@app.get("/udf/time")
def udf_time():
    """UDF server time — current epoch integer."""
    return int(time.time())


@app.get("/udf/search")
def udf_search(
    query: str = Query(""),
    limit: int = Query(30),
):
    """UDF symbol search — fuzzy match on ticker or company name."""
    q       = query.strip().lower()
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
def udf_symbols(symbol: str = Query(...)):
    """UDF symbol info — Asia/Dhaka timezone, 1000-1430 session."""
    clean = symbol.split(":")[-1].upper().strip()
    row   = CONSTITUENTS_DF[CONSTITUENTS_DF["Ticker"] == clean]
    name  = str(row["Name"].values[0]) if not row.empty else clean
    return {
        "name":                   clean,
        "description":            name,
        "type":                   "stock",
        "session":                "1000-1430",
        "timezone":               "Asia/Dhaka",
        "exchange":               "DSE",
        "listed_exchange":        "DSE",
        "minmov":                 1,
        "pricescale":             100,
        "has_intraday":           False,
        "has_daily":              True,
        "has_weekly_and_monthly": True,
        "supported_resolutions":  ["D", "W", "M"],
        "volume_precision":       0,
        "has_volume":             True,
    }


@app.get("/udf/history")
def udf_history(
    symbol:    str = Query(...),
    resolution: str = Query(...),
    from_time: int = Query(..., alias="from"),
    to_time:   int = Query(..., alias="to"),
):
    """
    UDF historical bars — core TradingView chart feed.

    Uses _get_ohlcv() which directly calls dsebd.org/day_end_archive.php
    with params: startDate, endDate, inst, archive=data
    (same as DsePriceService.getHistData() in bd-stock-api).

    Dates from DSE are YYYY-MM-DD (confirmed from sample_history.json).
    """
    try:
        clean = symbol.split(":")[-1].upper().strip()

        from_dt   = datetime.utcfromtimestamp(from_time)
        to_dt     = datetime.utcfromtimestamp(to_time)
        from_str  = from_dt.strftime("%Y-%m-%d")
        to_str    = to_dt.strftime("%Y-%m-%d")

        df = _get_ohlcv(clean, from_str, to_str)
        if df is None or df.empty:
            return {"s": "no_data", "nextTime": from_time}

        # Set datetime index for resampling
        df = df.set_index("date").sort_index()

        # Resample for weekly / monthly
        if resolution in ("W", "M"):
            # DSE week: Sun–Thu → end-of-week on Thursday
            rule = "W-THU" if resolution == "W" else "ME"
            try:
                df = (
                    df[["open", "high", "low", "close", "volume"]]
                    .resample(rule)
                    .agg({"open": "first", "high": "max",
                          "low": "min",   "close": "last",
                          "volume": "sum"})
                    .dropna(subset=["open"])
                )
            except Exception:
                # pandas < 2.2 fallback: ME → M
                df = (
                    df[["open", "high", "low", "close", "volume"]]
                    .resample("W-THU" if resolution == "W" else "M")
                    .agg({"open": "first", "high": "max",
                          "low": "min",   "close": "last",
                          "volume": "sum"})
                    .dropna(subset=["open"])
                )

        if df.empty:
            return {"s": "no_data", "nextTime": from_time}

        return {
            "s": "ok",
            "t": [int(ts.timestamp()) for ts in df.index],
            "o": [round(float(v), 2) for v in df["open"]],
            "h": [round(float(v), 2) for v in df["high"]],
            "l": [round(float(v), 2) for v in df["low"]],
            "c": [round(float(v), 2) for v in df["close"]],
            "v": [int(v) for v in df["volume"].fillna(0)],
        }

    except Exception as exc:
        print(f"[UDF/history] {symbol}: {exc}")
        return {"s": "no_data", "nextTime": from_time}


# ─────────────────────────────────────────────────────────────────────────────
# ─── SECTION 10: Company analysis widgets ────────────────────────────────────
# ─────────────────────────────────────────────────────────────────────────────

@app.get("/dsex_ticker_profile")
def dsex_ticker_profile(
    symbol: str = Query(default="GP"),
):
    """Company profile card — reads static metadata from CONSTITUENTS_DF."""
    try:
        clean = symbol.strip().upper()
        row   = CONSTITUENTS_DF[CONSTITUENTS_DF["Ticker"] == clean]
        if row.empty:
            return [{"Field": "Error", "Value": f"Ticker '{clean}' not in DSEX."}]
        r = row.iloc[0]
        return [
            {"Field": "Ticker Symbol",  "Value": str(r.get("Ticker",   "N/A"))},
            {"Field": "Company Name",   "Value": str(r.get("Name",     "N/A"))},
            {"Field": "Primary Sector", "Value": str(r.get("Sector",   "N/A"))},
            {"Field": "Industry Group", "Value": str(r.get("Industry", "N/A"))},
            {"Field": "Market Region",  "Value": str(r.get("Country",  "Bangladesh"))},
            {"Field": "Trading Venue",  "Value": str(r.get("Exchange", "DSE"))},
        ]
    except Exception as exc:
        return [{"Field": "Error", "Value": str(exc)}]


def _scrape_valuation(symbol: str) -> list[dict]:
    """Scrape fundamental metrics from dsebd.org/displayCompany.php."""
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
        url  = f"{DSE_URLS['COMPANY']}?name={symbol.upper()}"
        resp = requests.get(url, headers=HEADERS, timeout=10)
        resp.raise_for_status()
        text = BeautifulSoup(resp.text, "html.parser").get_text(" ", strip=True)

        def _rx(pattern: str) -> str:
            m = re.search(pattern, text, re.IGNORECASE)
            return m.group(1).strip().replace(",", "") if m else "N/A"

        div_m = re.search(r'Dividend\s+Yield[^0-9]*([+-]?[\d,]+(?:\.\d+)?)\s*%?',
                          text, re.IGNORECASE)

        return [
            {"Metric": "Market Capitalization",
             "Value": _rx(r'Market\s+Capitalization[^0-9]*([\d,]+(?:\.\d+)?)'),
             "Unit":  "BDT mn"},
            {"Metric": "Moving P/E Ratio",
             "Value": _rx(r'(?:Moving\s+)?P\s*/\s*E[^0-9]*([+-]?[\d,]+(?:\.\d+)?)'),
             "Unit":  "x"},
            {"Metric": "Basic EPS",
             "Value": _rx(r'(?:Basic\s+)?EPS[^0-9]*([+-]?[\d,]+(?:\.\d+)?)'),
             "Unit":  "BDT"},
            {"Metric": "Dividend Yield",
             "Value": (div_m.group(1).strip() + "%") if div_m else "N/A",
             "Unit":  "%"},
            {"Metric": "Face Value",
             "Value": _rx(r'Face\s+Value[^0-9]*([\d,]+(?:\.\d+)?)'),
             "Unit":  "BDT"},
            {"Metric": "Authorized Capital",
             "Value": _rx(r'Authorized?\s+Capital[^0-9]*([\d,]+(?:\.\d+)?)'),
             "Unit":  "BDT mn"},
            {"Metric": "Data Source", "Value": "dsebd.org", "Unit": ""},
            {"Metric": "URL",         "Value": url,          "Unit": ""},
        ]
    except Exception as exc:
        print(f"[valuation] {symbol}: {exc}")
        fallback.append({"Metric": "Error", "Value": str(exc), "Unit": ""})
        return fallback


@app.get("/dsex_valuation_metrics")
def dsex_valuation_metrics(symbol: str = Query(default="GP")):
    """Fundamental valuation metrics scraped from dsebd.org/displayCompany.php."""
    return _scrape_valuation(symbol.strip().upper())


@app.get("/dsex_price_performance")
def dsex_price_performance(symbol: str = Query(default="GP")):
    """
    Historical price-performance tracker.
    Fetches 400 days of daily OHLCV from day_end_archive.php,
    calculates total-return % over 1-W / 1-M / 3-M / 1-Y horizons.
    """
    HORIZONS = [
        ("1-Week  (5 td)",    5),
        ("1-Month (22 td)",  22),
        ("3-Month (66 td)",  66),
        ("1-Year  (250 td)", 250),
    ]
    try:
        clean     = symbol.strip().upper()
        end_str   = datetime.utcnow().strftime("%Y-%m-%d")
        start_str = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")

        df = _get_ohlcv(clean, start_str, end_str)
        if df is None or df.empty:
            return [{"Horizon": "Error", "Return %": "N/A",
                     "Start Price": "N/A", "End Price": "N/A",
                     "Note": "No data from day_end_archive.php for this ticker."}]

        closes  = df.set_index("date").sort_index()["close"].dropna()
        current = float(closes.iloc[-1])
        rows    = []

        for label, n in HORIZONS:
            if len(closes) >= n + 1:
                past   = float(closes.iloc[-(n + 1)])
                ret    = round(((current - past) / past) * 100, 2)
                arrow  = "▲" if ret >= 0 else "▼"
                rows.append({
                    "Horizon":     label,
                    "Return %":    f"{arrow} {abs(ret):.2f}%",
                    "Start Price": f"৳ {past:,.2f}",
                    "End Price":   f"৳ {current:,.2f}",
                })
            else:
                rows.append({
                    "Horizon":     label,
                    "Return %":    "Insufficient data",
                    "Start Price": "N/A",
                    "End Price":   f"৳ {current:,.2f}",
                })

        rows.append({"Horizon": "Current Price", "Return %": "—",
                     "Start Price": "—", "End Price": f"৳ {current:,.2f}"})
        return rows
    except Exception as exc:
        print(f"[price_perf] {symbol}: {exc}")
        return [{"Horizon": "Error", "Return %": str(exc),
                 "Start Price": "N/A", "End Price": "N/A"}]


@app.get("/dsex_technical_gauge")
def dsex_technical_gauge(symbol: str = Query(default="GP")):
    """
    SMA dashboard — 20 / 50 / 200-day Simple Moving Averages.
    Data sourced from day_end_archive.php via _get_ohlcv().
    """
    SMA_PERIODS = [("SMA 20", 20), ("SMA 50", 50), ("SMA 200", 200)]
    try:
        clean     = symbol.strip().upper()
        end_str   = datetime.utcnow().strftime("%Y-%m-%d")
        start_str = (datetime.utcnow() - timedelta(days=400)).strftime("%Y-%m-%d")

        df = _get_ohlcv(clean, start_str, end_str)
        if df is None or df.empty:
            return [{"Indicator": "Error", "Current Price": "N/A",
                     "SMA Value": "N/A", "Market Condition": "No data."}]

        closes  = df.set_index("date").sort_index()["close"].dropna()
        current = float(closes.iloc[-1])
        rows    = []

        for label, period in SMA_PERIODS:
            if len(closes) >= period:
                sma = round(float(closes.rolling(period).mean().iloc[-1]), 2)
                cond = "🟢 Bullish (Above SMA)" if current >= sma else "🔴 Bearish (Below SMA)"
                rows.append({
                    "Indicator":        label,
                    "Current Price":    f"৳ {current:,.2f}",
                    "SMA Value":        f"৳ {sma:,.2f}",
                    "Market Condition": cond,
                })
            else:
                rows.append({
                    "Indicator":        label,
                    "Current Price":    f"৳ {current:,.2f}",
                    "SMA Value":        "Insufficient data",
                    "Market Condition": f"Need ≥ {period} trading days",
                })
        return rows
    except Exception as exc:
        print(f"[tech_gauge] {symbol}: {exc}")
        return [{"Indicator": "Error", "Current Price": "N/A",
                 "SMA Value": "N/A", "Market Condition": str(exc)}]


# ─────────────────────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=5050)
