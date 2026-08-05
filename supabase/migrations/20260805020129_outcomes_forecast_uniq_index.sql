-- B2: document outcomes_forecast_uniq, which exists live but had no
-- migration file recording it -- it was applied ad-hoc at some point
-- before this session, in violation of the standing rule that schema
-- changes go in supabase/migrations/ as versioned files. This migration
-- doesn't change anything (the index already exists); it exists so the
-- schema history is complete and reproducible from a fresh database.
--
-- This is also the constraint that makes the B3/B6 fixes' correctness
-- externally verifiable: without it, list_pending_outcomes duplicate-
-- writing the same forecast_id twice would succeed silently.
--
-- IF NOT EXISTS makes this idempotent -- safe whether the index already
-- exists (the live/current state) or not (a fresh database applying every
-- migration from scratch).

create unique index if not exists outcomes_forecast_uniq
  on public.outcomes using btree (forecast_id);
