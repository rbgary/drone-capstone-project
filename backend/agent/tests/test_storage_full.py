"""A full storage bucket no longer stops every upload (found 2026-11-08).

File storage and the database are separate resources with separate quotas.
When storage refuses a file — the flight's log, the one file the syncer sends
since the camera went (2026-11-15; frames and enhanced frames were cases here
until then) — it waits on this computer, and every database row still goes.
The next pass tries storage again, so uploads resume by themselves once space
is freed."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from dronedeck.sync import flight_playback
from dronedeck.sync.cloud import CloudError, StorageError, storage_quota_refused
from dronedeck.sync.outbox import Kind, Outbox
from dronedeck.sync.syncer import STORAGE_FULL, Syncer
from tests.test_playback import FLIGHT, write_flight
from tests.test_playback_cloud import flight_record
from tests.test_results_upload import ResultsCloud, finding, queue, write_result
from tests.test_sync import add_flight


class StorageCloud(ResultsCloud):
    """A cloud whose file storage can be full, or failing for another reason."""

    def __init__(self) -> None:
        super().__init__()
        self.storage_refuses: StorageError | None = None
        self.storage_calls = 0
        self.result_upserts = 0

    def _storage(self) -> None:
        self.storage_calls += 1
        if self.storage_refuses is not None:
            raise self.storage_refuses

    def upload_flight_csv(self, object_path, csv_path):
        self._storage()
        super().upload_flight_csv(object_path, csv_path)

    def upsert_result(self, row):
        self.result_upserts += 1
        super().upsert_result(row)


FULL = StorageError("could not upload the flight log: StorageApiError", full=True)


@pytest.fixture
def rig(tmp_path):
    """A flight that has landed and its processed result, waiting to upload."""
    outbox = Outbox(tmp_path / "outbox")
    add_flight(outbox, tmp_path)
    write_result(tmp_path / "r", findings=[finding("a")])
    queue(outbox, tmp_path / "r")
    cloud = StorageCloud()
    return outbox, cloud, Syncer(outbox, lambda: cloud, sleep=lambda s: None)


class TestStorageFull:
    def test_the_rows_go_and_the_files_wait(self, rig):
        outbox, cloud, syncer = rig
        cloud.storage_refuses = FULL
        status = syncer.sync_once()

        # Every database row reached Supabase…
        assert sorted(cloud.telemetry["flight-1"]) == [0, 1, 2, 3, 4]
        assert cloud.results["flight-1"]["findings_count"] == 1
        assert set(cloud.findings) == {"a"}
        # …and the log file did not: the flight stays pending until it is up.
        assert cloud.uploads == [] and cloud.objects == {}
        assert status.pending_flights == 1
        # A result waits on the flight's rows, not its log file: it is sent.
        assert outbox.count_pending(Kind.RESULTS) == 0

    def test_storage_is_tried_once_a_pass_not_once_a_file(self, rig):
        _, cloud, syncer = rig
        cloud.storage_refuses = FULL
        syncer.sync_once()
        assert cloud.storage_calls == 1
        syncer.sync_once()
        assert cloud.storage_calls == 2

    def test_the_window_is_told_storage_is_full(self, rig):
        _, cloud, syncer = rig
        cloud.storage_refuses = FULL
        status = syncer.sync_once()
        assert status.storage_full is True and status.storage_error == STORAGE_FULL
        assert status.last_error is None            # every row went: the pass succeeded
        sent = status.to_dict()
        assert sent["storage_full"] is True and sent["storage_error"] == STORAGE_FULL

    def test_freed_space_resumes_without_sending_anything_twice(self, rig):
        outbox, cloud, syncer = rig
        cloud.storage_refuses = FULL
        syncer.sync_once()
        cloud.storage_refuses = None
        status = syncer.sync_once()

        assert [path for path, _ in cloud.uploads] == ["flight-1.csv.gz"]
        assert cloud.backfills == [("flight-1", "flight-1.csv.gz")]
        assert cloud.objects == {}                  # no frames to send any more
        assert cloud.result_upserts == 1            # the rows went once, while storage was full
        assert status.everything_sent
        assert outbox.count_pending(Kind.RESULTS) == 0
        assert status.storage_error is None and status.storage_full is False

    def test_a_reprocess_while_full_sends_the_new_rows(self, rig, tmp_path):
        outbox, cloud, syncer = rig
        cloud.storage_refuses = FULL
        syncer.sync_once()
        write_result(tmp_path / "r", findings=[finding("a", "critical")])
        queue(outbox, tmp_path / "r")                # processed again: the record is replaced
        syncer.sync_once()
        assert cloud.findings["a"]["severity"] == "critical"


class TestOtherFailures:
    def test_a_storage_failure_that_is_not_the_quota_says_so(self, rig):
        _, cloud, syncer = rig
        cloud.storage_refuses = StorageError("could not upload the flight log: ConnectError")
        status = syncer.sync_once()
        assert status.storage_full is False
        assert "did not upload" in (status.storage_error or "")
        assert cloud.results                         # the rows still went

    def test_a_database_failure_still_ends_the_pass(self, rig):
        _, cloud, syncer = rig
        cloud.fail_with = CloudError("could not save audit events: ConnectError")
        status = syncer.sync_once()
        assert status.last_error and cloud.results == {} and cloud.storage_calls == 0

    def test_a_pass_that_never_reached_storage_keeps_what_it_last_said(self, rig):
        _, cloud, syncer = rig
        cloud.storage_refuses = FULL
        syncer.sync_once()
        cloud.fail_with = CloudError("offline")      # ends the next pass before any file
        status = syncer.sync_once()
        assert status.storage_full is True and status.storage_error == STORAGE_FULL


def test_playback_goes_once_the_rows_are_up_though_the_log_is_waiting(tmp_path):
    root = tmp_path / "data"
    write_flight(root)
    outbox = Outbox(tmp_path / "outbox")
    flight_record(outbox, root, sent=False)
    outbox.update(Kind.FLIGHT, FLIGHT, lambda p: p.__setitem__("_rows_sent", True))
    flight_playback.queue(outbox, FLIGHT)
    cloud = StorageCloud()
    Syncer(outbox, lambda: cloud, data_root=root)._send_playback(cloud)
    assert FLIGHT in cloud.plans


@pytest.mark.parametrize(("error", "full"), [
    (SimpleNamespace(status=402, code="", message="Payment required"), True),
    (SimpleNamespace(status=400, code="exceed_storage_size_quota", message=""), True),
    (Exception("Storage quota exceeded for this project"), True),
    (SimpleNamespace(status=413, code="Payload too large",
                     message="The object exceeded the maximum allowed size"), False),
    (ConnectionError("connection reset"), False),
])
def test_what_counts_as_the_quota(error, full):
    assert storage_quota_refused(error) is full
