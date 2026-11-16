"""Refresh the Gold Phase Radar data and decide whether to raise an alert.

Run by the GitHub Action every weekday evening (IST):
  1. Gold BeES NAV from AMFI history via api.mfapi.in (scheme 140088).
     Every month from Dec 2019 onward is set to that month's last NAV, so the
     current month always shows the latest NAV.
  2. Gold ETF net flow and AUM from AMFI's monthly report
     (portal.amfiindia.com/spages/am<mon><yyyy>repo.xls) for any finished month
     that has no flow yet. AMFI publishes around the 10th.
  3. Works out this month's zone (same rules as index.html) and writes
     data/state.json and data/state.csv (the CSV is read by Google Sheets).
  4. Writes alert.md when the zone changes or a new AMFI month arrives; the
     workflow turns it into a GitHub issue, which GitHub emails to the owner.

Exits non-zero (so GitHub emails a failure) when a report downloads but has no
Gold ETF row (AMFI changed its layout), or when last month's report is still
missing after the 20th. Data is saved before exiting either way.
"""
import csv, io, json, re, sys, datetime as dt
from pathlib import Path
import requests
import numpy as np
import pandas as pd

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
CSV = DATA / "gold_flows.csv"
META = DATA / "meta.json"
STATE_JSON = DATA / "state.json"
STATE_CSV = DATA / "state.csv"
ALERT = ROOT / "alert.md"
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))
UA = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124 Safari/537.36"}
MON = ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"]
SPLIT_FROM = "2019-12"  # Gold BeES 1:100 split; CSV NAV equals raw NAV from here on
HOLD_PCT, ADD_PCT, FRENZY_SHARE = 0.30, 0.60, 0.15
ZONE_LABEL = {"add": "Accumulate more", "sip": "Accumulate (normal SIP)", "hold": "Hold (pause new money)", "none": "No signal"}
ZONE_ACTION = {
    "add": "Keep the gold SIP and invest half of any gold cash parked during Hold.",
    "sip": "Keep the normal gold SIP. No extra cash, no pause.",
    "hold": "Pause new gold money and park it in a liquid fund until flows or price turn.",
    "none": "Not enough history.",
}
problems = []


def today():
    return dt.datetime.now(IST).date()


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
    return last[max(last)][0]


def gold_row_from_xls(content):
    xl = pd.read_excel(io.BytesIO(content), sheet_name=None, header=None)
    for df in xl.values():
        for _, r in df.iterrows():
            name = str(r.iloc[1]) if len(r) > 1 else ""
            if re.fullmatch(r"\s*gold\s+etfs?\s*", name, re.I):
                vals = r.iloc[2:].tolist()  # schemes, folios, mobilised, redemption, net flow, AUM, ...
                return float(vals[4]), float(vals[5])
    return None


def update_flows(rows):
    t = today()
    added = []
    m = max(k for k, r in rows.items() if r["flow_cr"])
    y, mo = int(m[:4]), int(m[5:])
    while True:
        mo += 1
        if mo == 13:
            y, mo = y + 1, 1
        if (y, mo) >= (t.year, t.month):
            break  # only finished months
        key = f"{y}-{mo:02d}"
        got, downloaded = None, False
        for ext in ("xls", "xlsx"):
            url = f"https://portal.amfiindia.com/spages/am{MON[mo-1]}{y}repo.{ext}"
            try:
                resp = requests.get(url, headers=UA, timeout=60)
            except requests.RequestException as e:
                print("fetch failed", url, e)
                continue
            if resp.status_code == 200 and len(resp.content) > 2000:
                downloaded = True
                got = gold_row_from_xls(resp.content)
                if got:
                    break
        if not got:
            if downloaded:
                problems.append(f"AMFI report for {key} downloaded but has no 'Gold ETF' row; the layout may have changed.")
            else:
                print(f"AMFI report for {key} not published yet")
            break
        net, aum = got
        if not (0 < aum < 1e7) or abs(net) > aum:
            problems.append(f"AMFI {key}: implausible Gold ETF figures (net {net}, AUM {aum}); not saved.")
            break
        row = rows.setdefault(key, {"month": key, "nav": "", "flow_cr": "", "aum_cr": ""})
        row["flow_cr"], row["aum_cr"] = f"{net:.2f}", f"{aum:.2f}"
        added.append(key)
    return added


