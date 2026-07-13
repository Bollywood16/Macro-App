-- Market Memory M5: model registry
-- Paste this into the Supabase SQL editor (project anzbpxqvibgpxnwgyqoc) and
-- run once, after 001_market_memory_schema.sql. Idempotent via IF NOT EXISTS,
-- so a repeat paste is harmless.
--
-- Spec 9.4/9.5's champion-challenger lifecycle is manual/offline in v1 —
-- this table is a plain append-only ledger to record it against (create a
-- candidate, mark it shadow, later mark a promotion), not an automated
-- shadow-mode evaluation pipeline. Same immutability discipline as every
-- other Market Memory table: no UPDATE/DELETE grant, so a status change is
-- a new row, not an edit to an old one.

create extension if not exists pgcrypto;

create table if not exists public.model_registry (
  id            uuid primary key default gen_random_uuid(),
  model_version text not null,
  status        text not null check (status in
                   ('candidate','shadow','champion','retired')),
  approver      text,
  reason        text,
  metrics_json  jsonb,
  commit_sha    text,
  created_at    timestamptz not null default now()
);

create index if not exists model_registry_version_idx
  on public.model_registry (model_version, created_at desc);

alter table public.model_registry enable row level security;
revoke all on public.model_registry from public, anon, authenticated;
grant select, insert on public.model_registry to service_role;

-- No policies for anon/authenticated: same passphrase-gated-Edge-Function-
-- only access pattern as every other table in this schema.
