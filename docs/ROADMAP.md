# Roadmap

100 stages from empty repo to demo. Tick them off as they land.

Ordering is by dependency and by risk: the agent comes first because everything
else needs its data shapes, and Tauri comes last because it needs `rustup` and
is the only piece that can be cut without losing the capstone.

Legend: `[x]` done · `[ ]` not started · **⚠** blocked on something external.

---

## Phase 1 — Repo skeleton  ✅

1. [x] Create `drone-capstone/`, `git init`, author `samsmoaky@gmail.com`
2. [x] `.gitignore` — venv, node_modules, Rust target, secrets, flight CSVs
3. [x] `CLAUDE.md` — architecture, invariants, conventions, git rules
4. [x] `docs/README.md` router
5. [x] Move the 10 kit photos + 2 reference PDFs into `docs/hardware/`
6. [x] First commit

## Phase 2 — Flight agent core  ✅

7. [x] `pyproject.toml` — cflib, FastAPI, pydantic, supabase; dev + analysis extras
8. [x] Package layout `flight/ telemetry/ safety/ api/ sync/`
9. [x] `flight/preflight.py` — deck check, `sys.canfly`, base stations
10. [x] `wait_for_stable_z` — sliding window, never a fixed sleep
11. [x] Endurance model from voltage, not linear-in-duration
12. [x] `flight/core.py` — `connect()`, `session()`, `Flight`
13. [x] Ground-relative altitude throughout
14. [x] `send_notify_setpoint_stop()` in `_configure`
15. [x] Mellinger controller selection with PID fallback
16. [x] `_cut_motors` on every exit path
17. [x] `telemetry/correction.py` — thermal engine, constants ported verbatim
18. [x] Derived quantities: air density, QFF, pressure altitude
19. [x] `telemetry/sinks.py` — `TelemetrySink` protocol, `CsvSink`, `SupabaseSink`, `FanOutSink`
20. [x] `cli.py` — `check`, `hover`, `goto` with real arguments
21. [x] Tests: correction engine (10), sinks (9), preflight model (10)
22. [x] Gates green — ruff, mypy, pytest

## Phase 3 — Telemetry pipeline  ✅

23. [x] `telemetry/reader.py` — `LogConfig` subscription at 10 Hz
24. [x] Detect barometer variable names across firmware versions
25. [x] `telemetry/row.py` — one typed row schema, shared by CSV and Postgres
26. [x] Wire correction engine into the reader
27. [x] Ambient temperature as a launch parameter, unit recorded in the row
28. [x] Tests: reader assembles a full row from fake log data
29. [x] Tests: unit selection round-trips through the row
30. [x] `dronedeck hover` writes a real CSV

## Phase 4 — Safety  ✅

31. [x] `safety/occupancy.py` — load YAML + PGM occupancy grid
32. [x] World coordinates → map cells
33. [x] Bresenham line check between two points
34. [x] `safety/geofence.py` — axis-aligned bounds check
35. [x] Reject waypoints outside bounds, with the reason
36. [x] Reject paths crossing blocked cells
37. [x] Tests: geofence accepts inside, rejects outside, rejects on the boundary
38. [x] Tests: Bresenham finds a wall between two clear points
39. [x] Wire safety checks into `flight/missions.py` before any `goto`

## Phase 5 — Missions  ✅

40. [x] `flight/missions.py` — `Mission` model, ordered waypoints
41. [x] `hover`, `lawnmower`, `waypoint` as mission types
42. [x] Lawnmower generator — area, step, altitude layers → waypoints
43. [x] Dry-run mode: validate and print, never arm
44. [x] Abort handling — land on exception, on Ctrl+C, on critical voltage
45. [x] Mission progress events (`started`, `waypoint_reached`, `landed`, `aborted`)
46. [x] Tests: lawnmower covers the area with no gaps
47. [x] Tests: abort mid-mission still lands
48. [x] `dronedeck mission --file plan.json`

## Phase 6 — Supabase schema  ✅

49. [x] `supabase/config.toml`, local dev via `supabase start`
50. [x] `migrations/0001_init.sql` — `profiles`, `drones`
51. [x] `flights` table — one row per flight, status, started/ended
52. [x] `telemetry` table — FK to flight, indexed on `(flight_id, recorded_at)`
53. [x] `missions` table — the queue: `queued`/`claimed`/`running`/`done`/`failed`
54. [x] `predictions` table — zone health output
55. [x] `zones` table — greenhouse grid definition
56. [x] RLS on every table, deny by default
57. [x] Policy: authenticated users read their own org's data
58. [x] Policy: only the agent role may claim a mission
59. [x] `claim_next_mission()` RPC — atomic, avoids two agents taking one job
60. [x] Realtime enabled on `telemetry` and `missions`
61. [ ] `seed.sql` — one greenhouse, a zone grid, a demo flight (zones, a drone and a user are seeded; **no flight** is)
62. [x] Generate `types/database.ts` from the schema

