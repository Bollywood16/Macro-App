-- B6 fix (revised twice): trading_date column, as_of_ts left untouched.
--
-- CORRECTION TO THE ORIGINAL B6 APPROACH: as_of_ts is the honest record of
-- when a run actually executed, including GitHub Actions' scheduling
-- delays (observed up to ~10h) -- it should never be silently overwritten,
-- and doing so would erase the evidence of the scheduling problem itself.
-- trading_date is a new, separate column: which trading day this
-- forecast's price/features are actually anchored to.
--
-- TRUE SCOPE (this is why the original weekend-only fix was insufficient):
-- a run delayed past midnight UTC on a WEEKDAY produces a wrong-but-
-- plausible as_of_ts that no weekend filter can catch. Confirmed live,
-- read-only, before writing this file:
--   - 1918 of 2083 forecast rows (92%) have provider_ts::date <>
--     as_of_ts::date -- the real mislabeling scope, not the 322/323
--     weekend rows found initially.
--   - 1596 of those 1918 mismatches are on a as_of_ts weekday -- entirely
--     invisible to a Saturday/Sunday filter.
--   - 593 of the 611 currently-scored outcomes (97%) were computed via
--     outcome_scoring.py's find_entry_pos(), which locates the entry bar
--     by searchsorted(as_of_ts.normalize()) -- against the WRONG date for
--     97% of them. This migration does not rescore anything; it exists so
--     Fix 4's rebuild can join on the correct anchor when it does.
--
-- BACKFILL SOURCE: quote_snapshots.provider_ts, joined via
-- forecasts.quote_snapshot_id -- the actual correct trading day, already
-- computed correctly by forecast_engine.py at generation time (confirmed
-- 0 quote_snapshots rows have a weekend provider_ts). Only 1 of 2083
-- forecast rows has no provider_ts to fall back to (forecast id
-- 8aa357bc-2a02-4b09-b51e-ad252992e175, ticker SMH, model_version
-- 'm1-smoketest', a pre-launch row, source='manual'); it falls back to its
-- own as_of_ts's date, the only information available for it.
--
-- TIMEZONE: provider_ts is derived from a 16:00 America/New_York close,
-- converted to UTC. Casting straight to ::date implicitly uses whatever
-- timezone the DB session assumes (normally UTC on this project) -- for
-- this specific dataset that happens to be safe (16:00 ET converts to
-- 20:00 or 21:00 UTC, never close enough to midnight to roll onto the
-- next calendar day; confirmed by comparing both casts across every
-- quote_snapshots row -- 0 differ). Using the explicit
-- `AT TIME ZONE 'America/New_York'` form anyway rather than relying on
-- that margin: it's correct regardless of session timezone and handles
-- DST transitions automatically via the named zone.
--
-- SAFETY: as_of_ts is never written to. trading_date/scheduler_drift_days
-- start nullable, are backfilled, THEN get NOT NULL (+ default for
-- scheduler_drift_days) -- a partial run can't leave either column
-- rejecting existing rows. No row is deleted or excluded.
--
-- DOWNSTREAM: also redefines pending_outcomes (20260805010941) to filter/
-- order on trading_date instead of as_of_ts -- per the standing rule that
-- the resolver, calibration, and scorecards join on trading_date from now
-- on, never as_of_ts.

alter table public.forecasts add column if not exists trading_date date;
alter table public.forecasts add column if not exists scheduler_drift_days integer;

update public.forecasts f
set trading_date = (qs.provider_ts at time zone 'America/New_York')::date
from public.quote_snapshots qs
where f.quote_snapshot_id = qs.id
  and qs.provider_ts is not null
  and f.trading_date is null;

-- The 1 no-provider_ts row: no better ground truth exists than its own
-- as_of_ts date.
update public.forecasts
set trading_date = as_of_ts::date
where trading_date is null;

alter table public.forecasts alter column trading_date set not null;
create index if not exists forecasts_trading_date_idx on public.forecasts (trading_date);

-- scheduler_drift_days: as_of_ts's date minus trading_date, in days -- how
-- many calendar days late this run's wall-clock execution was relative to
-- the trading day it's actually reporting on. 0 means the run executed on
-- (or before, same UTC day as) the trading day itself; a Friday-evening
-- run delayed into Saturday/Sunday/Monday morning shows 1-3. This
-- replaces an earlier draft called is_market_day, which only checked
-- as_of_ts's own weekday -- a boolean that measures whether the
-- SCHEDULER fired on a weekday, not whether the market was open, and
-- (per the 92%-vs-323-row finding above) would have silently missed the
-- large majority of actual drift, since a weekday-delayed run still looks
-- "fine" under that check.
update public.forecasts
set scheduler_drift_days = (as_of_ts::date - trading_date)
where scheduler_drift_days is null;

alter table public.forecasts alter column scheduler_drift_days set default 0;
alter table public.forecasts alter column scheduler_drift_days set not null;

-- Redefine pending_outcomes (supersedes 20260805010941_pending_outcomes_
-- view.sql's definition) to join/filter/order on trading_date -- as_of_ts
-- can legitimately be a different calendar day than the data it's
-- actually anchored to, which is the entire reason trading_date exists.
create or replace view public.pending_outcomes as
select f.*
from public.forecasts f
where not exists (
  select 1 from public.outcomes o where o.forecast_id = f.id
)
and (f.trading_date::timestamptz + (f.horizon_days || ' days')::interval) <= now();

alter view public.pending_outcomes set (security_invoker = true);
revoke all on public.pending_outcomes from public, anon, authenticated;
grant select on public.pending_outcomes to service_role;
