# Docs router

One line per doc. Read this first, then open only what your work touches.
Feature docs are plain `.txt`, grouped by the surface they describe.

## Start here — the whole system

| Doc | What it covers |
|---|---|
| [features/architecture-at-a-glance.txt](features/architecture-at-a-glance.txt) | **The short version**: components, tech, connections, the numbers. One screen |
| [features/architecture.txt](features/architecture.txt) | The long version: the flows end to end, the layers, the decisions, and which arrows are designed but not wired |

## Backend — the flight agent (`backend/agent/`)

| Doc | What it covers |
|---|---|
| [features/backend/flight-status.txt](features/backend/flight-status.txt) | **Where flight actually is**: every symptom diagnosed, what is proven, what has never flown |
| [features/backend/flight-safety.txt](features/backend/flight-safety.txt) | The pre-flight checks and the in-flight guards. **Read this one first** |
| [features/backend/telemetry-stream.txt](features/backend/telemetry-stream.txt) | The single 10 Hz subscription every reader shares |
| [features/backend/sessions-and-modes.txt](features/backend/sessions-and-modes.txt) | The session state machine, Auto and Manual, ending an operation |
| [features/backend/manual-control.txt](features/backend/manual-control.txt) | Assisted manual flight from held keys, the 50 Hz loop, holding the spot |
| [features/backend/agent-api.txt](features/backend/agent-api.txt) | The local HTTP/WebSocket surface, the control token, CORS |
| [features/backend/offline-sync.txt](features/backend/offline-sync.txt) | The outbox, CSV-first recording, idempotent upload |
| [features/backend/audit-trail.txt](features/backend/audit-trail.txt) | Every action and safety decision, against a person and a drone |
| [features/backend/flight-tuning.txt](features/backend/flight-tuning.txt) | Why the drone bounced and spun, the tuning applied per flight, the 10 Hz trace |
| [features/backend/session-history.txt](features/backend/session-history.txt) | Every session kept on the laptop; Auto/Manual filtered per reading |
| [features/backend/running-from-source.txt](features/backend/running-from-source.txt) | Running the agent from a terminal, for developers — not something operators set up |

## Desktop — the Tauri app (`desktop/`)

| Doc | What it covers |
|---|---|
| [features/desktop/desktop-app.txt](features/desktop/desktop-app.txt) | The shell, the bundled agent sidecar, installers and CI |
| [features/desktop/camera.txt](features/desktop/camera.txt) | The camera path — test pattern and the AI deck's Wi-Fi stream, the window's one feed, the session's recording, how fast it really is |
| [features/desktop/camera-firmware.txt](features/desktop/camera-firmware.txt) | The AI deck's two chips: the wire format, why the feed is slow and stalls, the ESP32 throughput patch and the JPEG streamer (built, lab pending) |
| [features/desktop/capture.txt](features/desktop/capture.txt) | Capture (its own sidebar page): pictures by hand in a session, the auto-capture switch, and which frames count as captured |
| [features/desktop/setup.txt](features/desktop/setup.txt) | The Set up page: install the camera software on a new drone, then Wi-Fi, then a session |
| [features/desktop/drone-wifi.txt](features/desktop/drone-wifi.txt) | The drone's camera joins the operator's Wi-Fi, set over the radio: the firmware addition, the flasher, the dialog |
| [features/desktop/pages-and-windows.txt](features/desktop/pages-and-windows.txt) | The sidebar shell, Control's two columns and its console, the five live sensor windows, and how the layout is measured |
| [features/desktop/manual-room-map.txt](features/desktop/manual-room-map.txt) | Manual's room drawn from the two base stations, the drone in it, F/B/L/R where the keys move it, stations heard |
| [features/desktop/auto-control.txt](features/desktop/auto-control.txt) | Control in Auto: ① Mission (plan, edit, pick) → ② Check → ③ Fly, the monitor on the right |
| [features/desktop/mission-playback.txt](features/desktop/mission-playback.txt) | Playback (its own sidebar page) — every flight, then Play back: anyone's recorded flight watched again on the room map (2-D/3-D, full screen) with its readings, events and report — this laptop's files first, Supabase for the rest; recorded, never simulated |

## Missions — autonomous flight (`backend/agent/dronedeck/mission/`)

