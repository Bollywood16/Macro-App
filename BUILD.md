Read BUILD.md and follow it. This is an upgrade to the existing app — inventory the repo first and show me your integration plan before writing any code.# Market Memory — Upgrade Build (for Claude Code)

**Target repo:** `github.com/Bollywood16/Macro-App` (existing app — this is an UPGRADE, not a greenfield build).
**Read this whole file before touching anything.** This package contains the engines, components, SQL, and mockups produced in a design session on 2026-07-17. Your job is to integrate them into the existing app, preserving what works.

---

## 0. Prime directive & guardrails (do not violate)

1. **Python/SQL computes; the LLM/UI only interprets.** Never let a language model generate the numbers. Every stat originates in a Python engine and is translated by a deterministic `plain` field or a template. This is the core design constraint of the whole app.
2. **The decision log is immutable and sacred.** There is an existing Supabase journal with ~81 pending forecasts. **Do not migrate destructively, do not drop it, do not alter existing rows.** Add columns/tables additively. Confirm a backup exists before any schema change.
3. **Confidence gates presentation.** Low sample size (`n` small / "likely mined") can NEVER render as a green/BUY verdict. Amber-cap it with a caveat sentence. This rule already exists in `dip_context.py` (`build_verdict`) and `relative_strength.py`; keep it everywhere.
4. **Verdicts must be equal-weight.** WAIT and AVOID render as prominently as BUY. Do not make green louder than amber.
5. **No in-app chat.** Decision (this session): the tear sheet answers "should I buy the dip in X" as a structured verdict; novel/comparative/behavioral questions go through a **handoff** ("Copy for Claude" button that assembles a context bundle + a pre-written prompt). Do not build a chat backend.

---

## 1. What already exists in the repo (do NOT rebuild — verify & reuse)

- 4 Python analysis engines + a calibrated forecast engine (analog models, regime-conditioned base rates).
- Immutable Supabase Postgres journal, RLS deny-all.
- Daily scanner w/ confidence gating, outcome scoring loop, calibration scorecards.
- PWA shell installable to iPhone.
- FRED/yfinance ingestion (predecessor), GitHub Actions cron.

**First action:** inventory the repo and map these existing pieces to the new modules below. Reuse the forecast engine and journal; wrap, don't replace.

---

## 2. New/updated ENGINES (in `engines/`)

| File | Status | Purpose |
|---|---|---|
| `dip_context.py` | built this session | regime classify + conditional fwd-return stats + volume forensics + plain language + confidence-gated verdict |
| `tech_read.py` | built this session | trend/momentum/S-R/volume read across a series; STRONG/WEAK reclaim discriminator; plain read; chart series for the frontend |
| `bottom_scenarios.py` | built this session | Monte Carlo trough estimation (episode-sampled + break branch); fan-chart data + plain readout |
| `relative_strength.py` | **new this session** | rolling z/percentile vs. multiple benchmarks; **percentile first (fat-tail honest)**; historical resolution; confidence-gated flag |
| `episodes.py` | **new this session** | "Episodes like this" discovery: screens history for statistical analogs, merges stored causal narratives, builds the handoff prompt |

**Integration tasks:**
- Point `tech_read` / `dip_context` at BOTH daily and intraday (15m) bars. Add a lookback-window guard for short intraday series.
- Add a **benchmark lookup table** so any ETF resolves its three comparators, e.g. `SMH -> {S&P: SPY, Nasdaq: QQQ, Peers: SOXX}`, `XLE -> {SPY, XOP, ...}`. This is what makes "pull any ETF" work.
- Add a **forecast-horizon runner**: run the existing regime-conditioned base-rate forecast at horizons `[1d, 5d, 1mo, 3mo, 6mo, 1yr]`, each with its own confidence. Confidence often FALLS at longer horizons (fewer independent samples) — show that honestly.
- Wire all engines to a single OHLCV fetch per ticker (one fetch serves every module).

---

## 3. DATA LAYER (in `sql/schema.sql`) — additive migration

Three linked tables + calibration view. **Apply additively to the existing Supabase project.**
- `episodes` — the library. Populated by Python (fingerprint) and enriched by handoff research written back (cause / what_ended_it). Reused so events aren't re-researched.
- `decisions` — the immutable log: `model_verdict/confidence/forecast_json` (what the model said) + `user_action/size/rationale/invalidation` (what the user decided), linked to the episodes that informed it. **Insert+select only; no update/delete policy => immutable.**
- `outcomes` — filled when horizons mature; scores each decision; `process_grade` is graded on RULE ADHERENCE, not P&L.
- `calibration` view — the ONLY "learning": compares stated confidence to realized hit-rate as outcomes mature. **Slow, calibration-based. Do NOT build a fast self-retraining predictor** — at ~0 matured outcomes that would overfit noise and be actively dangerous.

Map the existing 81-forecast journal into `decisions` (or view-bridge it) WITHOUT losing history.

---

## 4. FRONTEND — the tear sheet (in `components/`)

Design is settled. Reference mockups: `mockups/market-memory-v3.html` (as-of, dated axes, live averages, **finger-scrubbable price chart**) and `market-memory-v2.html` (two-page structure, indigo header, technicals page). Reuse `DipContextCard.jsx` and `TAReadModal.jsx` as starting points; restyle to the tokens below.

