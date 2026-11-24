"""The gauge worker: batches the web queued → readings in the database.

A fake store plays Supabase (claims are atomic like the real update), and a
fake reader plays the pipeline; one test runs the real child process end to end.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from dronedeck.gauge_worker import (
    GaugeWorker,
    Outcome,
    PhotoFailed,
    annotated_path,
    read_in_child,
)
from dronedeck.sync.cloud import CloudError
from tests.pipeline.test_gauge import draw_gauge, encode


class FakeStore:
    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.images: dict[str, dict[str, Any]] = {}
        self.objects: dict[str, bytes] = {}
        self.offline = False
        self.requeued_before: str | None = None

    def add_batch(self, batch_id: str, photos: dict[str, bytes], status: str = "queued") -> None:
        self.batches[batch_id] = {"id": batch_id, "status": status, "image_count": len(photos),
                                  "done_count": 0, "failed_count": 0}
        for name, data in photos.items():
            image_id = f"{batch_id}-{name}"
            path = f"{batch_id}/original/{image_id}.png"
            self.objects[path] = data
            self.images[image_id] = {"id": image_id, "batch_id": batch_id, "filename": name,
                                     "original_path": path, "status": "pending"}

    def _online(self) -> None:
        if self.offline:
            raise CloudError("offline")

    def gauge_requeue_stale(self, claimed_before: str) -> None:
        self._online()
        self.requeued_before = claimed_before

    def gauge_pending(self, limit: int) -> list[dict[str, Any]]:
        self._online()
        return [dict(i) for i in self.images.values()
                if i["status"] == "pending"
                and self.batches[i["batch_id"]]["status"] in ("queued", "processing")][:limit]

    def gauge_claim(self, image_id: str, claimed_at: str) -> bool:
        self._online()
        image = self.images[image_id]
        if image["status"] != "pending":
            return False
        image.update(status="processing", claimed_at=claimed_at)
        return True

    def gauge_download(self, object_path: str) -> bytes:
        self._online()
        return self.objects[object_path]

    def gauge_upload(self, object_path: str, data: bytes, content_type: str) -> None:
        self._online()
        self.objects[object_path] = data

    def gauge_save_image(self, image_id: str, values: dict[str, Any]) -> None:
        self._online()
        self.images[image_id].update(values)

    def gauge_image_statuses(self, batch_id: str) -> list[str]:
        self._online()
        return [i["status"] for i in self.images.values() if i["batch_id"] == batch_id]

    def gauge_update_batch(self, batch_id: str, values: dict[str, Any]) -> None:
        self._online()
        self.batches[batch_id].update(values)


def fake_reader(data: bytes, filename: str) -> Outcome:
    if data == b"broken":
        raise PhotoFailed("not an image (UnidentifiedImageError)")
    if data == b"crash":
        raise RuntimeError("boom")
    kpa = float(data.decode())
    state = "danger" if kpa >= 400 else "warning" if kpa > 200 else "ok"
    return Outcome({"kpa": kpa, "state": state, "error": None, "reader": "color",
                    "detail": {}}, b"\x89PNG-" + filename.encode())


def worker(store: FakeStore | None) -> GaugeWorker:
    return GaugeWorker(lambda: store, reader=fake_reader)


def test_a_queued_batch_is_read_and_its_readings_stored():
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"150", "b.png": b"450"})
    assert worker(store).run_once() == 2
    a, b = store.images["b1-a.png"], store.images["b1-b.png"]
    assert (a["status"], a["kpa"], a["state"]) == ("done", 150.0, "ok")
    assert (b["status"], b["kpa"], b["state"]) == ("done", 450.0, "danger")
    assert a["annotated_path"] == annotated_path("b1", "b1-a.png") == "b1/annotated/b1-a.png.png"
    assert store.objects[a["annotated_path"]].startswith(b"\x89PNG")
    assert a["processed_at"]
    assert store.batches["b1"]["status"] == "done"
    assert store.batches["b1"]["done_count"] == 2 and store.batches["b1"]["failed_count"] == 0
    assert store.batches["b1"]["finished_at"]


def test_a_batch_still_uploading_is_left_alone():
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"150"}, status="uploading")
    assert worker(store).run_once() == 0
    assert store.images["b1-a.png"]["status"] == "pending"


def test_a_bad_file_fails_alone_and_the_batch_carries_on():
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"broken", "b.png": b"300", "c.png": b"crash"})
    assert worker(store).run_once() == 1
    assert store.images["b1-a.png"]["status"] == "failed"
    assert "not an image" in store.images["b1-a.png"]["error"]
    assert store.images["b1-a.png"]["state"] == "unreadable"
    assert store.images["b1-b.png"]["status"] == "done"
    crashed = store.images["b1-c.png"]
    assert crashed["status"] == "failed" and "RuntimeError" in crashed["error"]
    assert store.batches["b1"]["status"] == "done"
    assert (store.batches["b1"]["done_count"], store.batches["b1"]["failed_count"]) == (3, 2)


def test_a_photo_another_agent_holds_is_not_read_twice():
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"150"})
    reads = []

    def counting(data: bytes, filename: str) -> Outcome:
        reads.append(filename)
        return fake_reader(data, filename)

    mine = GaugeWorker(lambda: store, reader=counting)
    real_pending = store.gauge_pending

    def stale_view(limit: int) -> list[dict[str, Any]]:
        rows = real_pending(limit)
        store.images["b1-a.png"]["status"] = "processing"      # someone claims it first
        return rows

    store.gauge_pending = stale_view                            # type: ignore[method-assign]
    assert mine.run_once() == 0
    assert reads == []


def test_going_offline_puts_the_photo_back_and_stops():
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"150", "b.png": b"300"})
    real = store.gauge_download

    def drop(path: str) -> bytes:
        raise CloudError("the download failed")                  # the link drops mid-photo

    store.gauge_download = drop                                  # type: ignore[method-assign]
    assert worker(store).run_once() == 0
    store.gauge_download = real                                  # type: ignore[method-assign]
    assert store.images["b1-a.png"]["status"] == "pending"
    assert store.batches["b1"]["status"] == "queued"
    assert worker(store).run_once() == 2                         # the next pass reads both


def test_a_photo_stuck_when_the_link_is_fully_down_waits_for_the_stale_requeue():
    """The release needs the network too. If it fails the photo stays 'processing',
    and the NEXT pass's stale requeue (15 minutes on) puts it back."""
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"150"})

    def down(path: str) -> bytes:
        store.offline = True
        raise CloudError("offline")

    store.gauge_download = down                                  # type: ignore[method-assign]
    assert worker(store).run_once() == 0
    assert store.images["b1-a.png"]["status"] == "processing"


