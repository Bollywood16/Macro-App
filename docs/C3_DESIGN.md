# C3 Design — Point-in-Time Data Store

**Status: investigation and proposal only. Nothing in this document has been implemented.**
Gate per `MARKET_MEMORY_V2_BUILD.md` §10: reviewed and approved before C3 (the store) or
C2 (`replay()`, which depends on it) get built.

Scope, per §4 of the spec: (1) what's actually in the git-history JSON files today, (2) a
proposed Parquet schema with `effective_date`/`processed_date` defined per table, (3) the
exact lookahead-safety guarantee for `PointInTimeDataContext` and how it's tested. All
three sections below are backed by things actually run against this repo and against
yfinance/FRED today (2026-08-05), not assumptions — commands are reproducible.

---

## 0. Summary — read this first

1. **The git-history JSON files cannot seed the replay store.** They cover ~19
   non-contiguous days (2026-07-11 → 2026-08-04, one dead 2-week stretch), and even on the
   days they exist, they hold *derived summary snapshots* (today/1m/3m/6m/1y point reads
   and mined episode stats), never a raw daily price series. There is nothing to extract
   for 2005–2025. §1.5 of the spec's claim that "`git log --follow` is a free PIT store"
   is true only as a ~month-deep regression-diff safety net, not as replay's data source.
2. **A real, currently-live data gap: FRED's ICE BofA high-yield OAS series
   (`BAMLH0A0HYM2`, used for the credit regime dimension everywhere) now only serves data
   from 2023-08-07 forward.** This isn't a replay-only problem — it's true of the fetch
   the *live* app runs today. Every regime-conditioned computation for dates before
   2023-08-07 is silently missing its credit dimension. Full finding and options in §2.4.
