<!-- This file was modified on 11/4/2026 with the assistance of Claude Code in VS Code
(Claude Opus 5.5, Anthropic, 2026). It removes the "Manual flight control is locked"
section: the lock was retired by the team, so anyone on it may change manual flight.
It also has each teammate commit as themselves, never under a former member's
identity, and credits AI assistance in file headers rather than commit lines. -->

# CLAUDE.md

> **Picking this up fresh?** Read `docs/README.md` — it routes to a doc per
> feature. **What is built and what is left comes from `docs/features/`**,
> starting with the DESIGNED BUT NOT WIRED list in
> `features/architecture-at-a-glance.txt` — never from `docs/ROADMAP.md`, a
> tick-list that understates how far the project has come. Run `/feature`'s
> Step 0 on every new request, not just the first in a session; before writing
> up a finding, grep `docs/features/` for it and extend its existing home.
>
> The project's working memory is committed, so a Claude Code cloud session
> continues where a local one left off: `docs/PROJECT_PROFILE.txt` (repos,
> gates, ship rules), `docs/GRAPH_STATE.txt` (which docs to trust),
> `docs/design/PREFERENCES.txt` (standing rules). Keep them current.
>
> There used to be a `NEXT-STEPS.txt` here. It was deleted on 2026-09-22 after
> drifting six days out of date — it still claimed 177 tests (501), that the
> public pages were unbuilt (they ship), and that nothing had ever talked to a
> real Crazyradio (see `docs/flight-log.txt`). Everything in it that was still
> true was moved into the docs below. **Do not recreate it:** a hand-maintained
> status file beside a generated router is the thing that drifted.

**drone-capstone** — DroneDeck: an autonomous indoor inspection system built around a
Crazyflie 2.1. The drone flies a route of inspection points, records temperature,
pressure and images at each, and the findings surface on a web dashboard. Named
CropWatcher until 2026-09-28 (a greenhouse brief), then renamed DroneDeck; the
2026-10-25 rename removed the CropWatcher name from the code and moved the data
folder across (migrated on first run) — see `docs/features/desktop/desktop-app.txt`,
"THE NAME". The project's parts, stories and owners come from
`web/public/docs/dronedeck-project-management-planning.pdf`.

## The constraint everything follows from

**The Crazyradio is a USB dongle. Only the machine it is plugged into can command the
drone.** No cloud service can reach it. Every architectural decision here is downstream of
that one fact.

```
browser ──▶ Supabase ──poll──▶ agent ──radio──▶ drone     (autonomous: delay is harmless)
browser ──────── WebSocket, same network ─────▶ agent     (manual: needs 50 Hz)
```

The agent is the only process that touches the radio. Nothing else — ever.

## Layout

| Path | What | Runs where |
|---|---|---|
| `backend/agent/` | Python · FastAPI · cflib | The laptop with the dongle |
| `backend/supabase/` | Migrations, seed, RLS | Supabase cloud |
| `web/` | Next.js 16 · React 19 · Tailwind v4 · Zustand | Vercel |
| `desktop/` | Tauri v2 — manual control, bundles the agent | User's laptop |
| `firmware/` | Crazyflie app (drone_wifi) built on the stock release | The drone's STM32 |
| `docs/hardware/` | Kit photos + the two reference PDFs | — |

`backend/` nests `supabase/` rather than renaming it: the Supabase CLI resolves
`supabase/` relative to the working directory, so run it as `cd backend && supabase ...`.

## Commands

```bash
# install the desktop app from a terminal — how everyone installs it: macOS, Windows or Linux
node scripts/install.mjs                     # setup, build, install; --open, --no-install

# set up a machine for development (the first step of install.mjs, on its own)
node scripts/setup.mjs --dev                 # --check: only check; drop --dev: no test tools

# agent
cd backend/agent && source .venv/bin/activate
ruff check . && mypy dronedeck && pytest   # gates
dronedeck serve                            # serve (the api module has no entry point)
dronedeck process --fixture                # the data pipeline, on the test fixture
dronedeck setup                            # install the drone's software (DroneDeck quit); --check, --reinstall

# web
cd web && pnpm dev
pnpm typecheck && pnpm lint && pnpm build    # gates

# desktop
cd desktop && pnpm build                     # gate (tsc, then vite)
cd desktop/src-tauri && cargo test           # the shell's own tests
cd desktop && pnpm app                       # freeze + verify the agent, build, open
```

`pnpm typecheck` runs `next typegen` first, and must. A bare `tsc` fails on a
fresh clone because the `PageProps` route types only exist after generation.

**With the radio plugged in and a charged battery**, the lab pass is:

```bash
cd backend/agent && source .venv/bin/activate
dronedeck check
dronedeck hover --height 0.3 --secs 30 --ambient 74F   # writes organized_flights/
pytest -m hardware                                       # deselected without a drone
```

Report pass, fail or skipped honestly. Never claim done on a gate that was not
run.

### Three things only the project owner can do

1. **Rotate the Supabase secret key and the database password.** Both were
   shared outside the dashboard during development, and the secret key bypasses
   every RLS policy. Nothing in this repo reads either, so rotating breaks
   nothing here. CI no longer uses them either (it stopped publishing
   installers on 2026-09-25), so also **delete the `SUPABASE_URL` and
   `SUPABASE_SERVICE_ROLE_KEY` GitHub Actions secrets**: a key that bypasses
   RLS should not sit where nothing needs it.
2. **Register the Google redirect URL** — see
   `docs/features/supabase/accounts-and-roles.txt`.
