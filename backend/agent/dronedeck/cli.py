"""Command line entry point.

For the lab and for diagnostics. The desktop app is what an operator uses; this
is what you reach for when a drone will not connect and there is no window to
click in.

    dronedeck check                       # every gate, no motors
    dronedeck proptest                    # the firmware's motor test
    dronedeck hover --height 0.3 --secs 10 --ambient 74F
    dronedeck mission --id <mission id> --dry-run
    dronedeck process --flight <flight id>   # the data pipeline
    dronedeck upscale --session <session id> # the display chain, after it
    dronedeck mission-report --flight <flight id> [--json]
    dronedeck serve                       # the local API for the desktop app

Every flight here goes through the same :class:`DroneLink` the app uses, so the
checks and the in-flight guards are identical. Nothing prompts, and nothing has
to be edited between flights.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time
from pathlib import Path
from typing import Any

from dronedeck import paths
from dronedeck.flight import core
from dronedeck.flight import geometry as geometry_estimation
from dronedeck.flight.checks import CheckResult, ChecksFailed, CheckStatus, ReadyReport, collect
from dronedeck.flight.control import FlightAborted
from dronedeck.flight.link import DroneLink, LinkError
from dronedeck.flight.manual import CLIMB_RATE_M_S, MOVE_SPEED_M_S
from dronedeck.flight.programs import HoverTest, run_hover_test
from dronedeck.mission.controller import TERMINAL_STATES, MissionController, MissionEvent
from dronedeck.mission.plan.floorplan import PlanError
from dronedeck.mission.plan.store import NotFound, PlanStore
from dronedeck.mission.plan.validate import errors, flyable_bound, outer_bound, validate_mission
from dronedeck.paths import flights_dir
from dronedeck.safety.flight_guard import Action as GuardAction
from dronedeck.safety.flight_guard import assess_positioning
from dronedeck.telemetry.reader import FlightRecorder
from dronedeck.telemetry.row import parse_ambient
from dronedeck.telemetry.sinks import CsvSink

log = logging.getLogger("dronedeck")


def _add_common(p: argparse.ArgumentParser) -> None:
    p.add_argument("--uri", default=core.DEFAULT_URI, help="Crazyflie radio URI")


def _add_flight_common(p: argparse.ArgumentParser) -> None:
    _add_common(p)
    p.add_argument(
        "--ambient", default="22C",
        help="room temperature, e.g. 74F or 22C. The unit you use here is the "
             "unit every temperature in this flight is stored in.",
    )
    p.add_argument("--fence", type=float, default=2.0,
                   help="half-extent of the square geofence, metres")
    p.add_argument("--no-log", action="store_true", help="fly without recording a CSV")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="dronedeck", description=__doc__)
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="command", required=True)

    check = sub.add_parser("check", help="run every gate; never spins a motor")
    _add_common(check)

    stations = sub.add_parser(
        "stations", help="watch which base stations the drone can see, live")
    _add_common(stations)
    stations.add_argument("--seconds", type=float, default=0.0,
                          help="stop after this long (default: until Ctrl-C)")

    geometry = sub.add_parser(
        "geometry", help="measure where the base stations are; no motors spin")
    _add_common(geometry)
    geometry.add_argument("--distance", type=float, default=1.0,
                          help="how far the x-axis sample is from the origin, in metres")
    geometry.add_argument("--quick", action="store_true",
                          help="one sample, one position, no measuring — restores "
                               "position hold now; forward/back may come out mirrored")
    geometry.add_argument("--countdown", type=float, metavar="SECONDS",
                          help="take each sample after a countdown instead of on Enter, "
                               "for when both hands are holding the drone")
    geometry.add_argument("--dry-run", action="store_true",
                          help="solve but do not write the result to the drone")

    proptest = sub.add_parser("proptest", help="the firmware's propeller test — motors spin")
    _add_common(proptest)

    hover = sub.add_parser("hover", help="take off, hold a steady height, land")
    hover.add_argument("--height", type=float, default=0.3, help="metres above the floor")
    hover.add_argument("--secs", type=float, default=10.0, help="hold duration")
    _add_flight_common(hover)

    mission = sub.add_parser(
        "mission", help="check, then fly, a mission saved on this laptop (made in the app)")
    mission.add_argument("--id", required=True, help="the mission's id")
    mission.add_argument("--dry-run", action="store_true",
                         help="check and describe the mission; never connect")
    _add_flight_common(mission)

    from dronedeck.setup_cli import add_parser as add_setup_parser
    add_setup_parser(sub)

    serve = sub.add_parser("serve", help="run the local API for the desktop app")
    serve.add_argument("--host", default="127.0.0.1",
                       help="binding beyond localhost exposes drone control")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument(
        "--exit-with-parent", action="store_true",
        help="land, cut motors and exit when stdin closes. The desktop app passes "
             "this so quitting it cannot strand an agent holding the radio.",
    )

    process = sub.add_parser(
        "process", help="run the data pipeline over a recorded flight; never connects")
    which = process.add_mutually_exclusive_group(required=True)
    which.add_argument("--flight", help="the flight's id (a session's flights list it)")
    which.add_argument("--fixture", action="store_true",
                       help="run on the test fixture in tests/pipeline (source checkout only)")
    process.add_argument("--live-point", metavar="POINT",
                         help="with --flight: the flight so far, as the drone finishes "
                              "holding at POINT (story 4.9); writes that point's verdict")
    which.add_argument("--session",
                       help="a session's id: its samples around the flights, on the ground")
    which.add_argument("--all", action="store_true",
                       help="every flight and session this laptop recorded that has no "
                            "current result")

    upscale = sub.add_parser(
        "upscale", help="the display chain over a session's captured frames: Real-ESRGAN, "
                        "then CLAHE — for showing, never as evidence; never connects")
    which_up = upscale.add_mutually_exclusive_group(required=True)
    which_up.add_argument("--session", help="the session's id")
    which_up.add_argument("--all", action="store_true",
                          help="every session on this laptop with frames not yet done")
    upscale.add_argument("--chunk", type=int, default=0, metavar="N",
                         help="with --session: at most N frames, then stop and say what is "
                              "left (the agent's queue runs it this way); 0, every one")

    calibrate = sub.add_parser(
        "calibrate",
        help="measure what a KNOWN heat source did to the sensor (the hand-warmer flights) "
             "and recommend the anomaly thresholds; never connects, never edits code")
    calibrate.add_argument("--flight", action="append", required=True,
                           help="a flight flown with the heat source; repeat for several")
    calibrate.add_argument("--at", action="append", required=True, metavar="POINT",
                           help="the inspection point the heat source sat at — one per "
                                "--flight, in the same order")

    report = sub.add_parser(
        "mission-report",
        help="what happened at each inspection point of a mission flight; never connects")
    report.add_argument("--flight", action="append", required=True,
                        help="a mission flight's id; repeat it to set the constants from "
                             "several flights")
    report.add_argument("--json", action="store_true", help="print the report as JSON")

    sub.add_parser("selftest", help="load every library the agent only loads on demand")
    return parser


# ── shared ───────────────────────────────────────────────────────────────


def _print_step(step: CheckResult) -> None:
    if step.status is CheckStatus.RUNNING:
        return
    # A warning is not a failure: the checks carry on past it, and printing
    # FAIL above a closing READY read as a contradiction.
    mark = {CheckStatus.PASSED: "ok  ", CheckStatus.WARNING: "warn"}.get(step.status, "FAIL")
    print(f"  {mark} {step.label:22} {step.detail}")


def _run_checks(link: DroneLink) -> ReadyReport:
    print("  checks:")
    return collect(link.checks(), _print_step)


def _record(link: DroneLink, report: ReadyReport, args, flight_id: str,
            point_id: Any = None) -> FlightRecorder | None:
    if args.no_log:
        return None
    ambient_c, unit = parse_ambient(args.ambient)
    sink = CsvSink(root=flights_dir(), prefix=f"flight_{flight_id}")
    assert link.stream is not None
    recorder = FlightRecorder(
        link.stream, sink, ambient_c=ambient_c, unit=unit,
        ground_z=report.ground_z_m, flight_id=flight_id, point_id=point_id,
    )
    recorder.start()
    print(f"  recording to {sink.path}")
    return recorder


def _confirm_area() -> bool:
    """The same confirmation the desktop app requires before motors turn."""
    answer = input("  Is the drone on the floor with the area clear? [y/N] ").strip().lower()
    return answer in ("y", "yes")


# ── commands ─────────────────────────────────────────────────────────────


def cmd_check(args: argparse.Namespace) -> int:
    with DroneLink(args.uri) as link:
        report = _run_checks(link)
        print(f"\n  drone      {report.hardware_id}")
        print(f"  battery    {report.vbat:.2f} V, ~{report.endurance_s:.0f}s of hover")
        print(f"  stations   {', '.join(str(s) for s in report.positioning.usable)}")
        print(f"  ground z   {report.ground_z_m:+.3f} m "
              f"(settled to {report.estimate_spread_m * 100:.1f} cm)")
        print("\n  READY")
    return 0


def cmd_stations(args: argparse.Namespace) -> int:
    """Watch the Lighthouse, live, while someone moves or powers a station.

    Written in the lab (2026-09-21) with one station reaching the drone and the
    other set up but unseen. Standing at the drone, reading what the DRONE
    reports, is the only way to tell "the LED is on" from "the drone can see
    it" — and two received stations is the difference between a drone that
    holds position and one that hops sideways.

    Reads only. No motors, no arming.
    """
    def which(mask: float | None) -> str:
        if mask is None:
            return "—"
        bits = [str(i) for i in range(16) if int(mask) >> i & 1]
        return ", ".join(bits) if bits else "none"

    with DroneLink(args.uri) as link:
        print("\n  Watching the Lighthouse. Ctrl-C to stop.\n")
        print("   received   calibrated   geometry   estimate (x, y, z)        verdict")
        deadline = time.monotonic() + args.seconds if args.seconds else None
        try:
            while deadline is None or time.monotonic() < deadline:
                snap = link.snapshot()
                status = assess_positioning(snap)
                x = snap.get("stateEstimate.x")
                y = snap.get("stateEstimate.y")
                z = snap.get("stateEstimate.z")
                where = (f"{x:+.2f} {y:+.2f} {z:+.2f}"
                         if None not in (x, y, z) else "      —      ")
                verdict = ("READY — hold position"
                           if status.ready else
                           " ".join(status.problems())[:46])
                print(f"   {which(snap.get('lighthouse.bsReceive')):<10} "
                      f"{which(snap.get('lighthouse.bsCalVal')):<12} "
                      f"{which(snap.get('lighthouse.bsGeoVal')):<10} "
                      f"{where:<24} {verdict}")
                time.sleep(1.0)
        except KeyboardInterrupt:
            print("\n  stopped\n")
    return 0


def cmd_geometry(args: argparse.Namespace) -> int:
    """Measure where the base stations are, with the drone carried by hand.

    Stale geometry is silent: the stations disagree and the estimate jumps
    rather than failing, which is what the lab drone was doing on 2026-09-21 —
    21 cm in a tenth of a second while sitting still. Nothing downstream can
    recover from a position that is lying, so this is the first thing to fix
    when a drone will not hold still.
    """
    with DroneLink(args.uri) as link:
        scf = link.scf
        # What is actually RECEIVED decides the method — not what the drone has
        # stored, which is last session's memory and says nothing about now.
        received = assess_positioning(link.snapshot()).received
        alone = len(received) < 2

        print("\n  MEASURING WHERE THE BASE STATIONS ARE")
        print("  The drone is carried by hand throughout. No motor will spin.")
        print("  Keep yourself out of the line between the stations and the drone.")
        seen = ", ".join(str(b) for b in sorted(received))
        if args.quick:
            print("\n  QUICK: one sample at one position, no measuring.")
            print("  This restores position hold now. It can pick the mirror of the")
            print("  true answer, so if forward turns out to be backward, run this")
            print("  again without --quick.")
        elif alone and seen:
            print(f"\n  Only base station {seen} is being received, so this measures")
            print("  that one alone, from two samples a measured distance apart.")
            print("  Both samples must see it, and the drone must face the same")
            print("  way for both — the second sample is what rules out a")
            print("  mirror-image answer where forward comes out backward.")
        elif alone:
            print("\n  No base station is being received YET. Put the drone flat with")
            print("  its top clear and give it a few seconds; the sample will wait.")
        print()

        reader = geometry_estimation.SweepAngles(
            scf.cf, min_stations=1 if alone else 2)

        def ready(prompt: str) -> None:
            """Wait for the operator — by the keyboard, or by the clock.

            Someone alone in a cage is holding the drone with both hands at
            the position being measured. Reaching back to a keyboard moves the
            very thing the sample is of.
            """
            if args.countdown is None:
                input(f"  {prompt}: ")
                return
            print(f"  {prompt} — sampling in:", end="", flush=True)
            for remaining in range(int(args.countdown), 0, -1):
                print(f" {remaining}", end="", flush=True)
                time.sleep(1.0)
            print("  now — hold still")

        def collect(step: geometry_estimation.GeometryStep, index: int) -> object:
            total = step.count
            label = f" ({index + 1} of {total})" if total > 1 else ""
            while True:
                print(f"\n  {step.instruction}{label}")
                ready("Press Enter when it is there and steady"
                      if args.countdown is None else "Get it into position")
                try:
                    sample = reader.record()
                except (TimeoutError, ValueError) as e:
                    print(f"    {e}")
                    continue
                print("    recorded")
                return sample

        def heading() -> float | None:
            """The drone's yaw, so a failure can say if it was turned."""
            value = link.snapshot().get("stabilizer.yaw")
            return None if value is None else float(value)

        try:
            if args.quick:
                result = geometry_estimation.estimate_quick(
                    scf.cf, collect, write=not args.dry_run)
            elif alone:
                result = geometry_estimation.estimate_single(
                    scf.cf, collect,
                    reference_distance_m=args.distance,
                    write=not args.dry_run,
                    heading=heading,
                )
            else:
                result = geometry_estimation.estimate(
                    scf.cf, collect,
                    reference_distance_m=args.distance,
                    write=not args.dry_run,
                )
        except KeyboardInterrupt:
            print("\n  stopped — nothing was written to the drone\n")
            return 1

        print()
        if not result.converged:
            print(f"  {result.message}\n")
            return 1
        print(f"  {result.message}")
        if not alone:
            print(f"  error   mean {result.mean_error_m * 100:.1f} cm, "
                  f"worst {result.max_error_m * 100:.1f} cm")
        if result.written:
            print("  stored on the drone — it survives a power cycle")
        elif args.dry_run:
            print("  not written (--dry-run)")
        else:
            print("  WARNING: the drone did not confirm the write")
        if alone:
            print("\n  One station carries no redundancy: lose sight of it and x and y")
            print("  become the accelerometer integrating. Set DRONEDECK_MIN_STATIONS=1")
            print("  to let the checks accept it.")
        print("\n  Now run:  dronedeck check     (it should settle under 2 cm)\n")
    return 0