## Phase 7 — Agent ↔ Supabase  ✅

63. [x] `sync/client.py` — Supabase client from env, fails loudly if unset
64. [x] `sync/poller.py` — claim → execute → report, outbound only
65. [x] Backoff when the queue is empty; never hammer
66. [x] Mark a mission failed with its reason, never leave it `claimed`
67. [x] Resume safely after an agent restart (stale claim reclaim)
68. [x] `SupabaseSink` wired into live flights
69. [x] Tests: poller claims exactly one mission with two agents running
70. [x] Tests: a failed flight marks the mission failed, not stuck

## Phase 8 — Agent HTTP + WebSocket  ✅

71. [x] `api/rest.py` — FastAPI app, `/health`, `/status`, `/preflight`
72. [x] `POST /flight/mission`, `POST /flight/stop`
73. [x] Manual control socket — `/ws/live` in `api/rest.py`, loop in `flight/manual.py`
74. [x] Browser sends intent; agent generates 50 Hz setpoints itself
75. [x] Heartbeat — auto-land after 0.5 s of silence
76. [x] Bind to localhost by default; LAN only behind an explicit flag
77. [ ] Telemetry broadcast over the same socket. The agent side is **done** — `/ws/live` sends telemetry and the desktop console consumes it. Two gaps remain:
    - **Browser manual flight** — the detail lives in its own home, `features/architecture-at-a-glance.txt` under *DESIGNED BUT NOT WIRED*. Do not re-derive it here.
    - **Manual control starts no `TelemetryReader`**, so a manual flight records nothing at all. Fixing it means an in-memory broadcast sink plus starting the reader during manual control. **Needs a lab check first: logging and 50 Hz control share one radio.**
77b. [ ] `serve` does not run the mission poller — also in *DESIGNED BUT NOT WIRED*.
77b. [ ] `serve` does not run the mission poller (confirmed: nothing calls `claim_mission` on a loop — `sync/cloud.py:281` is the only reference). An installed desktop app therefore never flies missions queued from the website, which is the whole point of the Supabase leg. Run the poller inside the desktop agent.
78. [x] Tests: dropped heartbeat triggers land
79. [x] Tests: malformed control frame is rejected, not crashed on

## Phase 9 — Web foundation

80. [x] `pnpm create next-app` — Next 16, React 19, TS strict, Tailwind v4
81. [x] Copy-adapt `lib/supabase/{client,server,middleware}.ts`
82. [x] Copy-adapt `.env.example` with the service-role warning
83. [x] `app/globals.css` — tokens on `:root`, raw hex only in definitions, dark theme
84. [x] Google OAuth — `app/auth/callback`, middleware gating
85. [x] `lib/queries.ts` (`server-only` + React `cache`) and `lib/mutations.ts`
86. [x] Zustand store — live telemetry, socket status, manual state only
87. [x] Four async states on every surface: loading, empty, error+retry, content
88. [ ] PWA manifest + icons (no manifest exists)

## Phase 10 — Web features

89. [x] Dashboard — latest flight, battery, zone summary
90. [x] Live telemetry page via Supabase Realtime
91. [x] Flight path map from `x_m`/`y_m`
92. [x] Time series — one chart per unit, table view on each
93. [x] Mission planner — click zones, review, queue
94. [x] Manual control page — keyboard, connects to the agent socket
95. [x] Run comparison across flights
96. [x] Zone health map (renders predictions; none exist until 97–98)

### Added after review — the visitor-facing manual

The original page list had no setup guide, which left the Owner's Manual PDF as
the only instructions. Added:

- [x] `/setup` — numbered walkthrough, live agent-status banner, and a
      troubleshooting list written symptom-first from real bring-up failures
- [x] `/setup/hardware` — every kit component photographed, including why most
      sensors are invisible
- [x] Download buttons wired to Supabase Storage (show "coming soon" until CI uploads a build)

## Phase 11 — ML

