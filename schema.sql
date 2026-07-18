-- ============================================================
-- Market Memory — data layer (Supabase / Postgres)
-- Three linked tables + a calibration view.
--   1. episodes        : the growing library (handoff research written back)
--   2. decisions       : immutable log — model-said + user-decided
--   3. outcomes        : what actually happened, scores each decision
-- ============================================================

-- 1. EPISODE LIBRARY -----------------------------------------
-- Statistical fingerprint (from Python) + causal narrative (from handoff).
-- Re-usable: once a date is annotated, no re-research needed.
create table if not exists episodes (
  id             uuid primary key default gen_random_uuid(),
  asset          text not null,               -- e.g. 'SMH'
  event_date     date not null,
  drawdown_pct   numeric,                      -- statistical fingerprint
  extension_pct  numeric,
  fwd_126d_pct   numeric,
  days_to_trough int,
  cause          text,                         -- from handoff research
  what_ended_it  text,                         -- from handoff research
  source         text,                         -- 'python' | 'handoff' | 'manual'
  created_at     timestamptz default now(),
  unique (asset, event_date)
);

-- 2. DECISION LOG (immutable) --------------------------------
-- Written at decision time, BEFORE the outcome exists.
-- Captures what the MODEL said and what the USER decided, side by side.
create table if not exists decisions (
  id                uuid primary key default gen_random_uuid(),
  asset             text not null,
  decided_at        timestamptz not null default now(),
  -- what the model said:
  model_verdict     text not null,             -- 'BUY' | 'WAIT' | 'AVOID'
  model_confidence  int,                        -- 0-100
  forecast_json     jsonb not null,             -- full multi-horizon forecast snapshot
  regime_json       jsonb,                      -- regime read at the time
  informed_by       uuid[] default '{}',        -- episode ids that informed it
  -- what the user decided:
  user_action       text,                       -- 'bought' | 'waited' | 'passed' | 'trimmed'
  user_size         text,                        -- free text / tranche
  user_rationale    text,
  invalidation      text,                        -- the pre-committed stop/exit
  created_at        timestamptz default now()
);
-- immutability: allow insert, forbid update/delete via RLS (below).

-- 3. OUTCOMES ------------------------------------------------
-- Filled in later when the forecast horizon matures. Scores the decision.
create table if not exists outcomes (
  id             uuid primary key default gen_random_uuid(),
  decision_id    uuid not null references decisions(id),
  horizon        text not null,                 -- '5d' | '1mo' | '3mo' ...
  matured_at     timestamptz,
  realized_pct   numeric,                        -- actual forward return
  forecast_pct   numeric,                        -- what the model predicted
  hit            boolean,                        -- did direction match?
  process_grade  text,                           -- 'A'..'F' — graded on rule-adherence, NOT P&L
  notes          text,
  created_at     timestamptz default now(),
  unique (decision_id, horizon)
);

-- CALIBRATION VIEW -------------------------------------------
-- The "learning" is here: are moderate-confidence calls right ~their stated %?
-- Slow, outcome-based recalibration — NOT a fast self-retraining predictor.
create or replace view calibration as
select
  d.model_verdict,
  width_bucket(d.model_confidence, 0, 100, 5) as conf_bucket,
  o.horizon,
  count(*)                                   as n,
  avg(case when o.hit then 1 else 0 end)     as realized_hit_rate,
  avg(d.model_confidence) / 100.0            as stated_confidence
from decisions d
join outcomes o on o.decision_id = d.id
where o.matured_at is not null
group by d.model_verdict, conf_bucket, o.horizon;

-- ROW-LEVEL SECURITY -----------------------------------------
alter table decisions enable row level security;
alter table outcomes  enable row level security;
alter table episodes  enable row level security;

-- decisions: insert + read only for the owner; NO update/delete (immutable log)
create policy decisions_insert on decisions for insert with check (true);
create policy decisions_select on decisions for select using (true);
-- (deliberately no update/delete policy => those operations are denied)

-- outcomes: insert/select/update allowed (outcomes mature over time)
create policy outcomes_all on outcomes for all using (true) with check (true);

-- episodes: insert/select/update (library grows + gets annotated)
create policy episodes_all on episodes for all using (true) with check (true);