def cmd_proptest(args: argparse.Namespace) -> int:
    with DroneLink(args.uri) as link:
        _run_checks(link)
        if not _confirm_area():
            print("  cancelled")
            return 1
        result = link.prop_test()
        for motor in (1, 2, 3, 4):
            print(f"  motor {motor}   {'ok' if motor in result.passed else 'FAILED'}")
        return 0 if result.ok else 1


def cmd_hover(args: argparse.Namespace) -> int:
    program = HoverTest(height_m=args.height, hold_s=args.secs)
    with DroneLink(args.uri) as link:
        report = _run_checks(link)
        if program.duration_s() > report.budget_s():
            print(f"\n  this battery has about {report.budget_s():.0f}s of flying left; "
                  f"the program needs {program.duration_s():.0f}s")
            return 1
        if not _confirm_area():
            print("  cancelled")
            return 1

        recorder = _record(link, report, args, flight_id="cli")
        flight = link.guarded_flight(
            report, target_height_m=program.height_m, fence_half_extent_m=args.fence,
            on_phase=lambda event: print(f"  {event.phase}: {event.detail}"),
        )
        try:
            result = run_hover_test(flight, program)
        finally:
            if recorder is not None:
                recorder.stop()
        print(f"\n  {result.message}")
        return 0 if result.outcome.value == "completed" else 1


