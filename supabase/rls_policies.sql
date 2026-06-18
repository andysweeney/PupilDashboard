-- rls_policies.sql — multi-tenant isolation for the Pupil Progress Dashboard.
--
-- Tenancy model: ONE Supabase project, one bucket (dashboard-data), one set of tables.
-- Schools are separated by a `school_id` SLUG that is:
--   * the first path segment of every storage object   ({school_id}/data.json, {school_id}/uploads/raw/*)
--   * a column on app_settings / admins
--   * a claim stamped into each user's app_metadata at provisioning (service-role only,
--     so users cannot edit their own school_id)
--
-- THE RULE: the subdomain only routes. Every read/write below is gated on the *verified
-- token's* school_id, so a user can only ever touch their own school. The service-role
-- key used by the GitHub Actions rebuild bypasses RLS and can touch any school — that key
-- lives ONLY as a CI secret, never in the front end.
--
-- Run this in the Supabase SQL editor. Idempotent-ish: drops policies before recreating.

-- ─────────────────────────────────────────────────────────────────────────────
-- 0. Helper: the caller's school_id, read from the verified JWT's app_metadata.
-- ─────────────────────────────────────────────────────────────────────────────
create or replace function public.current_school_id()
returns text
language sql
stable
as $$
  select nullif(auth.jwt() -> 'app_metadata' ->> 'school_id', '')
$$;

-- ─────────────────────────────────────────────────────────────────────────────
-- 1. schools — public lookup so the static shell can resolve subdomain -> id + branding.
--    Holds NO pupil data. Anon-readable; only the service role writes it.
-- ─────────────────────────────────────────────────────────────────────────────
create table if not exists public.schools (
  school_id    text primary key check (school_id ~ '^[a-z0-9][a-z0-9-]{1,62}$'),
  display_name text not null,
  branding     jsonb not null default '{}'::jsonb,
  created_at   timestamptz not null default now()
);
alter table public.schools enable row level security;

drop policy if exists schools_read_all on public.schools;
create policy schools_read_all on public.schools
  for select to anon, authenticated using (true);
-- (no insert/update/delete policy => only the service role can modify schools)

-- ─────────────────────────────────────────────────────────────────────────────
-- 2. admins — tenant-scoped. Add school_id, then gate reads to your own school.
-- ─────────────────────────────────────────────────────────────────────────────
alter table public.admins add column if not exists school_id text;
-- Backfill / enforce before making NOT NULL in production:
-- update public.admins set school_id = '<slug>' where school_id is null;
alter table public.admins enable row level security;

drop policy if exists admins_read_own_school on public.admins;
create policy admins_read_own_school on public.admins
  for select to authenticated
  using (school_id = public.current_school_id());
-- (no write policy => admin membership is managed by the service role only)

-- ─────────────────────────────────────────────────────────────────────────────
-- 3. app_settings — tenant-scoped, server-wins config (subject_map, scales, the key, …).
--    school_id joins the primary key so each school has its own row per setting id.
-- ─────────────────────────────────────────────────────────────────────────────
alter table public.app_settings add column if not exists school_id text;
-- Migrate the existing single-school rows first, e.g.:
--   update public.app_settings set school_id = '<slug>' where school_id is null;
-- Then make (school_id, id) the key:
--   alter table public.app_settings drop constraint if exists app_settings_pkey;
--   alter table public.app_settings add primary key (school_id, id);
alter table public.app_settings enable row level security;

drop policy if exists app_settings_read_own on public.app_settings;
create policy app_settings_read_own on public.app_settings
  for select to authenticated
  using (school_id = public.current_school_id());

-- Only admins of the school may write config. Reads stay open to all of the school's users
-- so everyone sees the same applied key.
drop policy if exists app_settings_write_admin on public.app_settings;
create policy app_settings_write_admin on public.app_settings
  for insert to authenticated
  with check (
    school_id = public.current_school_id()
    and exists (select 1 from public.admins a
                where a.user_id = auth.uid() and a.school_id = public.current_school_id())
  );
drop policy if exists app_settings_update_admin on public.app_settings;
create policy app_settings_update_admin on public.app_settings
  for update to authenticated
  using (school_id = public.current_school_id())
  with check (
    school_id = public.current_school_id()
    and exists (select 1 from public.admins a
                where a.user_id = auth.uid() and a.school_id = public.current_school_id())
  );

-- ─────────────────────────────────────────────────────────────────────────────
-- 4. Storage (storage.objects) — the real data-isolation wall.
--    Bucket dashboard-data, objects namespaced as {school_id}/...
--    (storage.foldername(name))[1] is the first path segment = the owning school.
-- ─────────────────────────────────────────────────────────────────────────────

-- READ: any signed-in user may read objects under THEIR OWN school's prefix
-- (this is what serves {school_id}/data.json to the dashboard).
drop policy if exists tenant_read_own on storage.objects;
create policy tenant_read_own on storage.objects
  for select to authenticated
  using (
    bucket_id = 'dashboard-data'
    and (storage.foldername(name))[1] = public.current_school_id()
  );

-- WRITE (upload raw): only ADMINS of the school, and only under {school_id}/uploads/.
-- This is the genuine gate behind the upload page — a hidden tab is not enforcement.
drop policy if exists tenant_admin_upload on storage.objects;
create policy tenant_admin_upload on storage.objects
  for insert to authenticated
  with check (
    bucket_id = 'dashboard-data'
    and (storage.foldername(name))[1] = public.current_school_id()
    and (storage.foldername(name))[2] = 'uploads'
    and exists (select 1 from public.admins a
                where a.user_id = auth.uid() and a.school_id = public.current_school_id())
  );

-- UPDATE/replace raw (the "replace-latest" lifecycle for current-AY exports): same gate.
drop policy if exists tenant_admin_update on storage.objects;
create policy tenant_admin_update on storage.objects
  for update to authenticated
  using (
    bucket_id = 'dashboard-data'
    and (storage.foldername(name))[1] = public.current_school_id()
    and (storage.foldername(name))[2] = 'uploads'
    and exists (select 1 from public.admins a
                where a.user_id = auth.uid() and a.school_id = public.current_school_id())
  );

-- DELETE raw (corrections): admins, within their own uploads only.
drop policy if exists tenant_admin_delete on storage.objects;
create policy tenant_admin_delete on storage.objects
  for delete to authenticated
  using (
    bucket_id = 'dashboard-data'
    and (storage.foldername(name))[1] = public.current_school_id()
    and (storage.foldername(name))[2] = 'uploads'
    and exists (select 1 from public.admins a
                where a.user_id = auth.uid() and a.school_id = public.current_school_id())
  );

-- NOTE: {school_id}/data.json and {school_id}/flags.json are written ONLY by the rebuild
-- job via the service-role key (which bypasses RLS). No browser write policy is granted
-- for them, so a compromised admin client still cannot forge a school's published data.
