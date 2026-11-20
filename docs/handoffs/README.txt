HANDOFFS — work orders for the team
===================================

STATUS: READY. The skeleton every ticket builds against is on main (#72,
2026-09-28; plan: ../plans/2026-09-28-missions-and-pipeline.txt) — start your
branch from main. UPDATED 2026-09-29 for what landed after it (below, and a
dated note at the top of each ticket it changes). Where a ticket and the code
disagree, the code wins — tell Samuel so the ticket is fixed.

A handoff is one piece of work given to one person, written so they can build
it on their own and have it fit when it is plugged into the whole.

HANDOFFS ARE NOT FEATURE DOCS. docs/features/ describes what is built and
verified, and a checker compares it with the code. A ticket describes what
does not exist yet, so it would fail that check. When a ticket is done, its
owner writes what actually shipped into docs/features/ (template:
docs/_templates/feature.txt) and the ticket moves to its sprint's done/
(below). Once its FOLLOW-UPS are closed too, the ticket is deleted. Git
keeps the history.


SPRINTS — WHERE A TICKET LIVES (2026-09-29, the owner's ask)

Tickets are filed by sprint, and inside each sprint by whether the work is
done. WE ARE IN SPRINT 1.

  sprint-1/
    undone/   work not built yet — start here if a ticket is yours
    done/     built and on main; what is left is each ticket's FOLLOW-UPS

  - A ticket is written into the current sprint's undone/.
  - It moves to done/ (git mv, same sprint) when its work is on main and
    its STATUS line says BUILT with the PR numbers. Its open FOLLOW-UPS stay
    listed in it.
  - When a sprint ends, open follow-ups and unfinished tickets are carried
    into the next sprint's undone/ (sprint-2/undone/ …); the old sprint's
    folders are left as the record of what that sprint did.
  - Moving a ticket moves its path: grep the repo for its file name and fix
    every reference — the code's comments and messages name tickets too
    (backend/agent/dronedeck/, tests/). `python3 .claude/feature-kit/kit.py
    check` finds the broken doc links; it cannot see Python strings.


THE TICKETS — SPRINT 1

  Ticket                          Owner    Stories (planning PDF)     State
  ──────────────────────────────────────────────────────────────────────────────
  done/mission-controller.txt     Samuel,  3.4, 3.5 (flying half),    built —
                                  Hannah   autonomous flight — the    Samuel's
                                           controller                 with #102
                                                                      alongside
  done/mission-verification.txt   Samuel,  autonomous flight — the    built in
                                  Yordi    proof: simulated drone,    simulation;
                                           mission report, lab        lab rungs
                                           flights, constants         open
  done/dpp-clean.txt              Kevin,   4.2 — robust@1 replaced    built —
                                  Samuel   hampel@1 (2026-10-09)      follow-ups
                                                                      open
  done/dpp-classify.txt           Samuel,  4.6 sensors — blocks@1;    sensors
                                  Reagan   4.4 image model is Reagan's built; 4.4
                                                                      open
  done/mission-planner.txt        Samuel   mission planning; 3.4      built —
                                           (holds ≥ 5 s)              follow-ups
                                                                      open
  done/mission-start.txt          Samuel   autonomous flight (the     built —
                                           session's half)            follow-ups
                                                                      open
  done/dpp-switch.txt             Samuel   4.5 (the trigger, the      built —
                                           results on screen)         follow-ups
                                                                      open
  done/control-layout.txt         Samuel   — (the standing layout     built —
                                           rule)                      follow-ups
                                                                      open
  done/dpp-contract.txt           every    4.5                        built —
                                  pipeline                            the shared
                                  owner                               agreement
                                                                      every
                                                                      undone/dpp-*
                                                                      builds on

All paths above are under sprint-1/.

sprint-1/undone/mission-controller.txt and
sprint-1/undone/mission-verification.txt are ONE piece of work split
between two people (2026-09-29): Hannah builds the controller, Yordi proves
it. They meet at "HOW THE TWO HALVES MEET" and THE SPEC — both in the
controller ticket — and land in the order written there.


WHAT IS ALREADY BUILT — the skeleton everyone plugs into

Mission system (backend/agent/dronedeck/mission/, docs/features/missions/)
  - plan/: rooms (closed geofence, obstacles, clearance), missions (home,
    inspection points), the agent's validation, saving on the laptop.
  - ManualController.fly_to(), goal_active, operator_override
    (flight/manual.py) — the goal the mission controller drives.
  - controller/: the MissionController INTERFACE, its states and events, and
    a placeholder body that refuses to fly (BUILT = False).
  - Session.run_mission() and POST /session/mission: every refusal before
    anything arms; the room's fence to the in-flight guard; point_id stamped
    into every telemetry row and frames.csv row while holding.
  - tests/fakes.py (FakeCommander, FakeClock); tests/mission/.
  - The desktop Auto page: ① Mission (list, view, edit, save) → ② Check →
    ③ Fly (Start mission, the scene with the mission loaded).
  - 2026-09-29: THE FLIGHT STARTS FROM THE DRONE — Mission.from_start puts
    the start where the drone is, keeps the points, drops points after the
    operator's END POINT, and re-validates; that is what the controller is
    handed (sprint-1/undone/mission-controller.txt, "THE MISSION YOU ARE
    HANDED IS THE MISSION AS FLOWN").
    A mission needs at least one point to be saved. The desktop draws rooms
    in 2-D or 3-D; obstacles may carry a height (drawn only — never flown
    over).

Data pipeline (backend/agent/dronedeck/pipeline/, docs/features/pipeline/)
  - contracts.py — every type in sprint-1/done/dpp-contract.txt.
  - A stub for clean, enhance and classify; interpret (labels → verdicts).
  - LocalFlightSource, LocalResultSink, the runner, compose.py.
  - `dronedeck process --flight <id>` and `--fixture`.
  - 2026-09-29: THE DPP SWITCH — with it on, the agent runs that command for
    every flight when it lands, in a child process, and the desktop shows the
    verdicts (sprint-1/done/dpp-contract.txt, "HOW THE AGENT RUNS THE
    PIPELINE").
  - tests/pipeline/: a real flight fixture, conformance.py and its checks.


RULES THAT APPLY TO EVERY TICKET

- WORK WITH /feature, IN FOUR STEPS: tell the coding agent all your ideas
  first; it plans (/feature) and waits; you settle every disagreement until
  no doubt is left; then `/feature plan and implement- aim for
  completeness , thoroughness , accuracy and precision`. Written out in full
  in sprint-1/undone/mission-controller.txt, "HOW TO WORK THIS TICKET".

- READ CLAUDE.md AT THE REPO ROOT FIRST. It outranks these tickets. The
  flight invariants are there, and each cost real debugging time.

- SET UP ONCE:
    node scripts/setup.mjs --dev
    cd backend/agent && source .venv/bin/activate

- GATES, ALL THREE, BEFORE EVERY PULL REQUEST, from backend/agent/:
    ruff check . && mypy dronedeck && pytest
  Report pass or fail honestly in the PR, including failing output.

- GIT: work on the branch named in your ticket, and open a pull request to
  main. Only Samuel can merge into main (a repository rule), so a refused
  push to main is expected. Commit under your own name. No AI co-author or
  footer lines (CLAUDE.md, "Git").

- UNITS: metres and seconds everywhere. Temperatures are stored in the unit
  the operator chose (temp_unit, "C" or "F"), recorded with every flight.
  Never assume Celsius.

- NEVER INVENT A THRESHOLD. Measure it on real data, and write the
  measurement next to the constant (CLAUDE.md, flight invariant 1 and "The
  running system is the authority").

- STUCK, OR THE CONTRACT SEEMS WRONG? Tell Samuel before working around it. A
  private workaround breaks when your part is plugged into the whole.