| Doc | What it covers |
|---|---|
| [features/missions/README.txt](features/missions/README.txt) | The index: what to fly, and how it is flown |
| [features/missions/how-autonomous-flight-works.txt](features/missions/how-autonomous-flight-works.txt) | **The whole autonomous system in one page**: light → position → green → plan → plan that will fly → Start → flight |
| [features/missions/floor-plans.txt](features/missions/floor-plans.txt) | Rooms (closed geofence, obstacles, the room's map), missions, inspection points, the agent's checks, saving |
| [features/missions/mission-controller.txt](features/missions/mission-controller.txt) | How a mission flies: fly_to in the manual system, run_mission, point_id stamping; the controller body is Hannah's, its proof (simulated drone, mission report, lab flights) Yordi's |

## Pipeline — the data pipeline (`backend/agent/dronedeck/pipeline/`)

| Doc | What it covers |
|---|---|
| [features/pipeline/data-pipeline.txt](features/pipeline/data-pipeline.txt) | **The index**: `dronedeck process` — clean → enhance → classify → interpret once over the flight, laptop-first, resumed when the app restarts; the DPP switch (top of Control, on by default); `process --all` |
| [features/pipeline/clean.txt](features/pipeline/clean.txt) | robust@1: sensor faults flagged, never edited — scored against hampel@1 on 66 real flights |
| [features/pipeline/enhance.txt](features/pipeline/enhance.txt) | clahe@1: clearer copies of dark frames, and each frame's quality |
| [features/pipeline/classify.txt](features/pipeline/classify.txt) | blocks@1: stretches where temperature or pressure departs from what was expected |
| [features/pipeline/interpret.txt](features/pipeline/interpret.txt) | findings@1: each stretch judged and put into words, a verdict per point |
| [features/pipeline/scene.txt](features/pipeline/scene.txt) | scene@1: whether the camera's view changed during a finding — supports it, never contradicts |
| [features/pipeline/session.txt](features/pipeline/session.txt) | The session itself processed — its 1 Hz samples around the flights, judged on the ground (ground@1), Auto and Manual |
| [features/pipeline/live.txt](features/pipeline/live.txt) | Live verdicts (story 4.9): each inspection point judged as its hold ends, shown in Progress |
| [features/pipeline/display-chain.txt](features/pipeline/display-chain.txt) | The display chain: captured frames upscaled (Real-ESRGAN, then CLAHE) after the pipeline, in chunks, never in flight — for showing, never evidence; `dronedeck upscale` |
| [features/pipeline/calibrate.txt](features/pipeline/calibrate.txt) | `dronedeck calibrate`: the hand-warmer flights measured, thresholds recommended — the lab procedure |
| [features/pipeline/results.txt](features/pipeline/results.txt) | Results on the web: the Processed data page (`/app/processed`), anomalies highlighted in the readings and on the frames with their meaning, the session and flight pages, notified until read |

## Frontend — the web app (`web/`)

| Doc | What it covers |
|---|---|
| [features/frontend/operator-dashboard.txt](features/frontend/operator-dashboard.txt) | The `/app` pages: live, flights, zones, compare, settings |
| [features/frontend/mission-planner.txt](features/frontend/mission-planner.txt) | Building, validating, queuing and cancelling missions |
| [features/frontend/portfolio.txt](features/frontend/portfolio.txt) | Public projects and team pages, and the /admin that edits them |
| [features/frontend/gallery.txt](features/frontend/gallery.txt) | Albums of photos and videos, and the admin that manages them |
| [features/frontend/site-pages-and-views.txt](features/frontend/site-pages-and-views.txt) | Editable wording for every page; visitor vs. operator view, Dashboard |

## Supabase — the database (`backend/supabase/`)

| Doc | What it covers |
|---|---|
| [features/supabase/data-model.txt](features/supabase/data-model.txt) | The tables and what ties them together |
| [features/supabase/rls-and-grants.txt](features/supabase/rls-and-grants.txt) | Authorization, and the 15-check proof that it holds |
| [features/supabase/accounts-and-roles.txt](features/supabase/accounts-and-roles.txt) | One account for web and desktop; creating an operator |
| [features/supabase/storage-and-backfill.txt](features/supabase/storage-and-backfill.txt) | The two buckets and the CSV backfill |

## Development — how work here is done

| Doc | What it covers |
|---|---|
| [features/development/skills.txt](features/development/skills.txt) | The `/feature`, `/copy-adapt`, `/preflight` and `/adapt` commands and the feature-kit, committed in `.claude/` so a cloud session has them |
| [features/development/releases.txt](features/development/releases.txt) | How a merge to main becomes a download on /apps, on both platforms |
| [features/development/commands.txt](features/development/commands.txt) | Every shell command in the project — agent CLI, web, desktop, scripts, Supabase, gates, hardware — where to run it and what it does |

## The project's working memory — read before starting

Committed so a Claude Code cloud session starts where a local one stopped.

| Doc | What it covers |
|---|---|
| [PROJECT_PROFILE.txt](PROJECT_PROFILE.txt) | The repo, its surfaces, gates, conventions and ship rules |
| [GRAPH_STATE.txt](GRAPH_STATE.txt) | Which docs can be trusted right now — `python3 docs/graph_check.py` regenerates the findings |
| [design/PREFERENCES.txt](design/PREFERENCES.txt) | What the owner asked for more than once, and the rules that became standing |
| [design/components.txt](design/components.txt) | The shared building blocks to reach for first (not filled in yet) |
| [design/layout.txt](design/layout.txt) | **Standing rule**: every screen at 720–1600 px (desktop) / 390–1440 px (web), both themes, laid out by container width |

## The lab

[flight-log.txt](flight-log.txt) — what was flown, what it measured, and what changed
because of it. Newest session first, plus the test ladder every session follows. The one
doc here that is deliberately a journal: flight work cannot be re-run, so the readings
are the only thing that survives a session.

## Plans

[plans/2026-09-16-sessions-flight-portfolio.txt](plans/2026-09-16-sessions-flight-portfolio.txt) — the 70-stage plan for sessions, barometer flight, the desktop redesign and the portfolio.

[plans/2026-09-22-desktop-ui-redesign.txt](plans/2026-09-22-desktop-ui-redesign.txt) — the 100-stage plan for the desktop sidebar shell, the two-column Control page and its three console tabs. What shipped is in [features/desktop/pages-and-windows.txt](features/desktop/pages-and-windows.txt).

[plans/2026-09-25-any-machine-desktop.txt](plans/2026-09-25-any-machine-desktop.txt) — the 50-stage plan that made the desktop app build, run and explain its failures on any Mac or Windows PC: one setup command, one build path, the Windows radio driver, the Intel installer.

[plans/2026-09-25-linux.txt](plans/2026-09-25-linux.txt) — the 40-stage plan that brought the terminal install, a real sign-in check and the radio's USB permission to Linux.

[plans/2026-09-28-missions-and-pipeline.txt](plans/2026-09-28-missions-and-pipeline.txt) — the 100-stage plan for autonomous missions (floor plans, the mission controller through the manual system), the data pipeline, and the Control page remodelled for Auto. What shipped is in features/missions/, features/pipeline/ and features/desktop/auto-control.txt.

[plans/2026-10-09-data-pipeline-v2.txt](plans/2026-10-09-data-pipeline-v2.txt) — the plan for the pipeline built whole: clean, enhance, classify, interpret, results on the web, notifications and owners. What shipped is in features/pipeline/.

[plans/2026-10-09-pipeline-completion.txt](plans/2026-10-09-pipeline-completion.txt) — the pipeline completed: the session processed too, the camera's part (scene@1), live verdicts, calibration, the marks and the error table — and what is still owed.

## Handoffs — work orders given to teammates

[handoffs/](handoffs/README.txt) — one ticket per piece of work, filed by sprint: `sprint-1/done/` holds every sprint-1 ticket — the mission planner, mission start, the mission controller and its proof (the lab rungs still open), the DPP switch and contract, the pipeline's clean, enhance and classify stages (4.4's image model still Reagan's), the Control layout; only follow-ups left. `sprint-1/undone/` is empty. We are in sprint 1. Not feature docs: a ticket moves to `done/` when its work is on main, and is deleted once its follow-ups are closed and what shipped is written into `features/`.

## Platform — not written yet

`platform/architecture.md`, `platform/flight-safety.md` (superseded by the
backend doc above), `platform/temperature-correction.md` and
`platform/data-model.md` do not exist. CLAUDE.md names the temperature
constants' source; the engine itself is
`backend/agent/dronedeck/telemetry/correction.py`.

## Hardware

[hardware/](hardware/) — photos of the actual kit, plus the previous team's capstone
report and owner's manual as reference PDFs. The photos are the only record of what the
kit contains; `01`–`02` are the mainboard, `03`–`04` the AI deck, `05` the Crazyradio,
`06`–`10` power and spares.

Note the Lighthouse deck appears in **none** of the photos but **is** fitted —
confirmed by querying `deck.bcLighthouse4` on the drone. Trust the drone, not
the photos.