def cmd_mission(args: argparse.Namespace) -> int:
    """Check a saved mission, and fly it the way the app does.

    The same path as Session.run_mission: validate in its room, the checks, the
    battery budget, the mission checked again from where the drone is (its
    position is the start), a confirmation — then the manual flight
    system armed, the mission controller giving it goals, and the guard watching
    with the room's own geofence. This terminal is the operator's window: it
    keeps the heartbeat going, and Ctrl-C lands.
    """
    store = PlanStore()
    try:
        mission = store.mission(args.id)
        room = store.room(mission.room_id)
    except (NotFound, PlanError) as e:
        print(f"  {e}")
        return 2
    problems = validate_mission(mission, room,
                                outer=outer_bound(room, default_half_extent_m=args.fence))
    needed = mission.estimated_duration_s(move_speed_m_s=MOVE_SPEED_M_S,
                                          climb_rate_m_s=CLIMB_RATE_M_S)
    print(f"\n  {mission.name}  (revision {mission.revision}, room {room.name})")
    ends = f", ending at {mission.end_point_id}" if mission.end_point_id else ""
    print(f"  {len(mission.flown_points)} inspection points{ends}, "
          f"{mission.path_length_m():.1f} m of path from the planned start, about "
          f"{needed:.0f} s")
    for point in mission.flown_points:
        print(f"    {point.id:6} ({point.x_m:+.2f}, {point.y_m:+.2f}) m at {point.z_m:.2f} m, "
              f"hold {point.hold_s:.0f} s{f'  {point.label}' if point.label else ''}")
    for problem in problems:
        print(f"  {problem.severity:7}  {problem.message}")
    if errors(problems):
        print("\n  not safe to fly — fix the plan in the app")
        return 2
    if args.dry_run:
        print("\n  checked; nothing connected")
        return 0
    if not MissionController.BUILT:
        print("\n  The mission controller is not built yet (docs/handoffs/sprint-1/undone/"
              "mission-controller.txt). Nothing was armed.")
        return 2

    with DroneLink(args.uri) as link:
        report = _run_checks(link)
        if not report.assisted:
            print("\n  a mission needs the drone to know where it is — get the base "
                  "stations seen and run the checks again")
            return 1
        if needed > report.budget_s():
            print(f"\n  this battery has about {report.budget_s():.0f}s of flying left; "
                  f"the mission needs {needed:.0f}s")
            return 1
        snap = link.snapshot()
        x, y = snap.get("stateEstimate.x"), snap.get("stateEstimate.y")
        if x is None or y is None:
            print("\n  the drone's position is not being reported — the mission cannot "
                  "be checked from where it is")
            return 1
        # The flight starts from the drone, as in the app (Session.run_mission).
        mission = mission.from_start((float(x), float(y)))
        from_here = errors(validate_mission(
            mission, room, outer=flyable_bound(room, default_half_extent_m=args.fence)))
        if from_here:
            print(f"\n  from where the drone is ({x:+.2f}, {y:+.2f}) m: "
                  f"{from_here[0].message}")
            return 1
        needed = mission.estimated_duration_s(move_speed_m_s=MOVE_SPEED_M_S,
                                              climb_rate_m_s=CLIMB_RATE_M_S)
        if needed > report.budget_s():
            print(f"\n  from where the drone is, the mission needs {needed:.0f}s; this "
                  f"battery has about {report.budget_s():.0f}s")
            return 1
        if not _confirm_area():
            print("  cancelled")
            return 1

        controller = link.manual(report)
        holder: dict[str, MissionController] = {}
        recorder = _record(link, report, args, flight_id="cli", point_id=lambda: (
            holder["flying"].current_point_id if "flying" in holder else None))
        guard = link.manual_guard(report, fence_half_extent_m=args.fence,
                                  fence=room.geofence.contains)

        def on_event(event: MissionEvent) -> None:
            print(f"  {event.kind}: {event.detail}")

        controller.arm()
        controller.start()
        flying = MissionController(mission, controller, on_event=on_event)
        holder["flying"] = flying
        try:
            flying.start()
            while flying.state not in TERMINAL_STATES:
                controller.heartbeat()
                verdict = guard.check(link.snapshot(), time.monotonic())
                if not verdict.ok:
                    print(f"\n  {verdict.message}")
                    if verdict.action is GuardAction.STOP:
                        controller.emergency_stop()
                    else:
                        controller.land()
                    flying.abort(verdict.message)
                time.sleep(0.1)
        except KeyboardInterrupt:
            print("\n  interrupted — landing")
            flying.abort("interrupted from the terminal")
            controller.land()
        finally:
            deadline = time.monotonic() + 8.0
            while str(controller.state) == "landing" and time.monotonic() < deadline:
                controller.heartbeat()
                time.sleep(0.1)
            controller.stop()
            link.restore_estimator()
            if recorder is not None:
                recorder.stop()
        store.mark_flown(mission.id, mission.revision)
        print(f"\n  mission {flying.state}")
        return 0 if str(flying.state) == "done" else 1


