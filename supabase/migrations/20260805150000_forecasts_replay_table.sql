-- Market Memory: forecasts_replay table (Phase C4, additive, NOT deployed/run
-- yet -- committed for review only, same posture as every other migration in
-- this directory that's landed ahead of its own deploy step; see
-- docs/C3_DESIGN.md #7/MARKET_MEMORY_V2_BUILD.md #4 C4).
--
-- WHY A SEPARATE TABLE, NOT MORE ROWS IN `forecasts`: MARKET_MEMORY_V2_BUILD.md
-- #4's own C4 spec calls for this explicitly ("Writes to forecasts_replay
-- (separate table, same schema)") -- the standing guardrail in this repo is
-- "never mix sources, vintages, voters, or market/non-market days in one
-- calibration number" (#3), and a replay/backfill row is a categorically
-- different kind of measurement than a live one even when every other field
-- matches: it was computed AFTER the fact, from a point-in-time reconstruction,
-- never a genuine ex-ante prediction nobody could have cheated on by
-- construction. Mixing the two tables would make that distinction
-- unenforceable at query time. `docs/CREDIT_SERIES.md`'s own framing ("two
-- ledgers, permanently separate... model-vs-user, but the same reasoning
-- applies") is the same principle one level up.
--
-- SCHEMA: mirrors `public.forecasts` (db/001_market_memory_schema.sql +
-- every additive column since: db/004_forecast_voter_column.sql,
-- 20260805014219_trading_date_column.sql,
-- 20260805120000_regime_model_version_column.sql) column-for-column, with
-- two deliberate differences, both because replay() never creates a real
-- quote snapshot (it runs dry_run=True -- see scripts/replay.py):
--   1. `quote_snapshot_id` is NULLABLE here and carries NO foreign key to
--      quote_snapshots -- every replay row's quote_snapshot_id is NULL,
--      always, by construction. Enforcing the live table's NOT NULL + FK
--      would make every backfill insert fail.
--   2. `ticker` carries NO foreign key to `assets` -- not because replay
--      tickers aren't in `assets` (they likely are; not verified here), but
--      because a batch-insert failing on one bad FK reference shouldn't be
--      able to roll back the other N-1 rows in the same batch over a data
--      hygiene issue unrelated to replay's own correctness. Confirm assets
--      coverage for all 17 replay tickers before running C4 for real, but
--      don't gate the schema on it.
-- Everything else (all NOT NULL / CHECK constraints, all column types) is
-- identical to `forecasts` as of this migration's timestamp.
--
-- SAFETY CONTRACT (same posture as every prior additive migration in this
-- directory):
--   * CREATE TABLE IF NOT EXISTS, no DROP/RENAME/ALTER of anything existing.
--   * RLS: service_role only, no anon/authenticated policies -- same as
--     `forecasts`. This table is written exclusively by the C4 backfill
--     workflow (via a new mm-journal op, create_forecast_replay_batch, added
--     in this same change -- see supabase/functions/mm-journal/index.ts /
--     mm-journal-edge-function.txt) and read by Phase D's scoring pass, never
--     by a live user-facing surface.
--
-- DEPLOYMENT ORDER (do not reverse, same failure mode every prior migration's
-- own note already documents -- deploying the edge function change first
-- means it queries a table that doesn't exist yet):
--   1. Run this file in the Supabase SQL editor.
--   2. Redeploy the mm-journal edge function (index.ts / the .txt mirror)
--      with create_forecast_replay_batch added.
--   3. Only then dispatch the C4 backfill workflow -- NOT done as part of
--      this change; see docs/C3_DESIGN.md #8 for why (runtime estimate,
--      explicitly not dispatched pending review).

create table if not exists public.forecasts_replay (
  id                    uuid primary key default gen_random_uuid(),
  ticker                text not null,
  as_of_ts              timestamptz not null,
  trading_date          date not null,
  scheduler_drift_days  integer not null default 0,
  effective_price       numeric(18,6) not null check (effective_price > 0),
  quote_snapshot_id     uuid,
  horizon_days          int not null check (horizon_days > 0),
  benchmark             text not null default 'SPY',
  p_positive            numeric(8,6) check (p_positive between 0 and 1),
  p_beat_benchmark      numeric(8,6) check (p_beat_benchmark between 0 and 1),
  q20                   numeric(12,8),
  q50                   numeric(12,8),
  q80                   numeric(12,8),
  expected_mae          numeric(12,8),
  n_independent         int,
  confidence_score      numeric(8,6),
  confidence_label      text,
  model_version         text not null,
  voter                 text not null check (voter in ('forecast', 'dip_context')),
  regime_model_version  text not null,
  features_json         jsonb,
  evidence_json         jsonb,
  created_at            timestamptz not null default now()
);

create index if not exists forecasts_replay_ticker_trading_date_idx
  on public.forecasts_replay (ticker, trading_date desc);
create index if not exists forecasts_replay_voter_idx
  on public.forecasts_replay (voter);
create index if not exists forecasts_replay_regime_model_version_idx
  on public.forecasts_replay (regime_model_version);
create index if not exists forecasts_replay_horizon_idx
  on public.forecasts_replay (horizon_days);

alter table public.forecasts_replay enable row level security;
revoke all on public.forecasts_replay from public, anon, authenticated;
grant select, insert on public.forecasts_replay to service_role;
