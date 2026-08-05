-- B6 follow-up: NULL out the one orphan row's guessed trading_date instead
-- of keeping a fallback that made the post-migration check ("no row has a
-- weekend trading_date") fail on a false positive.
--
-- 20260805014219_trading_date_column.sql's post-check
-- (count(*) where extract(dow from trading_date) in (0,6)) returned 1, not
-- 0: forecast id 8aa357bc-2a02-4b09-b51e-ad252992e175 (ticker SMH,
-- model_version 'm1-smoketest', a pre-launch row with source='manual' and
-- no linked quote_snapshot.provider_ts at all). That migration fell back
-- to the row's own as_of_ts::date for lack of any better ground truth --
-- which happens to be a Sunday (2026-07-12), so the row is real, not a
-- calculation bug, but a guess presented as data.
--
-- CORRECTION: NULL is more honest than a guess here. trading_date's
-- NOT NULL constraint is relaxed; a NULL trading_date means "we have no
-- reliable trading day for this row" and is enforced to ONLY be possible
-- when the row's linked quote_snapshot genuinely has no provider_ts --
-- Postgres CHECK constraints can't reference another table, so this is
-- enforced via a BEFORE INSERT OR UPDATE trigger instead, which is the
-- correct mechanism for a cross-table invariant. Same treatment for
-- scheduler_drift_days: 0 there was a fallback (drift relative to a
-- guessed date), not a real measurement, and would have quietly skewed
-- any future drift statistics -- NULL is now allowed there too, no
-- special constraint needed since it isn't a join/filter key downstream.
--
-- PRACTICAL EFFECT: pending_outcomes' WHERE clause
-- (trading_date::timestamptz + horizon_days days <= now()) evaluates to
-- NULL, not true, when trading_date IS NULL -- so this one row is now
-- permanently excluded from the automatic resolver rather than being
-- silently (and possibly wrongly) scored against a guessed date. This is
-- the accepted tradeoff for a single pre-launch smoketest row, not a
-- meaningful loss to the ledger.
--
-- Going forward this table can't gain a second row like this: forecast_
-- engine.py (post-Phase-A) always computes trading_date explicitly for
-- every row it writes (falling back to as_of.date() for intraday_proxy
-- rows, never to NULL) -- this orphan predates that code path entirely.

alter table public.forecasts alter column trading_date drop not null;
alter table public.forecasts alter column scheduler_drift_days drop not null;

update public.forecasts
set trading_date = null, scheduler_drift_days = null
where id = '8aa357bc-2a02-4b09-b51e-ad252992e175';

create or replace function public.enforce_forecasts_trading_date_null_guard()
returns trigger as $$
begin
  if new.trading_date is null then
    if exists (
      select 1 from public.quote_snapshots qs
      where qs.id = new.quote_snapshot_id and qs.provider_ts is not null
    ) then
      raise exception
        'forecasts.trading_date may only be NULL when the linked quote_snapshot has no provider_ts (forecast id=%, quote_snapshot_id=%)',
        new.id, new.quote_snapshot_id;
    end if;
  end if;
  return new;
end;
$$ language plpgsql;

drop trigger if exists trg_forecasts_trading_date_null_guard on public.forecasts;
create trigger trg_forecasts_trading_date_null_guard
  before insert or update on public.forecasts
  for each row execute function public.enforce_forecasts_trading_date_null_guard();