def cmd_mission_report(args: argparse.Namespace) -> int:
    """The mission report (mission/review.py): per point, the leg, transit,
    settle, hold and hold drift; the flown time against the estimate; and what
    the mission controller's constants should be. Reads files; touches no radio."""
    from dronedeck.mission.review import ReportError, as_json, as_text, load, suggest

    try:
        reviews = [load(paths.data_dir(), flight) for flight in args.flight]
    except ReportError as e:
        print(f"  {e}")
        return 2
    suggestions = suggest(reviews)
    print(as_json(reviews, suggestions) if args.json else as_text(reviews, suggestions))
    return 0


def cmd_process(args: argparse.Namespace) -> int:
    """The data pipeline over one recorded flight: load, clean, enhance,
    classify, interpret, save (story 4.5). Reads files; touches no radio."""
    from dronedeck.pipeline.compose import default_stages
    from dronedeck.pipeline.runner import run_flight
    from dronedeck.pipeline.sinks import LocalResultSink
    from dronedeck.pipeline.sources import FlightNotFound, LocalFlightSource

    if args.all:
        return _process_all()
    if args.session:
        return _process_session(args.session)
    if args.live_point:
        return _process_live(args.flight, args.live_point)
    if args.fixture:
        root = Path(__file__).resolve().parents[1] / "tests" / "pipeline" / "fixtures" / "data"
        if not root.exists():
            print("  the fixture is only in a source checkout (tests/pipeline/fixtures)")
            return 2
        flight_id = json.loads((root / "fixture.json").read_text(encoding="utf-8"))["flight_id"]
        results = root.parent / "results"
    else:
        root, flight_id, results = paths.data_dir(), args.flight, paths.results_dir()
    try:
        result, where = run_flight(flight_id, source=LocalFlightSource(root),
                                   sink=LocalResultSink(results), stages=default_stages())
    except FlightNotFound as e:
        print(f"  {e}")
        return 2
    print(f"\n  flight {result.flight_id}")
    for stage, name in result.stages.items():
        print(f"    {stage:10} {name}")
    for point in result.points:
        detail = result.summary.get(point.point_id, {})
        print(f"  {point.point_id:8} {point.verdict:18} {detail.get('readings', 0)} readings, "
              f"{detail.get('frames', 0)} frames — {'; '.join(point.reasons)}")
    for finding in result.findings:
        print(f"  {finding.severity.upper():8} {finding.sentence}")
    for failure in result.failures:
        print(f"  FAILED   {failure.point_id} {failure.stage}: {failure.reason}")
    print(f"\n  saved {where}")
    if not args.fixture:
        _queue_result_upload(result.flight_id, Path(where).parent)
    return 0


