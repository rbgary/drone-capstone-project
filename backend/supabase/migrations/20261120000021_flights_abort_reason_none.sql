-- This file was created on 11/20/2026 with the assistance of Claude Code in VS Code
-- (Claude Opus 5.5, Anthropic, 2026). It clears the abort reason "none" that completed
-- hover tests were saved with, and turns any later "none" into null as it arrives.

-- ── flights.abort_reason: "none" is not a reason ───────────────────────────
--
-- Until 2026-11-20 the agent saved a COMPLETED hover test with
-- abort_reason = 'none' — the text of the guard's Reason.NONE, which means
-- "no reason". Every reader takes a non-empty abort_reason as an abort, so
-- completed hover tests showed as "Aborted: none" on the desktop's Sessions
-- page and as "· none" on the web's session page. The agent stopped writing
-- it on 2026-11-20 (session.py _do_program); this cleans what is stored.
--
-- 'none' is never a legitimate abort reason (safety/flight_guard.py Reason:
-- NONE = "none" is the absence of one), so clearing it loses nothing.
--
-- TWO PARTS:
--   1. Every stored 'none' becomes null, once.
--   2. A trigger turns 'none' into null on every insert and update from now
--      on. An agent from before 2026-11-20 — on a laptop not yet updated, or
--      a flight still waiting in a laptop's outbox — keeps sending 'none'.
--      It is CORRECTED, not refused: a check constraint would make that
--      upload fail and retry forever, and the flight would never arrive.
--
-- The laptop's own copies (sessions/<id>/meta.json) are corrected when the
-- agent reads them (history.py FlightSummary), not rewritten.
--
-- VERIFY after applying (both should return 0, and the trigger 1 row):
--   select count(*) from public.flights where abort_reason = 'none';
--   select tgname from pg_trigger where tgname = 'flights_abort_reason_none';

create or replace function public.clear_abort_reason_none()
returns trigger
language plpgsql
set search_path = public
as $$
begin
  if new.abort_reason = 'none' then
    new.abort_reason := null;
  end if;
  return new;
end;
$$;

-- Safe to run twice (pasted into the SQL Editor, then pushed): the function is
-- replaced, the trigger dropped and made again, the update finds nothing.
drop trigger if exists flights_abort_reason_none on public.flights;
create trigger flights_abort_reason_none
  before insert or update on public.flights
  for each row execute function public.clear_abort_reason_none();

update public.flights set abort_reason = null where abort_reason = 'none';

comment on column public.flights.abort_reason is
  'Why the flight ended early; null when it did not. Never ''none'' — the '
  'flights_abort_reason_none trigger clears it (20261120000021).';