3. **The whole lookahead-safety question reduces to one choke point, which is good news
   and bad news.** Every engine function in the repo (`engines/*.py` and
   `forecast_engine.py`'s analog model) trusts that the series it's handed already ends at
   "now" — none of them independently check a date bound. That means `as_of()` /
   `PointInTimeDataContext` is the *only* place a leak can be introduced, but also the
   *only* place stopping one — nothing downstream will catch a mistake. Full argument and
   a concrete unguarded code path already found in §3.
4. **`pyarrow` isn't installed anywhere in this environment or any workflow's pip step.**
   Trivial to add, but C4's workflow will need it and none of the existing `pip install
   yfinance pandas ...` lines have it.

---

## 1. What's actually in the git-history JSON files

Extraction methodology: `git log --format=%ad --date=short -- <file>` for each digest,
plus loading the current version of each file to see what fields exist at all.

| File | Tickers covered | Commit-days in git history | What's actually stored |
|---|---|---|---|
| `data/research_digest.json` | 5 — `SMH, ^SOX, SPY, QQQ, GLD` (`scripts/universe_config.json`) | 17 | Per-asset `current.snapshots` (today/1m/3m/6m/1y point reads: price, rsi14, macd_hist, ext200_pct, from_hi20_pct) + `studies` (mined trigger/episode statistics). **No raw OHLCV.** |
| `data/rotation_digest.json` | 14 — 11 sectors (`XLK,XLF,XLV,XLE,XLI,XLY,XLP,XLU,XLB,XLRE,XLC`) + 3 size/style (`RSP,IWM,MGK`) (`scripts/rotation_config.json`) | 20 | `rs_table` (1m/3m/6m/1y % return + rank, current only), `hmm_regime`, `cftc_positioning`/`cboe_putcall` (current snapshot only, explicitly documented in its own `meta.caveats` as having no history wired up), `episodes`. **No raw OHLCV.** |
| `data/scanner_digest.json` | Reads back the union universe (`scanned_universe_count: 19` = 5 + 14 above) | 10 | Today's trigger/setup list only — a read of that day's `forecasts` table, not underlying price data. |
| `data/extension_overlay.json` | 1 (SMH/^SOX study only — single-asset by design) | 18 | `current` (today's extension read) + `cohorts`/`median_paths` (mined episode paths). **No raw OHLCV.** |

Total distinct tickers across all four: 19 (5 research + 14 rotation; scanner/extension
don't add new ones). That happens to be exactly the "17 replay tickers + the 2 excluded
(`XLC`, `XLRE`)" from spec §2 — so *coverage-by-name* is complete, but coverage-by-content
is not: **none of these files ever held a raw price series to begin with**, on any date.
They're computed-metric snapshots, generated fresh each run from a `yfinance
period="max"` pull that already only reflects today's (adjusted, retroactively-revised)
view of history — see §2.4. Diffing across commits would only ever show "what the
snapshot metrics were on run day N," which is a fine trail for catching a live regression
(e.g. "did `ext200_pct` jump for a reason") but contributes zero rows toward a 2005–2025
backtest.

**Gap in the run history itself, independent of content:** 2026-07-15 → 2026-07-29 has no
commits in `data/` at all (verified via `git log --format=%ad --date=short -- data/ |
sort -u`) — a ~2-week window where the daily workflows either didn't run or didn't push.
Not investigated further here since it's outside C3's scope, but worth a one-line mention
to whoever owns workflow health.

**Conclusion:** the Parquet store in §2 has to be built from a fresh yfinance/FRED
backfill (C4), not extracted from anything currently in git. The git-history JSONs remain
useful for exactly one thing going forward: a cheap trip-wire that catches a live-pipeline
regression by diffing today's snapshot against yesterday's — not a replay data source.

---

## 2. Proposed Parquet schema

### 2.1 Directory layout (unchanged from spec §4, C3 section)

```
data/pit/prices/{ticker}/{year}.parquet     # OHLCV, adjusted as-of-date
data/pit/flows/{ticker}/{year}.parquet      # net flow, shares out, NAV
data/pit/options/{ticker}/{year}.parquet    # EOD chain summary
data/pit/macro/{series}/{year}.parquet      # as-published vintages
```

One reader for all four, per spec:

```python
def as_of(table: str, ticker: str, date: date) -> pd.DataFrame:
    """Rows whose processed_date <= date. Raises on empty."""
```

### 2.2 `effective_date` vs `processed_date`, defined per table

The spec is right that these mean different things per table — here's the concrete
definition for each, because "prices" genuinely doesn't need the distinction the other
three tables do.

| Table | `effective_date` (what the row is *about*) | `processed_date` (when it *became knowable*) | Do they diverge in practice? |
|---|---|---|---|
| **prices** | The trading date the bar covers. | Same trading date, end-of-day (~4pm ET close + a short print delay). | **No, by design.** A daily close is known same-day after the close. Keep both columns for schema uniformity (every table goes through the same `as_of()` filter on `processed_date`) but expect `processed_date == effective_date` for every row, always. If a future ingestion ever back-fills a corrected bar (a vendor restatement), that correction gets a *new row* with the later `processed_date` and the original `effective_date` — never an in-place edit (mirrors the existing "freeze at creation" guardrail on `forecasts`). |
| **flows** (net flow, shares out, NAV) | The date the flow/NAV is *for*. | When the issuer/vendor actually published it. **Real lag, T+1 to T+several.** Confirmed no fetch code for this exists in the repo yet (`grep`ped `scripts/`+`engines/` for `shares_out`/`net_flow`/`NAV` — zero hits); this table is schema-only until the Phase F paid-data work lands. | **Yes, materially.** This is the one table where getting the distinction wrong is highest-stakes: if a backfill script ever populates this from a source that only exposes *current, latest-known* flow figures, every historical `processed_date` would need to be reconstructed from the vendor's own publication calendar, not assumed same-day. Flag any flows backfill that can't source a real `processed_date` as unsafe to gate replay decisions on — mark it `context_only` (spec §3's own escape hatch) rather than guess. |
| **options** (EOD chain summary) | The trading date the chain snapshot is for. | End of that trading day (chain data doesn't restate the way flows/macro do). | **No**, same reasoning as prices. No fetch code exists yet either — also Phase F. |
| **macro** (VIX, credit spread, rates, etc.) | The date the observation is *for* (e.g. "the OAS reading for 2019-03-04"). | When FRED/the source actually published that observation — for **survey/administrative series this genuinely lags** (initial claims, GDP, and similar get revised after original publication); for **market-observed series** (VIX, credit spreads, Treasury yields) publication is same-day but a subtler problem replaces the lag question — see §2.4. | **Yes for revision-prone series** (not really relevant to what this app currently pulls — VIX/HY-OAS/10yr are all market-quoted, not survey-revised) **but yes in a different way for HY OAS specifically, today** — see below. |

### 2.3 Per-ticker price depth (verified against yfinance today, not assumed)

Replay universe is 17 tickers: the 5 in `scripts/universe_config.json` (`SMH, ^SOX, SPY,
QQQ, GLD`) + the 14 in `scripts/rotation_config.json` minus the 2 the spec excludes
(`XLC`, `XLRE`) = 9 sectors + 3 size/style. Checked each ticker's actual yfinance history
start (`yf.download(ticker, period="max", auto_adjust=True)`):

| Ticker | History starts | Covers 2005–2025 window fully? |
|---|---|---|
| SPY, ^SOX, XLK/XLF/XLV/XLE/XLI/XLY/XLP/XLU/XLB (all 1998-12-22 vintage) | 1993–1998 | Yes |
| QQQ | 1999-03-10 | Yes |
| SMH, IWM | 2000-06-05 / 2000-05-26 | Yes |
| RSP | 2003-05-01 | Yes |
| GLD | 2004-11-18 | Yes (starts 6 weeks before the window) |
| **MGK** | **2007-12-27** | **No — missing ~2005-01 through 2007-12, ~3 years** |

Everything clears 2005 except `MGK`, which has a genuine ~3-year hole inside the replay
window. This isn't a reason to exclude it (same logic that already keeps `XLRE`/`XLC` in
the *live* rotation panel while excluding them from *replay* — "the engine uses whatever
exists on each date," per `rotation_config.json`'s own `_readme`) — just something the
schema and the C4 backfill/block-count reporting need to handle explicitly per ticker,
the same way `rotation_digest`'s `meta.caveats` already documents uneven sector history
today. Recommend the Parquet writer record each ticker's actual first available date
(from the fetch itself, not hardcoded) so `as_of()` can raise a clear "no data before
{date} for {ticker}" rather than silently returning empty or NaN-filled rows.

### 2.4 The adjusted-close question (prices) and the FRED licensing gap (macro)

Two data-fidelity issues, both verified live, that the schema needs to make an explicit,
documented choice about rather than inherit silently from the current fetchers:

**Prices — `auto_adjust=True` is retroactive.** `research_engine.fetch_history()` /
`fetch_ohlcv()` call yfinance with `auto_adjust=True`, which bakes in *every* split and
dividend up to *today* into every historical bar, including bars from 2005. This is
standard backtesting practice (it's a deterministic scale factor, not new information
about future price direction) but it is, read literally, a violation of "use only data
published on or before D" — a 2005 bar's stored value depends on a 2019 stock split that
hadn't happened yet in 2005. Recommend the design doc's review explicitly ratify
adjusted-close as an accepted, documented exception (which is what every quant backtest
does) rather than have it be an unstated assumption someone finds later and treats as a
bug.

**Macro — `BAMLH0A0HYM2` (ICE BofA US High-Yield OAS, the credit-regime input used
throughout `forecast_engine.py`'s `regime_series()` and `dip_context.py`) now only serves
data from 2023-08-07 forward on FRED.** Verified today, not assumed:

```
BAMLH0A0HYM2   → 2023-08-07 .. 2026-08-03  (793 rows)
BAMLH0A0HYM2EY → 2023-08-07 .. 2026-08-03  (793 rows)   # same family
BAMLC0A0CM     → 2023-08-07 .. 2026-08-03  (793 rows)   # same family
T10Y2Y (non-ICE control) → 1976-06-01 .. 2026-08-04  (13,091 rows)
DGS10  (non-ICE control) → 1962-01-02 .. 2026-08-04  (16,850 rows)
```

All three ICE BofA (`BAML*`) series truncate at the identical date regardless of query
parameters (tried with and without explicit `cosd=1996-01-01`); non-ICE FRED series from
the same endpoint return full history with no special parameters. This is consistent with
ICE's 2022 licensing change restricting public FRED access to a rolling window for its
index-family series — not a bug in this repo's fetch code, a change in what FRED is
allowed to serve.

**This is not a replay-only problem.** `fetch_hy_oas()` is called by the *live* forecast
engine today. `oas.reindex(idx).ffill()` on a series that only starts 2023-08-07 produces
`NaN` for every earlier date (ffill only propagates forward from an existing value, it
doesn't backfill), which `regime_series()` currently renders as `"unknown"` — silently
indistinguishable from a genuine missing-data day. Any regime-conditioned statistic that
looks back further than ~3 years already has a blind credit dimension today, live, before
any replay work happens. Recommend flagging this to be fixed independently of C3 (it's a
one-line fetch change, not a design question), and noting it explicitly here because C4's
backfill would otherwise inherit the same 3-year ceiling for the entire 2005–2023 span of
the replay window — the majority of it.

Two options worth naming for the eventual fix (not deciding here — proposal only):
1. Accept the gap and mark `credit_lab='context_only'` (not `'unknown'`) for
   `processed_date < 2023-08-07`, and let `regime_conditioned_positions()`'s existing
   3→2→1→0-dim backoff do its job — it's already built to degrade gracefully on missing
   dimensions, just needs the label to say "known absent" rather than "unknown."
2. Substitute a longer-history proxy for the credit dimension pre-2023 —
   `BAA10Y` (Moody's Baa − 10yr Treasury) has full history back to 1986 on FRED and isn't
   ICE-licensed. Different methodology (investment-grade spread, not high-yield OAS), so
   this would need its own regime thresholds, not a reuse of the existing >0.25/-0.25
   widening/narrowing cutoffs tuned for OAS's scale.

Either way, the macro Parquet writer's `processed_date` for HY-OAS-sourced rows needs to
honestly reflect "not available before 2023-08-07," not silently forward-fill or leave a
gap that `as_of()` papers over.

**Resolved 2026-08-05 — option 2 (`BAA10Y` substitution) chosen and implemented**, live
in `forecast_engine.py`/`dip_context.py`/`rotation_engine.py`/`scanner.py`; see
`docs/CREDIT_SERIES.md` for the full before/after measurement and rationale. Two
follow-on requirements for C4's backfill that fall out of that implementation, flagged
here so they aren't rediscovered mid-backfill:

**Percentile-window warmup pushes C4's data-fetch start date back, independent of the
replay window's own start.** `credit_regime_series()`'s regime classifier ranks the
63-day credit change against its own trailing **1260-trading-day (~5 calendar year)**
window — see `docs/CREDIT_SERIES.md` §0b. If C4's backfill fetches `BAA10Y` starting only
at the replay window's own start (2005-01-01, per spec §2), the first ~5 years of the
replay (2005-2010) get `credit_lab='unknown'` for the same structural reason the original
HY-OAS bug did — a different truncation, same failure shape. **C4 must fetch `BAA10Y`
starting no later than 1999-01-01** (BAA10Y itself has been available since 1986, so this
is a fetch-range choice, not a data-availability limit) to give the window a full 5-year
runway before 2005-01-01, plus roughly a year of margin. Audited every other trailing
window in the codebase for the same risk (`grep` for `rolling(`/`.diff(`/`.shift(` across
`scripts/*.py` and `engines/*.py`): the next-longest is 252 trading days (~1 calendar
year — `hi252`/`roll_high`/`mom_12m`/`trail252`, several files), a quarter of credit's
window. None of them need a similarly extended pre-2005 fetch; `BAA10Y`'s 1260-day window
is the outlier by roughly 5x, not one case of a general pattern.

**`deployment_ladder.py`'s blowout guardrail cannot run in replay before 2023-08-07.**
`hy_oas_blowout()` deliberately still reads the real ICE BofA OAS series (kept, unchanged,
in `research_engine.fetch_hy_oas()` — see `docs/CREDIT_SERIES.md` §0b) because its
halt threshold is calibrated to that series' own scale; the `BAA10Y` swap above applies
only to the regime-classification dimension, not this guardrail. Since ICE OAS itself
has no data before 2023-08-07, **any C4 replay date before 2023-08-07 has no
live-equivalent credit-blowout halt check available** — not a bug to chase during the
backfill, a structural gap in what replay can faithfully reproduce for that guardrail
specifically. Whoever builds C4 should surface this as an explicit caveat on any
pre-2023-08-07 replay result that depends on `deployment_ladder.py`'s halt logic, rather
than silently treating "guardrail didn't fire" as "guardrail checked and passed."

### 2.5 Flows / options tables are schema-only for now

Confirmed via `grep` across `scripts/` and `engines/` — no fetch code exists for ETF flow
data, shares-outstanding, NAV, or options chains anywhere in the repo today. The spec
already defers paid data to Phase F. The Parquet layout above reserves the shape (so C3's
`as_of()` contract doesn't need to change later) but these two tables should ship *empty*
with C3/C4, not stubbed with placeholder data — matches the existing guardrail "no
synthetic history."

### 2.6 Dependency gap

`pyarrow` is not installed in this environment and appears in none of the 9 workflow
files' `pip install` lines (checked all of them). C4's backfill workflow will need `pip
install pyarrow` (or `fastparquet`) added explicitly — flagging now so it's not a
surprise mid-implementation.

---

## 3. Lookahead-safety guarantee for `PointInTimeDataContext`

### 3.1 The guarantee, stated exactly

> For any call through `PointInTimeDataContext(as_of=D)` — `close()`, `ohlcv()`, `vix()`,
> `hy_oas()`, `episodes()`, `benchmark()` — every row returned has `processed_date <= D`,
> and the returned series' last index is the latest available date `<= D` (never later).

That's the entire contract. Everything downstream is built to consume exactly that shape
and nothing more sophisticated — which is both the argument for why this is tractable and
the reason it has to be gotten right in exactly one place.

### 3.2 Why this is a single choke point (verified by reading the call sites, not assumed)

Every `engines/*.py` module (`dip_context.py`, `tech_read.py`, `bottom_scenarios.py`,
`relative_strength.py`, `episodes.py`) computes its "current" reading via `series.iloc[-1]`
and its historical comparison stats via `series.mean()`/`series.std()` over whatever it
was handed — e.g. `relative_strength.py`: `cur = s.iloc[-1]; z = (cur - s.mean()) /
s.std(ddof=1)`. None of these functions take a `date` parameter or check one. They are
correct *only* because every caller today happens to hand them a series that already ends
at "now" — the contract is enforced entirely by convention, not by the functions
themselves.

`forecast_engine.py`'s analog model is architecturally different and worth flagging
specifically. `run_one()` does:

```python
df = build_feature_frame(close, spy_close_run, rotation_ctx, ticker)
query_pos = len(df) - 1                      # "today" = last row, by convention
...
X, norm_stats = normalize_matrix(df, FEATURE_FIELDS)   # z-normalizes over the WHOLE df
analog_pos, nearest_dist = analog_positions(X, query_pos)
```

and inside `analog_positions()`:

```python
def analog_positions(X: np.ndarray, query_pos: int):
    ...
    valid = ~np.isnan(X).any(axis=1)
    valid[query_pos] = False        # excludes ONLY the query row itself
    ...                              # everything else in X is a candidate --
                                     # including rows AFTER query_pos, if any exist
```

`regime_conditioned_positions()`, called two lines later in the same function, *does* get
explicitly sliced (`regime_tuples[:query_pos]`) before being searched — so the two
candidate-selection paths that get unioned into `ensemble_pos` are inconsistently guarded
today. Neither is a live bug right now, because `query_pos` is always `len(df)-1` on the
live path, so there is nothing after it to leak. But `analog_positions()` and
`normalize_matrix()` have no internal defense if `C2`'s `replay()` ever calls this same
function with a `df`/`X` that extends past the replay date (e.g. a naive replay
implementation that fetches the full modern series once and threads a historical
`query_pos` through it, rather than truncating the frame itself before calling in) — the
nearest-neighbor search would then be free to match the query date against future price
action, and the z-normalization would be computed over statistics that include the
future. This is exactly the "excellent numbers, no error" contamination the spec's item 3
warns about — a `moderate_long_candidate` call sitting on an analog match from six months
after the replay date would look like skill.

**Conclusion for C2's design (flagging for that phase, not solving here):** `replay()`
must truncate every series/frame at the `DataContext` boundary — never pass a full-length
array with an index pointer into `forecast_engine.py`'s analog functions. `as_of()`
returning an already-truncated frame makes this automatic and removes the need to trust
`analog_positions()`'s internals at all. Worth also adding an explicit `assert
query_pos == len(df) - 1` at the top of `run_one()` / wherever C2 reuses it, cheap
insurance that turns a silent contamination into a loud crash if the truncation
assumption is ever violated by a future refactor.

### 3.3 How this gets tested — mutation/canary tests, not just equality checks

The spec's own framing is the right bar: *"a contaminated backtest does not error, it
produces excellent numbers."* An equality test between "truncated store" and "full store
filtered to the same date" is a weaker test than it looks, because if the truncation
logic itself has a bug, both code paths can share that bug and agree by coincidence. The
test that actually catches a leak has to inject a signal that *couldn't* be there
honestly and check it has zero effect:

1. **Canary/mutation test (primary mechanism).** Take a fixture store truncated at date
   `D`. Run `replay(ticker, D)` and record the output. Then splice an obviously
   out-of-family value into the *same* fixture at `D+1` (e.g. force `close = $1` or a
   +500% single-day move, and if flows/macro are wired up by then, an extreme flow/OAS
   spike too) and re-run `replay(ticker, D)` against the mutated store. **Assert the
   output is byte-identical.** If it isn't, something downstream of `as_of()` saw data
   past `D` — mechanically detected, not inferred from suspiciously good calibration
   numbers months later. This should run for every table (`prices`, `macro` once wired,
   `flows`/`options` once they exist) and for every engine surfaced through the bundle
   (dip_context, tech_read, bottom_scenarios, relative_strength, episodes), not just the
   top-level forecast — a leak in one card while the headline number stays clean is still
   a leak.
2. **`as_of()` itself gets a direct unit test independent of any engine**: build a tiny
   fixture with rows straddling a boundary date, call `as_of(table, ticker, D)`, and
   assert both that every returned row's `processed_date <= D` *and* that a row with
   `processed_date == D+1` is excluded even though its `effective_date` might be `<= D`
   (this is exactly the flows-table failure mode from §2.2 — an NAV figure *for* day D
   that wasn't *published* until D+2 must not leak into a `D+1` replay).
3. **Runtime assertion inside `PointInTimeDataContext` itself (belt-and-suspenders, not a
   substitute for #1).** Every method return passes through one shared check —
   `assert result.index.max() <= as_of_date` (or the `processed_date` equivalent for
   non-time-indexed returns like `episodes()`) — before handing data back to a caller.
   Cheap, always-on, and turns any *future* violation (a new engine added later that
   doesn't get a canary test written for it) into an immediate crash in dev/CI rather than
   a silently contaminated number three phases later.
4. **Tie into the acceptance test the spec already names** (§4 C2: `replay('SPY', <random
   2019 date>)` byte-identical against a store truncated at that date). That test proves
   determinism; it does not by itself prove *lookahead-freedom* — a function could be
   both deterministic and leaking (e.g., always looking exactly 30 days ahead) and still
   pass a determinism-only check. Recommend it get extended with the canary/mutation
   variant in #1 as a named, separate acceptance criterion for C3 specifically (distinct
   from C2's), since it's testing a different property.

### 3.4 What's *not* proposed here

No isotonic/self-retraining anything, no change to the confidence-gating logic, no
decision on the `BAMLH0A0HYM2` fix (§2.4) beyond naming two options for review. This
section is scoped to "how do we know the pipe doesn't leak," not "what do we do once it
doesn't."

---

## 4. Open questions for review

1. ~~**BAMLH0A0HYM2 fix.**~~ **Resolved 2026-08-05 — `BAA10Y` substituted, own
   (percentile-based) thresholds, see §2.4 and `docs/CREDIT_SERIES.md`.** Two follow-on
   requirements this creates for C4 specifically (fetch-start date, blowout-guardrail
   replay gap) are now documented at the end of §2.4 — read before starting the backfill.
2. **Adjusted-close as an accepted exception (§2.4)** — ratify explicitly, or does the
   store need to carry raw (unadjusted) prices + a separate corporate-actions table for
   stricter purity? The former is standard practice and far less work; flagging because
   it's currently an unstated assumption, not because there's an obvious problem with it.
3. **`MGK`'s 2007-12-27 start (§2.3)** — **Resolved 2026-08-05:** documented as a 3-year
   hole, same handling as today's rotation panel ("the engine uses whatever exists on
   each date"). No proxy/synthetic series.
4. **Flows/options tables (§2.5)** — confirm shipping the directory shape now with no data
   (Phase F populates later) is preferred over deferring the schema entirely until Phase F
   defines its own needs.

**Correction to an earlier note in this doc:** a prior draft flagged live `HORIZONS`
still including `126, 252` as possibly out of sync with the spec. That's not a
discrepancy — the spec's §2 "Replay horizons: 1d/5d/20d/60d" decision applies to the
**replay grid only**, to save backfill compute. Live keeps producing all six horizons;
those rows already exist in the ledger and cost nothing extra to keep computing. Live
`HORIZONS` is not to be changed.

---

## 5. Suggested next step

Question 1 (credit series) is now the higher-priority track — see
`docs/CREDIT_SERIES.md`, since the same missing dimension may already explain the ~0
resolution finding in the live calibration read, not just a replay-time concern. Question
4 (flows/options schema-now-vs-later) is the remaining open item before implementing
`as_of()`. Everything else in §2/§3 can be built as designed. Not starting implementation
per the
instruction this doc was requested under.
