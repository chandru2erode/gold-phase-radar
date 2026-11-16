"""Refresh data/gold_flows.csv and data/meta.json.

Run by the GitHub Action every weekday evening (IST):
  1. Gold BeES NAV from AMFI history via api.mfapi.in (scheme 140088).
     Every month from Dec 2019 onward is set to that month's last NAV, so the
     current month always shows the latest NAV.
  2. Gold ETF net flow and AUM from AMFI's monthly report
     (portal.amfiindia.com/spages/am<mon><yyyy>repo.xls) for any finished month
     that has no flow yet. AMFI publishes around the 10th, so a missing report
     is normal before then and is simply retried the next day.
"""
import csv, io, json, re, sys, datetime as dt
from pathlib import Path
import requests
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
CSV = ROOT / "data" / "gold_flows.csv"
META = ROOT / "data" / "meta.json"
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
SPLIT_FROM = "2019-12"  # Gold BeES 1:100 split; CSV NAV equals raw NAV from here on


def load():
    with CSV.open() as f:
        return {r["month"]: r for r in csv.DictReader(f)}


def save(rows):
    with CSV.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["month", "nav", "flow_cr", "aum_cr"])
        w.writeheader()
        for m in sorted(rows):
            w.writerow(rows[m])


def update_nav(rows):
    r = requests.get("https://api.mfapi.in/mf/140088", headers=UA, timeout=60)
    r.raise_for_status()
    last = {}
    for d in r.json()["data"]:  # newest first, dates dd-mm-yyyy
        day, mon, yr = d["date"].split("-")
        key = f"{yr}-{mon}"
        if key >= SPLIT_FROM and key not in last:
            last[key] = (f"{yr}-{mon}-{day}", float(d["nav"]))
    for key, (_, nav) in last.items():
        row = rows.setdefault(key, {"month": key, "nav": "", "flow_cr": "", "aum_cr": ""})
        row["nav"] = f"{nav:.4f}"
    newest = max(last)
    return last[newest][0]


def gold_row_from_xls(content):
    xl = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    for df in xl.values():
        for _, r in df.iterrows():
            name = str(r.iloc[1]) if len(r) > 1 else ""
            if re.fullmatch(r"\s*gold\s+etfs?\s*", name, re.I):
                vals = [v for v in r.iloc[2:].tolist()]
                # columns: schemes, folios, mobilised, redemption, net flow, AUM, avg AUM, ...
                net, aum = float(vals[4]), float(vals[5])
                return net, aum
    return None


def update_flows(rows):
    today = dt.date.today()
    added = []
    flow_months = [m for m, r in rows.items() if r["flow_cr"] not in ("", None)]
    m = max(flow_months)
    y, mo = int(m[:4]), int(m[5:])
    while True:
        mo += 1
        if mo == 13:
            y, mo = y + 1, 1
        if (y, mo) >= (today.year, today.month):
            break  # only finished months
        key = f"{y}-{mo:02d}"
        got = None
        for ext in ("xls", "xlsx"):
            url = f"https://portal.amfiindia.com/spages/am{MON[mo-1]}{y}repo.{ext}"
            try:
                resp = requests.get(url, headers=UA, timeout=60)
            except requests.RequestException as e:
                print("fetch failed", url, e)
                continue
            if resp.status_code == 200 and len(resp.content) > 2000:
                got = gold_row_from_xls(resp.content)
                if got:
                    break
        if not got:
            print(f"AMFI report for {key} not available yet")
            break
        net, aum = got
        row = rows.setdefault(key, {"month": key, "nav": "", "flow_cr": "", "aum_cr": ""})
        row["flow_cr"], row["aum_cr"] = f"{net:.2f}", f"{aum:.2f}"
        added.append(key)
    return added


def main():
    rows = load()
    nav_date = update_nav(rows)
    added = update_flows(rows)
    save(rows)
    last_flow = max(m for m, r in rows.items() if r["flow_cr"] not in ("", None))
    META.write_text(json.dumps({
        "updated_at": dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30))).isoformat(timespec="minutes"),
        "nav_date": nav_date,
        "last_flow_month": last_flow,
    }, indent=1))
    print("NAV as of", nav_date, "| flows added:", added or "none", "| last flow month", last_flow)


if __name__ == "__main__":
    sys.exit(main())
