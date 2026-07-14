"""
Options/positioning data adapters — Stage 2b of the Model Upgrade directive.

Both sources are free, public, and fetched the same fail-soft way as the
GDELT/FRED adapters in research_engine.py/rotation_engine.py: network
errors, format drift, or the source disappearing all just mean the field is
None in the digest, never an exception that breaks the run.

CFTC Commitments of Traders (Traders in Financial Futures, futures-only,
short format) — verified working and column-checked against this session's
live pull (2026-07-13): Open_Interest_All == Tot_Rept_Positions_Long_All +
NonRept_Positions_Long_All, and the same identity holds on the short side,
which only holds if the column indices below are right.

CBOE total put/call ratio — every endpoint this session tried
(cdn.cboe.com CSV and JSON chart paths, several ticker-symbol guesses)
returned HTTP 403 from this sandbox, including well-known real symbols like
VIX, so the 403s read as bot-protection rather than a real outage. The
parser below targets the long-documented classic totalpc.csv schema
(Date,Calls,Puts,Total,P/C Ratio after a few metadata header lines), but
this has NOT been verified against a live response in this session —
confirm it actually parses on a real Actions run before trusting the field.
"""

import csv
import io
import urllib.error
import urllib.request

CFTC_TFF_URL = "https://www.cftc.gov/dea/newcot/FinFutWk.txt"
CFTC_CONTRACT_NAME = "E-MINI S&P 500 - CHICAGO MERCANTILE EXCHANGE"
# 0-indexed columns in the TFF futures-only short-format CSV. Verified via
# the identity check in the module docstring — do not hand-edit without
# re-verifying against a live pull (CFTC's column count/order does shift
# occasionally between report format revisions).
COL_OPEN_INTEREST = 7
COL_LEV_MONEY_LONG = 14
COL_LEV_MONEY_SHORT = 15

CBOE_PUTCALL_URL = "https://cdn.cboe.com/api/global/us_indices/daily_prices/totalpc.csv"

_HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; MarketMemoryBot/1.0)"}

# Net leveraged-money position beyond this, as a fraction of open interest,
# is labeled "stretched" rather than "neutral". This is a fixed reference
# threshold for this milestone (no historical percentile yet — see
# fetch_cftc_positioning's docstring), chosen as a round, conventionally
# recognized level in CoT-positioning commentary, not fitted to this data.
STRETCHED_THRESHOLD = 0.20


def fetch_cftc_positioning(contract_name: str = CFTC_CONTRACT_NAME):
    """Latest weekly CFTC leveraged-money (hedge fund / CTA) net position
    in the given financial future, as a fraction of open interest.

    Leveraged-money net-short in equity index futures is NOT reliably a
    directional bearish signal on its own — a large share is basis-trade /
    arbitrage flow, not macro conviction — so this is surfaced as a
    positioning fact with its own caveat, not interpreted as sentiment.

    Only the latest snapshot is available here (this milestone has no
    historical CFTC time series wired up yet), so "stretched" is judged
    against a fixed reference threshold, not a rolling percentile like the
    VIX/credit regime dimensions elsewhere in this app. That's a real
    difference in rigor — labeled honestly in the returned dict's `note`
    field rather than presented as equivalent to the calibrated dimensions.
    """
    try:
        req = urllib.request.Request(CFTC_TFF_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", errors="replace")
        for row in csv.reader(io.StringIO(text)):
            if not row or contract_name not in row[0]:
                continue
            oi = float(row[COL_OPEN_INTEREST])
            lev_long = float(row[COL_LEV_MONEY_LONG])
            lev_short = float(row[COL_LEV_MONEY_SHORT])
            if oi <= 0:
                return None
            net_pct = (lev_long - lev_short) / oi
            label = ("stretched_short" if net_pct <= -STRETCHED_THRESHOLD else
                      "stretched_long" if net_pct >= STRETCHED_THRESHOLD else
                      "neutral")
            return {
                "contract": contract_name,
                "report_date": row[2],
                "open_interest": int(oi),
                "lev_money_net_pct_oi": round(net_pct, 4),
                "positioning_label": label,
                "note": ("Leveraged-money (hedge fund/CTA) net position as "
                         "% of open interest, E-mini S&P 500 futures, "
                         "weekly CFTC report. NOT a reliable standalone "
                         "directional signal -- much of this flow is "
                         "basis-trade/arbitrage, not macro conviction. "
                         "'stretched' is a fixed +/-20% of OI reference "
                         "threshold, not a calibrated historical "
                         "percentile (no CFTC history wired up yet)."),
            }
        print(f"[warn] cftc_positioning: contract '{contract_name}' not "
              f"found in report")
        return None
    except urllib.error.HTTPError as e:
        print(f"[warn] cftc_positioning: HTTP {e.code}")
        return None
    except Exception as e:
        print(f"[warn] cftc_positioning: {e}")
        return None


def fetch_cboe_putcall():
    """Latest CBOE total equity put/call ratio. See module docstring — the
    live endpoint returned HTTP 403 from every URL this session tried, so
    this fails soft and should be spot-checked against a real run."""
    try:
        req = urllib.request.Request(CBOE_PUTCALL_URL, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=20) as r:
            text = r.read().decode("utf-8", errors="replace")
        lines = [ln for ln in text.splitlines() if ln.strip()]
        # Classic format: a few metadata/title lines, then a header row
        # ("DATE,CALLS,PUTS,TOTAL,P/C Ratio" or similar), then data rows,
        # newest last. Find the header row by looking for "P/C" rather than
        # assuming a fixed line count, since the metadata-line count has
        # drifted before on this file historically.
        header_idx = next((i for i, ln in enumerate(lines)
                            if "P/C" in ln.upper() or "RATIO" in ln.upper()),
                           None)
        if header_idx is None:
            print("[warn] cboe_putcall: couldn't find header row")
            return None
        data_lines = lines[header_idx + 1:]
        if not data_lines:
            return None
        last = next(csv.reader([data_lines[-1]]))
        if len(last) < 5:
            return None
        ratio = float(last[4])
        return {
            "date": last[0],
            "calls": int(float(last[1])),
            "puts": int(float(last[2])),
            "total": int(float(last[3])),
            "put_call_ratio": round(ratio, 3),
            "regime": ("elevated_fear" if ratio >= 1.0 else
                       "elevated_greed" if ratio <= 0.6 else "neutral"),
            "note": ("CBOE total equity put/call ratio (calls+puts volume). "
                     ">=1.0 = puts trading roughly at or above call volume "
                     "(hedging-heavy skew); <=0.6 = call-heavy skew. Fixed "
                     "reference thresholds, not a calibrated percentile."),
        }
    except urllib.error.HTTPError as e:
        print(f"[warn] cboe_putcall: HTTP {e.code}")
        return None
    except Exception as e:
        print(f"[warn] cboe_putcall: {e}")
        return None