> **⚠ BLOCKED, AND NOT BY EFFORT.** Read this before planning Task 4 around the
> previous team's model. Established 2026-09-22 by reading
> `~/Desktop/drone project/code/pomegranate-tree-prediction.ipynb` directly.
>
> `best_lnn_stable.pt` is an **EfficientNet-B0 + 2-layer CfC** classifier whose
> input is
>
> ```
> IN_DIM = FEAT_DIM + len(ENV_COLS)   # 1291
> FEAT_DIM = 1280                     # EfficientNet-B0 image embedding
> ENV_COLS = ['temp','dewpt','rh','precip_rate','solar_rad',
>             'ghi','dhi','dni','pres','wind_spd','vis']
> ```
>
> **1280 of its 1291 inputs are a photograph.** `extract_features(df)` runs
> EfficientNet-B0 over `df['image_path']`, one image per `(fruit_id, week)` — a
> per-fruit weekly series of pomegranate photos. This drone carries no camera
> (removed 2026-11-15), and it would not
> photograph individual labelled fruits over weeks.
>
> **Of the remaining 11 columns, this drone can measure 2**: `temp` (after
> thermal correction) and `pres`. The other nine are outdoor weather-station
> readings — dew point, humidity, precipitation, three solar irradiance
> channels, wind speed, visibility — and several are meaningless for an INDOOR
> greenhouse.
>
> So the model cannot be loaded and fed. Zero-padding the 1280 image dimensions
> would produce confident output from an input the network has never seen, and
> that output would be written into `predictions` and used to colour zones on
> the dashboard. **Do not do that.** A plausible wrong number on a health map is
> worse than an empty one.
>
> There is a third cost even if the data existed: torch + torchvision + `ncps`
> inside the PyInstaller one-file binary, which today is 27 MB and ships in a
> 30 MB .dmg to operators (`docs/features/desktop/desktop-app.txt`).
>
> **The honest options**, none of which is "port the notebook":
> 1. **Zone-level thermal aggregates** — mean and spread of corrected
>    temperature per zone, position-tagged, deviation from the greenhouse mean.
>    Buildable today from data already recorded, and it is what the system
>    already promises. It is statistics, and must be labelled as statistics, not
>    as a model.
> 2. **Train something on this drone's own sensors.** Needs labelled data that
>    does not exist yet.
> 3. ~~**Fix the camera datalink first**, then revisit~~ — gone: the drone
>    carries no camera since 2026-11-15 (the AI deck is off it;
>    `docs/features/architecture.txt`, DECISIONS). The model was trained on
>    pomegranates anyway, not on our equipment.

97. [ ] ~~Port the notebook's feature extraction~~ — see above. Nine of the
    eleven environmental columns cannot be measured by this airframe, so there
    is nothing faithful to port.
98. [ ] ~~Load `best_lnn_stable.pt`, infer after correction~~ — blocked on the
    another team's photographs and on the feature space, not on time.
98b. [ ] **Decide what Task 4 actually is**, given the above. Option 1 is the
    only one that ships without new hardware.

## Phase 12 — Desktop + ship

99. [x] Tauri app — manual control window, agent bundled as a PyInstaller sidecar (macOS arm64 built and verified locally)
100. [ ] GitHub Actions — build macOS `.dmg` + Windows `.exe` on tag (workflow written; never run)

### Keyboard control (Tauri manual window)

Keys mirror `keyboard_fly.py` from the bring-up session, which flew.

| Key | Action |
|---|---|
| `↑` `↓` | pitch — forward / back |
| `←` `→` | roll — left / right |
| `W` `S` | thrust — up / down |
| `A` `D` | yaw — rotate left / right |
| `P` | **cut motors immediately** |
| `Q` | land gently and disarm |

Rules the implementation must honour:

- **The browser sends held-key state, never one command per keypress.** The
  agent generates the 50 Hz setpoint stream itself. A key-per-packet design
  breaks the moment one packet is late, and the commander cuts out below ~10 Hz.
- **Thrust decays toward zero whenever `W` is not held**, so releasing the
  keyboard brings the drone down rather than leaving it pinned at power.
- **Heartbeat at 10 Hz; auto-land after 0.5 s of silence.** Covers a closed tab,
  a frozen browser, a lost socket.
- `P` is checked before anything else in the frame, so a panic stop is never
  queued behind a movement.
- Keys are repeated OS-level while held — track key *state* (down/up events),
  not the repeat stream, or the rate is at the mercy of the OS repeat delay.
- Show every binding on screen. A manual-control surface whose keys are not
  visible is a crash waiting to happen.

---

## Verification gates

Run before calling any phase done, and report pass/fail/skipped honestly:

```bash
cd backend/agent && source .venv/bin/activate
ruff check . && mypy dronedeck && pytest      # agent

cd web && pnpm typecheck && pnpm lint && pnpm build   # web
```

Hardware verification needs the Crazyradio plugged in:

```bash
dronedeck check                          # preflight only, no motors
dronedeck hover --height 0.3 --secs 30   # reproduce the 25 s held hover
```