def _process_all() -> int:
    """`process --all`: every recorded flight whose result is missing or from
    an older pipeline, oldest first, each queued for the web. A flight with no
    readings is skipped, not a failure — a session can end before a row is
    written. Running it again only does what is left."""
    from dronedeck import history
    from dronedeck.pipeline import PIPELINE_VERSION
    from dronedeck.pipeline.compose import default_stages
    from dronedeck.pipeline.runner import run_flight
    from dronedeck.pipeline.sinks import LocalResultSink
    from dronedeck.pipeline.sources import FlightNotFound, LocalFlightSource

    source, sink = LocalFlightSource(paths.data_dir()), LocalResultSink(paths.results_dir())
    done = skipped = failed = findings = 0
    sessions_done = sessions_failed = 0
    for flight_id in history.recorded_flights():
        if _result_version(paths.results_dir() / flight_id) == PIPELINE_VERSION:
            continue
        try:
            result, where = run_flight(flight_id, source=source, sink=sink,
                                       stages=default_stages())
        except FlightNotFound:
            skipped += 1
            continue
        except Exception as e:  # noqa: BLE001 — one bad flight must not stop the rest
            failed += 1
            print(f"  {flight_id[:8]} FAILED {type(e).__name__}: {e}")
            history.set_flight_processing(flight_id, "failed", str(e))
            continue
        done += 1
        findings += len(result.findings)
        print(f"  {flight_id[:8]} {len(result.findings)} finding(s)")
        history.set_flight_processing(flight_id, "done")
        _queue_result_upload(result.flight_id, Path(where).parent)
    for session_id in history.recorded_sessions():
        if _result_version(session_results_dir() / session_id) == PIPELINE_VERSION:
            continue
        code = _process_session(session_id, quiet=True)
        if code == 0:
            sessions_done += 1
        elif code == 1:
            sessions_failed += 1
    print(f"\n  processed {done} flight(s), {findings} finding(s); "
          f"{skipped} with no readings, {failed} failed")
    print(f"  processed {sessions_done} session(s); {sessions_failed} failed")
    return 1 if failed or sessions_failed else 0


