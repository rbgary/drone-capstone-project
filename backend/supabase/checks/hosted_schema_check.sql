-- Does a database already hold what migrations 0001–0019 make? READ-ONLY.
--
-- Written 2026-11-18 for the hosted project tmswmgwomoabvftdreeb: the move
-- from the old project brought the schema across but not the migration
-- history (`supabase migration list` showed every migration local only), so
-- `db push` would have run all of them again. Before marking 0001–0019 as
-- applied with `supabase migration repair`, this proves they are.
--
-- Paste into the Supabase dashboard's SQL Editor and run. Every row should
-- read present = true; anything false sorts to the top and must be looked at
-- before the repair. The last row counts the public tables WITHOUT row-level
-- security, which must be 0 (CLAUDE.md: RLS on every table).

with expected(kind, name, made_by) as (values
  ('table', 'profiles', '0001'), ('table', 'drones', '0001'), ('table', 'zones', '0001'),
  ('table', 'missions', '0001'), ('table', 'flights', '0001'), ('table', 'telemetry', '0001'),
  ('table', 'predictions', '0001'),
  ('table', 'audit_events', '0006'), ('table', 'sessions', '0006'),
  ('table', 'projects', '0007'), ('table', 'project_members', '0007'), ('table', 'team_members', '0007'),
  ('table', 'gallery_albums', '0008'), ('table', 'gallery_items', '0008'),
  ('table', 'site_pages', '0009'),
  ('table', 'session_samples', '0013'),
  ('table', 'pipeline_results', '0016'), ('table', 'pipeline_findings', '0016'),
  ('table', 'notifications', '0017'),
  ('table', 'pipeline_session_results', '0018'),
  ('table', 'flight_plans', '0019'), ('table', 'flight_traces', '0019'),
  ('column', 'drones.hardware_id', '0006'), ('column', 'flights.outcome', '0006'),
  ('column', 'telemetry.roll_deg', '0006'),
  ('column', 'team_members.hobbies', '0010'),
  ('column', 'telemetry.point_id', '0012'),
  ('column', 'sessions.operator_email', '0015'), ('column', 'flights.operator_email', '0015'),
  ('column', 'pipeline_findings.scope', '0018'),
  ('function', 'is_operator', '0002'), ('function', 'handle_new_user', '0001'),
  ('function', 'claim_next_mission', '0002'), ('function', 'release_stale_claims', '0002'),
  ('function', 'claim_mission', '0006'),
  ('function', 'stamp_session_owner', '0015'), ('function', 'stamp_flight_owner', '0015'),
  ('function', 'notify_operators_of_finding', '0017'),
  ('function', 'delete_session_findings', '0018'),
  ('trigger', 'on_auth_user_created', '0001'),
  ('trigger', 'sessions_stamp_owner', '0015'), ('trigger', 'flights_stamp_owner', '0015'),
  ('trigger', 'pipeline_findings_notify', '0017'),
  ('trigger', 'sessions_delete_session_findings', '0018'),
  ('bucket', 'installers', '0005'), ('bucket', 'portfolio-images', '0007'),
  ('bucket', 'flight-frames', '0011'), ('bucket', 'flight-logs', '0011')
)
select e.kind, e.name, e.made_by,
  case e.kind
    when 'table' then exists (select 1 from information_schema.tables
                              where table_schema = 'public' and table_name = e.name)
    when 'column' then exists (select 1 from information_schema.columns
                               where table_schema = 'public'
                                 and table_name = split_part(e.name, '.', 1)
                                 and column_name = split_part(e.name, '.', 2))
    when 'function' then exists (select 1 from pg_proc p join pg_namespace n on n.oid = p.pronamespace
                                 where n.nspname = 'public' and p.proname = e.name)
    when 'trigger' then exists (select 1 from pg_trigger where tgname = e.name and not tgisinternal)
    when 'bucket' then exists (select 1 from storage.buckets where id = e.name)
  end as present
from expected e
union all
select 'rls', 'public tables without row-level security: ' ||
       (select count(*) from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity)::text,
       'all',
       (select count(*) = 0 from pg_class c join pg_namespace n on n.oid = c.relnamespace
        where n.nspname = 'public' and c.relkind = 'r' and not c.relrowsecurity)
order by present, kind, name;
