# Gold Phase Radar

A one-page dashboard that places Gold BeES in one of four zones (Accumulate more, Accumulate, Hold, Trim). The zone comes from industry-wide Gold ETF flows (AMFI) and Gold BeES's price against its 10-month average.

## How it stays current

`.github/workflows/update.yml` runs `scripts/update.py` at 7:07 pm IST every weekday. The script:

1. Fetches Gold BeES NAV history from AMFI via `api.mfapi.in` (scheme 140088) and sets the current month to the latest NAV.
2. Downloads AMFI's monthly report for any finished month that is still missing (published around the 10th) and adds the Gold ETF net flow and AUM.
3. Commits `data/` if anything changed. GitHub Pages then republishes the site within a minute or two.

To refresh by hand, go to **Actions → Update gold data → Run workflow**.

## One-time setup

1. Push these files to the `main` branch.
2. **Settings → Pages → Build and deployment**: Source "Deploy from a branch", branch `main`, folder `/ (root)`.
3. **Settings → Actions → General → Workflow permissions**: "Read and write permissions".
4. Run the workflow once from the Actions tab to check it.

## Files

| Path | What it is |
|---|---|
| `index.html` | The page. It loads d3 from cdnjs and the data files below, and computes the zones in the browser. |
| `data/gold_flows.csv` | `month, nav, flow_cr, aum_cr` from Feb 2009. |
| `data/meta.json` | NAV date, last flow month, last refresh time. |
| `data/backtest.json` | Static backtest results (Jan 2012 to Nov 2026). |
| `scripts/update.py` | Data refresher. |

## Zone rules (checked in this order, using the previous month's data)

- **Trim:** a single month's inflow above 15% of Gold ETF AUM. Sell 25%.
- **Hold:** 3-month flow percentile (vs the trailing 36 months) at 30 or below **and** NAV below its 10-month average.
- **Accumulate more:** flow percentile at 60 or above. Deploy half of any parked cash.
- **Accumulate:** otherwise, normal SIP.

This is a backtested guide, not investment advice.