def test_signed_out_does_nothing():
    assert worker(None).run_once() == 0


def test_a_batch_with_work_left_is_processing():
    store = FakeStore()
    store.add_batch("b1", {"a.png": b"150"})
    store.images["b-extra"] = {"id": "b-extra", "batch_id": "b1", "filename": "x",
                               "original_path": "p", "status": "processing"}
    worker(store).run_once()
    assert store.batches["b1"]["status"] == "processing"
    assert "finished_at" not in store.batches["b1"]


def test_stale_claims_are_requeued_each_pass():
    store = FakeStore()
    worker(store).run_once()
    assert store.requeued_before is not None


def test_the_real_reader_runs_in_a_child_process():
    outcome = read_in_child(encode(draw_gauge(450)), "My photo.png")
    assert outcome.values["state"] == "danger"
    assert outcome.values["kpa"] == pytest.approx(450, abs=5)
    assert outcome.annotated_png and outcome.annotated_png.startswith(b"\x89PNG")
    json.dumps(outcome.values)                       # what goes to PostgREST is plain JSON


def test_the_real_reader_reports_a_file_that_is_not_an_image():
    with pytest.raises(PhotoFailed, match="not an image"):
        read_in_child(b"hello", "x.png")


# ── the real client's calls, against a recording fake of supabase-py ─────────

class Recorder:
    """Records every chained call; `execute()` answers with `data`."""

    def __init__(self, data: Any = None, fail: Exception | None = None) -> None:
        self.calls: list[tuple[str, tuple[Any, ...]]] = []
        self.data, self.fail = data, fail

    def __getattr__(self, name: str) -> Any:
        def call(*args: Any, **_kw: Any) -> Any:
            self.calls.append((name, args))
            if name == "execute":
                if self.fail:
                    raise self.fail
                return type("Response", (), {"data": self.data})()
            return self
        return call

    @property
    def not_(self) -> Recorder:
        return self


class RecordingClient:
    def __init__(self, recorder: Recorder) -> None:
        self.recorder = recorder
        self.uploads: list[tuple[str, str, dict[str, str]]] = []
        self.tables: list[str] = []
        self.storage = self

    def table(self, name: str) -> Recorder:
        self.tables.append(name)
        return self.recorder

    def from_(self, bucket: str) -> RecordingClient:
        self.bucket = bucket
        return self

    def upload(self, path: str, data: bytes, options: dict[str, str]) -> None:
        self.uploads.append((self.bucket, path, options))


def cloud_with(recorder: Recorder) -> tuple[Any, RecordingClient]:
    from dronedeck.sync.cloud import SupabaseCloud
    client = RecordingClient(recorder)
    return SupabaseCloud("http://x", "key", client=client), client


def test_the_queue_read_asks_for_released_batches_oldest_first():
    rows = [{"id": "i1", "batch_id": "b1", "filename": "a", "original_path": "p",
             "gauge_batches": {"status": "queued"}}]
    cloud, client = cloud_with(Recorder(rows))
    found = cloud.gauge_pending(25)
    assert client.tables == ["gauge_images"]
    calls = dict(client.recorder.calls)
    assert calls["eq"] == ("status", "pending")
    assert calls["in_"] == ("gauge_batches.status", ["queued", "processing"])
    assert calls["limit"] == (25,)
    assert found == [{"id": "i1", "batch_id": "b1", "filename": "a", "original_path": "p"}]


def test_a_claim_succeeds_only_when_a_row_matched():
    assert cloud_with(Recorder([{"id": "i1"}]))[0].gauge_claim("i1", "now") is True
    assert cloud_with(Recorder([]))[0].gauge_claim("i1", "now") is False


def test_the_annotated_picture_replaces_an_earlier_one():
    cloud, client = cloud_with(Recorder())
    cloud.gauge_upload("b1/annotated/i1.png", b"png", "image/png")
    assert client.uploads == [("gauge-photos", "b1/annotated/i1.png",
                               {"content-type": "image/png", "upsert": "true"})]


def test_a_missing_migration_says_which_one_to_apply():
    cloud, _ = cloud_with(Recorder(fail=RuntimeError("relation gauge_images does not exist")))
    with pytest.raises(CloudError, match="20261123000022_gauge_batches"):
        cloud.gauge_pending(5)
