"""Prove migration 20261009000018 (a session's own pipeline result) against a
running database, beside results_check.py and notifications_check.py.

Operators A and B, and a viewer V. Checks that A uploads a session's result
and a session finding (no flight, scope 'session'); B and V read them; V
cannot write; anonymous reads nothing; re-uploading replaces; every finding
still belongs to a flight or a session; retracting a session's findings never
touches a flight's; a warning session finding notifies the operators (when
20261009000017 is applied); deleting the session takes its result and its
own findings with it.

Runs against the LOCAL stack (`cd backend && supabase start`), with the same
helpers as results_check.py. Creates its own users and rows and deletes them
after. Exit code 0 = every check held.

    python backend/supabase/checks/session_results_check.py
"""
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from results_check import (  # noqa: E402
    ANON,
    PREFER,
    UPSERT,
    call,
    check,
    cleanup,
    results,
    sql,
    user,
)


def finding(fid, session, *, flight=None, scope="session", severity="warning"):
    return {"id": fid, "flight_id": flight, "session_id": session, "scope": scope,
            "signal": "temperature", "severity": severity,
            "title": "Warmer than expected on the ground",
            "sentence": "From 1:00 to 1:40 into the session, on the ground …",
            "start_index": 60, "end_index": 100, "t_start_s": 60.0, "t_end_s": 100.0,
            "unit": "C", "observed": 36.0, "expected": 34.0, "delta": 2.0, "z": 12.0,
            # No image columns: what the agent sends since migration
            # 20261118000020 (results_check.py still sends them, as an older
            # agent does — both must be accepted).
            "point_ids": [], "pipeline_version": "3"}


def result_row(session, count=1):
    return {"session_id": session, "pipeline_version": "3", "stages": {"classify": "ground@1"},
            "created_at": "2026-10-09T18:00:00+00:00", "temp_unit": "C",
            "points": [{"point_id": "session", "verdict": "anomaly"}],
            "findings_count": count, "worst_severity": "warning"}


def main():
    tag = uuid.uuid4().hex[:8]
    a_id, a = user(f"ses-a-{tag}@check.local", "operator")
    b_id, b = user(f"ses-b-{tag}@check.local", "operator")
    v_id, v = user(f"ses-v-{tag}@check.local", "viewer")
    session, flight = str(uuid.uuid4()), str(uuid.uuid4())
    s1, s2, f1 = str(uuid.uuid4()), str(uuid.uuid4()), str(uuid.uuid4())
    try:
        call("POST", "/rest/v1/sessions", a, {"id": session, "operator_id": a_id})
        call("POST", "/rest/v1/flights", a, {"id": flight, "session_id": session,
                                             "created_by": a_id, "status": "completed"})

        status, _ = call("POST", "/rest/v1/pipeline_session_results", a, result_row(session),
                         PREFER)
        check("an operator uploads a session's result", status == 201, status)
        status, _ = call("POST", "/rest/v1/pipeline_session_results?on_conflict=session_id",
                         a, {**result_row(session, 2), "pipeline_version": "3.1"}, UPSERT)
        _, rows = call("GET", f"/rest/v1/pipeline_session_results?session_id=eq.{session}"
                              f"&select=pipeline_version,findings_count", b)
        check("re-uploading replaces it, and another operator reads it",
              status in (200, 201) and rows == [{"pipeline_version": "3.1",
                                                 "findings_count": 2}], rows)
        _, rows = call("GET", f"/rest/v1/pipeline_session_results?session_id=eq.{session}", v)
        check("a viewer reads it", len(rows) == 1, rows)
        status, _ = call("POST", "/rest/v1/pipeline_session_results?on_conflict=session_id",
                         v, result_row(session), UPSERT)
        check("a viewer cannot write it", status in (401, 403), status)
        status, rows = call("GET", "/rest/v1/pipeline_session_results", ANON)
        check("anonymous reads nothing", status in (401, 403) or rows == [], (status, rows))

        status, _ = call("POST", "/rest/v1/pipeline_findings", a, finding(s1, session), PREFER)
        check("a session finding has no flight", status == 201, status)
        status, _ = call("POST", "/rest/v1/pipeline_findings", a,
                         finding(s2, None), PREFER)
        check("a session finding with no session is refused", status == 400, status)
        status, _ = call("POST", "/rest/v1/pipeline_findings", a,
                         finding(f1, session, scope="flight"), PREFER)
        check("a flight finding with no flight is refused", status == 400, status)
        status, _ = call("POST", "/rest/v1/pipeline_findings", a,
                         finding(f1, session, flight=flight, scope="flight"), PREFER)
        check("a flight finding still goes in as before", status == 201, status)

        has_notifications = sql("select to_regclass('public.notifications') is not null") == "t"
        if has_notifications:
            n = sql(f"select count(*) from public.notifications where finding_id = '{s1}' "
                    f"and flight_id is null and user_id in ('{a_id}', '{b_id}')")
            check("a warning on the ground notifies every operator", n == "2", n)

        # The agent's retraction: session findings only.
        status, _ = call("DELETE", f"/rest/v1/pipeline_findings?session_id=eq.{session}"
                                   f"&scope=eq.session&id=not.in.({s2})", a)
        left = sql(f"select string_agg(scope, ',' order by scope) from public.pipeline_findings "
                   f"where session_id = '{session}'")
        check("retracting a session's findings leaves the flight's", left == "flight", left)

        call("POST", "/rest/v1/pipeline_findings", a, finding(s1, session), PREFER)
        sql(f"delete from public.flights where id = '{flight}'")
        sql(f"delete from public.sessions where id = '{session}'")
        gone = sql(f"select (select count(*) from public.pipeline_findings where id = '{s1}') "
                   f"+ (select count(*) from public.pipeline_session_results "
                   f"where session_id = '{session}')")
        check("deleting the session takes its result and its own findings", gone == "0", gone)
    finally:
        cleanup([a_id, b_id, v_id], [session])
    print(f"\n  {sum(results)}/{len(results)} held")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())
