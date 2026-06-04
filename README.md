# DSEX Backend for OpenBB Workspace

A full custom backend for the **Dhaka Stock Exchange Broad Index (DSEX)** — sourced live from [dsebd.org](https://www.dsebd.org).

## Features

| Widget | Description |
|--------|-------------|
| **DSEX Market Overview** | Index level + market stats from DSE |
| **DSEX Live Prices** | All 389 constituents with live price, change %, volume |
| **DSEX Top Gainers** | Top N stocks by % gain today |
| **DSEX Top Losers** | Top N stocks by % loss today |
| **DSEX Constituents** | Full constituent list with sector/industry filter |
| **DSEX Sector Breakdown** | Table: companies per sector |
| **DSEX Sector Chart** | Plotly bar or pie chart by sector |

The dashboard has **3 tabs**:
- **Overview** — Market stats + gainers/losers side by side
- **Live Prices** — Full price table with sector filter
- **Constituents** — Static metadata list + sector charts

---

## Setup

### 1. Install dependencies

```bash
pip install -r requirements.txt
```

### 2. Run the backend

```bash
uvicorn main:app --reload --port 5050
```

The server starts at `http://localhost:5050`.

### 3. Connect to OpenBB Workspace

1. Open [pro.openbb.co](https://pro.openbb.co)
2. Go to **Data Connectors** → **Custom Backend**
3. Enter: `http://localhost:5050`
4. Click **Connect**
5. The **DSEX Dashboard** app will appear automatically

---

## File Structure

```
dsex-backend/
├── main.py          ← FastAPI app (all endpoints)
├── widgets.json     ← Widget definitions for OpenBB
├── apps.json        ← Dashboard layout (3 tabs)
├── DSEX.csv         ← 389 DSEX constituents (static metadata)
├── requirements.txt
└── README.md
```

---

## Data Sources

| Data | Source | Refresh |
|------|--------|---------|
| Live prices | `dsebd.org/latest_share_price_scroll_l.php` | 2-min cache |
| Market stats | `dsebd.org/market-statistics.php` | Per request |
| Constituents | `DSEX.csv` (pre-built from dsebd.org) | Static |

Live data is scraped from DSE's public website. Prices update every 2 minutes
during trading hours (Sun–Thu, 10:00–14:30 BST).

---

## Notes

- DSE market is **closed on Fridays and Saturdays** (Bangladesh weekend).
- Trading hours: **10:00 AM – 2:30 PM BST** (Sun–Thu).
- During Ramadan hours are shorter: **10:00 AM – 2:00 PM BST**.
- The DSEX has a base value of **1,000 on 27 January 2013**.
- Index methodology: float-adjusted market cap, developed with **S&P Dow Jones Indices**.
