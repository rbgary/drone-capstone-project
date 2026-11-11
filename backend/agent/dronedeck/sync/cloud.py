"""Everything the agent says to Supabase.

The agent signs in as a **real operator account** using the anon key and acts as
`authenticated` under RLS. It deliberately does not use the service-role key:
this code ships inside a desktop installer, and a service-role key there would
hand every downloader unrestricted access and bypass every policy. It also makes
actions attributable — a flight traces to a person, not to a superuser.

Every write is **idempotent**, because the same record may be sent again after
a laptop comes back online:

- rows and records upsert on their own ids
- telemetry upserts on ``(flight_id, index)`` and ignores duplicates
- a CSV already in storage is treated as success, not an error
"""

from __future__ import annotations

import gzip
import logging
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

log = logging.getLogger(__name__)

TELEMETRY_BUCKET = "flight-logs"
#: Private, like flight-logs — migration 20260924000011_flight_frames.sql.
FRAMES_BUCKET = "flight-frames"
BACKFILL_FUNCTION = "import-flight-log"


def without_empty_point_id(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop the point_id key from a batch in which no row carries one.

    telemetry.point_id arrives with migration 20260928000012. A laptop agent
    can run before that migration reaches the database, and PostgREST refuses a
    whole batch over one unknown column — so a flight with no mission (every
    flight until the mission controller lands) uploads exactly as it did
    before. A batch that does carry an inspection point keeps the key on every
    row, since PostgREST also needs one set of keys across a batch.
    """
    if any(row.get("point_id") for row in rows):
        return rows
    return [{k: v for k, v in row.items() if k != "point_id"} for row in rows]


class CloudError(RuntimeError):
    """Talking to Supabase failed. Always recoverable: the record stays local."""


class CloudRefused(CloudError):
    """Supabase refused this record by policy — it will refuse it every time.
    The syncer gives up on the record rather than stalling every pass behind
    it (e.g. a plan for a flight another account flew)."""


class StorageError(CloudError):
    """A file upload (a frame, an index, a flight log) failed — storage, not
    the database. The syncer skips the rest of that pass's file uploads and
    lets the database rows through: a full bucket used to stall every row
    behind it (found 2026-11-08). `full` is True when it looked like the
    project's storage quota rather than the network."""

    def __init__(self, message: str, *, full: bool = False) -> None:
        super().__init__(message)
        self.full = full


def storage_quota_refused(error: Exception) -> bool:
    """Whether a storage refusal looks like the quota being used up.

    Not documented by Supabase and not yet seen on this project (the bucket
    was half full on 2026-11-08): reported refusals name the quota, and a
    restricted project answers 402. A 413 is NOT counted — that is one file
    over the per-file size limit. Only the wording of the message depends on
    this; every storage failure is isolated from the rows either way."""
    status = str(getattr(error, "status", "") or getattr(error, "status_code", ""))
    text = f"{getattr(error, 'code', '')} {getattr(error, 'message', '')} {error}".lower()
    return status == "402" or "quota" in text


class AuthError(RuntimeError):
    """Sign-in failed. Never carries the password or the raw provider error."""


class CloudTimeout(AuthError):
    """Supabase did not answer in time — no verdict on the account.

    A subclass so every caller that already turns AuthError into words for the
    operator keeps working, while the one that must tell the two apart can:
    restoring a saved sign-in deletes it on AuthError, and a slow network must
    not sign anyone out.
    """


#: The longest a sign-in may take, end to end: loading the Supabase library,
#: the auth call, and reading the profile. The window's fetch gives up after
#: about 60 s of silence, and it then says only "did not answer" — so the
#: agent must answer first, with the reason. Measured on an M-series Mac: 1.3 s
#: for a first sign-in, 0.1 s after. The profile read alone could previously
#: wait 120 s (PostgREST's default timeout).
SIGN_IN_DEADLINE_S = 25.0

TIMED_OUT = (
    "Supabase did not answer within {seconds:.0f} seconds. Check this computer's internet "
    "connection, and that no firewall or security software is blocking DroneDeck — "
    "then try again."
)
STILL_WAITING = "The last sign-in is still waiting for Supabase. Try again in a moment."


@dataclass
class _Attempt:
    """One sign-in running on its own thread, and whether anyone still wants it."""

    lock: threading.Lock
    done: bool = False
    abandoned: bool = False
    result: Operator | None = None
    error: BaseException | None = None


@dataclass(frozen=True)
class Operator:
    id: str
    email: str
    full_name: str | None
    role: str

    @property
    def is_operator(self) -> bool:
        return self.role == "operator"


class Cloud(Protocol):
    """What the session and the sync need. Faked in tests."""

    def sign_in(self, email: str, password: str) -> Operator: ...
    def sign_out(self) -> None: ...
    def upsert_drone(self, hardware_id: str, name: str, uri: str) -> str: ...
    def upsert_session(self, row: dict[str, Any]) -> None: ...
    def upsert_flight(self, row: dict[str, Any]) -> None: ...
    def insert_audit(self, rows: list[dict[str, Any]]) -> None: ...
    def max_telemetry_index(self, flight_id: str) -> int | None: ...
    def insert_telemetry(self, rows: list[dict[str, Any]]) -> None: ...
    def insert_session_samples(self, rows: list[dict[str, Any]]) -> None: ...
    def upload_flight_csv(self, object_path: str, csv_path: Path) -> None: ...
    def request_backfill(self, flight_id: str, object_path: str) -> None: ...
    def upload_frame(self, object_path: str, path: Path, content_type: str,
                     *, replace: bool = False) -> None: ...
    def upsert_result(self, row: dict[str, Any]) -> None: ...
    def upsert_findings(self, rows: list[dict[str, Any]]) -> None: ...
    def delete_findings_except(self, flight_id: str, keep: list[str]) -> None: ...
    def upsert_session_result(self, row: dict[str, Any]) -> None: ...
    def delete_session_findings_except(self, session_id: str, keep: list[str]) -> None: ...
    def insert_flight_plan(self, row: dict[str, Any]) -> None: ...
    def insert_flight_traces(self, rows: list[dict[str, Any]]) -> None: ...


#: PostgREST answers at most this many rows per request (supabase/config.toml
#: max_rows; the hosted default too) — reads page through it.
PAGE = 1000

#: The columns of public.flights the flight list shows. operator_* are stamped
#: by the server from profiles (20261009000015): no profile is read.
FLIGHT_COLUMNS = ("id, session_id, started_at, ended_at, status, mode, program, outcome, "
                  "abort_reason, operator_email, operator_name")

_PLAYBACK_HINT = " — apply migration 20261011000019_flight_playback.sql"


class SupabaseCloud:
    """The real implementation."""

    def __init__(
        self, url: str, anon_key: str, client: Any | None = None,
        *, deadline_s: float = SIGN_IN_DEADLINE_S,
    ) -> None:
        self._url = url
        self._anon_key = anon_key
        self._client = client
        self._deadline_s = deadline_s
        # The warm-up thread and a sign-in can both reach _connect at once.
        self._connect_lock = threading.Lock()
        # A sign-in past its deadline keeps running (a thread cannot be
        # stopped); until it ends, no second one starts, so its late result can
        # be discarded without touching a newer sign-in.
        self._pending: threading.Thread | None = None
        self.operator: Operator | None = None

    # ── auth ─────────────────────────────────────────────────────────────

    def _connect(self) -> Any:
        with self._connect_lock:
            if self._client is None:
                try:
                    from supabase import create_client
                except ImportError as e:  # pragma: no cover
                    raise CloudError("the supabase package is not installed") from e
                self._client = create_client(self._url, self._anon_key)
            return self._client

    def warm(self) -> None:
        """Load the Supabase library now, so the first sign-in does not.

        Called on a background thread at startup. On a slow machine the import
        is a large part of the first sign-in (the agent took 14 s to 2 min 25 s
        to start on an Intel Mac, 2026-09-25). No network: create_client only
        builds the clients.
        """
        if not self._url or not self._anon_key:
            return                          # sign-in is not configured; nothing to load
        try:
            self._connect()
        except Exception:
            log.exception("could not prepare the Supabase client; sign-in will try again")

    def _within_deadline(self, work: Callable[[], Operator]) -> Operator:
        """Run a sign-in, answering within the deadline whatever Supabase does."""
        if self._pending is not None and self._pending.is_alive():
            raise CloudTimeout(STILL_WAITING)
        attempt = _Attempt(lock=threading.Lock())

        def run() -> None:
            result: Operator | None = None
            error: BaseException | None = None
            try:
                result = work()
            except BaseException as e:  # handed to the caller, or logged below
                error = e
            with attempt.lock:
                attempt.done, attempt.result, attempt.error = True, result, error
                abandoned = attempt.abandoned
            if abandoned:
                log.warning("a sign-in finished after its deadline (%s); discarding it",
                            "succeeded" if result else type(error).__name__)
                if result is not None:
                    self._discard_session()

        thread = threading.Thread(target=run, daemon=True, name="cloud-sign-in")
        thread.start()
        thread.join(self._deadline_s)
        with attempt.lock:
            if not attempt.done:
                attempt.abandoned = True
                self._pending = thread
                log.warning("sign-in exceeded %.0f s; answering the window without it",
                            self._deadline_s)
                raise CloudTimeout(TIMED_OUT.format(seconds=self._deadline_s))
        if attempt.error is not None:
            raise attempt.error
        assert attempt.result is not None
        return attempt.result

    def _discard_session(self) -> None:
        """Forget a session nobody is waiting for — on this computer only.

        "local" scope: the default revokes the account's sessions everywhere,
        which would sign the operator out of the website too.
        """
        self.operator = None
        if self._client is not None:
            try:
                self._client.auth.sign_out({"scope": "local"})
            except Exception:
                log.debug("discarding a late session failed; it expires by itself")

    def sign_in(self, email: str, password: str) -> Operator:
        return self._within_deadline(lambda: self._sign_in(email, password))

    def _client_for_sign_in(self) -> Any:
        """The Supabase client, or a plain AuthError saying why there is none.

        Building it loads the library and constructs its HTTP clients. That ran
        outside every handler, so a failure here escaped as an unexpected error
        instead of words the operator could act on.
        """
        try:
            return self._connect()
        except Exception as e:
            log.exception("the Supabase client could not be created")
            raise AuthError(
                f"Sign-in could not start on this computer: the Supabase client failed to "
                f"load ({type(e).__name__}). The agent's log has the details."
            ) from None

    def _sign_in(self, email: str, password: str) -> Operator:
        client = self._client_for_sign_in()
        try:
            response = client.auth.sign_in_with_password({"email": email, "password": password})
        except Exception as e:
            # Never echo the password or the provider's raw error, which can
            # contain the submitted credentials.
            raise AuthError(
                f"Could not sign in as {email}. Check the email and password, and that "
                f"this computer is online. ({type(e).__name__})"
            ) from None

        user = getattr(response, "user", None)
        if user is None:
            raise AuthError("Sign-in did not return a user.")
        return self._load_operator(client, user, email)

    def restore(self, refresh_token: str) -> Operator:
        """Sign back in from a stored refresh token — no password.

        Fails with AuthError when the token was revoked (signed out elsewhere)
        or expired; the caller then shows the sign-in form. Fails with
        CloudTimeout when Supabase did not answer — the token may be fine.
        """
        return self._within_deadline(lambda: self._restore(refresh_token))

    def _restore(self, refresh_token: str) -> Operator:
        client = self._client_for_sign_in()
        try:
            response = client.auth.refresh_session(refresh_token)
        except Exception as e:
            raise AuthError(
                f"The saved sign-in has expired. Please sign in again. ({type(e).__name__})"
            ) from None
        user = getattr(response, "user", None)
        if user is None:
            raise AuthError("The saved sign-in has expired. Please sign in again.")
        return self._load_operator(client, user, getattr(user, "email", "") or "")

    @property
    def refresh_token(self) -> str | None:
        if self._client is None:
            return None
        session = self._client.auth.get_session()
        return getattr(session, "refresh_token", None) if session else None

    def _load_operator(self, client: Any, user: Any, email: str) -> Operator:
        try:
            profile = (
                client.table("profiles").select("*").eq("id", user.id).maybe_single().execute()
            )
        except Exception as e:
            # Signed in, but who they are is unknown: do not keep half a
            # session. Before, this escaped as a bare 500 — "That did not work".
            self._discard_session()
            raise AuthError(
                f"Signed in, but your profile could not be read. Check this computer's "
                f"internet connection and try again. ({type(e).__name__})"
            ) from None
        row = getattr(profile, "data", None) or {}
        operator = Operator(
            id=user.id,
            email=str(row.get("email") or getattr(user, "email", None) or email),
            full_name=row.get("full_name"),
            role=row.get("role", "viewer"),
        )
        self.operator = operator
        log.info("signed in as %s (%s)", operator.email, operator.role)
        return operator

    def sign_out(self) -> None:
        if self._client is not None:
            try:
                self._client.auth.sign_out()
            except Exception:
                log.debug("sign-out failed; clearing the local session anyway")
        self.operator = None

    @property
    def access_token(self) -> str | None:
        if self._client is None:
            return None
        session = self._client.auth.get_session()
        return getattr(session, "access_token", None) if session else None

    # ── records ──────────────────────────────────────────────────────────

    def _table(self, name: str) -> Any:
        return self._connect().table(name)

    def upsert_drone(self, hardware_id: str, name: str, uri: str) -> str:
        try:
            found = (
                self._table("drones").select("id").eq("hardware_id", hardware_id).execute()
            )
            rows = getattr(found, "data", None) or []
            if rows:
                return str(rows[0]["id"])
            created = (
                self._table("drones")
                .insert({"hardware_id": hardware_id, "name": name, "uri": uri})
                .execute()
            )
            return str((getattr(created, "data", None) or [{}])[0]["id"])
        except Exception as e:
            raise CloudError(f"could not register the drone: {type(e).__name__}") from e

    def upsert_session(self, row: dict[str, Any]) -> None:
        self._upsert("sessions", row)

    def upsert_flight(self, row: dict[str, Any]) -> None:
        self._upsert("flights", row)

    def _upsert(self, table: str, row: dict[str, Any]) -> None:
        try:
            self._table(table).upsert(row, on_conflict="id").execute()
        except Exception as e:
            raise CloudError(f"could not save the {table[:-1]} record: {type(e).__name__}") from e

    def insert_audit(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        try:
            self._table("audit_events").upsert(
                rows, on_conflict="id", ignore_duplicates=True
            ).execute()
        except Exception as e:
            raise CloudError(f"could not save audit events: {type(e).__name__}") from e

    # ── telemetry ────────────────────────────────────────────────────────

    def max_telemetry_index(self, flight_id: str) -> int | None:
        """The highest row the server already holds — the resume point.

        Asked of the server rather than tracked locally, so it survives a crash,
        a reinstall, or rows uploaded by the server-side import.
        """
        try:
            response = (
                self._table("telemetry")
                .select("index")
                .eq("flight_id", flight_id)
                .order("index", desc=True)
                .limit(1)
                .execute()
            )
        except Exception as e:
            raise CloudError(f"could not read the upload progress: {type(e).__name__}") from e
        rows = getattr(response, "data", None) or []
        return int(rows[0]["index"]) if rows else None

    def insert_session_samples(self, rows: list[dict[str, Any]]) -> None:
        """A session's vitals (sync/samples.py); a re-sent batch changes nothing."""
        if not rows:
            return
        try:
            self._table("session_samples").upsert(
                rows, on_conflict="session_id,seq", ignore_duplicates=True
            ).execute()
        except Exception as e:
            hint = (" — apply migration 20261006000013_session_samples.sql"
                    if "session_samples" in str(e) else "")
            raise CloudError(f"could not upload session vitals: {type(e).__name__}{hint}") from e

    def insert_telemetry(self, rows: list[dict[str, Any]]) -> None:
        if not rows:
            return
        rows = without_empty_point_id(rows)
        try:
            self._table("telemetry").upsert(
                rows, on_conflict="flight_id,index", ignore_duplicates=True
            ).execute()
            return
        except Exception as e:
            # The database has not had migration 20260928000012 yet. A failed
            # batch aborts the whole sync pass (syncer.sync_once), so refusing
            # here would stall every later flight and every frame behind this
            # one. Send the readings without the tag instead: the local CSV
            # keeps point_id, and the pipeline reads the CSV.
            if "point_id" not in str(e):
                raise CloudError(f"could not upload telemetry: {type(e).__name__}") from e
            log.warning("the database has no telemetry.point_id yet (apply migration "
                        "20260928000012); uploading these rows without it")
        try:
            self._table("telemetry").upsert(
                [{k: v for k, v in row.items() if k != "point_id"} for row in rows],
                on_conflict="flight_id,index", ignore_duplicates=True,
            ).execute()
        except Exception as e:
            raise CloudError(f"could not upload telemetry: {type(e).__name__}") from e

    # ── the flight log file ──────────────────────────────────────────────

    def upload_flight_csv(self, object_path: str, csv_path: Path) -> None:
        """Upload the flight's CSV, compressed. ~150 KB for a full-battery flight."""
        try:
            payload = gzip.compress(csv_path.read_bytes())
            self._connect().storage.from_(TELEMETRY_BUCKET).upload(
                object_path, payload,
                {"content-type": "text/csv", "content-encoding": "gzip"},
            )
        except Exception as e:
            # Already there: a previous attempt succeeded and the reply was lost.
            if "Duplicate" in str(e) or "already exists" in str(e):
                log.info("flight log %s already uploaded", object_path)
                return
            raise StorageError(f"could not upload the flight log: {type(e).__name__}",
                               full=storage_quota_refused(e)) from e

    # ── camera frames ────────────────────────────────────────────────────

    def upload_frame(self, object_path: str, path: Path, content_type: str,
                     *, replace: bool = False) -> None:
        """Upload one recorded frame (or, with replace, a session's frames.csv).

        A frame never changes, so "already exists" is success: a previous try
        landed and its reply was lost. The index is replaced, since it is
        written once more at the session's end."""
        try:
            options = {"content-type": content_type}
            if replace:
                options["upsert"] = "true"
            self._connect().storage.from_(FRAMES_BUCKET).upload(
                object_path, path.read_bytes(), options)
        except FileNotFoundError:
            log.warning("recorded frame %s is gone from disk; skipping it", path)
        except Exception as e:
            if "Duplicate" in str(e) or "already exists" in str(e):
                return
            raise StorageError(f"could not upload a camera frame: {type(e).__name__}",
                               full=storage_quota_refused(e)) from e

    # ── the data pipeline's results ──────────────────────────────────────

    _RESULTS_HINT = " — apply migration 20261009000016_pipeline_results.sql"

    def _results_error(self, what: str, e: Exception) -> CloudError:
        hint = self._RESULTS_HINT if "pipeline_" in str(e) else ""
        return CloudError(f"could not upload {what}: {type(e).__name__}{hint}")

    def upsert_result(self, row: dict[str, Any]) -> None:
        """A flight's pipeline result; re-processing replaces it."""
        try:
            self._table("pipeline_results").upsert(row, on_conflict="flight_id").execute()
        except Exception as e:
            raise self._results_error("the pipeline result", e) from e

    def upsert_findings(self, rows: list[dict[str, Any]]) -> None:
        """A flight's findings; a stretch keeps its id, so this replaces."""
        if not rows:
            return
        try:
            self._table("pipeline_findings").upsert(rows, on_conflict="id").execute()
        except Exception as e:
            raise self._results_error("the findings", e) from e

    def delete_findings_except(self, flight_id: str, keep: list[str]) -> None:
        """Retract the findings a re-process no longer makes."""
        try:
            query = self._table("pipeline_findings").delete().eq("flight_id", flight_id)
            if keep:
                query = query.not_.in_("id", keep)
            query.execute()
        except Exception as e:
            raise self._results_error("the findings", e) from e

    _SESSION_RESULTS_HINT = " — apply migration 20261009000018_session_results.sql"

    def upsert_session_result(self, row: dict[str, Any]) -> None:
        """A session's own pipeline result; re-processing replaces it."""
        try:
            self._table("pipeline_session_results").upsert(
                row, on_conflict="session_id").execute()
        except Exception as e:
            hint = self._SESSION_RESULTS_HINT if "pipeline_session" in str(e) else ""
            raise CloudError(f"could not upload the session's result: "
                             f"{type(e).__name__}{hint}") from e

    def delete_session_findings_except(self, session_id: str, keep: list[str]) -> None:
        """Retract the session findings a re-process no longer makes — never a
        flight's (scope 'flight')."""
        try:
            query = (self._table("pipeline_findings").delete()
                     .eq("session_id", session_id).eq("scope", "session"))
            if keep:
                query = query.not_.in_("id", keep)
            query.execute()
        except Exception as e:
            raise self._results_error("the session's findings", e) from e

    def request_backfill(self, flight_id: str, object_path: str) -> None:
        """Ask the server to fill any missing rows from the uploaded file.

        Best-effort and deliberately not awaited: once the file is up, the
        server can finish the import even if this laptop closes immediately.
        """
        try:
            self._connect().functions.invoke(
                BACKFILL_FUNCTION,
                {"body": {"flight_id": flight_id, "object_path": object_path}},
            )
        except Exception as e:
            log.info("backfill request failed (%s); the laptop will finish the upload",
                     type(e).__name__)

    # ── playback: plans, traces, and reading flights back ────────────────

    def _playback_error(self, what: str, e: Exception) -> CloudError:
        text = str(e)
        if "row-level security" in text or "42501" in text:
            return CloudRefused(f"Supabase refused {what}: not this account's flight")
        hint = _PLAYBACK_HINT if "flight_plans" in text or "flight_traces" in text else ""
        return CloudError(f"could not upload {what}: {type(e).__name__}{hint}")

    def insert_flight_plan(self, row: dict[str, Any]) -> None:
        """A flight's plan as flown (sync/flight_playback.py). Append-only:
        a re-send is ignored, never an update."""
        try:
            self._table("flight_plans").upsert(
                row, on_conflict="flight_id", ignore_duplicates=True).execute()
        except Exception as e:
            raise self._playback_error("the plan as flown", e) from e

    def insert_flight_traces(self, rows: list[dict[str, Any]]) -> None:
        """A flight's control trace, a batch at a time; a re-sent batch changes nothing."""
        if not rows:
            return
        try:
            self._table("flight_traces").upsert(
                rows, on_conflict="flight_id,seq", ignore_duplicates=True).execute()
        except Exception as e:
            raise self._playback_error("the control trace", e) from e

    def _read_pages(self, table: str, flight_id: str, order: str, limit: int,
                    what: str) -> list[dict[str, Any]]:
        rows: list[dict[str, Any]] = []
        try:
            while len(rows) < limit:
                start = len(rows)
                end = min(start + PAGE, limit) - 1          # inclusive, as range() takes it
                response = (self._table(table).select("*").eq("flight_id", flight_id)
                            .order(order).range(start, end).execute())
                page = list(getattr(response, "data", None) or [])
                rows += page
                if len(page) < end - start + 1:
                    break                                   # a short page is the last
        except Exception as e:
            hint = _PLAYBACK_HINT if table in ("flight_plans", "flight_traces") else ""
            raise CloudError(f"could not read {what}: {type(e).__name__}{hint}") from e
        return rows

    def read_telemetry(self, flight_id: str, limit: int) -> list[dict[str, Any]]:
        """A flight's telemetry rows in order, at most `limit`."""
        return self._read_pages("telemetry", flight_id, "index", limit, "the flight's readings")

    def read_flight_traces(self, flight_id: str, limit: int) -> list[dict[str, Any]]:
        """A flight's control trace in order, flattened back to the trace CSV's
        columns (the extra ones were kept in `values`). [] before it is uploaded."""
        rows = self._read_pages("flight_traces", flight_id, "seq", limit, "the control trace")
        return [{**(row.get("values") or {}), **{k: v for k, v in row.items() if k != "values"}}
                for row in rows]

    def read_flight_plan(self, flight_id: str) -> dict[str, Any] | None:
        """The plan as flown, shaped like the file it came from; None if not uploaded."""
        try:
            response = (self._table("flight_plans").select("mission, room, moves")
                        .eq("flight_id", flight_id).limit(1).execute())
        except Exception as e:
            raise CloudError(f"could not read the plan as flown: {type(e).__name__}"
                             f"{_PLAYBACK_HINT}") from e
        rows = getattr(response, "data", None) or []
        return dict(rows[0]) if rows else None

    def list_flights(self, limit: int, mode: str | None = None) -> list[dict[str, Any]]:
        """Every signed-in person's flights, newest first (RLS: any signed-in
        user reads every flight), each with whether its plan is uploaded."""
        try:
            query = self._table("flights").select(f"{FLIGHT_COLUMNS}, flight_plans(flight_id)")
            if mode:
                query = query.eq("mode", mode)
            response = query.order("started_at", desc=True).limit(min(limit, PAGE)).execute()
        except Exception as e:
            # Before migration 20261011000019 the embed fails: list without it.
            try:
                query = self._table("flights").select(FLIGHT_COLUMNS)
                if mode:
                    query = query.eq("mode", mode)
                response = query.order("started_at", desc=True).limit(min(limit, PAGE)).execute()
            except Exception:
                raise CloudError(f"could not list the flights: {type(e).__name__}") from e
        out: list[dict[str, Any]] = []
        for row in getattr(response, "data", None) or []:
            plans = row.pop("flight_plans", None)
            row["has_plan"] = bool(plans)
            out.append(row)
        return out

    # ── missions queued on the web ───────────────────────────────────────

    def queued_missions(self) -> list[dict[str, Any]]:
        try:
            response = (
                self._table("missions")
                .select("*")
                .eq("status", "queued")
                .order("created_at")
                .execute()
            )
            return list(getattr(response, "data", None) or [])
        except Exception as e:
            raise CloudError(f"could not read the mission queue: {type(e).__name__}") from e

    def claim_mission(self, mission_id: str, agent_id: str) -> dict[str, Any] | None:
        try:
            response = self._connect().rpc(
                "claim_mission", {"mission_id": mission_id, "agent_id": agent_id}
            ).execute()
        except Exception as e:
            raise CloudError(f"could not claim the mission: {type(e).__name__}") from e
        data = getattr(response, "data", None)
        if isinstance(data, list):
            data = data[0] if data else None
        return data or None

    def update_mission(self, mission_id: str, values: dict[str, Any]) -> None:
        try:
            self._table("missions").update(values).eq("id", mission_id).execute()
        except Exception as e:
            raise CloudError(f"could not update the mission: {type(e).__name__}") from e