**Design tokens (light theme):**
- paper `#fbfbf9`, surface `#fff`, line `#e7e8e4`, ink `#1c2128`, muted `#5c6470`, faint `#949aa4`
- **indigo `#4338ca`** = chrome/navigation, used as a **header OUTLINE/border, not a fill**
- teal `#2b6d78` = analysis accent; verdict: buy `#3f8f5b`, wait `#b0812f`, avoid `#c0574f` (all muted, equal weight)
- display face Fraunces (verdicts/numbers), body Inter, mono IBM Plex Mono (data/labels)
- **Signature element: the confidence meter** — a calibrated gauge on the verdict and every forecast horizon; visually caps how loud a verdict can be.

**App home:** NOT chat. Make it a watchlist/dashboard the user taps into for tear sheets (or a daily briefing of tracked asset classes). Confirm which with the user before building the home tab.

**Tear sheet = two pages (tabs):**

**Page 1 — Overview** (top → bottom):
1. Header (indigo outline): ticker, name, price. **As-of timestamp** ("As of <date> · <time> ET", "15-min delayed") directly below.
2. **Verdict banner** (top, as requested): pill BUY/WAIT/AVOID + one takeaway sentence + confidence meter.
3. Regime read: "what today looks like" + "how we got here" (plain).
4. **Price chart**: dated x-axis, period label, live values row (Level / 50-day / 200-day as NUMBERS), and **finger-scrubbable** — drag shows crosshair with date + value + **stretch (z/percentile) at that point**, updating the Level readout live. See v3 mockup JS for the interaction.
5. Relative-strength flag (condensed; full detail on Technicals).
6. Monte Carlo (3 stat tiles: % low is in / likely trough / tail risk).
7. Forecast strip: all horizons, sentence + confidence bar each.
8. **"Copy for Claude"** button (indigo) — assembles the handoff bundle (see §5).

**Page 2 — Technicals:**
- **Model trend callouts at the TOP** (before charts) — the things the user could miss (e.g. momentum divergence, lower-highs-on-fading-volume). This is the point of the page.
- Period selector (1D/1W/3M/6M/1Y/5Y).
- Stacked charts, each with dated axis + live current-value readout + scrubbable: price+MAs, volume, RSI, MACD.
- Full relative-strength breakdown (all windows × benchmarks) + "plain read across periods" that handles timeframe disagreement (name the long-term trend as tiebreaker).
- **"Episodes like this"** module: the dates `episodes.py` found, with resolution stats; annotated ones show cause/what-ended-it; a button runs the handoff for un-annotated dates.

**Interactions:** iPhone-style **double-tap status bar → scroll to top** (see v3 JS). Reduced-motion respected, visible focus, responsive to mobile.

---

## 5. THE HANDOFF (replaces in-app chat)

"Copy for Claude" assembles a **context bundle** to the clipboard:
- the current tear-sheet JSON (verdict, regime, forecast, RS, MC, technicals),
- the user's framework rules + gates as system context (so the conversation inherits the discipline — encode the anti-chase / regime-overrides-chart / no-anticipatory-DRAM style rules),
- recent relevant `decisions` rows (journal history),
- for episode research: the **pre-written prompt from `episodes.build_handoff_prompt()`** so the user never has to know which events (e.g. "DeepSeek") to ask about — Python found the dates, the prompt asks why.

**Write-back:** provide a simple paste-back path so the causal narrative Claude returns is saved into `episodes` (cause / what_ended_it). This is how the library compounds and stops needing re-research.

---

## 6. BUILD ORDER (phases — ship each before the next)

1. **Data layer** — apply `sql/schema.sql` additively; bridge the 81-forecast journal into `decisions`. Verify immutability (update/delete denied).
2. **Engines** — drop in the 5 engines; add benchmark table + horizon runner; wire to one OHLCV fetch (daily first, intraday second).
3. **"Pull any ETF"** — ticker input → fetch → benchmarks → run all engines → JSON. This is the highest-leverage step; it turns the demo into a tool.
4. **Tear sheet Page 1** (Overview) with the scrubbable price chart. Use TradingView **Lightweight Charts** (free, ~40KB) for real candlesticks; the mockup SVGs are placeholders.
5. **Tear sheet Page 2** (Technicals) + Episodes module.
6. **Handoff** bundle + write-back.
7. **Calibration** — wire the view to a scorecard screen. This only becomes meaningful as outcomes mature; **the single most valuable near-term action is letting the 81 forecasts resolve**, not adding features.

---

## 7. Honest constraints to surface to the user (don't paper over)

- **Real-time:** yfinance gives 15-min-delayed intraday for ~60 days, free. True live requires a paid feed (Polygon/Databento/Alpaca). Delayed is fine for a confirmation-based framework; start free.
- **"Any ETF":** newer/thin-history ETFs yield low-confidence conditional stats — the confidence gate will correctly show that rather than hide it.
- **"Learning":** means calibration (confidence adjusts to realized hit-rate over months), NOT fast self-retraining. Enforce the patience.
- **Robinhood:** no third-party API; app stays a decision tool beside it. Design language only.

---

## 8. Files in this package

```
engines/    dip_context.py  tech_read.py  bottom_scenarios.py  relative_strength.py  episodes.py
components/ DipContextCard.jsx  TAReadModal.jsx
sql/        schema.sql
mockups/    market-memory-v3.html  market-memory-v2.html
```

Start by inventorying the repo and producing an integration plan that maps each existing module to §2–§5 before writing code. Confirm the journal backup and the app-home choice (§4) with the user before destructive-looking steps.
