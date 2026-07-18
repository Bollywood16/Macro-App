-- Market Memory M6: tear-sheet data layer (additive)
-- Paste into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and run
-- once, after 001_market_memory_schema.sql and 002_model_registry.sql.
-- Idempotent via IF NOT EXISTS / ADD COLUMN IF NOT EXISTS, so a repeat paste
-- is harmless.
--
-- SAFETY CONTRACT (verified against a live schema dump taken 2026-07-17
-- before writing this file — see MM_UPGRADE conversation backup):
--   * No DROP, no RENAME, no column type change, anywhere in this file.
--   * Every new column on an existing table (decisions, outcomes) is
--     nullable with no default that could retroactively imply a value —
--     existing rows (2 in decisions, 166 in outcomes as of the backup)
--     are unaffected.
--   * `episodes` is the only new table; it does not touch assets,
--     quote_snapshots, forecasts, decisions, outcomes, model_registry,
--     or amendments.
--   * Nothing here relaxes the live app's security model: RLS stays
--     enabled with ZERO anon/authenticated policies (deny-all) on every
--     table, exactly like 001/002. Access stays service_role-only,
--     reached exclusively through passphrase-gated Edge Functions. The
--     original draft schema.sql shipped with this package had
--     `using (true)` policies on decisions/outcomes/episodes — those are
--     NOT applied here, because they would have opened those tables to
--     anon/authenticated clients directly, contradicting 001's own
--     deny-all design. episodes gets the same grant-based, policy-free
--     pattern as every other table.

create extension if not exists pgcrypto;

-- ------------------------------------------------------------- episodes
-- Net-new table. The statistical-analog library: fingerprinted by Python
-- (episodes.py), enriched by handoff research written back (cause /
-- what_ended_it). Reused so an event is never re-researched twice.

create table if not exists public.episodes (
  id             uuid primary key default gen_random_uuid(),
  asset          text not null,
  event_date     date not null,
  drawdown_pct   numeric,
  extension_pct  numeric,
  fwd_126d_pct   numeric,
  days_to_trough int,
  cause          text,
  what_ended_it  text,
  source         text check (source in ('python','handoff','manual')),
  created_at     timestamptz not null default now(),
  unique (asset, event_date)
);

create index if not exists episodes_asset_date_idx
  on public.episodes (asset, event_date desc);

alter table public.episodes enable row level security;
revoke all on public.episodes from public, anon, authenticated;
-- insert (Python fingerprint) + select (frontend) + update (handoff
-- write-back annotates cause/what_ended_it on an existing row) — the one
-- table in this layer that legitimately needs update, since annotation
-- happens strictly after the row already exists.
grant select, insert, update on public.episodes to service_role;

-- ------------------------------------------------------- decisions (additive)
-- Existing table (db/001), 2 live rows. Adds room for the tear-sheet's
-- richer decision-time context that today's decisions table has nowhere
-- to put: the regime read at the moment of the call, and which episodes
-- (if any) informed it. Both nullable — old rows simply have them null.

alter table public.decisions
  add column if not exists regime_json jsonb,
  add column if not exists informed_by uuid[] default '{}';

-- No grant change: decisions already has select+insert only for
-- service_role (db/001) and no update/delete grant — stays immutable.

-- -------------------------------------------------------- outcomes (additive)
-- Existing table (db/001), 166 live rows. The one concept in the tear
-- sheet's outcome model that the live table doesn't already cover:
-- process_grade, graded on rule adherence (not P&L). Everything else the
-- new schema.sql wanted for outcomes (hit/realized/forecast pct, horizon)
-- is already better covered by the live columns (event_occurred,
-- abs_return, excess_return, brier, log_loss) — no duplicate columns
-- added. Nullable; written once at INSERT time by whatever grades the
-- decision (outcomes has insert-only, no update grant, so this must be
-- populated at insert, matching the immutable-once-matured design of the
-- rest of the table).

alter table public.outcomes
  add column if not exists process_grade text
    check (process_grade is null or process_grade in ('A','B','C','D','F'));

-- No grant change: outcomes already has select+insert only for
-- service_role (db/001).

-- ------------------------------------------------------------ calibration
-- The rewritten version of the new schema.sql's calibration view. The
-- original referenced decisions.model_verdict / outcomes.hit /
-- outcomes.matured_at — none of which exist on the live tables (that
-- draft was written against the aspirational schema, not this one).
-- Confidence actually lives on `forecasts` (confidence_label /
-- confidence_score), so this joins forecasts -> outcomes directly: every
-- forecast the model made gets scored, not only the ones the user acted
-- on, which is the more honest "does stated confidence match realized
-- hit-rate" question this view exists to answer (BUILD.md §3).

create or replace view public.calibration as
select
  f.ticker,
  f.horizon_days,
  f.confidence_label,
  count(*)                                        as n,
  avg(case when o.event_occurred then 1 else 0 end) as realized_hit_rate,
  avg(f.confidence_score)                          as stated_confidence,
  avg(o.brier)                                     as avg_brier
from public.forecasts f
join public.outcomes o on o.forecast_id = f.id
group by f.ticker, f.horizon_days, f.confidence_label;

-- Views inherit RLS from their underlying tables under a SECURITY INVOKER
-- read (Postgres default) — since forecasts/outcomes have zero
-- anon/authenticated policies, calibration is equally inaccessible to
-- those roles without any extra statement needed here.