def _process_live(flight_id: str | None, point_id: str) -> int:
    """Story 4.9: one point's verdict while the flight goes on."""
    from dronedeck.pipeline.compose import default_stages
    from dronedeck.pipeline.runner import run_live_point
    from dronedeck.pipeline.sources import FlightNotFound, LocalFlightSource
    from dronedeck.processing import FLIGHT_ID, POINT_ID

    if not flight_id:
        print("  --live-point needs --flight")
        return 2
    if not FLIGHT_ID.match(flight_id) or not POINT_ID.match(point_id):
        print("  not a flight id and point id")
        return 2
    try:
        out, where = run_live_point(flight_id, point_id,
                                    source=LocalFlightSource(paths.data_dir()),
                                    results_root=paths.results_dir(), stages=default_stages())
    except (FlightNotFound, ValueError) as e:
        print(f"  {e}")
        return 2
    print(f"  {point_id}: {out['verdict']} — {len(out['findings'])} finding(s)")
    print(f"  saved {where}")
    return 0


def cmd_upscale(args: argparse.Namespace) -> int:
    """The display chain over a session's captured frames (display/frames.py).
    With --session, the LAST LINE printed is the chunk's report as JSON —
    what the agent's queue reads (display/queue.py). 0 done, 2 no such
    session, 3 the model is not installed or not the one MODELS.json names."""
    from dronedeck import history
    from dronedeck.display.frames import run_chunk, sessions_with_frames
    from dronedeck.display.steps import ModelUnavailable, default_steps
    from dronedeck.processing import FLIGHT_ID

    try:
        steps = default_steps()
    except ModelUnavailable as e:
        print(f"  the display chain cannot run: {e}")
        return 3
    results = paths.results_dir()
    if args.all:
        made = failed = 0
        for folder in sessions_with_frames(history.sessions_dir()):
            chunk = run_chunk(folder, results, steps)
            made, failed = made + chunk.done, failed + chunk.failed
            if chunk.done or chunk.failed:
                print(f"  {folder.name[:8]} {chunk.done} made, {chunk.failed} failed")
        print(f"\n  {made} frame(s) made, {failed} failed")
        return 0
    if not FLIGHT_ID.match(args.session):
        print(f"  {args.session!r} is not a session id")
        return 2
    folder = history.sessions_dir() / args.session
    if not (folder / "frames.csv").exists():
        print(f"  session {args.session} has no frames on this laptop")
        return 2
    chunk = run_chunk(folder, results, steps, limit=max(0, args.chunk))
    print(json.dumps(chunk.to_dict()))
    return 0


def session_results_dir() -> Path:
    """<data folder>/results/sessions/<session id>/ — a session's own result."""
    return paths.results_dir() / "sessions"


