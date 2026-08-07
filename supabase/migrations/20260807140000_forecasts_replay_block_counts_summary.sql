-- Market Memory: forecasts_replay_block_counts_summary (additive)
-- Paste into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and run
-- once. Idempotent via CREATE TABLE IF NOT EXISTS, so a repeat paste is
-- harmless.
--
-- WHY: mm-journal's `forecasts_replay_block_counts` op (added alongside
-- 20260805150000_forecasts_replay_table.sql) ran 12 sequential
-- `count: "exact"` queries against forecasts_replay at READ time -- fine
-- when the table was empty/small, but forecasts_replay is now the full
-- 17-ticker x ~2005-2025 replay ledger (~530,000 rows, C4 backfill,
-- MARKET_MEMORY_V2_BUILD.md #4 / docs/C3_DESIGN.md #13-14) and the op
-- reproducibly 500s (`{"error":"db_error","detail":""}`, 3/3 retries) --
-- almost certainly the edge function's execution window running out
-- across 12 round-trip COUNT(*)s against a table this size.
--
-- FIX SHAPE: precompute once (a single grouped query, cheap even at full
-- table size since it's one sequential scan not 12), store the result as a
-- single summary row, and have the read-time op serve that row instead of
-- recomputing it live. A new mm-journal op, `refresh_forecasts_replay_
-- block_counts`, does the precompute and upsert; a new scheduled workflow
-- (.github/workflows/replay-block-counts-refresh.yml) calls it nightly.
-- `forecasts_replay_block_counts` itself becomes a single-row SELECT.
--
-- SINGLETON-ROW PATTERN: `id` is constrained to always be 1 (`check (id =
-- 1)`), so there is exactly one row, always upserted in place -- callers
-- never need to know or guess an id, and `computed_at` lets a caller see
-- how stale the snapshot is instead of silently trusting a live-looking
-- number that's actually last night's.
--
-- SAFETY CONTRACT (same posture as every prior additive migration in this
-- directory): CREATE TABLE IF NOT EXISTS, no DROP/RENAME/ALTER of
-- anything existing. RLS: service_role only, no anon/authenticated
-- policies -- same as forecasts_replay itself. Written only by the new
-- refresh op (nightly workflow, occasionally a manual dispatch); read only
-- by the now-cheap forecasts_replay_block_counts op.
--
-- DEPLOYMENT ORDER (do not reverse, same failure mode every prior
-- migration's own note already documents):
--   1. Run this file in the Supabase SQL editor.
--   2. Redeploy the mm-journal edge function (index.ts / the .txt mirror)
--      with refresh_forecasts_replay_block_counts added and
--      forecasts_replay_block_counts switched to read from this table.
--   3. Dispatch replay-block-counts-refresh.yml once by hand (or wait for
--      its next scheduled run) to populate the first real row -- until
--      then forecasts_replay_block_counts returns an empty {} with
--      computed_at null, same "no data yet" shape the old op returned for
--      an empty table.

create table if not exists public.forecasts_replay_block_counts_summary (
  id           smallint primary key default 1 check (id = 1),
  counts       jsonb not null,
  computed_at  timestamptz not null default now()
);

alter table public.forecasts_replay_block_counts_summary enable row level security;
revoke all on public.forecasts_replay_block_counts_summary from public, anon, authenticated;
grant select, insert, update on public.forecasts_replay_block_counts_summary to service_role;