def compute_state(rows, nav_date):
    df = pd.DataFrame([rows[k] for k in sorted(rows)])
    df = df.replace("", np.nan)
    for c in ("nav", "flow_cr", "aum_cr"):
        df[c] = df[c].astype(float)
    f3 = df.flow_cr.rolling(3).sum() / df.aum_cr.shift(3)
    pct = []
    for i in range(len(df)):
        w = f3.iloc[max(0, i - 35): i + 1].dropna().values
        v = f3.iloc[i]
        pct.append(np.nan if np.isnan(v) or len(w) < 24 else ((w < v).sum() + 0.5 * (w == v).sum()) / len(w))
    df["pct"] = pct
    df["f1"] = df.flow_cr / df.aum_cr.shift(1)
    df["sma"] = df.nav.rolling(10).mean()
    df["trend"] = df.nav / df.sma - 1
    k = df.flow_cr.last_valid_index()
    last, cur = df.loc[k], df.iloc[-1]
    if np.isnan(last.pct):
        zone = "none"
    elif last.pct <= HOLD_PCT and last.trend < 0:
        zone = "hold"
    elif last.pct >= ADD_PCT:
        zone = "add"
    else:
        zone = "sip"
    frenzy = bool(last.f1 > FRENZY_SHARE)
    win = np.sort(f3.iloc[max(0, k - 34): k + 1].dropna().values)
    base = df.flow_cr[k] + df.flow_cr[k - 1]
    aum3 = df.aum_cr[k - 2]
    nxt = df.month[k + 1] if k + 1 < len(df) else None
    # Month-end close that keeps a 10-month average downtrend: close < mean of the previous 9 month-ends
    cur_i = len(df) - 1
    month_end_line = float(df.nav.iloc[cur_i - 9: cur_i].mean())
    return {
        "month": cur.month,
        "zone": zone,
        "zone_label": ZONE_LABEL[zone],
        "action": ZONE_ACTION[zone] + (" Frenzy warning: last month's inflow topped 15% of AUM; consider trimming gold back to your target weight." if frenzy else ""),
        "frenzy": frenzy,
        "data_month": last.month,
        "flow_cr": round(float(last.flow_cr), 2),
        "flow_percentile": int(round(last.pct * 100)),
        "signal_trend_pct": round(float(last.trend) * 100, 1),
        "nav": round(float(cur.nav), 4),
        "nav_date": nav_date,
        "sma10_now": round(float(cur.sma), 2),
        "trend_now_pct": round(float(cur.trend) * 100, 1),
        "month_end_line": round(month_end_line, 2),
        "next_flow_month": nxt,
        "hold_if_flow_below_cr": round(float(np.quantile(win, HOLD_PCT) * aum3 - base)),
        "add_if_flow_above_cr": round(float(np.quantile(win, ADD_PCT) * aum3 - base)),
        "frenzy_if_flow_above_cr": round(float(FRENZY_SHARE * df.aum_cr[k])),
    }


def main():
    prev = json.loads(STATE_JSON.read_text()) if STATE_JSON.exists() else {}
    rows = load()
    nav_date = update_nav(rows)
    added = update_flows(rows)
    save(rows)
    t = today()
    last_flow = max(m for m, r in rows.items() if r["flow_cr"])
    prev_month = (t.replace(day=1) - dt.timedelta(days=1)).strftime("%Y-%m")
    if t.day >= 20 and last_flow < prev_month:
        problems.append(f"AMFI's report for {prev_month} is still missing on {t.isoformat()}; it is normally out by the 10th.")
    st = compute_state(rows, nav_date)
    st["updated_at"] = dt.datetime.now(IST).isoformat(timespec="minutes")
    STATE_JSON.write_text(json.dumps(st, indent=1))
    with STATE_CSV.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow(["field", "value"])
        for k2, v in st.items():
            w.writerow([k2, v])
    META.write_text(json.dumps({"updated_at": st["updated_at"], "nav_date": nav_date, "last_flow_month": last_flow}, indent=1))

    changed = prev and (prev.get("zone") != st["zone"] or prev.get("frenzy") != st["frenzy"])
    if added or changed:
        title = f"Gold zone: {st['zone_label']}" + (" (frenzy warning)" if st["frenzy"] else "") + f", {st['data_month']} AMFI data"
        lines = [
            f"**Zone for {st['month']}: {st['zone_label']}**" + (f" (was {prev.get('zone_label')})" if changed else ""),
            "", st["action"], "",
            f"- {st['data_month']} Gold ETF net inflow: Rs {st['flow_cr']:,.0f} cr, flow percentile {st['flow_percentile']}",
            f"- Gold BeES NAV Rs {st['nav']} ({nav_date}), {st['trend_now_pct']:+}% vs 10-month average Rs {st['sma10_now']}",
            f"- Month-end line for {st['month']}: a close below Rs {st['month_end_line']} keeps the price downtrend",
            "", "https://chandru2erode.github.io/gold-phase-radar/",
        ]
        ALERT.write_text(title + "\n" + "\n".join(lines))
    print("NAV", nav_date, "| flows added", added or "none", "| zone", st["zone"], "| frenzy", st["frenzy"])
    if problems:
        for p in problems:
            print("PROBLEM:", p)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
