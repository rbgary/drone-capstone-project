-- ── pressure-gauge photo batches ─────────────────────────────────────────
--
-- A user uploads a batch of gauge photos on the web. Each photo is stored,
-- read by the pipeline (backend/agent/dronedeck/pipeline/gauge/), and its
-- pressure reading comes back to the page and stays in the database.
--
-- The pipeline runs on an agent (sync/gauge_batches.py), not here: it is
-- OpenCV, which Edge Functions cannot run. The agent polls these tables the
-- way it polls the mission queue — the browser and the agent never talk to
-- each other, only to Supabase.
--
--   gauge_batches   one row per upload: its status and counters
--   gauge_images    one row per photo: the original, BOTH gauges' readings,
--                   the one annotated picture
--   gauge-photos    private bucket:
--                     <batch_id>/original/<image_id>.<ext>   (the web uploads)
--                     <batch_id>/annotated/<image_id>.png    (the agent uploads)
--
-- THE WEB'S STEPS, in order (docs/features/pipeline/gauge.txt):
--   1. insert a gauge_batches row                      status 'uploading'
--   2. per photo: upload the file, then insert its gauge_images row
--   3. update the batch: status 'queued', image_count = N   <- releases it
-- The agent only touches a batch once it is 'queued', so it never reads a
-- half-uploaded one.
--
-- Everyone signed in reads; operators write. anon: nothing. Explicit grants —
-- "a policy without a grant is a 403" (docs/features/supabase/rls-and-grants.txt).

create table if not exists public.gauge_batches (
  id           uuid primary key default gen_random_uuid(),
  created_by   uuid default auth.uid() references public.profiles (id) on delete set null,
  created_at   timestamptz not null default now(),
  name         text,
  -- uploading → queued (the web) → processing → done (the agent)
  status       text not null default 'uploading'
               check (status in ('uploading', 'queued', 'processing', 'done')),
  image_count  integer not null default 0 check (image_count >= 0),
  done_count   integer not null default 0 check (done_count >= 0),
  failed_count integer not null default 0 check (failed_count >= 0),
  finished_at  timestamptz
);

create index if not exists gauge_batches_status_idx on public.gauge_batches (status, created_at);
create index if not exists gauge_batches_owner_idx on public.gauge_batches (created_by, created_at desc);

create table if not exists public.gauge_images (
  id               uuid primary key default gen_random_uuid(),
  batch_id         uuid not null references public.gauge_batches (id) on delete cascade,
  filename         text not null,                      -- as the user named it
  original_path    text not null,                      -- in gauge-photos
  annotated_path   text,                               -- in gauge-photos, once read
  created_at       timestamptz not null default now(),
  -- pending → processing (claimed by an agent) → done | failed
  status           text not null default 'pending'
                   check (status in ('pending', 'processing', 'done', 'failed')),
  claimed_at       timestamptz,
  processed_at     timestamptz,
  -- Each photo shows TWO gauges and is read for both, drawn on ONE picture.
  -- state 'unreadable' = the photo was opened but that gauge was not read (see
  -- error); status 'failed' = the file could not be processed at all.
  --
  -- the PRESSURE gauge, 0-600 kPa: ok up to 200, warning above 200 and below 400,
  -- danger 400-600
  kpa              double precision,
  raw_kpa          double precision,                   -- before the 0-600 range check
  state            text check (state in ('ok', 'warning', 'danger', 'unreadable')),
  error            text,
  reader           text check (reader in ('sensor', 'color')),
  dial_diameter_px integer,
  -- the SYSTEM STATUS gauge, 0-100: ok up to 30, warning above 30 and below 70,
  -- danger 70-100
  system_value     double precision,
  system_raw_value double precision,                   -- before the 0-100 range check
  system_state     text check (system_state in ('ok', 'warning', 'danger', 'unreadable')),
  system_error     text,
  system_reader    text check (system_reader in ('sensor', 'color')),
  system_dial_diameter_px integer,
  width_px         integer,
  height_px        integer,
  -- each gauge's circle, needle tip and tail, in photo px:
  -- {"pressure": {circle, tip, tail}, "system": {circle, tip, tail}}
  detail           jsonb not null default '{}'::jsonb
);

