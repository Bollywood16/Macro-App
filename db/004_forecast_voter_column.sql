-- Market Memory: Phase B — structural voter column on forecasts (additive)
-- Paste into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and run
-- once, after 001/002/003. Idempotent via IF NOT EXISTS / ADD COLUMN IF NOT
-- EXISTS, so a repeat paste is harmless.
--
-- WHY: A6 (Phase A) started persisting dip_context's own read as its own
-- forecasts row (same table, one row per horizon it computed), tagged only
-- via model_version ILIKE 'mm-dipcontext%' so every display surface could
-- exclude it via excludeDipContextGate(). String-matching model_version was
-- always meant to be a stopgap (see the "until Phase B's `source` column
-- exists" comment in mm-journal/index.ts) — fragile if MODEL_VERSION or
-- MODEL_VERSION_DIP_CONTEXT ever change shape, and it overloads
-- model_version with a second meaning (which model built the row) it
-- wasn't designed to carry.
--
-- NAMING NOTE: this column is `voter`, not `source`. `features_json.source`
-- already exists on every forecasts row and means something different — the
-- CLI/run-trigger tag ('batch' | 'on_demand' | 'chat' | 'scanner', set by
-- forecast_engine.py's --source flag). `voter` instead mirrors
-- agreement_engine.py's existing Ballot.voter vocabulary ('forecast' |
-- 'dip_context'), which is exactly the distinction this column encodes.
--
-- SAFETY CONTRACT (same posture as 003_tearsheet_layer.sql):
--   * No DROP, no RENAME, no column type change.
--   * New column starts nullable; backfilled from model_version for every
--     existing row (both ensemble and dip_context rows are already live —
--     A6 shipped in b48ce34, before this migration exists — so unlike
--     003's brand-new columns, this backfill is NOT a no-op and must run
--     before anything reads/writes `voter`).
--   * NOT NULL + CHECK are only applied after the backfill statement, so a
--     partial run (backfill fails, constraint step never reached) can't
--     leave the column in a state that rejects existing rows.
--   * Nothing here touches RLS — forecasts stays service_role-only, no
--     anon/authenticated policies, same as every other table.
--
-- DEPLOYMENT ORDER (matters — do not reverse):
--   1. Run this file in the Supabase SQL editor.
--   2. Redeploy the mm-journal edge function (index.ts / the .txt mirror)
--      with its excludeDipContextGate()/REQUIRED_FIELDS voter changes.
--   3. Ship forecast_engine.py's voter-populating change (already in this
--      commit) — its next run (scheduled or on-demand) starts writing
--      `voter` directly instead of relying on the backfill.
-- Reversing 1 and 2 means the edge function queries a column that doesn't
-- exist yet and every mm-journal call errors.

alter table public.forecasts add column if not exists voter text;

update public.forecasts
set voter = case when model_version ilike 'mm-dipcontext%' then 'dip_context'
                  else 'forecast' end
where voter is null;

alter table public.forecasts alter column voter set default 'forecast';
alter table public.forecasts alter column voter set not null;

alter table public.forecasts drop constraint if exists forecasts_voter_check;
alter table public.forecasts
  add constraint forecasts_voter_check check (voter in ('forecast', 'dip_context'));

-- Every gated read (get_latest_forecast, watchlist, unactioned-forecasts
-- queue) filters voter <> 'dip_context' — this index makes that filter
-- (and the inverse, scoring dip_context's own ledger in Phase D) cheap
-- without touching the existing forecasts_ticker_asof_idx.
create index if not exists forecasts_voter_idx on public.forecasts (voter);