3. **Delete the two junk Vercel projects, `agent` and `agent-kpqe`.** They are
   still connected to this repo and fail on every push to `main` with "No
   FastAPI entrypoint found" — those are the failed-deployment emails.
   `drone-capstone` itself deploys fine. See *Deploying* below for why giving
   them an entrypoint is the wrong fix.

## Flight invariants

These cost real debugging time to discover. Each is a rule, with the failure it prevents.

1. **Read `sys.canfly` before commanding flight.** The firmware supervisor refuses to arm
   below roughly 3.75 V and reports this itself. Do not invent a voltage threshold — a
   guessed 3.70 V refused flights the drone would have allowed, repeatedly.

2. **Call `send_notify_setpoint_stop()` before using the high-level commander.** Any prior
   low-level `send_setpoint()` in the same power cycle holds setpoint priority and
   silently locks out the high-level commander: `takeoff()` returns successfully and the
   motors stay at exactly 0.

3. **Wait for the Kalman estimate to settle after a reset — never a fixed sleep.** The
   estimate keeps converging for seconds afterwards. Sampling too early produced a ground
   reference 0.14 m wrong, which put the takeoff target below the floor. Require the
   spread across a sliding window to fall under ~2 cm before trusting a value.

4. **Lighthouse `z = 0` is not the floor.** Measured floor level ranged 0.85–1.40 m across
   runs in the same room. Always capture ground at takeoff and treat commanded altitude as
   relative to it, or "1 m" becomes a 15 cm hop.

5. **The running system is the authority, not the docs or the photos.** `deck.bcLighthouse4`
   answers "is the positioning deck fitted" definitively; inferring it from kit photos was
   wrong for hours. Query the drone.

6. **Write the CSV first, then upload.** A local write cannot fail; a flight cannot be
   re-run. Supabase upload is best-effort and retried, never in the critical path.

7. **Docker cannot reach USB on macOS.** Docker Desktop runs a Linux VM with no USB
   passthrough, so the agent runs bare in a venv — and so does the build: one
   `node scripts/setup.mjs` for every OS, never a container. (There is no
   Dockerfile in the repo; if one is ever added, it is for CI and Linux only.)

## Conventions

- **Units:** metres and seconds internally, everywhere. Feet appear only in UI labels,
  converted at the boundary. Temperature is stored in the unit the operator selected at
  launch, with the unit recorded in the row.
- **Data layer:** all Supabase access goes through `web/lib/queries.ts` (reads,
  `import "server-only"` + React `cache`) and `web/lib/mutations.ts` (writes). Never
  `createClient()` inside a component — it also keeps a future Tauri mobile static export
  viable.
- **Zustand holds only ephemeral live state** — telemetry buffer, socket status, manual
  control state. Anything persistent comes from Supabase.
- **Design tokens:** CSS custom properties on `:root` in `web/app/globals.css`. Raw hex is
  confined to token definitions; components reference the variables only.
- **Authorization is server-side**, enforced by RLS on every table. Client-side checks are
  UX only. The app is publicly reachable and commands a physical drone.

## Deploying

**Vercel's Root Directory must be set to `web`.** Left unset, Vercel scans the
repo root, finds `backend/agent/pyproject.toml`, detects FastAPI and tries to
deploy the flight agent — the build then fails asking for a FastAPI entrypoint.

Never give it one. The agent talks to a USB radio: it cannot run in the cloud,
and deploying it would put operator credentials on a server for no benefit.
`.vercelignore` is a second line of defence, but the Root Directory setting is
the actual fix.

Env vars needed on Vercel: `NEXT_PUBLIC_SUPABASE_URL`,
`NEXT_PUBLIC_SUPABASE_ANON_KEY`, `NEXT_PUBLIC_SITE_URL`. The build succeeds
without them — `isSupabaseConfigured()` degrades the operator area rather than
blanking the site — but sign-in will not work until they are set.

## Git

- **Commit as yourself**: the name and email in your own git config. Never
  commit under another person's identity, including a former team member's.
- **No Claude co-author or footer lines** in commit messages or PR
  descriptions. Author is the user only. AI assistance is credited instead in
  a citation comment at the top of each file it changed.
- Branch: `main`. Web deploys via Vercel on push.
- **`main` is owner-only** (repository ruleset "main: owner only", id 23996708,
  set 2026-09-25): nobody can delete it, force-push it, or push or merge into
  it — except the repository admin, i.e. the owner. Collaborators work on
  branches and open pull requests; anyone can clone and pull. Even the owner's
  merges must use the bypass explicitly: `gh pr merge <n> --squash --admin`,
  or the "bypass rules" checkbox on GitHub. A plain merge being refused is the
  rule working, not a failure.
- **After every build that passes its gates: commit, push and merge** — on a
  short-lived branch, merged into `main` through a pull request.
- Project skills `/feature`, `/copy-adapt`, `/preflight` and `/adapt` live in
  `.claude/commands/`, and the feature-kit they run in `.claude/feature-kit/` —
  see `docs/features/development/skills.txt`.

## Out of scope

The AI deck camera. Its Wi-Fi datalink never worked in the original capstone and was cut
from final scope — the deck is fitted (`deck.bcAI = 1`) but treated as diagnostic only.
Docker for the agent, real-time stick control over the internet, and haptics as a
requirement (`navigator.vibrate` is Android-only) are also out.

## Reference material

`~/Desktop/drone project/` (a separate folder, outside this repo) holds the previous
team's implementation in `code/`, plus the working flight scripts from the bring-up
session. Read-only: consult it, never import from it.

The one thing worth taking verbatim is the tuned temperature-correction constants — see
`docs/platform/temperature-correction.md`. `short_hover.py` there is the reference
implementation for the invariants above; it is the script that achieved the first
successful autonomous hover.