def _process_session(session_id: str, *, quiet: bool = False) -> int:
    """A session around its flights: its one-a-second samples and the frames
    taken outside its flights (pipeline/runner.py run_session). 0 done, 1
    failed, 2 nothing to process."""
    from dronedeck import history
    from dronedeck.pipeline.compose import session_stages
    from dronedeck.pipeline.runner import run_session
    from dronedeck.pipeline.sinks import LocalResultSink
    from dronedeck.pipeline.sources import LocalSessionSource, SessionNotFound
    from dronedeck.processing import FLIGHT_ID

    if not FLIGHT_ID.match(session_id):
        print(f"  {session_id!r} is not a session id")
        return 2
    try:
        result, where = run_session(session_id, source=LocalSessionSource(paths.data_dir()),
                                    sink=LocalResultSink(session_results_dir()),
                                    stages=session_stages())
    except SessionNotFound as e:
        if not quiet:
            print(f"  {e}")
        return 2
    except Exception as e:  # noqa: BLE001 — said, recorded, and the caller goes on
        print(f"  session {session_id[:8]} FAILED {type(e).__name__}: {e}")
        history.set_session_processing(session_id, "failed", str(e))
        return 1
    history.set_session_processing(session_id, "done")
    _queue_session_result_upload(session_id, Path(where).parent)
    point = result.points[0] if result.points else None
    print(f"  session {session_id[:8]}: {point.verdict if point else 'no verdict'}, "
          f"{len(result.findings)} finding(s), {len(result.flags)} flag(s), "
          f"{len(result.frames)} frame(s)")
    if not quiet:
        for finding in result.findings:
            print(f"  {finding.severity.upper():8} {finding.sentence}")
        for failure in result.failures:
            print(f"  FAILED   {failure.stage}: {failure.reason}")
        print(f"\n  saved {where}")
    return 0