create index if not exists gauge_images_batch_idx on public.gauge_images (batch_id, created_at);
create index if not exists gauge_images_pending_idx
  on public.gauge_images (created_at) where status in ('pending', 'processing');

alter table public.gauge_batches enable row level security;
alter table public.gauge_images enable row level security;

drop policy if exists "signed-in read gauge batches" on public.gauge_batches;
create policy "signed-in read gauge batches"
  on public.gauge_batches for select to authenticated using (true);
drop policy if exists "operators create gauge batches" on public.gauge_batches;
create policy "operators create gauge batches"
  on public.gauge_batches for insert to authenticated with check (public.is_operator());
drop policy if exists "operators update gauge batches" on public.gauge_batches;
create policy "operators update gauge batches"
  on public.gauge_batches for update to authenticated
  using (public.is_operator()) with check (public.is_operator());
drop policy if exists "operators delete gauge batches" on public.gauge_batches;
create policy "operators delete gauge batches"
  on public.gauge_batches for delete to authenticated using (public.is_operator());

drop policy if exists "signed-in read gauge images" on public.gauge_images;
create policy "signed-in read gauge images"
  on public.gauge_images for select to authenticated using (true);
drop policy if exists "operators create gauge images" on public.gauge_images;
create policy "operators create gauge images"
  on public.gauge_images for insert to authenticated with check (public.is_operator());
drop policy if exists "operators update gauge images" on public.gauge_images;
create policy "operators update gauge images"
  on public.gauge_images for update to authenticated
  using (public.is_operator()) with check (public.is_operator());
drop policy if exists "operators delete gauge images" on public.gauge_images;
create policy "operators delete gauge images"
  on public.gauge_images for delete to authenticated using (public.is_operator());

grant select, insert, update, delete on public.gauge_batches to authenticated;
grant select, insert, update, delete on public.gauge_images to authenticated;
revoke all on public.gauge_batches from anon;
revoke all on public.gauge_images from anon;

-- ── the photos ───────────────────────────────────────────────────────────
-- Private, like flight-frames: a photo is operator data. The web reads them
-- back through signed URLs. Only image types the reader can open.

insert into storage.buckets (id, name, public, file_size_limit, allowed_mime_types)
values ('gauge-photos', 'gauge-photos', false, 26214400,
        array['image/png', 'image/jpeg', 'image/heic', 'image/heif'])
on conflict (id) do update
  set public = excluded.public,
      file_size_limit = excluded.file_size_limit,
      allowed_mime_types = excluded.allowed_mime_types;

drop policy if exists "operators upload gauge photos" on storage.objects;
create policy "operators upload gauge photos"
  on storage.objects for insert to authenticated
  with check (bucket_id = 'gauge-photos' and public.is_operator());

drop policy if exists "signed-in read gauge photos" on storage.objects;
create policy "signed-in read gauge photos"
  on storage.objects for select to authenticated
  using (bucket_id = 'gauge-photos');

-- The agent replaces an annotated picture when a batch is read again.
drop policy if exists "operators replace gauge photos" on storage.objects;
create policy "operators replace gauge photos"
  on storage.objects for update to authenticated
  using (bucket_id = 'gauge-photos' and public.is_operator())
  with check (bucket_id = 'gauge-photos' and public.is_operator());

drop policy if exists "operators delete gauge photos" on storage.objects;
create policy "operators delete gauge photos"
  on storage.objects for delete to authenticated
  using (bucket_id = 'gauge-photos' and public.is_operator());

-- ── realtime ─────────────────────────────────────────────────────────────
-- The page watches its batch fill in, photo by photo. Row-level security
-- decides what each socket receives.
do $$
declare t text;
begin
  foreach t in array array['gauge_batches', 'gauge_images'] loop
    if not exists (select 1 from pg_publication_tables
                   where pubname = 'supabase_realtime' and schemaname = 'public'
                     and tablename = t) then
      execute format('alter publication supabase_realtime add table public.%I', t);
    end if;
  end loop;
end;
$$;
