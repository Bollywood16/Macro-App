-- Market Memory M1: Schema + write path
-- Paste this into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and
-- run once. Idempotent via IF NOT EXISTS / ON CONFLICT so a repeat paste is
-- harmless.
--
-- Tables: assets, quote_snapshots, forecasts, decisions, outcomes, amendments
-- All six: RLS enabled with zero policies (deny-all to anon/authenticated),
-- and service_role is granted SELECT/INSERT only — never UPDATE or DELETE.
-- That is the immutability guarantee: no role, including the one the Edge
-- Functions connect as, can overwrite a row. Corrections go through
-- `amendments`, which is itself insert/select-only for the same reason.

create extension if not exists pgcrypto;

-- ---------------------------------------------------------------- assets

create table if not exists public.assets (
  id          uuid primary key default gen_random_uuid(),
  ticker      text not null unique,
  label       text not null,
  category    text,
  created_at  timestamptz not null default now()
);

-- ------------------------------------------------------------- quote_snapshots

create table if not exists public.quote_snapshots (
  id             uuid primary key default gen_random_uuid(),
  ticker         text not null references public.assets(ticker),
  price          numeric(18,6) not null check (price > 0),
  source         text not null check (source in ('provider','manual')),
  provider_ts    timestamptz,
  retrieved_ts   timestamptz not null default now(),
  market_status  text check (market_status in ('open','closed','pre','post','unknown')),
  is_indicative  boolean not null default false,
  created_at     timestamptz not null default now()
);

create index if not exists quote_snapshots_ticker_created_idx
  on public.quote_snapshots (ticker, created_at desc);

-- ----------------------------------------------------------------- forecasts

create table if not exists public.forecasts (
  id                 uuid primary key default gen_random_uuid(),
  ticker             text not null references public.assets(ticker),
  as_of_ts           timestamptz not null,
  effective_price    numeric(18,6) not null check (effective_price > 0),
  quote_snapshot_id  uuid not null references public.quote_snapshots(id),
  horizon_days       int not null check (horizon_days > 0),
  benchmark          text not null default 'SPY',
  p_positive         numeric(8,6) check (p_positive between 0 and 1),
  p_beat_benchmark   numeric(8,6) check (p_beat_benchmark between 0 and 1),
  q20                numeric(12,8),
  q50                numeric(12,8),
  q80                numeric(12,8),
  expected_mae       numeric(12,8),
  n_independent      int,
  confidence_score   numeric(8,6),
  confidence_label   text,
  model_version      text not null,
  features_json      jsonb,
  evidence_json      jsonb,
  created_at         timestamptz not null default now()
);

create index if not exists forecasts_ticker_asof_idx
  on public.forecasts (ticker, as_of_ts desc);

-- ----------------------------------------------------------------- decisions

create table if not exists public.decisions (
  id               uuid primary key default gen_random_uuid(),
  forecast_id      uuid not null references public.forecasts(id),
  action           text not null check (action in
                     ('buy','sell','hold','watch','stand_down')),
  exec_price       numeric(18,6),
  units            numeric,
  notional         numeric(18,2),
  pct_portfolio    numeric(9,6),
  horizon_days     int,
  user_confidence  int check (user_confidence between 0 and 100),
  thesis           text,
  invalidation     text,
  agree_reason     text,
  outside_info     boolean not null default false,
  created_at       timestamptz not null default now()
);

create index if not exists decisions_forecast_idx
  on public.decisions (forecast_id);

-- ------------------------------------------------------------------ outcomes

create table if not exists public.outcomes (
  id                 uuid primary key default gen_random_uuid(),
  forecast_id        uuid not null references public.forecasts(id),
  evaluated_at       timestamptz not null,
  end_price          numeric(18,6),
  benchmark_return   numeric,
  abs_return         numeric,
  excess_return      numeric,
  max_adverse_exc    numeric,
  max_favorable_exc  numeric,
  event_occurred     boolean,
  interval_covered   boolean,
  brier              numeric,
  log_loss           numeric,
  notes              text,
  created_at         timestamptz not null default now()
);

create index if not exists outcomes_forecast_idx
  on public.outcomes (forecast_id);

-- ---------------------------------------------------------------- amendments

create table if not exists public.amendments (
  id            uuid primary key default gen_random_uuid(),
  parent_table  text not null check (parent_table in
                   ('quote_snapshots','forecasts','decisions','outcomes')),
  parent_id     uuid not null,
  payload_json  jsonb not null,
  reason        text,
  created_at    timestamptz not null default now()
);

create index if not exists amendments_parent_idx
  on public.amendments (parent_table, parent_id);

-- --------------------------------------------------------- RLS + grants (deny-all)

do $$
declare
  t text;
begin
  for t in select unnest(array[
    'assets','quote_snapshots','forecasts','decisions','outcomes','amendments'
  ]) loop
    execute format('alter table public.%I enable row level security;', t);
    execute format('revoke all on public.%I from public, anon, authenticated;', t);
    execute format('grant select, insert on public.%I to service_role;', t);
  end loop;
end $$;

-- No policies are created for anon/authenticated: this app has no direct
-- client access to Postgres, only through passphrase-gated Edge Functions
-- running as service_role.

-- ------------------------------------------------------------------- seed

insert into public.assets (ticker, label, category) values
  ('SMH',  'Semiconductors (SMH)', 'sector_thematic'),
  ('^SOX', 'SOX Index',            'sector_thematic'),
  ('SPY',  'S&P 500 (SPY)',        'broad_market'),
  ('QQQ',  'Nasdaq 100 (QQQ)',     'broad_market'),
  ('GLD',  'Gold (GLD)',           'commodity'),
  ('XLK',  'Technology',           'sector'),
  ('XLF',  'Financials',           'sector'),
  ('XLV',  'Health Care',          'sector'),
  ('XLE',  'Energy',               'sector'),
  ('XLI',  'Industrials',          'sector'),
  ('XLY',  'Cons. Discretionary',  'sector'),
  ('XLP',  'Cons. Staples',        'sector'),
  ('XLU',  'Utilities',            'sector'),
  ('XLB',  'Materials',            'sector'),
  ('XLRE', 'Real Estate',          'sector'),
  ('XLC',  'Communications',       'sector'),
  ('RSP',  'Equal-weight S&P',     'size_style'),
  ('IWM',  'Small caps',           'size_style'),
  ('MGK',  'Mega-cap growth',      'size_style')
on conflict (ticker) do nothing;
