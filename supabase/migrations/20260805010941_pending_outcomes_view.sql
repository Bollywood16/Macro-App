-- B3 fix: replace mm-journal's broken list_pending_outcomes filter with a
-- real anti-join.
--
-- ROOT CAUSE: supabase/functions/mm-journal/index.ts's list_pending_outcomes
-- op used `.from("forecasts").select("*, outcomes!left(id)").is("outcomes.id",
-- null)`. PostgREST's embedded-resource `.is(col, null)` filter does not
-- reliably express "no matching row" for a left-embed -- confirmed live: the
-- oldest 500 forecasts by as_of_ts (computed with a plain SQL query, no
-- resolution filter at all) were byte-identical to what this op had been
-- returning on every run for at least four days straight (three separate
-- outcome-scoring.yml logs, Aug 1 - Aug 4, all showed not_yet_matured=249
-- identically -- the window never advanced). Once outcomes_forecast_uniq
-- existed, the 251 already-resolved rows in that stuck window started
-- bouncing off it as duplicate-key errors every single night
-- (matured_and_scored=0 errored=251) instead of silently re-inserting.
--
-- FIX: a real view using NOT EXISTS, which Postgres can actually plan as an
-- anti-join, plus a maturity pre-filter (`as_of_ts + horizon_days days`).
-- That calendar-day math is intentionally a LOWER bound, not the authoritative
-- maturity check: horizon_days counts TRADING days, and trading-day maturity
-- always requires at least as many calendar days (weekends/holidays only add
-- time, never subtract it). So this predicate can under-select (leave a
-- genuinely-matured row out for a few extra calendar days near a long
-- weekend) but can never over-select a row that isn't matured yet in
-- calendar terms -- and outcome_scoring.py's score_forecast() still does the
-- real bar-counting check and returns None (skip, not an error) for
-- anything this view let through too early. Two-layer defense, same as
-- before, just with a filter that actually filters.
--
-- SAFETY: purely additive -- one new view, no table/column changes, no data
-- touched. Same security_invoker + grant pattern as the existing
-- `calibration` view (see 20260717205802_tearsheet_calibration_lockdown.sql)
-- so this can't reintroduce that RLS gap: service_role only, no
-- anon/authenticated access.

create or replace view public.pending_outcomes as
select f.*
from public.forecasts f
where not exists (
  select 1 from public.outcomes o where o.forecast_id = f.id
)
and f.as_of_ts + (f.horizon_days || ' days')::interval <= now();

alter view public.pending_outcomes set (security_invoker = true);
revoke all on public.pending_outcomes from public, anon, authenticated;
grant select on public.pending_outcomes to service_role;
