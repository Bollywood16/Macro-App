-- Market Memory: regime_model_version column on forecasts (additive)
-- Paste into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and run
-- once. Idempotent via IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so a repeat
-- paste is harmless. See docs/CREDIT_SERIES.md item 5 for the full context.
--
-- WHY, AND WHY SEPARATE FROM model_version: regime_conditioned_positions()
-- and dip_context._matched_positions() just changed what "credit regime"
-- means for every future forecast row -- source (BAA10Y, not ICE BofA HY
-- OAS) and classification (percentile against a trailing 1260-day window,
-- not an absolute +/-0.25pp cutoff). Every row written before this change
-- was regime-conditioned against the OLD input; every row written after is
-- conditioned against a genuinely different, wider-coverage credit read.
-- These are not the same measurement, even though they land in the same
-- `forecasts` table with the same `model_version` -- model_version tracks
-- the ensemble/analog methodology, which did NOT change here, and will
-- change again for unrelated reasons later. Folding this into model_version
-- would make it impossible to slice calibration by regime-input vintage
-- independently of ensemble vintage. A dedicated column, done once now, is
-- cheap; doing it after rows have already accumulated under the ambiguity
-- is not (see: the exact problem trading_date's migration existed to fix
-- for as_of_ts, and voter's migration existed to fix for model_version's
-- overloaded second meaning -- same lesson, same fix shape, again).
--
-- CALIBRATION QUERIES MUST NEVER POOL ROWS ACROSS DIFFERENT
-- regime_model_version VALUES. Same posture as the standing "never mix
-- sources, vintages, voters, or market/non-market days in one calibration
-- number" guardrail (MARKET_MEMORY_V2_BUILD.md §3) -- this column exists
-- specifically so that rule is enforceable on the regime-input dimension,
-- which previously had no explicit vintage marker at all.
--
-- SAFETY CONTRACT (same posture as 004_forecast_voter_column.sql):
--   * No DROP, no RENAME, no column type change.
--   * New column starts nullable; backfilled for every existing row with the
--     LEGACY marker below (every row written before this migration was, by
--     construction, computed under the old OAS/absolute-threshold logic --
--     labelled, not left null).
--   * NOT NULL + default are only applied after the backfill statement, so
--     a partial run can't leave the column in a state that rejects existing
--     rows.
--   * Nothing here touches RLS.
--
-- DEPLOYMENT ORDER (matters -- do not reverse):
--   1. Run this file in the Supabase SQL editor.
--   2. Redeploy the mm-journal edge function (index.ts / the .txt mirror)
--      with regime_model_version added to create_forecast's accepted/
--      REQUIRED_FIELDS handling.
--   3. Ship forecast_engine.py's / dip_context.py's regime_model_version-
--      populating change (already in this commit) -- takes effect on the
--      next run once 1-2 are live. Sending the field before step 2 is
--      deployed is harmless (the edge function ignores unrecognized payload
--      keys) but the column stays at its default until then.
-- Reversing 1 and 2 means the edge function queries a column that doesn't
-- exist yet and every mm-journal call errors -- same failure mode voter's
-- migration already documented, same reason to respect the order.

alter table public.forecasts add column if not exists regime_model_version text;

update public.forecasts
set regime_model_version = 'regime-v0-oas-abs-0.25'
where regime_model_version is null;

alter table public.forecasts
  alter column regime_model_version set default 'regime-v1-baa10y-pctile-1260d-20-80';
alter table public.forecasts alter column regime_model_version set not null;

create index if not exists forecasts_regime_model_version_idx
  on public.forecasts (regime_model_version);
