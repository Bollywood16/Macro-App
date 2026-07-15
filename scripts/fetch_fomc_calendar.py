#!/usr/bin/env python3
"""
Scrapes FOMC meeting dates from the Federal Reserve's own published
calendar (https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm)
and merges them into deployment_ladder_config.json's fomc_dates, so that
config's fomc_dates no longer depends on someone manually retyping the
Fed's calendar every year.

WHY THIS FAILS LOUD, NOT SOFT, UNLIKE EVERY OTHER FETCHER IN THIS REPO:
Every other fail-soft fetcher here (research_engine.fetch_hy_oas,
positioning_adapters.py) degrades a DIGEST FIELD to None on error -- a
missing data point in a report nobody acts on blindly. fomc_dates instead
feeds a real capital-deployment guardrail (deployment_ladder.py's
blackout gate). Silently writing a partial or wrong scrape into that
config would itself BE a silent wrong answer -- exactly what this app's
"never invent/backfill a calendar fact" discipline
(deployment_ladder_config.json's _readme, MASTER_AGENT_PROMPT.md #4)
exists to prevent. So on ANY fetch/parse anomaly (page unreachable, HTML
layout changed, a year's panel missing, month/day counts not lining up)
this script prints an error, leaves the config file byte-for-byte
UNCHANGED, and exits non-zero -- a loud, visible CI failure for the owner
to notice and fix, rather than a config quietly holding wrong dates.

PARSING APPROACH: this is a regex scrape, not a full HTML parser, because
the page's own markup for this section is simple and (checked against
every year panel from 2021-2027 live on 2026-07-15) stable:
  <a id="...">{year} FOMC Meetings</a>                  -- year panel start
  <div class="fomc-meeting__month ..."><strong>June</strong></div>
  <div class="fomc-meeting__date ...">16-17*</div>       -- "*" = SEP meeting
(the Fed's own footnote: "* Meeting associated with a Summary of Economic
Projections.") One row = one month label + one date-range entry, always
paired in document order -- the month/date COUNT MISMATCH check below is
what catches a template change and fails loud instead of parsing garbage
if the Fed ever restructures the page.

CLI:
  python scripts/fetch_fomc_calendar.py [--year YYYY] [--config PATH] [--dry-run]
  (--year defaults to the current year -- run in January, that's "the new
  year," whose calendar the Fed has reliably already published months
  earlier: as of 2026-07-15 the 2027 panel was already live.)
"""

import argparse
import json
import os
import re
import sys
import urllib.error
import urllib.request
from datetime import date, datetime, timezone

HERE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(HERE, "deployment_ladder_config.json")

FOMC_CALENDAR_URL = "https://www.federalreserve.gov/monetarypolicy/fomccalendars.htm"
_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketMemoryBot/1.0)"}

MONTHS = {m: i for i, m in enumerate(
    ["January", "February", "March", "April", "May", "June", "July",
     "August", "September", "October", "November", "December"], start=1)}

YEAR_HEADING_RE = r'<a id="\d+">{year} FOMC Meetings</a>'
MONTH_RE = re.compile(r'fomc-meeting__month[^>]*><strong>(\w+)</strong>')
DATE_RE = re.compile(r'fomc-meeting__date[^>]*>\s*(\d{1,2})(?:-(\d{1,2}))?(\*)?')


def fetch_page() -> str:
    req = urllib.request.Request(FOMC_CALENDAR_URL, headers=_HEADERS)
    with urllib.request.urlopen(req, timeout=20) as resp:
        return resp.read().decode("utf-8", errors="replace")


def extract_year_panel(html: str, year: int) -> str:
    m = re.search(YEAR_HEADING_RE.format(year=year), html)
    if not m:
        raise RuntimeError(
            f"No '{year} FOMC Meetings' heading found on the page -- the "
            f"Fed may not have published this year's calendar yet, or the "
            f"page layout changed")
    start = m.end()
    next_panel = html.find("panel panel-default", start)
    return html[start:next_panel] if next_panel != -1 else html[start:start + 20000]


def parse_year(html: str, year: int) -> list:
    panel = extract_year_panel(html, year)
    months = MONTH_RE.findall(panel)
    dates = DATE_RE.findall(panel)
    if not months or len(months) != len(dates):
        raise RuntimeError(
            f"Parsed {len(months)} month label(s) but {len(dates)} date "
            f"entrie(s) for {year} -- page layout likely changed, "
            f"refusing to guess")

    out = []
    for month_name, (day1_s, day2_s, star) in zip(months, dates):
        month_num = MONTHS.get(month_name)
        if month_num is None:
            raise RuntimeError(f"Unrecognized month label {month_name!r} for {year}")
        note = "SEP" if star else None
        day1 = int(day1_s)
        out.append({"date": date(year, month_num, day1).isoformat(), "note": note})
        if day2_s:
            day2 = int(day2_s)
            if day2 >= day1:
                d2 = date(year, month_num, day2)
            else:  # meeting spans a month boundary (rare) -- roll forward
                nm, ny = (month_num + 1, year) if month_num < 12 else (1, year + 1)
                d2 = date(ny, nm, day2)
            out.append({"date": d2.isoformat(), "note": note})
    return out


def merge_into_config(cfg: dict, year: int, fetched: list) -> dict:
    """Replace any existing entries for `year` with the freshly scraped
    ones; entries for every other year are left untouched, so the config
    accumulates across years as this runs each January."""
    existing = cfg.get("fomc_dates") or []
    kept = [e for e in existing
            if not (isinstance(e, dict) and e.get("date", "").startswith(f"{year}-"))]
    cfg = dict(cfg)
    cfg["fomc_dates"] = sorted(kept + fetched, key=lambda e: e["date"])
    return cfg


def main():
    ap = argparse.ArgumentParser(
        description="Scrape FOMC meeting dates into deployment_ladder_config.json")
    ap.add_argument("--year", type=int, default=datetime.now(timezone.utc).year)
    ap.add_argument("--config", default=CONFIG_PATH)
    ap.add_argument("--dry-run", action="store_true",
                     help="Print what would change; don't write the config")
    args = ap.parse_args()

    try:
        html = fetch_page()
        fetched = parse_year(html, args.year)
    except (urllib.error.URLError, RuntimeError) as e:
        print(f"[error] FOMC calendar fetch/parse failed for {args.year}: {e}",
              file=sys.stderr)
        print("[error] Config left UNCHANGED -- fix the parser or the "
              "underlying issue and re-run.", file=sys.stderr)
        return 1

    print(f"Fetched {len(fetched)} FOMC date(s) for {args.year}:")
    for e in fetched:
        print(f"  {e['date']}" + (f"  ({e['note']})" if e["note"] else ""))

    if args.dry_run:
        print("[dry-run] config not written")
        return 0

    with open(args.config) as f:
        cfg = json.load(f)
    new_cfg = merge_into_config(cfg, args.year, fetched)
    with open(args.config, "w") as f:
        json.dump(new_cfg, f, indent=2)
        f.write("\n")
    print(f"Wrote {args.config}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
