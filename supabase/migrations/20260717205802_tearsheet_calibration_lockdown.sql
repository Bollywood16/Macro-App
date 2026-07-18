-- Fix-up for 20260717205603_tearsheet_layer.sql: the `calibration` view was
-- created without an explicit REVOKE/GRANT, so it fell through to this
-- database's pre-existing default-privilege rule (ALTER DEFAULT PRIVILEGES
-- FOR ROLE postgres ... GRANT ALL ON TABLES TO anon/authenticated, visible
-- in the 001/002 schema dump) and ended up grantable to anon/authenticated.
-- Because the view is owned by `postgres` (BYPASSRLS in Supabase), that
-- would let anon/authenticated read calibration's aggregated forecast/
-- outcome data through PostgREST, bypassing the deny-all RLS model every
-- other object in this schema enforces. Confirmed via a post-apply schema
-- dump before this fix went out.

revoke all on public.calibration from public, anon, authenticated;
grant select on public.calibration to service_role;

-- Defense in depth: make the view evaluate RLS as the invoking role rather
-- than the owner (Postgres 15+ option), so even a future stray grant can't
-- silently bypass RLS via the owner's BYPASSRLS the way this one did.
alter view public.calibration set (security_invoker = true);
