"""Publish the DroneDeck project pages: an overview and one page per part.

    SUPABASE_API=https://<ref>.supabase.co SUPABASE_SERVICE=<secret key> \\
    python backend/supabase/seed/dronedeck_projects.py            # show the plan
    ... python backend/supabase/seed/dronedeck_projects.py --apply    # write it

The six parts are the six our Project Management Planning document divides
the work into (web/public/docs/dronedeck-project-management-planning.pdf):
every user story, owner, estimate and acceptance criterion below is from it,
and every "built" is something in this repository today (2026-09-28).

What it writes, and nothing else:

  - seven `projects` rows, matched by slug: created if missing, updated if
    present. The team edits them in /admin afterwards; re-running this
    overwrites those edits, so re-run it only to restore these versions.
  - their `project_members` rows, from the people already in team_members
    (matched by slug — nobody is created, renamed or re-photographed).
  - the six starter projects from seed/portfolio.py set to `draft`: hidden,
    not deleted, and republished from /admin with one click.

It never touches `design-document` (the team's own design document, entered
in /admin) or anything outside `projects` and `project_members`.

Uses the SECRET key, so it runs on a developer's machine only — never commit
the key. Unlike seed/portfolio.py it does not touch the team: that script
matches people by name, and a name edited in /admin would be re-created.
"""

from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import sys
import urllib.error
import urllib.request
from typing import Any

PDF = "/docs/dronedeck-project-management-planning.pdf"
NEVER_TOUCH = {"design-document"}
RETIRE = [
    "dronedeck", "flight-safety", "desktop-flight-app",
    "operator-dashboard", "temperature-correction", "offline-flight-data",
]

# Team member slugs, as in team_members (checked before anything is written).
SAMUEL, HANNAH, KEVIN, REAGAN, YORDANOS = (
    "samuel-enam-zih", "hannah", "kevin", "reagan", "yordanos",
)

# ── BlockNote builders (the shape lib/blocknote/render.ts renders) ─────────


def t(text: str, **styles: bool) -> dict[str, Any]:
    return {"type": "text", "text": text, "styles": styles}


def b(text: str) -> dict[str, Any]:
    return t(text, bold=True)


def code(text: str) -> dict[str, Any]:
    return t(text, code=True)


def link(text: str, href: str) -> dict[str, Any]:
    return {"type": "link", "href": href, "content": [t(text)]}


def _runs(parts: tuple[Any, ...]) -> list[dict[str, Any]]:
    return [t(x) if isinstance(x, str) else x for x in parts]


def h2(text: str) -> dict[str, Any]:
    return {"type": "heading", "props": {"level": 2}, "content": [t(text)]}


def h3(text: str) -> dict[str, Any]:
    return {"type": "heading", "props": {"level": 3}, "content": [t(text)]}


def p(*parts: Any) -> dict[str, Any]:
    return {"type": "paragraph", "content": _runs(parts)}


def li(*parts: Any) -> dict[str, Any]:
    return {"type": "bulletListItem", "content": _runs(parts)}


def quote(text: str) -> dict[str, Any]:
    return {"type": "quote", "content": [t(text)]}


def img(url: str, caption: str = "") -> dict[str, Any]:
    return {"type": "image", "props": {"url": url, "caption": caption, "previewWidth": 900, "showPreview": True}}


def table(header: list[str], rows: list[list[str]]) -> dict[str, Any]:
    """A table whose first row is the header, set in bold."""
    return {
        "type": "table",
        "content": {
            "type": "tableContent",
            "rows": [{"cells": [[b(h)] for h in header]}]
            + [{"cells": [[t(str(c))] for c in row]} for row in rows],
        },
    }


STORY_HEADER = ["ID", "User story", "MVP / stretch", "Owner", "Estimate", "Status"]