def _result_version(folder: Path) -> str | None:
    try:
        raw = json.loads((folder / "result.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return str(raw.get("pipeline_version")) if isinstance(raw, dict) else None


def cmd_calibrate(args: argparse.Namespace) -> int:
    """The hand-warmer flights → the anomaly thresholds, measured
    (pipeline/calibrate.py). Prints the measurements and a recommendation, and
    saves the report under <data folder>/calibration/."""
    import tempfile

    from dronedeck.pipeline.calibrate import Marked, measure, save
    from dronedeck.pipeline.sources import FlightNotFound, LocalFlightSource

    if len(args.flight) != len(args.at):
        print("  give one --at for every --flight, in the same order")
        return 2
    marked = [Marked(f, p) for f, p in zip(args.flight, args.at, strict=True)]
    try:
        with tempfile.TemporaryDirectory() as work:
            report = measure(marked, LocalFlightSource(paths.data_dir()), Path(work))
    except (FlightNotFound, ValueError) as e:
        print(f"  {e}")
        return 2
    print("\n  marked point            readings  noise °C  peak °C   peak/σ  found  severity")
    for p in report.points:
        print(f"  {p.flight_id[:8]} {p.point_id:<12} {p.readings:8d}  {p.noise_c:8.3f}  "
              f"{p.peak_c:+7.2f}  {p.peak_z:7.1f}  {'yes' if p.detected else 'no ':>5}  "
              f"{p.severity or '—'}")
    print(f"\n  normal wander elsewhere on these flights: up to {report.normal_wander_c:.2f} °C")
    for key, value in report.recommended.items():
        mark = "" if value == report.current[key] else f"   (now {report.current[key]})"
        print(f"  {key:<16} {value}{mark}")
    for note in report.notes:
        print(f"  • {note}")
    print(f"\n  saved {save(report, paths.data_dir())}")
    print("  Change a constant in a reviewed pull request, citing this report in "
          "ml/anomaly-eval/MEASUREMENTS.txt.")
    return 0


def _queue_session_result_upload(session_id: str, folder: Path) -> None:
    """Queue a session's own result for the web (sync: Kind.SESSION_RESULTS)."""
    from datetime import UTC, datetime

    from dronedeck.sync.outbox import Kind, Outbox

    try:
        Outbox().put(Kind.SESSION_RESULTS, session_id, {
            "session_id": session_id, "folder": str(folder),
            "occurred_at": datetime.now(UTC).isoformat(),
        })
    except OSError as e:
        print(f"  could not queue the upload ({e}); the result stays on this computer")


def _queue_result_upload(flight_id: str, folder: Path) -> None:
    """Queue the result for the web (sync/results.py). The agent's syncer —
    whichever process runs it — uploads it once the flight's own row is up.
    Processing again re-queues it, which replaces what was uploaded."""
    from datetime import UTC, datetime

    from dronedeck.sync.outbox import Kind, Outbox

    try:
        Outbox().put(Kind.RESULTS, flight_id, {
            "flight_id": flight_id, "folder": str(folder),
            "occurred_at": datetime.now(UTC).isoformat(),
        })
        print("  queued for upload")
    except OSError as e:
        # The result is saved; only the web copy waits for the next process.
        print(f"  could not queue the upload ({e}); the result stays on this computer")


#: Everything the agent imports only when a feature is first used — inside a
#: function, so starting the agent proves nothing about them. A frozen Intel
#: agent (2026-09-25) started and served, while `supabase` could not be
#: imported at all: cryptography, which it needs, had been compiled against
#: Homebrew's OpenSSL, and the frozen bundle kept Python's older libssl under
#: the same file name. Sign-in was the first thing to import it.
SELFTEST_MODULES = (
    "supabase",                                         # sign-in, sync
    "cryptography.hazmat.bindings._rust",               # its native half, via PyJWT
    "numpy",                                            # geometry
    "cv2",                                              # the pipeline's enhancer
    "cflib.localization.lighthouse_geo_estimation_manager",   # geometry (scipy)
    "cflib.localization",                               # geometry, config writer
    "cflib.crazyflie.mem.lighthouse_memory",            # geometry
    "cflib.bootloader",                                 # firmware flashing
    "cflib.cpx",                                        # the AI deck's camera
    "libusb_package",                                   # the radio, and radio.py
    "usb.backend.libusb1",
    "uvicorn",                                          # serve
)


def cmd_selftest(_args: argparse.Namespace) -> int:
    """Import every lazily loaded library; report each one that will not load.

    packaging/verify_sidecar.py runs this against the frozen binary on every
    build, so a library that is broken inside the bundle fails the build on
    that machine instead of failing a feature on an operator's desk.
    """
    import importlib

    failed = []
    for name in SELFTEST_MODULES:
        try:
            importlib.import_module(name)
        except Exception as e:                 # ImportError, OSError from dlopen, …
            failed.append(name)
            print(f"  FAILED  {name}: {type(e).__name__}: {e}")
    # PyJWT treats a missing cryptography as "no asymmetric algorithms" rather
    # than an error, so an excluded or unimportable one would pass the loop.
    try:
        from jwt.algorithms import has_crypto
    except Exception as e:
        failed.append("jwt.algorithms")
        print(f"  FAILED  jwt.algorithms: {type(e).__name__}: {e}")
    else:
        if not has_crypto:
            failed.append("jwt crypto")
            print("  FAILED  PyJWT loaded without cryptography")
    if failed:
        print(f"\n  {len(failed)} of {len(SELFTEST_MODULES) + 1} did not load")
        return 1
    print(f"  imports ok ({len(SELFTEST_MODULES) + 1} checked)")
    return 0


def cmd_serve(args: argparse.Namespace) -> int:
    from dronedeck.api.rest import serve as run_server

    print(f"  agent API on http://{args.host}:{args.port}")
    run_server(host=args.host, port=args.port, exit_with_parent=args.exit_with_parent)
    return 0


def _start_logging(*, verbose: bool) -> None:
    """Log to the terminal AND to a file that outlives the process.

    The desktop app discards the sidecar's stdout, so a crash inside the agent
    left the operator with "Could not reach the flight agent" and no way to
    find out why. The file is capped and rotated: a flight log is worth
    keeping, a gigabyte of it is not.
    """
    handlers: list[logging.Handler] = [logging.StreamHandler()]
    try:
        from logging.handlers import RotatingFileHandler
        handlers.append(RotatingFileHandler(
            paths.log_file(), maxBytes=2_000_000, backupCount=3, encoding="utf-8"))
    except Exception:                       # a read-only home must not stop a flight
        pass
    # One line per repeating message (logfilter.py): the 50 Hz flight loop and
    # the camera watchdog otherwise rotate the useful lines out of the file.
    from dronedeck.logfilter import RepeatFilter
    repeats = RepeatFilter()
    for handler in handlers:
        handler.addFilter(repeats)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        handlers=handlers,
        force=True,
    )
    # An exception that kills a thread otherwise vanishes with it.
    def _thread_died(args: Any) -> None:
        logging.getLogger("dronedeck").critical(
            "unhandled exception in thread %s",
            getattr(args.thread, "name", "?"),
            exc_info=(args.exc_type, args.exc_value, args.exc_traceback),
        )
    threading.excepthook = _thread_died
    sys.excepthook = lambda *e: logging.getLogger("dronedeck").critical(
        "unhandled exception", exc_info=e)


def _cmd_setup(args: argparse.Namespace) -> int:
    from dronedeck.setup_cli import cmd_setup
    return cmd_setup(args)


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    _start_logging(verbose=args.verbose)

    handlers = {
        "check": cmd_check,
        "stations": cmd_stations,
        "geometry": cmd_geometry,
        "proptest": cmd_proptest,
        "hover": cmd_hover,
        "mission": cmd_mission,
        "process": cmd_process,
        "upscale": cmd_upscale,
        "mission-report": cmd_mission_report,
        "calibrate": cmd_calibrate,
        "serve": cmd_serve,
        "selftest": cmd_selftest,
        "setup": _cmd_setup,
    }
    try:
        return handlers[args.command](args)
    except LinkError as e:
        print(f"\n  {e}\n")
        return 1
    except ChecksFailed as e:
        print(f"\n  {e.result.detail}\n")
        return 1
    except FlightAborted as e:
        print(f"\n  {e.verdict.message}\n")
        return 1
    except KeyboardInterrupt:
        print("\n  interrupted — the drone lands on the way out")
        return 130


if __name__ == "__main__":
    sys.exit(main())
