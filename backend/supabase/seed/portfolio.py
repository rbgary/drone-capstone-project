"""Fill the public portfolio: the team, their photos, and starter projects.

Run once against a project that has migration 0007:

    SUPABASE_API=https://<ref>.supabase.co SUPABASE_SERVICE=<secret key> \\
    python backend/supabase/seed/portfolio.py --samuel-photo "<path to headshot.png>"

Uses the SECRET key, so it runs on a developer's machine only — never commit
the key, never ship this. Idempotent: team members are matched by name and
projects by slug, so re-running updates rather than duplicates. It overwrites
the fields it sets, so once someone edits a project in /admin, edit there.

The projects are starter content drawn from this repository's own docs, for the
team to rewrite in the admin. Team roles, emails and websites that were not
given are left blank rather than invented.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

BUCKET = "portfolio-images"

# Display order is the order here — Samuel last, as asked.
TEAM: list[dict[str, Any]] = [
    {"full_name": "Yordanos"},
    {"full_name": "Hannah"},
    {"full_name": "Kevin"},
    {"full_name": "Reagan"},
    {"full_name": "Samuel Enam Zih", "website_url": "https://samuelzih.savorar.com/"},
]


# ── BlockNote builders (same shape as ../doctor-portfolio/scripts/content/blocks.mjs)


def t(text: str, **styles: bool) -> dict[str, Any]:
    return {"type": "text", "text": text, "styles": styles}


def h2(text: str) -> dict[str, Any]:
    return {"type": "heading", "props": {"level": 2}, "content": [t(text)]}


def p(text: str) -> dict[str, Any]:
    return {"type": "paragraph", "content": [t(text)]}


def li(text: str) -> dict[str, Any]:
    return {"type": "bulletListItem", "content": [t(text)]}


PROJECTS: list[dict[str, Any]] = [
    {
        "slug": "cropwatcher",  # as created; "dronedeck" is the overview (dronedeck_projects.py)
        "title": "CropWatcher",
        "subtitle": "Autonomous indoor crop-health scouting on a Crazyflie 2.1",
        "summary": "A palm-sized drone that flies a greenhouse, records position-tagged "
                   "environmental data, and turns it into zone-level crop-health estimates.",
        "category": "Systems",
        "date_label": "2026",
        "content": [
            h2("Overview"),
            p("CropWatcher flies autonomous scouting missions over a greenhouse, logs "
              "position-tagged environmental readings, and surfaces them as zone-level "
              "crop-health estimates in a web dashboard."),
            h2("The constraint everything follows from"),
            p("The Crazyradio is a USB dongle, so only the machine it is plugged into can "
              "command the drone. No cloud service can reach it. The flight agent runs on "
              "that laptop; the dashboard reads what it records."),
            h2("What it is built from"),
            li("A Crazyflie 2.1 with a Lighthouse positioning deck."),
            li("A Python flight agent that owns the radio."),
            li("A Tauri desktop app for flying, and a Next.js dashboard for the history."),
            li("Supabase for accounts, flight records and the audit trail."),
        ],
    },
    {
        "slug": "flight-safety",
        "title": "Flight safety after the lab crash",
        "subtitle": "Checks before every flight, guards during it",
        "summary": "A drone flew blind into a wall. The fix: count base stations that are "
                   "received, not stored, and watch every sample in the air.",
        "category": "Safety",
        "date_label": "2026",
        "content": [
            h2("What happened"),
            p("During a hover test the drone flew sideways into a wall. Its own data showed "
              "why: four base stations were stored, none were being received, and the "
              "preflight had counted the stored ones."),
            h2("The checks"),
            li("Battery: the firmware's own sys.canfly, never a guessed voltage."),
            li("Positioning: stations received, calibrated and with geometry."),
            li("A settled estimate on all three axes, not height alone."),
            h2("The guards"),
            p("In the air, every telemetry sample is checked for a tumble, lost positioning, "
              "drift, height error, the geofence and a low battery — and the drone lands "
              "or stops by itself."),
        ],
    },
    {
        "slug": "desktop-flight-app",
        "title": "The desktop flight app",
        "subtitle": "One install, the flight agent bundled inside",
        "summary": "A Tauri app with auto and manual modes, live sensor windows, session "
                   "history and a hold-to-activate emergency stop.",
        "category": "Software",
        "date_label": "2026",
        "content": [
            h2("Why a desktop app"),
            p("The laptop holding the radio is the only place the drone can be flown from, "
              "so the flying happens in a desktop app that bundles the Python agent as a "
              "sidecar. An operator installs one thing and never sees Python."),
            h2("Flying it"),
            li("Auto: a start test that spins each motor, and a hover test."),
            li("Manual: held keys move a height target; letting go holds it."),
            li("Land is always one key away; emergency stop needs a one-second hold."),
            h2("Sessions"),
            p("Every session is recorded with who ran it, when, and what the drone "
              "reported, and each sensor window keeps a log per session."),
        ],
    },
    {
        "slug": "operator-dashboard",
        "title": "Operator dashboard and mission planner",
        "subtitle": "Recorded flights, zone health and planned missions on the web",
        "summary": "Live telemetry, flight history, zone maps, flight comparison and a "
                   "planner that queues missions for the agent to fly.",
        "category": "Web",
        "date_label": "2026",
        "content": [
            h2("What it shows"),
            p("The dashboard reads what the desktop app records: live telemetry, recorded "
              "flights, zone health and side-by-side comparisons."),
            h2("Planning a mission"),
            p("Operators click zones in the order to visit them, set altitude and hold "
              "time, and queue the mission. The server rebuilds and validates the plan "
              "itself before the agent may claim it."),
        ],
    },
    {
        "slug": "temperature-correction",
        "title": "Correcting a self-heating barometer",
        "subtitle": "Separating the electronics from the room",
        "summary": "The barometer sits on a board that heats itself. A two-timescale thermal "
                   "model recovers the room's temperature from it.",
        "category": "Sensing",
        "date_label": "2026",
        "content": [
            h2("The problem"),
            p("The drone's temperature sensor reads warm: it shares a board with "
              "electronics that heat up as they run."),
            h2("The approach"),
            p("A two-timescale thermal model separates the board's own heating from the "
              "room, so a reading reflects the air around the crop rather than the drone."),
        ],
    },
    {
        "slug": "offline-flight-data",
        "title": "Flying offline",
        "subtitle": "CSV first, uploaded when the laptop is back online",
        "summary": "Every reading is written to the laptop before any network call, then "
                   "uploaded exactly once — so a flight never depends on the internet.",
        "category": "Data",
        "date_label": "2026",
        "content": [
            h2("Write first, upload later"),
            p("A local write cannot fail and a flight cannot be re-run, so every sample "
              "goes to a CSV on the laptop first. Records sit in an outbox until Supabase "
              "has them."),
            h2("Exactly once"),
            p("Ids are generated on the laptop and every write upserts, so re-sending "
              "after a dropped connection never creates a duplicate."),
        ],
    },
]


class Api:
    def __init__(self, base: str, key: str) -> None:
        self.base = base.rstrip("/")
        self.key = key

    def request(self, method: str, path: str, body: Any = None, *,
                headers: dict[str, str] | None = None, raw: bytes | None = None) -> Any:
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        req = urllib.request.Request(self.base + path, data=data, method=method)
        req.add_header("apikey", self.key)
        req.add_header("Authorization", f"Bearer {self.key}")
        if raw is None:
            req.add_header("Content-Type", "application/json")
        for k, v in (headers or {}).items():
            req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as resp:
                payload = resp.read()
                return json.loads(payload) if payload else None
        except urllib.error.HTTPError as e:
            sys.exit(f"{method} {path} failed: {e.code} {e.read().decode()[:300]}")


def upload_photo(api: Api, path: Path, name: str) -> str:
    content_type = mimetypes.guess_type(path.name)[0] or "image/png"
    object_path = f"team/{name}{path.suffix.lower()}"
    api.request("POST", f"/storage/v1/object/{BUCKET}/{object_path}", raw=path.read_bytes(),
                headers={"Content-Type": content_type, "x-upsert": "true"})
    return f"{api.base}/storage/v1/object/public/{BUCKET}/{object_path}"


def upsert_member(api: Api, member: dict[str, Any], order: int) -> str:
    name = urllib.parse.quote(member["full_name"])
    found = api.request("GET", f"/rest/v1/team_members?full_name=eq.{name}&select=id")
    row = {**member, "display_order": order}
    if found:
        member_id = found[0]["id"]
        api.request("PATCH", f"/rest/v1/team_members?id=eq.{member_id}", row)
        return str(member_id)
    created = api.request("POST", "/rest/v1/team_members", row,
                          headers={"Prefer": "return=representation"})
    return str(created[0]["id"])


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--samuel-photo", type=Path, required=True)
    args = parser.parse_args()

    base, key = os.environ.get("SUPABASE_API"), os.environ.get("SUPABASE_SERVICE")
    if not base or not key:
        sys.exit("Set SUPABASE_API and SUPABASE_SERVICE (the secret key) in the environment.")
    if not args.samuel_photo.is_file():
        sys.exit(f"No such photo: {args.samuel_photo}")
    api = Api(base, key)

    team = [dict(m) for m in TEAM]
    team[-1]["avatar_url"] = upload_photo(api, args.samuel_photo, "samuel-enam-zih")
    print(f"  photo    {team[-1]['avatar_url']}")

    member_ids = [upsert_member(api, m, i + 1) for i, m in enumerate(team)]
    print(f"  team     {len(member_ids)} members")

    for order, project in enumerate(PROJECTS, start=1):
        row = {**project, "status": "published", "display_order": order}
        saved = api.request(
            "POST", "/rest/v1/projects?on_conflict=slug", row,
            headers={"Prefer": "resolution=merge-duplicates,return=representation"},
        )
        project_id = saved[0]["id"]
        api.request("DELETE", f"/rest/v1/project_members?project_id=eq.{project_id}")
        api.request("POST", "/rest/v1/project_members", [
            {"project_id": project_id, "member_id": mid, "display_order": i}
            for i, mid in enumerate(member_ids)
        ])
        print(f"  project  {project['slug']}")


if __name__ == "__main__":
    main()