def story(sid: str, title: str, *, kind: str, owner: str, estimate: str, status: str,
          want: str, tasks: list[str], accept: list[str], how: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One user story: the plan's own words, then how it was or will be built."""
    return [
        h3(f"{sid} · {title}"),
        p(b(f"{kind} · {status} · {owner} · {estimate}")),
        quote(want),
        p(b("Tasks")),
        *[li(x) for x in tasks],
        p(b("Done when")),
        *[li(x) for x in accept],
        *how,
    ]


def plan_link() -> dict[str, Any]:
    return p("Source: ", link("our Project Management Planning document (PDF)", PDF),
             " — the user stories, owners, estimates and acceptance criteria are quoted from it.")


# ══════════════════════════════════════════════════════════════════════════
# The overview
# ══════════════════════════════════════════════════════════════════════════

OVERVIEW: dict[str, Any] = {
    "slug": "dronedeck",
    "title": "DroneDeck",
    "subtitle": "An autonomous indoor drone that inspects the site it is deployed in",
    "summary": "The whole system in one page: a Crazyflie 2.1 that flies a route of inspection "
               "points, the desktop app that flies it, the dashboard that shows what it found, "
               "and the six parts the work is divided into — with our planning document attached.",
    "category": "Overview",
    "date_label": "Fall 2026",
    "location": "Team 18",
    "cover_image_url": "/projects/dronedeck.webp",
    "members": [HANNAH, REAGAN, KEVIN, YORDANOS, SAMUEL],
    "content": [
        h2("What DroneDeck is"),
        p("DroneDeck is an inspection system built around a Crazyflie 2.1 — a palm-sized "
          "quadcopter — for indoor spaces full of equipment worth watching: a plant room, a "
          "warehouse, a lab. Deployed there, it takes off by itself, flies a planned route from "
          "one inspection point to the next, records temperature, pressure and a greyscale image "
          "at each, and lands where it started. A pipeline then compares what it recorded against "
          "normal operating limits and against images of healthy equipment, and anything that "
          "looks wrong is flagged on a dashboard with the reason."),
        p("Two apps carry it. The ", b("desktop app"), " runs on the laptop the radio is "
          "plugged into and is the only thing that flies the drone — manually from the keyboard, "
          "or through an autonomous mission. The ", b("website"), " is the record: public pages "
          "about the project, and a signed-in dashboard with every flight, its readings and "
          "images, comparisons over time, and a mission planner."),
        img("/brand/drone-hero.webp", "DroneDeck's mark. The drone the team flies is a Crazyflie 2.1 with a "
                                      "Lighthouse positioning deck and an AI deck camera."),
        h2("The constraint everything follows from"),
        p("The Crazyflie is commanded through the Crazyradio, a USB dongle. Only the computer it "
          "is plugged into can talk to the drone — no website or cloud service can reach it. So "
          "one program on that laptop, the ", b("flight agent"), ", owns the radio, and nothing "
          "else ever touches it. The desktop app talks to the agent on the same machine; the "
          "agent writes every flight to disk first and uploads it to the database afterwards, "
          "where the website reads it."),
        table(["Piece", "Built with", "Runs on"], [
            ["Drone", "Crazyflie 2.1 · Lighthouse deck · AI deck (camera)", "The flight area"],
            ["Flight agent", "Python 3.11 · FastAPI · cflib", "The laptop with the Crazyradio"],
            ["Desktop app", "Tauri v2 · React 19 · TypeScript", "The same laptop"],
            ["Website and dashboard", "Next.js 16 · React 19 · Tailwind v4", "Vercel"],
            ["Database", "Supabase — Postgres, row-level security, Auth, Storage", "Supabase cloud"],
        ]),
        h2("The six parts"),
        p("Our planning document divides the work into six parts. Each has its own page — what "
          "it does, every user story in it with its owner and estimate, how it was built or how "
          "we will build it, and what is left:"),
        li(link("1 · Apps, manual flight and live view", "/projects/apps-and-manual-flight"),
           " — built: the desktop app and the website, sign-in, keyboard flight, live sensors and "
           "camera, flight history, pre-flight checks, a one-command install."),
        li(link("2 · Autonomous flight and navigation", "/projects/autonomous-flight"),
           " — in progress: the virtual fence and route planner are built; the first autonomous "
           "flights are Sprint 1."),
        li(link("3 · Data collection", "/projects/data-collection"),
           " — in progress: readings and images are recorded and uploaded; capture at each "
           "inspection point comes next."),
        li(link("4 · Anomaly detection and alerts", "/projects/anomaly-detection"),
           " — in progress: training data, data cleaning, image enhancement and the first "
           "classifier start in Sprint 1."),
        li(link("5 · Documentation", "/projects/documentation"),
           " — in progress: the setup guide is on this site; the operating and waypoint guides follow."),
        li(link("6 · Testing and experiment design", "/projects/testing"),
           " — in progress: the test boxes, reference instruments and a hand-warmer fault."),
        p("The team's design document — architecture diagram, component table and interface "
          "sketches — is its own page: ", link("Design Document", "/projects/design-document"), "."),
        h2("Who does what, and how much time there is"),
        p("Weeks run Monday to Sunday, from 28 September to 13 December: five two-week sprints, "
          "a Thanksgiving week and a final week. Everyone works at least 12 hours a week — Kevin "
          "15 — and Hannah and Reagan 10 in Thanksgiving week."),
        table(["Person", "Leads", "Hours a week", "Semester total"], [
            ["Hannah Alexander", "Autonomous flight", "12 (10 at Thanksgiving)", "130"],
            ["Reagan Gary", "Anomaly detection", "12 (10 at Thanksgiving)", "130"],
            ["Kevin Loi", "Data collection", "15", "165"],
            ["Yordanos Tessema", "Documentation", "12", "132"],
            ["Samuel Zih", "Apps, manual flight and live view", "12", "132"],
        ]),
        p("That is 689 person-hours across 41 user stories: 34 in the minimum viable product and "
          "7 stretch goals. Testing is shared."),
        h2("Where it stands"),
        table(["Stage", "When", "What"], [
            ["Sprint 0 — done", "16–27 Sep", "The apps and the flight software under them; 13 user stories built"],
            ["Sprint 1 — now", "28 Sep – 11 Oct", "Positioning measured, first A→B flights, test rig, data cleaning, first classifier"],
            ["Sprint 2", "12–25 Oct", "Return to base, capture at each point, one pipeline, alerts with reasons"],
            ["Sprint 3", "26 Oct – 8 Nov", "Missions from the website, alerts on the dashboard, full mission end to end"],
            ["Sprint 4", "9–22 Nov", "Waypoint guide; first stretch goals"],
            ["Thanksgiving", "23–29 Nov", "Lighter week"],
            ["Sprint 5", "30 Nov – 6 Dec", "Remaining stretch goals"],
            ["Final", "7–13 Dec", "Demo and hand-over"],
        ]),
        p("Sprint 0 and Sprint 1 are as written in the plan. The later sprints give the order the "
          "remaining stories are planned in; the date of every task is in our Jira timeline."),
        h2("The planning document"),
        p("Everything on these pages traces to it: the person-hours table, the full backlog with "
          "acceptance criteria, the Gantt chart and the Sprint 1 backlog. ",
          link("Read the Project Management Planning document (PDF)", PDF), "."),
        p("The code, and a working record of every decision and measurement behind it, is at ",
          link("github.com/rbgary/drone-capstone-project", "https://github.com/rbgary/drone-capstone-project"), "."),
    ],
}

# ══════════════════════════════════════════════════════════════════════════
# 1 · Apps, manual flight and live view
# ══════════════════════════════════════════════════════════════════════════

APPS: dict[str, Any] = {
    "slug": "apps-and-manual-flight",
    "title": "Apps, manual flight and live view",
    "subtitle": "Part 1 · The desktop app that flies the drone and the website that shows what it found",
    "summary": "A desktop app and a website with one sign-in, keyboard flight that holds its "
               "position, live sensors and camera, every flight saved for comparison, pre-flight "
               "checks, and a one-command install on macOS, Windows and Linux.",
    "category": "Software",
    "date_label": "Built 16–27 Sep 2026",
    "location": "Led by Samuel Zih",
    "cover_image_url": "/projects/apps.webp",
    "members": [SAMUEL, REAGAN],
    "content": [
        h2("What this part is"),
        p("Everything a person touches: the desktop app on the laptop with the radio, which flies "
          "the drone, and the website, whose signed-in dashboard is where every flight ends up. "
          "Eight of this part's ten user stories are built. What remains is showing the anomaly "
          "pipeline's alerts on the dashboard (it needs Part 4 first), and a stretch goal: "
          "commands in plain English."),
        table(STORY_HEADER, [
            ["1.1", "Clear, labelled pages in both apps", "MVP", "Samuel", "16 h", "Built"],
            ["1.2", "One account; only operators see data or fly", "MVP", "Samuel", "2 h", "Built"],
            ["1.3", "Manual flight with smooth, stable control", "MVP", "Samuel", "12 h", "Built"],
            ["1.4", "Live sensor readings", "MVP", "Samuel", "6 h", "Built"],
            ["1.5", "Live camera feed", "MVP", "Samuel", "6 h", "Built"],
            ["1.6", "Past flights saved and comparable", "MVP", "Samuel", "3 h", "Built"],
            ["1.7", "Checks before every flight", "MVP", "Samuel", "2 h", "Built"],
            ["1.8", "Install with one command on any OS", "MVP", "Samuel", "8 h", "Built"],
            ["1.9", "Flagged equipment highlighted with its alert", "MVP", "Samuel", "12 h", "Planned · Sprint 3"],
            ["1.10", "Commands in plain English", "Stretch", "Samuel, Reagan", "12 h each", "Planned · Sprint 5"],
        ]),
        h2("The user stories"),
        *story("1.1", "Clear, labelled pages", kind="MVP", owner="Samuel", estimate="10 h web + 6 h desktop",
               status="Built",
               want="As a user, I want web and desktop apps with clear, labelled pages for each feature, "
                    "so that I can find what I need quickly.",
               tasks=["A website with public pages and an operator area: Live, Flights, Zones, Compare, Plan, Settings.",
                      "A desktop app with a sidebar: Home, Control, Sessions, Set Up and Drone Wi-Fi."],
               accept=["Every page in both apps is reachable from the header or sidebar and clearly labelled."],
               how=[p("The website is Next.js 16 on Vercel. Visitors get an overview, the six part pages, "
                      "setup guides, a gallery and the team; every word on those pages is editable at "
                      "/admin without a deploy. Signed-in operators get the dashboard. The desktop app is "
                      "Tauri v2 with React 19: a collapsible sidebar with the pages, the Auto / Manual mode "
                      "switch and the account. Both share one set of colour tokens, a light and a dark "
                      "theme with a switch that remembers your choice, and the DroneDeck wordmark.")]),
        *story("1.2", "One account, and access enforced by the database", kind="MVP", owner="Samuel",
               estimate="1 h + 1 h", status="Built",
               want="As a user, I want to create an account and sign in, so that only authorized members "
                    "of the team can see flight data and fly the drone.",
               tasks=["Sign-up and sign-in, one account for both the web and desktop apps.",
                      "An operator role, enforced in the database."],
               accept=["Users can sign up and sign in without errors.",
                       "Only a signed-in operator can view flight data or fly the drone."],
               how=[p("Accounts are Supabase Auth. Authorization is not code in either app: it is row-level "
                      "security in Postgres, on every table — the website is public and ultimately commands "
                      "a physical drone, so a check in the browser would only be a suggestion. A 15-check "
                      "test proves the policies hold. The desktop app signs in through the flight agent, which "
                      "then uploads with the operator's own session — never a service key — so the drone's "
                      "uploads obey exactly the same rules a person does.")]),
        *story("1.3", "Manual flight that holds its position", kind="MVP", owner="Samuel", estimate="12 h",
               status="Built — flown 22 Sep",
               want="As an operator, I want to fly the drone within the inspection space manually from my "
                    "laptop with smooth, stable control, so that I can position it and test it by hand.",
               tasks=[("Keyboard control in the desktop app: the flight agent runs a 50 Hz control loop, "
                      "holds the spot when keys are released, and lands if the app goes quiet for 0.5 s.")],
               accept=["The drone climbs, moves, and holds its position when the keys are released."],
               how=[p("The app never sends flight commands. It reports ", b("which keys are held"),
                      ", about ten times a second, and the agent turns that into setpoints in its own "
                      "50 Hz loop — so a window that stalls for a moment cannot become a drone that falls. "
                      "If the app goes quiet for half a second, the agent lands the drone."),
                    p("The first version controlled speed and drifted: a speed never says where. The version "
                      "that works moves a ", b("commanded point"), ": every axis you are not pressing is "
                      "locked where the drone is, the keys move the point, and the drone corrects to it "
                      "continuously. Let go and it holds the spot. Its first flight held x and y to within "
                      "0.0006–0.017 m through a whole climb. Autonomous flight (Part 2) uses the same "
                      "position estimate, guards and checks.")]),
        *story("1.4", "Live sensor readings", kind="MVP", owner="Samuel", estimate="3 h desktop + 3 h web",
               status="Built",
               want="As an operator, I want to see live sensor readings during a flight, so that I can "
                    "watch the drone and the equipment in real time.",
               tasks=["Live sensor windows in the desktop app, at 10 readings a second.",
                      "A Live page on the website."],
               accept=["Temperature, pressure, battery and position update on screen while flying, in both apps."],
               how=[p("The agent keeps ", b("one"), " 10 Hz telemetry subscription, and everything reads it — "
                      "the windows, the safety guards, the recorder — so there is one version of the truth. "
                      "The desktop Control page shows the live windows beside the keys; the website's Live "
                      "page shows the same readings.")]),
        *story("1.5", "The drone's camera, live", kind="MVP", owner="Samuel", estimate="6 h", status="Built",
               want="As an operator, I want to see the camera feed live, so that I can see what the drone sees.",
               tasks=[("A Camera tab in the desktop app; the Set Up page installs the camera software on a "
                      "newly connected drone and joins it to the Wi-Fi.")],
               accept=[("The camera tab shows the drone's current view while it is connected — still frames "
                       "at about 3.7 a second is acceptable at a minimum.")],
               how=[p("The AI deck's GAP8 processor runs Bitcraze's image streamer, which the app flashes onto a "
                      "new drone over the radio. A small firmware addition lets the deck join the operator's "
                      "own Wi-Fi, configured over the radio too. Frames arrive as raw 324×244 greyscale; over "
                      "the university network at a weak signal it delivered 74 frames in 20 seconds — about "
                      "3.7 a second. When the deck's sender stalls, the app reconnects by itself.")]),
        *story("1.6", "Every flight saved, and comparable", kind="MVP", owner="Samuel", estimate="2 h + 1 h",
               status="Built",
               want="As an inspector, I want past flight sessions saved and made comparable (e.g., previous "
                    "week, previous month), so that I can spot changes over time and plan machine maintenance.",
               tasks=["A Flights page with each completed flight's raw readings and charts.",
                      "A Compare page for up to four flights at a time."],
               accept=["Data from any past flight can be opened and viewed.",
                       "Up to four flights can be compared side by side."],
               how=[p("The desktop app keeps every session on the laptop as well, so a flight is readable there "
                      "even before it has been uploaded. Compare keeps each flight's colour in the address, so "
                      "removing one never repaints the others.")]),
        *story("1.7", "Checks before every flight", kind="MVP", owner="Samuel", estimate="2 h", status="Built",
               want="As an operator, I want the app to check the drone before every flight, so that it "
                    "never takes off in an unsafe state.",
               tasks=[("Check the drone's identity, telemetry, motors, battery, decks, base stations and "
                      "settled position, writing every action into an audit trail.")],
               accept=["Success is reported; a failure blocks take-off and explains the reason in plain words."],
               how=[p("Each check is shown as it happens. Only three results block a flight: no telemetry, the "
                      "drone's own firmware refusing to arm, and motors it will not release. The battery is "
                      "judged by the firmware's own verdict, never a voltage we guessed — a guessed threshold "
                      "once refused flights the drone would have allowed. Positioning problems warn instead, "
                      "and flying without positioning needs a second, explicit confirmation.")]),
        *story("1.8", "One command to install", kind="MVP", owner="Samuel", estimate="8 h", status="Built",
               want="As a new team member, I want to install the system with one command on Mac, Windows, "
                    "or Linux, so that anyone can run it.",
               tasks=[("Bundle the flight agent inside the desktop app, so no Python set-up is needed, and "
                      "install it all with a single command.")],
               accept=["A fresh machine installs the software and signs in after following the one command."],
               how=[p("The agent is frozen with PyInstaller and shipped inside the app. ", code("node scripts/install.mjs"),
                      " checks the whole toolchain first, lists every missing piece with the exact command "
                      "to install it, then builds and installs. Each computer builds its own copy, which is "
                      "also what gives an Intel Mac and an Apple-silicon Mac each the right one. CI runs the "
                      "same command on an Apple-silicon Mac, an Intel Mac, Windows and Ubuntu and proves "
                      "sign-in works on each. The steps are in the ", link("setup guide", "/setup/desktop-app"), ".")]),
        *story("1.9", "Flagged equipment on the dashboard", kind="MVP", owner="Samuel",
               estimate="8 h + 4 h", status="Planned · Sprint 3",
               want="As an inspector, I want flagged equipment (abnormal or needing further inspection) "
                    "highlighted in the app with its alert, so that I can review and follow up.",
               tasks=["Show the pipeline's alerts for flagged inspection points on the dashboard.",
                      "Let the user open and review each alert with ease."],
               accept=["Flagged equipment stands out on the dashboard, with its alert.",
                       "Opening an alert shows the sensor reading or image, and the reason for the flag."],
               how=[p("How we will build it: the pipeline (Part 4) writes one verdict per inspection point per "
                      "flight, with its reason and the reading or frame behind it. The dashboard's Zones map "
                      "already shades each area by its latest result; flagged points will be marked there and "
                      "listed, newest first, each opening to the evidence.")]),
        *story("1.10", "Commands in plain English", kind="Stretch", owner="Samuel, Reagan",
               estimate="12 h each", status="Planned · Sprint 5",
               want="As an operator, I want to give the system commands in plain English, so that I do not "
                    "need to learn the interface.",
               tasks=["An LLM component that turns plain English into app actions."],
               accept=["A plain English request (e.g., \"show last week's flagged equipment\") returns the correct result."],
               how=[p("How we will build it: read-only first — questions answered from the dashboard's own "
                      "data, never an action that moves the drone.")]),
        h2("What is left"),
        li("1.9 — alerts on the dashboard, once Part 4 produces them."),
        li("1.10 — plain-English commands, a stretch goal."),
        plan_link(),
    ],
}

# ══════════════════════════════════════════════════════════════════════════
# 2 · Autonomous flight and navigation
# ══════════════════════════════════════════════════════════════════════════

FLIGHT: dict[str, Any] = {
    "slug": "autonomous-flight",
    "title": "Autonomous flight and navigation",
    "subtitle": "Part 2 · Take off, fly a route of inspection points, land where it started",
    "summary": "The drone flying itself: holding a position on Lighthouse positioning, going from one "
               "inspection point to the next inside a virtual fence, and returning to base. The fence "
               "and route planner are built; the first autonomous flights are Sprint 1.",
    "category": "Systems",
    "date_label": "Sprint 1 → Sprint 5",
    "location": "Led by Hannah Alexander",
    "cover_image_url": "/projects/autonomous-flight.webp",
    "members": [HANNAH, SAMUEL, YORDANOS, KEVIN, REAGAN],
    "content": [
        h2("What this part is"),
        p("An inspection is only repeatable if the drone flies the same route the same way every "
          "time, without a pilot. This part takes the drone from a take-off point through a planned "
          "list of inspection points — holding steady at each — and back to where it started, inside "
          "a fence it cannot leave. It is built on the same machinery as manual flight (Part 1): the "
          "same position estimate, the same safety guards, the same pre-flight checks."),
        table(STORY_HEADER, [
            ["2.1", "Take off, hover, land by itself", "MVP", "Samuel, Hannah", "4 h each", "Sprint 1"],
            ["2.2", "Hold position and height steadily", "MVP", "Samuel, Hannah", "10 h + 5 h", "Sprint 1"],
            ["2.3", "A virtual fence", "MVP", "Samuel", "3 h", "Built"],
            ["2.4", "Point A to Point B by itself", "MVP", "Samuel, Hannah", "14 h + 8 h", "Sprint 1"],
            ["2.5", "Plan a route of inspection points", "MVP", "Samuel, Reagan, Kevin", "15 h", "Planner built · pickup Sprint 3"],
            ["2.6", "Return to base when the mission ends", "MVP", "Hannah, Samuel", "6 h each", "Sprint 2"],
            ["2.7", "Return to base on low battery", "Stretch", "Hannah, Yordanos", "24 h + 20 h", "Sprint 4"],
            ["2.8", "Avoid walls, people and equipment", "Stretch", "Kevin", "30 h", "Sprint 5"],
        ]),
        h2("Where we start from"),
        p("Positioning is the precondition for all of it. The drone knows where it is from the "
          "Lighthouse deck, which reads sweeps from base stations mounted in the room. Without it, "
          "the drone falls back to its barometer — and indoors that is not good enough: in one "
          "measured flight the barometer swung through 72 cm while the height target never moved, "
          "against 1.3 cm on the Lighthouse, 55 times steadier. Every flight since has fallen back "
          "to the barometer for one reason: the base stations' geometry has never been measured in "
          "this room. Measuring it is the first task of Sprint 1."),
        img("/projects/autonomous-flight.webp", "Position control is the difference between hovering and wandering. (Stock photograph.)"),
        h2("The user stories"),
        *story("2.1", "Take off, hover and land by itself", kind="MVP", owner="Samuel, Hannah",
               estimate="2 h each, twice", status="Sprint 1 — code built, not yet flown on position",
               want="As an operator, I want the drone to take off, hover at a set height and land by itself, "
                    "so that a mission can start and end without anyone piloting it.",
               tasks=["Take off from the ground, hold position for 10 seconds, land safely.",
                      "Measure the Lighthouse geometry and fly on it."],
               accept=["The drone rises to 0.3 m above its take-off floor, holds for 10 seconds, and lands with no key pressed.",
                       "The flight trace shows it flew on the Lighthouse, not the barometer."],
               how=[p("Built: a take-off → hold → land program (", code("./dronedeck hover --height 0.3 --secs 10"),
                      ", and Auto mode in the desktop app), with every wait watched by the same guards as manual "
                      "flight. What remains is flying it with positioning, and reading the trace to prove it."),
                    p("Three rules this code already follows, each learned the hard way: ",
                      b("read the drone's own permission to fly"), " rather than a battery threshold; ",
                      b("hand over from manual to autonomous control explicitly"), ", or the drone accepts the "
                      "take-off command and never spins a motor; and ", b("wait for the position estimate to "
                      "settle"), " rather than a fixed pause — an early reading once put the take-off target "
                      "0.14 m below the floor.")]),
        *story("2.2", "Hold position and height steadily", kind="MVP", owner="Samuel, Hannah",
               estimate="10 h (Samuel) + 5 h (Hannah)", status="Sprint 1",
               want="As an operator, I want the drone to hold its position and height steadily indoors, so "
                    "that every reading and image is taken from the same spot.",
               tasks=["Confirm the Lighthouse deck is detected; measure the base-station geometry; confirm the position estimate settles."],
               accept=["The pre-flight check reports the estimate settled under 2 cm.",
                       "Drift stays under 10 cm during a 10 s hover."],
               how=[p("How we will do it: ", code("./dronedeck check"), " to confirm the deck is seen (on 23 "
                      "September it read as absent until the boards were reseated), ",
                      code("./dronedeck geometry --distance 1.0"), " to measure the stations, then ",
                      code("./dronedeck check"), " again for the settle. Drift is read from the flight trace "
                      "the agent writes beside every flight, not judged by eye."),
                    p("One more rule: the Lighthouse's zero is ", b("not"), " the floor — measured floor level "
                      "ranged from 0.85 to 1.40 m in the same room. Height is always measured from the floor "
                      "captured at take-off.")]),
        *story("2.3", "A virtual fence", kind="MVP", owner="Samuel", estimate="3 h", status="Built",
               want="As an operator, I want a programmed virtual fence around the flight area, so that the "
                    "drone can never leave the safe space.",
               tasks=["A geofence of ±2.0 m and a 1.0 m ceiling, checked every 0.1 s in flight; a breach lands the drone."],
               accept=["A route with a point outside the fence is refused before take-off.",
                       "Crossing the fence in flight lands the drone."],
               how=[p("Built into the agent's guards, which also watch for a tumble, lost positioning, drift, "
                      "height error, stale telemetry and the battery — each a verdict with an action and a "
                      "reason. Routes are checked against the fence, and against a map of known obstacles, "
                      "before a motor spins.")]),
        *story("2.4", "Point A to Point B by itself", kind="MVP", owner="Samuel, Hannah",
               estimate="7 h + 4 h, twice", status="Sprint 1",
               want="As an operator, I want the drone to fly a predefined inspection route from one marked "
                    "point of interest to another by itself, so that it can reach a piece of equipment to inspect.",
               tasks=["Take off, go to Point A, then Point B, 1 m apart.",
                      "Land at Point B without manual piloting, using the mission waypoint model already in the agent."],
               accept=["The drone arrives within 10 cm of Point B, three runs in a row.",
                       "At Point B it holds 5 s and lands with no manual input, three runs in a row."],
               how=[p("How we will do it: the agent already has a waypoint model and a go-to command. The work "
                      "is flying it on the Lighthouse, measuring arrival error from the trace, and repeating "
                      "until three consecutive runs pass.")]),
        *story("2.5", "Plan a route of inspection points", kind="MVP", owner="Samuel, Reagan, Kevin",
               estimate="5 h + 10 h", status="Planner built · pickup Sprint 3",
               want="As an operator, I want to plan an inspection route by picking the points of interest "
                    "in order, so that the drone visits every piece of equipment.",
               tasks=["A mission planner on the website that builds and validates the route.",
                      "The flight agent picks up routes queued on the website."],
               accept=["The planner can build and validate a route.",
                       "A route queued on the website is flown by the desktop app; points outside the fence are rejected."],
               how=[p("Built: the Plan page. You click the areas in the order to visit them, set the altitude and "
                      "hold time, and queue it. The server rebuilds the route from its own table of areas "
                      "rather than trusting what the browser sent, and validates it."),
                    p("To build: the agent claiming a queued route. The database function that hands a route to "
                      "exactly one agent exists; nothing calls it yet. Routes start from the desktop app until then.")]),
        *story("2.6", "Return to base when the mission ends", kind="MVP", owner="Hannah, Samuel",
               estimate="6 h each", status="Sprint 2",
               want="As an operator, I want the drone to return to base and land safely when the mission "
                    "ends, so that it is ready for the next run.",
               tasks=["A final return-to-base leg on every mission, landing on the starting point."],
               accept=["After the last point, the drone lands within 20 cm of where it took off."],
               how=[p("How we will do it: the take-off position is already captured for every flight; the "
                      "mission gains a last leg back to it.")]),
        *story("2.7", "Return to base on low battery", kind="Stretch", owner="Hannah, Yordanos",
               estimate="10 h + 8 h, then 14 h + 12 h", status="Sprint 4",
               want="As an operator, I want the drone to fly back to base and land safely when its battery "
                    "runs low, so that it never drops out of the air mid-mission.",
               tasks=["Define a low-battery threshold and detect it.",
                      "Return to base and land when it is reached."],
               accept=["Below the threshold (under 30), the drone reports it.",
                       "It then stops the mission, returns to base, and lands within 20 cm of where it took off."],
               how=[p("A battery guard already exists — today it lands the drone where it is. This adds "
                      "returning first, with enough margin to get home.")]),
        *story("2.8", "Avoid walls, people and equipment", kind="Stretch", owner="Kevin",
               estimate="14 h + 16 h", status="Sprint 5",
               want="As an operator, I want the drone to avoid collisions with walls, people and equipment, "
                    "so that it does not collide during a mission.",
               tasks=["Fit a range sensor for live detection.",
                      "Basic obstacle detection; routes are already checked against a map of known obstacles."],
               accept=["A route through a known obstacle is refused.",
                       "The drone stops before an unexpected obstacle."],
               how=[p("The known-obstacle check is built. Live detection needs hardware: the drone reports no "
                      "range-finding deck fitted today.")]),
        h2("What is left"),
        li("Sprint 1: measure positioning; fly 2.1, 2.2 and 2.4 until they pass."),
        li("Sprint 2: return to base (2.6). Sprint 3: missions from the website (2.5)."),
        li("Stretch: low-battery return (2.7) and obstacle avoidance (2.8)."),
        plan_link(),
    ],
}

# ══════════════════════════════════════════════════════════════════════════
# 3 · Data collection
# ══════════════════════════════════════════════════════════════════════════

DATA: dict[str, Any] = {
    "slug": "data-collection",
    "title": "Data collection",
    "subtitle": "Part 3 · Temperature, pressure, position and images — recorded, tagged and kept safe",
    "summary": "Every reading ten times a second and greyscale images from the drone's camera, "
               "written to the laptop first and uploaded after. Next: capturing at each inspection "
               "point, and tagging every reading with the point it belongs to.",
    "category": "Sensing",
    "date_label": "Built 16–27 Sep · Sprint 2",
    "location": "Led by Kevin Loi",
    "cover_image_url": "/projects/data-collection.webp",
    "members": [KEVIN, SAMUEL],
    "content": [
        h2("What this part is"),
        p("An inspection is only as good as what it records. This part is what the drone measures, "
          "how each measurement is labelled, and how it gets from the drone to the database without "
          "ever being lost. The recording and upload are built; what remains is making the drone "
          "record the same way at every inspection point, and labelling each reading with that point."),
        table(STORY_HEADER, [
            ["3.1", "Temperature and pressure through every flight", "MVP", "Samuel", "2 h", "Built"],
            ["3.2", "Greyscale images from the camera", "MVP", "Samuel", "2 h", "Built"],
            ["3.3", "Data sent off the drone and stored safely", "MVP", "Samuel", "2 h", "Built"],
            ["3.4", "Capture at each inspection point", "MVP", "Kevin", "16 h", "Sprint 2"],
            ["3.5", "Every reading tagged with time and point", "MVP", "Samuel, Kevin", "3 h + 6 h", "Time built · point ID Sprint 2"],
        ]),
        img("/projects/data-collection.webp", "Sensors, wired and read. (Stock photograph.)"),
        h2("The user stories"),
        *story("3.1", "Temperature and pressure through every flight", kind="MVP", owner="Samuel",
               estimate="2 h", status="Built",
               want="As an inspector, I want temperature and pressure recorded at inspection points "
                    "throughout every flight, so that I can see the conditions around each piece of equipment.",
               tasks=["Write raw and corrected temperature, pressure and position, ten times a second, to a file."],
               accept=["Every flight produces a file of timestamped temperature, pressure and position rows, with proper units."],
               how=[p("The temperature sensor sits on a board that heats itself as it runs, so the raw reading "
                      "is the board, not the room. A thermal correction separates the two — its constants are "
                      "the previous team's tuned values — and both raw and corrected are stored. Distances are "
                      "metres internally everywhere; the temperature is stored in the unit the operator chose, "
                      "with the unit recorded in the row.")]),
        *story("3.2", "Greyscale images from the drone's camera", kind="MVP", owner="Samuel", estimate="2 h",
               status="Built",
               want="As an inspector, I want greyscale images of inspected equipment from the drone's camera, "
                    "so that equipment can be checked visually.",
               tasks=[("The AI deck camera sends 324×244 greyscale frames over Wi-Fi (about 3.7 a second); the "
                      "frames are saved to disk and uploaded.")],
               accept=["A session's frames are saved on the laptop and can be viewed later on the website."],
               how=[p("About two frames a second are written to disk during a session, with an index of when "
                      "each was taken, and uploaded to private storage afterwards. The website shows them per flight.")]),
        *story("3.3", "Data sent off the drone and stored safely", kind="MVP", owner="Samuel", estimate="2 h",
               status="Built",
               want="As an inspector, I want the collected data wirelessly sent off the drone to an external "
                    "program during or immediately after flight, and then stored safely so that nothing is "
                    "lost when a flight ends.",
               tasks=["Radio to laptop, file written first; then upload to the database, retrying until it succeeds."],
               accept=["Flight data appears on the website after the flight, even if the internet was down during it."],
               how=[p("A local write cannot fail, and a flight cannot be re-run — so every reading is written to "
                      "a file on the laptop before anything else. Records wait in an outbox that retries every "
                      "60 seconds; ids are made on the laptop and every write is an upsert, so a re-send after "
                      "a dropped connection never creates a duplicate.")]),
        *story("3.4", "Capture at each inspection point", kind="MVP", owner="Kevin", estimate="16 h",
               status="Planned · Sprint 2",
               want="As an inspector, I want the drone to capture images and readings while it holds at each "
                    "inspection point, so that every piece of equipment is recorded the same way.",
               tasks=["Trigger a consistent capture of images and readings during the hold at each inspection point."],
               accept=["At least 10 frames and 5 seconds of readings are saved for every inspection point on the route."],
               how=[p("How we will build it: the mission already holds at each waypoint for a set time. The "
                      "capture opens with the hold and closes with it, and a point that comes back short is "
                      "marked incomplete rather than silently accepted.")]),
        *story("3.5", "Every reading tagged with its time and point", kind="MVP", owner="Samuel, Kevin",
               estimate="3 h + 6 h", status="Time and position built · point ID Sprint 2",
               want="As an inspector, I want every collected reading and image tagged with a timestamp and "
                    "associated inspection point ID, so that I know exactly where and when it was taken.",
               tasks=["Record the timestamp and drone position with every reading (Samuel).",
                      "Add the inspection point's ID while the drone holds there (Kevin)."],
               accept=["Every reading carries a timestamp and the drone's position.",
                       "Every reading captured at a point carries that point's unique ID."],
               how=[p("Every row already carries the time and the drone's x, y and z. The point ID joins them "
                      "once the capture window of 3.4 exists — the pipeline in Part 4 groups by it.")]),
        h2("What is left"),
        li("3.4 and the point ID of 3.5, in Sprint 2 — the inputs Part 4's pipeline needs."),
        plan_link(),
    ],
}

# ══════════════════════════════════════════════════════════════════════════
# 4 · Anomaly detection and alerts
# ══════════════════════════════════════════════════════════════════════════

ANOMALY: dict[str, Any] = {
    "slug": "anomaly-detection",
    "title": "Anomaly detection and alerts",
    "subtitle": "Part 4 · From a recorded flight to a verdict for every piece of equipment",
    "summary": "One pipeline per flight — clean the readings, sharpen the images, classify, interpret — "
               "ending in a verdict for each inspection point and an alert with its reason. Training "
               "data, data cleaning and the first classifier start in Sprint 1.",
    "category": "Data",
    "date_label": "Sprint 1 → Sprint 5",
    "location": "Led by Reagan Gary",
    "cover_image_url": "/projects/data-processing.webp",
    "members": [REAGAN, KEVIN, HANNAH, YORDANOS, SAMUEL],
    "content": [
        h2("What this part is"),
        p("This is where a recording becomes a finding. Every flight's readings and images go through "
          "one pipeline — clean, enhance, classify, interpret — and come out as a verdict for each "
          "inspection point: normal, or flagged, with the reason. The flags become alerts on the "
          "dashboard (Part 1, story 1.9)."),
        p("It starts this sprint. The previous team's model cannot be reused: 1,280 of its 1,291 "
          "inputs are an image embedding of that team's own photographs, and most of the rest are "
          "outdoor weather readings. So the training data is ours, of our own test equipment (Part 6)."),
        table(STORY_HEADER, [
            ["4.1", "Labelled images of normal and faulty equipment", "MVP", "Hannah, Yordanos", "2 h + 5 h", "Sprint 1"],
            ["4.2", "Bad sensor readings removed", "MVP", "Kevin", "8 h", "Sprint 1"],
            ["4.3", "Clearer images", "MVP", "Kevin, Reagan", "12 h each + 10 h", "Sprint 1"],
            ["4.4", "An image classifier: faulty or not", "MVP", "Reagan", "12 h", "Sprint 1"],
            ["4.5", "One pipeline per flight", "MVP", "Kevin, Reagan", "10 h each", "Sprint 2"],
            ["4.6", "Readings against normal limits, with alerts", "MVP", "Kevin, Reagan", "4 h + 14 h", "Sprint 2"],
            ["4.7", "Change against a baseline image", "Stretch", "Reagan, Kevin, Samuel", "14 h each", "Sprint 4"],
            ["4.8", "Images, readings and history together", "Stretch", "Reagan, Samuel", "12 h each", "Sprint 5"],
            ["4.9", "Anomalies flagged during the flight", "Stretch", "Samuel, Reagan, Kevin", "12 + 12 + 15 h", "Sprint 4"],
            ["4.10", "A second, closer pass over a flagged point", "Stretch", "Yordanos, Hannah", "10 h + 12 h", "Sprint 5"],
        ]),
        h2("The pipeline, end to end"),
        p("Its input already exists: every flight's readings file and camera frames are in storage, "
          "uploaded by Part 3, each reading with its time and position. The pipeline:"),
        li(b("Load"), " a flight's readings and frames from storage."),
        li(b("Clean"), " — mark and remove readings from a misbehaving sensor (4.2)."),
        li(b("Enhance"), " — raise the resolution of the 324×244 frames, and colourise them (4.3)."),
        li(b("Classify"), " — an image model (4.4) and a sensor model against normal limits (4.6)."),
        li(b("Interpret"), " — one verdict per inspection point, with the reason, saved for the dashboard (4.5)."),
        img("/projects/data-processing.webp", "Each flight ends in a verdict per point, not a spreadsheet. (Stock photograph.)"),
        h2("The user stories"),
        *story("4.1", "Labelled images of normal and faulty equipment", kind="MVP", owner="Hannah, Yordanos",
               estimate="2 h (Hannah) + 5 h (Yordanos)", status="Sprint 1",
               want="As a model developer, I want labeled images of the test equipment in normal and faulty "
                    "states, so that a model can learn the difference.",
               tasks=["Photograph the test machine in stable and faulty conditions, from several angles and positions."],
               accept=["A labelled image set with both classes, from several angles and positions."],
               how=[p("The equipment is the test boxes of Part 6, with their written definition of faulty.")]),
        *story("4.2", "Bad sensor readings removed", kind="MVP", owner="Kevin", estimate="8 h", status="Sprint 1",
               want="As an inspector, I want erroneous sensor readings removed, so that a faulty sensor is "
                    "not mistaken for faulty equipment.",
               tasks=["A method for identifying outliers and gaps when a sensor misbehaves, marking and removing them."],
               accept=["Erroneous values planted in a flight data file are flagged; correct rows are left untouched."],
               how=[p("Tested the way the acceptance criterion says: plant known-bad values in a real flight file "
                      "and check exactly those, and nothing else, are flagged.")]),
        *story("4.3", "Clearer images", kind="MVP", owner="Kevin, Reagan", estimate="12 h each; colour 10 h (Kevin)",
               status="Sprint 1",
               want="As an inspector, I want clearer images, so that even small defects are visible.",
               tasks=["A model or program that raises the quality and resolution of the 324×244 greyscale frames.",
                      "A model or program that colourises them after the fact (low priority)."],
               accept=["Before-and-after on real frames from the drone shows visibly more detail.",
                       "Greyscale images show the colour of objects accurately."],
               how=[p("Judged on the drone's own frames, not sample images — the camera's quirks are the problem being solved.")]),
        *story("4.4", "An image classifier: faulty or not", kind="MVP", owner="Reagan", estimate="12 h",
               status="Sprint 1",
               want="As an inspector, I want a model that looks at an image and can define it as faulty or "
                    "not, so that defects are found automatically.",
               tasks=["Train a simple classification model on the labelled images (faulty vs. not faulty)."],
               accept=["Accuracy reported on a held-out test set, with the misclassified examples reviewed."],
               how=[p("Simple first, measured honestly: a held-out set the model never trained on, and every "
                      "mistake looked at.")]),
        *story("4.5", "One pipeline per flight", kind="MVP", owner="Kevin, Reagan", estimate="10 h each",
               status="Sprint 2",
               want="As an inspector, I want each flight's images and sensor readings to go through one "
                    "pipeline (clean, enhance, classify, interpret), so that every flight ends with a verdict "
                    "for each piece of equipment without manual steps.",
               tasks=["One command that loads a flight's readings and frames from storage, runs the steps in order, and saves the result."],
               accept=["A single command turns a recorded flight into a verdict for each inspection point."],
               how=[p("The steps above, in order, keyed by the inspection-point IDs Part 3 adds.")]),
        *story("4.6", "Readings against normal limits, with alerts", kind="MVP", owner="Kevin, Reagan",
               estimate="4 h (Kevin) + 14 h (Reagan)", status="Sprint 2",
               want="As an inspector, I want an external program to compare sensor readings against defined "
                    "normal operating thresholds, so that overheating, pressure changes, or other anomalies are "
                    "flagged, with the reason, so that I can act on it and review it later.",
               tasks=["Define normal thresholds, then train a classifier on sensor data that flags deviations offline, with the reason."],
               accept=[("An anomaly is flagged and an alert shown with the original reading or image — a box "
                       "heated with a hand warmer is flagged; an unheated box is not.")],
               how=[p("The hand-warmer test is Part 6's: a known fault, made on purpose, checked against a "
                      "thermometer beside the box.")]),
        *story("4.7", "Change against a baseline image", kind="Stretch", owner="Reagan, Kevin, Samuel",
               estimate="14 h each", status="Sprint 4",
               want="As an inspector, I want visual changes in captured images flagged against a baseline "
                    "reference image, so that a change in the equipment is noticed even without a trained fault class.",
               tasks=["Store a baseline image per inspection point and compare each new image against it."],
               accept=["A visible change to a box is flagged; the same box unchanged is not."], how=[]),
        *story("4.8", "Images, readings and history together", kind="Stretch", owner="Reagan, Samuel",
               estimate="12 h each", status="Sprint 5",
               want="As an inspector, I want the model to use images and sensor data together, plus the "
                    "equipment's history, so that its verdicts are more reliable.",
               tasks=["Combine the image and sensor models with past readings for the same equipment."],
               accept=["The combined model is at least as accurate as either model alone on the test set."], how=[]),
        *story("4.9", "Anomalies flagged during the flight", kind="Stretch", owner="Samuel, Reagan, Kevin",
               estimate="12 h + 12 h + 15 h", status="Sprint 4",
               want="As an operator, I want anomalies flagged during the flight, not after it, so that the "
                    "mission can react right away.",
               tasks=["Run the classifier on live data as it arrives."],
               accept=["An alert appears while the drone is still at that point — a heated box flagged in real time."],
               how=[p("The live readings are already on the laptop at 10 Hz (Part 1, story 1.4); this runs the "
                      "model on them as they arrive.")]),
        *story("4.10", "A second, closer pass over a flagged point", kind="Stretch", owner="Yordanos, Hannah",
               estimate="10 h + 12 h", status="Sprint 5",
               want="As a user, I want the drone to alter its mission in real time and inspect a flagged point "
                    "again (a second, closer pass) when a sensor finds an issue, then return to its base.",
               tasks=["The steps for the drone to revisit a flagged location, collect new data, and return to the route."],
               accept=["A valid warning triggers the revisit; old, repeated or erroneous warnings do not.",
                       "New data is collected, the drone continues to the next waypoint, and failed checks are recorded."],
               how=[]),
        h2("What is left"),
        li("All of it — this part starts in Sprint 1 with 4.1 to 4.4, and 4.5 and 4.6 follow in Sprint 2."),
        plan_link(),
    ],
}

# ══════════════════════════════════════════════════════════════════════════
# 5 · Documentation
# ══════════════════════════════════════════════════════════════════════════

DOCS: dict[str, Any] = {
    "slug": "documentation",
    "title": "Documentation",
    "subtitle": "Part 5 · So that someone outside the team can run a mission",
    "summary": "Setup, operating and waypoint guides written for someone who has never seen the drone, "
               "and a record of every decision and problem kept each sprint.",
    "category": "Documentation",
    "date_label": "Every sprint",
    "location": "Led by Yordanos Tessema",
    "cover_image_url": "/projects/documentation.webp",
    "members": [YORDANOS, HANNAH, SAMUEL],
    "content": [
        h2("What this part is"),
        p("The test of this part is simple: someone outside the team picks up the guides and runs a "
          "mission, safely, without asking us anything. Three guides do that — setup, operating, and "
          "waypoints for a new space — and a running record explains how the system got the way it is."),
        table(STORY_HEADER, [
            ["5.1", "Decisions, changes and problems recorded", "MVP", "Yordanos", "2 h each sprint", "Every sprint"],
            ["5.2", "Setup instructions", "MVP", "Samuel, Yordanos, Hannah", "6 h each", "Desktop guide live · Sprint 2"],
            ["5.3", "Operating instructions", "MVP", "Yordanos, Hannah", "14 h each", "Sprint 3"],
            ["5.4", "A marker and waypoint scheme", "MVP", "Yordanos, Hannah", "10 h each", "Sprint 4"],
        ]),
        h2("The user stories"),
        *story("5.1", "Decisions, changes and problems recorded", kind="MVP", owner="Yordanos",
               estimate="2 h each sprint", status="Every sprint",
               want="As a user, I want documentation of the project, so that I can understand how it was implemented.",
               tasks=["Summarise the main software and hardware decisions, changes, current status and challenges each sprint."],
               accept=["Clear and complete notes on the main choices and problems, with unresolved issues marked."],
               how=[p("There is a lot to draw from: the code repository carries a document per feature, and a "
                      "flight log of every lab session — what was flown, what it measured, and what changed "
                      "because of it.")]),
        *story("5.2", "Setup instructions", kind="MVP", owner="Samuel, Yordanos, Hannah", estimate="6 h each",
               status="Desktop guide live · rest Sprint 2",
               want="As a user, I want set-up and operating instructions for the mini-drone system, so that I "
                    "can prepare the drone for a mission.",
               tasks=["Detailed instructions for hardware and software setup, and the safety checks before a flight."],
               accept=["A person outside the team can follow them and connect to the drone properly and safely."],
               how=[p("Live on this site: the ", link("desktop app setup guide", "/setup/desktop-app"),
                      " — what is in the box, the one install command for macOS, Windows and Linux, plugging "
                      "in the radio, positioning, the pre-flight checks, a list of useful commands, and "
                      "troubleshooting symptom by symptom — and a ", link("labelled photograph of every part",
                      "/setup/hardware"), ". The team adds the hardware and safety detail.")]),
        *story("5.3", "Operating instructions", kind="MVP", owner="Yordanos, Hannah", estimate="14 h each",
               status="Sprint 3",
               want="As a user, I want simple and clear instructions on how to operate the drone safely.",
               tasks=["How to start, monitor, land and end a mission, with fixes for common problems."],
               accept=["A user can run a mission with the guide, and find safe actions for any failed mission."], how=[]),
        *story("5.4", "A marker and waypoint scheme", kind="MVP", owner="Yordanos, Hannah", estimate="10 h each",
               status="Sprint 4",
               want="As a user, I want a defined scheme of markers and waypoints, so that I can operate the drone in a new indoor space.",
               tasks=["How to name, place and test markers and waypoints in a new indoor space."],
               accept=["Each marker has one waypoint inside the flight area; missing, repeated or blocked points are not accepted."],
               how=[p("The rule it builds on: waypoints are absolute Lighthouse positions, with height measured "
                      "from the floor at take-off — so moving a base station means measuring again.")]),
        h2("What is left"),
        li("Sprint 2: the rest of the setup guide. Sprint 3: the operating guide. Sprint 4: the waypoint guide."),
        plan_link(),
    ],
}

# ══════════════════════════════════════════════════════════════════════════
# 6 · Testing and experiment design
# ══════════════════════════════════════════════════════════════════════════

TESTING: dict[str, Any] = {
    "slug": "testing",
    "title": "Testing and experiment design",
    "subtitle": "Part 6 · A fault we make on purpose, and an instrument to check the drone against",
    "summary": "Boxes standing in for equipment, a written definition of faulty, a hand warmer for an "
               "abnormal temperature, and a thermometer and barometer to confirm what the drone measured.",
    "category": "Testing",
    "date_label": "Sprint 1 → Sprint 3",
    "location": "Shared — Hannah, Yordanos, Samuel",
    "cover_image_url": "/projects/testing.webp",
    "members": [HANNAH, YORDANOS, SAMUEL],
    "content": [
        h2("What this part is"),
        p("A detector is only trustworthy if it has caught a fault you know is there. This part builds "
          "that fault: simulated equipment, a definition of when it counts as faulty, a way to make it "
          "faulty on purpose, and independent instruments to confirm the drone saw a real change."),
        table(STORY_HEADER, [
            ["6.1", "Simulated equipment, and a definition of faulty", "MVP", "Hannah, Yordanos", "5 h + 10 h; 1 h each", "Sprint 1"],
            ["6.2", "Independent thermometer and barometer readings", "MVP", "Hannah, Yordanos", "3 h + 6 h", "Sprint 1"],
            ["6.3", "Abnormal temperatures on purpose", "MVP", "Hannah, Yordanos", "12 h + 16 h", "Sprint 2–3"],
            ["6.4", "The drone ready for flight and data collection", "MVP", "Samuel", "4 h", "Built"],
        ]),
        h2("The user stories"),
        *story("6.1", "Simulated equipment, and a definition of faulty", kind="MVP", owner="Hannah, Yordanos",
               estimate="5 h (Hannah) + 10 h (Yordanos); 1 h each for the definitions", status="Sprint 1",
               want="As the team, we want simulated equipment built from boxes, with a definition of \"faulty\", "
                    "so that we can test inspection without real machinery.",
               tasks=["Build the physical equipment model.", "Write preliminary definitions of \"faulty\"."],
               accept=["Test boxes are placed inside the flight area.", "Each box has a written normal and faulty definition."],
               how=[]),
        *story("6.2", "Independent thermometer and barometer readings", kind="MVP", owner="Hannah, Yordanos",
               estimate="1.5 h each to acquire; 1.5 h + 4.5 h of readings", status="Sprint 1",
               want="As the team, we want independent thermometer and barometer readings, so that we can "
                    "confirm the drone measured a real change.",
               tasks=["Acquire a thermometer and barometer.", "Take reference readings beside the test boxes."],
               accept=["Both instruments acquired and tested.",
                       "Reference readings recorded next to the drone's, for the same spot and time."],
               how=[p("This matters more than it sounds: the drone's temperature sensor shares a board with "
                      "electronics that heat up, and its readings are corrected for that (Part 3). An "
                      "independent instrument is how we know the correction is right in this room.")]),
        *story("6.3", "Abnormal temperatures on purpose", kind="MVP", owner="Hannah, Yordanos",
               estimate="12 h (Hannah) + 16 h (Yordanos)", status="Sprint 2–3",
               want="As the team, we want to create abnormal temperatures on purpose, so that we can check "
                    "that the system catches them.",
               tasks=["Simulate abnormal temperature readings with a hand warmer."],
               accept=["The drone's reading near the heated box differs from the unheated one by a measurable amount."],
               how=[p("This is the fault Part 4's alert (4.6) must catch, and Part 4's real-time stretch goal (4.9) catch in flight.")]),
        *story("6.4", "The drone ready for flight and data collection", kind="MVP", owner="Samuel",
               estimate="4 h", status="Built",
               want="As a contributor, I want the drone confirmed ready for flight and data collection, so "
                    "that test time is not wasted.",
               tasks=["The drone flies stably with manual controls; automated tests run on every change."],
               accept=["Manual flight works; the flight agent's 695 automated tests pass."],
               how=[p("Every lab session follows the same ladder, and writes what it measured to the flight log: ",
                      code("./dronedeck check"), " (every check, no motor spins), ", code("./dronedeck stations"),
                      " (which base stations the drone receives), ", code("./dronedeck geometry"),
                      " (where they are), then a short ", code("./dronedeck hover"),
                      ". A number with no units is not a measurement; a conclusion with no number is an opinion.")]),
        h2("What is left"),
        li("Sprint 1: the boxes, the definitions and the reference readings. Sprints 2–3: the hand-warmer fault."),
        plan_link(),
    ],
}

PROJECTS = [OVERVIEW, APPS, FLIGHT, DATA, ANOMALY, DOCS, TESTING]


# ── writing it ──────────────────────────────────────────────────────────────


class Api:
    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key

    def request(self, method: str, path: str, body: Any = None, *, headers: dict[str, str] | None = None) -> Any:
        data = json.dumps(body).encode() if body is not None else None
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("apikey", self.key)
        req.add_header("Authorization", f"Bearer {self.key}")
        req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:300]}")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--apply", action="store_true", help="write it; without this, only show the plan")
    parser.add_argument("--dump", action="store_true", help="print the projects as JSON and exit (no network)")
    args = parser.parse_args()

    if args.dump:
        print(json.dumps(PROJECTS))
        return

    base, key = os.environ.get("SUPABASE_API"), os.environ.get("SUPABASE_SERVICE")
    if not base or not key:
        sys.exit("Set SUPABASE_API and SUPABASE_SERVICE (the secret key) in the environment.")
    api = Api(base, key)

    members = {m["slug"]: m["id"] for m in api.request("GET", "/rest/v1/team_members?select=id,slug")}
    wanted = {s for proj in PROJECTS for s in proj["members"]}
    if missing := sorted(wanted - members.keys()):
        sys.exit(f"No team member with slug {', '.join(missing)} — refusing to write anything.")
    existing = {r["slug"]: r for r in api.request("GET", "/rest/v1/projects?select=id,slug,status")}
    for slug in NEVER_TOUCH:
        assert all(proj["slug"] != slug for proj in PROJECTS) and slug not in RETIRE

    print("\n  " + ("Writing" if args.apply else "Plan (dry run — add --apply to write)") + ":\n")
    for order, proj in enumerate(PROJECTS, start=1):
        print(f"  {'update' if proj['slug'] in existing else 'create'}  {proj['slug']:<24} "
              f"{len(proj['content']):>3} blocks · {len(proj['members'])} people · order {order}")
    for slug in RETIRE:
        if slug in existing and existing[slug]["status"] != "draft":
            print(f"  draft   {slug}")
    print(f"  keep    {', '.join(sorted(NEVER_TOUCH & existing.keys())) or '(none)'}\n")
    if not args.apply:
        return

    now = dt.datetime.now(dt.UTC).isoformat()
    for order, proj in enumerate(PROJECTS, start=1):
        row = {k: v for k, v in proj.items() if k != "members"}
        row |= {"status": "published", "display_order": order}
        if proj["slug"] in existing:
            api.request("PATCH", f"/rest/v1/projects?slug=eq.{proj['slug']}", row)
            project_id = existing[proj["slug"]]["id"]
        else:
            created = api.request("POST", "/rest/v1/projects", row | {"published_at": now},
                                  headers={"Prefer": "return=representation"})
            project_id = created[0]["id"]
        api.request("DELETE", f"/rest/v1/project_members?project_id=eq.{project_id}")
        api.request("POST", "/rest/v1/project_members", [
            {"project_id": project_id, "member_id": members[s], "display_order": i}
            for i, s in enumerate(proj["members"])
        ])
        print(f"  ✓ {proj['slug']}")
    for slug in RETIRE:
        if slug in existing:
            api.request("PATCH", f"/rest/v1/projects?slug=eq.{slug}", {"status": "draft"})
    print("  ✓ starter projects set to draft\n")


if __name__ == "__main__":
    main()
