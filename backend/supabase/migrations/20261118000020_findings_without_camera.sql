-- ── pipeline_findings without the camera ──────────────────────────────────
--
-- The AI deck is off the drone (2026-11-15, docs/features/architecture.txt,
-- DECISIONS): a finding no longer has frames, so nothing can say whether an
-- image backs it. Until now the agent sent every finding with neutral values
-- — evidence_frames '{}', image_support 'cannot_tell', image_note '' — only
-- because 20261009000016 made image_support NOT NULL with a check.
--
-- This makes image_support optional and drops its check, so the agent stops
-- sending all three (evidence_frames and image_note already have defaults).
-- The web no longer reads any of them.
--
-- The COLUMNS STAY for now: an agent from before this change still sends them
-- and must keep working. They are dropped in a later migration, once every
-- laptop runs an agent that no longer sends them. Migrations are only added
-- to — 20261009000016 is never edited.
--
-- APPLY THIS BEFORE (or with) the agent that stops sending image_support: an
-- older database refuses a finding without it, and the results wait in the
-- laptop's outbox until this is applied — nothing is lost, nothing reaches
-- the web either.

alter table public.pipeline_findings alter column image_support drop not null;
alter table public.pipeline_findings drop constraint if exists pipeline_findings_image_support_check;

comment on column public.pipeline_findings.evidence_frames is
  'Retired 2026-11-15 (no camera). Always empty for new findings; to be dropped.';
comment on column public.pipeline_findings.image_support is
  'Retired 2026-11-15 (no camera). Null for new findings; to be dropped.';
comment on column public.pipeline_findings.image_note is
  'Retired 2026-11-15 (no camera). Empty for new findings; to be dropped.';
