-- Market Memory: forecasts_replay_block_counts_agg() (additive)
-- Paste into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and run
-- once. Idempotent (CREATE OR REPLACE FUNCTION, CREATE INDEX IF NOT
-- EXISTS), so a repeat paste is harmless.
--
-- WHY THIS SECOND MIGRATION, NOT FOLDED INTO 20260807140000: that one
-- moved the WHEN (nightly refresh instead of live-read) but not the WHAT --
-- refresh_forecasts_replay_block_counts still ran the original 12
-- sequential `count: "exact"` PostgREST queries, just from a scheduled
-- caller instead of a request-time one. Verified directly (not assumed):
-- calling the refreshed op still returned the same
-- `{"error":"db_error","detail":""}` 500 forecasts_replay_block_counts
-- itself used to. Relocating 12 round trips to a schedule doesn't shrink
-- them below the function's own execution window if they were already
-- close to (or past) it -- each of the 12 is its own bitmap-index-scan
-- COUNT(*) plus its own HTTPS round trip through PostgREST, against a
-- table that grew to ~530,000 rows with the C4 backfill
-- (MARKET_MEMORY_V2_BUILD.md #4). The actual fix has to shrink the query
-- count, not just its scheduling: one grouped aggregate, one round trip,
-- one sequential-or-index scan, instead of 12 of each.
--
-- forecasts_replay_block_counts_agg(): a single `GROUP BY voter,
-- horizon_days` covers all 12 (voter, horizon_days) combinations the old
-- loop asked for individually, in one query plan. SECURITY DEFINER so
-- service_role (mm-journal's only caller) can execute it without needing
-- direct table SELECT beyond what it already has; STABLE (read-only, same
-- results within one statement) lets the planner cache/reuse across calls
-- in the same transaction where relevant.
--
-- Composite index (voter, horizon_days) added alongside -- the two
-- existing single-column indexes (forecasts_replay_voter_idx,
-- forecasts_replay_horizon_idx from 20260805150000) support the OLD
-- per-combo query shape (bitmap AND of two scans) but not this GROUP BY as
-- efficiently; a single composite index lets Postgres satisfy the whole
-- aggregate with one index-only-ish scan.
--
-- SAFETY CONTRACT (same posture as every prior additive migration in this
-- directory): no DROP/RENAME/ALTER of anything existing, CREATE OR REPLACE
-- / IF NOT EXISTS throughout. Function has no side effects (pure SELECT),
-- EXECUTE granted to service_role only -- same posture as every table in
-- this schema.
--
-- DEPLOYMENT ORDER: run this AFTER 20260807140000 (needs
-- forecasts_replay_block_counts_summary to exist is NOT required by this
-- file itself, but mm-journal's refresh op calling this function does
-- assume that table exists to upsert into -- same ordering already
-- documented there). Redeploy mm-journal (index.ts / the .txt mirror)
-- with refresh_forecasts_replay_block_counts switched to call this
-- function instead of looping, same step as before, no separate redeploy
-- needed if done together.

create index if not exists forecasts_replay_voter_horizon_idx
  on public.forecasts_replay (voter, horizon_days);

create or replace function public.forecasts_replay_block_counts_agg()
returns table(voter text, horizon_days int, cnt bigint)
language sql
stable
security definer
set search_path = public
as $$
  select voter, horizon_days, count(*) as cnt
  from public.forecasts_replay
  where horizon_days in (1, 5, 20, 60, 21, 63)
  group by voter, horizon_days;
$$;

revoke all on function public.forecasts_replay_block_counts_agg() from public, anon, authenticated;
grant execute on function public.forecasts_replay_block_counts_agg() to service_role;
