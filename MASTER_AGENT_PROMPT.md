# MARKET MEMORY — MASTER CODING-AGENT DIRECTIVE
### Adaptation of the Market Memory Build Spec v1.0 to the existing Macro-App
### Paste this file, plus the original spec docx, into the coding agent as project context.

## 0. Prime directives

1. You are extending a LIVE, WORKING application. No destructive rewrites.
   Every existing page, engine, workflow, and dataset must keep working
   after every milestone.
2. Implement milestones IN ORDER. Stop at each exit criterion for owner
   review. Never build the entire system in one pass.
3. All arithmetic lives in Python (GitHub Actions) or SQL (Supabase).
   The LLM layer interprets, retrieves, and explains. It never computes
   probabilities and never invents confidence.
4. Point-in-time discipline: a forecast may only use information available
   at its timestamp. Forecast and decision records are immutable —
   amendments create new rows linked to the original.
5. The scanner may return zero setups. Activity is not success;
   calibration and decision usefulness are.

## 1. Existing system map (verified live at bollywood16.github.io/Macro-App)

REPO: github.com/Bollywood16/Macro-App (public, main branch, GitHub Pages
from root)

    index.html                      Claude-style shell: sidebar, chat (PM
                                    synthesis), Sectors / Size / Macro views
    research.html                   Evidence engine UI (triggers, sampling,
                                    claims w/ computed confidence)
    rotation.html                   Leadership + relative performance
    overlay.html                    Original semis extension study (dark)
    pm.html                         Legacy standalone PM page
    scripts/research_engine.py      Asset-agnostic study engine (triggers ×
                                    sampling × regimes, claim mining,
                                    confidence scoring, GDELT news)
    scripts/rotation_engine.py      Sector/size RS, leadership regimes,
                                    conditional claims, macro block (FRED)
    scripts/extension_overlay.py    Original study engine
    scripts/*_config.json           universe / rotation / revision-regime
                                    configs (owner-editable)
    data/*.json                     Committed engine outputs (the app's
                                    point-in-time record so far)
    .github/workflows/*.yml         Weekday post-close crons + manual runs

SUPABASE: project ref anzbpxqvibgpxnwgyqoc
    Edge Function slug: smooth-service (display name pm-synthesis) —
    passphrase-gated (x-app-key vs APP_PASSPHRASE secret), holds
    ANTHROPIC_API_KEY, fetches Pages digests, returns structured PM JSON.
    Postgres: EMPTY — no tables yet. This is where Market Memory's
    journal/forecast/outcome store goes.

CONSTRAINTS CARRIED FORWARD FROM THE OWNER'S FRAMEWORK
    - Confidence is computed (n × consistency × conjunction-depth penalty ×
      decade coverage), never model-vibed.
    - Sampling must be deduplicated (one-per-uptrend style) before any
      statistic is presented as independent n.
    - Mined claims must carry the searched-conjunction denominator.
    - News evidence must be dated artifacts (GDELT 2017+); never backfill
      history from model memory.
    - No leverage recommendations; no single "buy X" outputs; sizing is
      presented as arithmetic (worst analog drawdown × size vs tolerance),
      never as instruction.

## 2. Architecture decision (already made — do not relitigate in M1–M5)

Implement the Market Memory loop ON THE EXISTING STACK:
    - Scheduler/cron  -> GitHub Actions (already in place)
    - Quant service   -> Python scripts in /scripts (already in place)
    - Immutable market states / digests -> committed JSON (already in place)
    - Forecasts, decisions, outcomes, scorecards -> Supabase Postgres,
      accessed ONLY through Edge Functions (service-role key stays server-
      side; the passphrase gate is the single-user auth layer)
    - UI -> static pages in the existing shell (new sidebar tabs)
    - LLM -> existing Edge Function pattern; add SQL-grounded retrieval
The spec's Next.js/Auth/pgvector monorepo is DEFERRED to a possible V2,
reconsidered only after M5 ships and the loop proves its value.

## 3. Milestones

M1 — Schema + write path
    Create Supabase tables via SQL migration (paste-ready .sql file):
      assets(id, ticker, label, category)
      quote_snapshots(id, ticker, price, source [provider|manual],
        provider_ts, retrieved_ts, market_status, is_indicative,
        created_at) — immutable
      forecasts(id, ticker, as_of_ts, effective_price, quote_snapshot_id,
        horizon_days, benchmark, p_positive, p_beat_benchmark,
        q20, q50, q80, expected_mae, n_independent, confidence_score,
        confidence_label, model_version, features_json, evidence_json,
        created_at) — immutable, append-only
      decisions(id, forecast_id, action [buy|sell|hold|watch|stand_down],
        exec_price, units, notional, pct_portfolio, horizon_days,
        user_confidence, thesis, invalidation, agree_reason,
        outside_info boolean, created_at) — immutable
      outcomes(id, forecast_id, evaluated_at, end_price, benchmark_return,
        abs_return, excess_return, max_adverse_exc, max_favorable_exc,
        event_occurred, interval_covered, brier, log_loss, notes)
      amendments(id, parent_table, parent_id, payload_json, created_at)
    RLS: deny-all; only service role writes/reads (single-user app, all
    access through Edge Functions).
    New Edge Function `mm-journal`: passphrase-gated CRUD-lite —
    create forecast (called by quant job), create decision (called by UI),
    read joined views. EXIT: a decision can be written and read back.

M2 — Forecast engine v1 (quant)
    New scripts/forecast_engine.py, reusing research_engine components:
      - Analog model: nearest independent episodes in normalized feature
        space (features: returns 1/5/20/60/120d, RSI, MA distances,
        drawdown, relative-vs-SPY, regime dims from existing engines).
      - Regime-conditioned base rates from the existing claims machinery.
      - Output per spec 7.1 targets (1/5/20/60d horizons): p_positive,
        p_beat_benchmark, q20/q50/q80, expected MAE, n_independent,
        computed confidence.
      - Calibration by simple isotonic/Platt on walk-forward folds; store
        model_version string; persist a features_json snapshot.
    Runs in Actions post-close for the configured universe; also callable
    ad hoc with a manual price (writes intraday_proxy=true and a
    confidence discount when the price is user-supplied).
    EXIT: on-demand forecast for SMH at a manual price produces the spec
    4.3 recommendation card fields, persisted via mm-journal.

M3 — UI: On-demand analysis + Decision Journal
    New sidebar tab "Forecast": ticker + optional manual price ->
    calls mm-journal (which invokes stored latest engine output or a
    lightweight recompute), renders the recommendation card (spec 4.3)
    including data-quality warnings, then shows the decision form
    (spec 4.4) ONLY AFTER the forecast row is persisted.
    New tab "Journal": open decisions, approaching evaluations, history.
    EXIT: spec MVP items 2–5.

M4 — Daily scanner + outcome scoring
    Extend the post-close Action: event triggers (spec 8.2 subset:
    return percentile, RSI extreme, drawdown threshold, relative
    underperformance, regime transition), gate by confidence and
    no-signal rule, rank by downside-adjusted expected excess return,
    write "Today's Setups" JSON + forecast rows. Scheduled outcome job:
    trading-calendar horizon evaluation writing outcomes rows with Brier /
    log-loss / interval coverage. EXIT: spec MVP items 6–7.

M5 — Scorecards + grounded search
    Calibration view (reliability by probability bucket, Brier, coverage)
    and user-judgment view (stand-down value, override value, chase/early-
    exit patterns — mirrors the owner's existing psychology log).
    Extend the chat Edge Function with SQL-grounded retrieval over
    forecasts/decisions/outcomes (start with parameterized SQL tools;
    pgvector deferred). Every number in an answer must originate from a
    query result echoed in an evidence block. EXIT: spec MVP items 8–10
    (challenger training remains manual/offline; registry table only).

## 4. Acceptance criteria (every milestone)
    - No existing page or workflow broken (smoke-check all five pages).
    - New tables have RLS deny-all + service-role-only access.
    - No secrets in the public repo. Passphrase gate on every function.
    - Immutability enforced (no UPDATE/DELETE grants on forecast/decision
      tables; amendments table only).
    - A written runbook line for anything the owner must click manually.

## 5. Provider note
    yfinance remains the V1 price source inside Actions (it already powers
    the engines). Implement the spec's provider interface as a thin Python
    adapter so alpha_vantage/twelve_data can be swapped in later. Delayed
    or manual quotes must be labeled per spec 5.1/5.3 — never silently.
