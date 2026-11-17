"""Every registered stage implementation, against the contract's rules.

The list of implementations is conformance.py; this file is the checks."""

from __future__ import annotations

import inspect
import subprocess
import sys
from pathlib import Path

import pytest

from dronedeck.pipeline.contracts import (
    CleanResult,
    FlightContext,
    InspectionPoint,
    PointData,
)
from dronedeck.pipeline.sources import LocalFlightSource
from tests.pipeline.conformance import CLASSIFIERS, CLEANERS

FIXTURE = Path(__file__).parent / "fixtures" / "data"
FORBIDDEN = ("dronedeck.flight", "dronedeck.session", "dronedeck.api",
             "dronedeck.sync")


@pytest.fixture(scope="module")
def flight():
    source = LocalFlightSource(FIXTURE)
    return source.load("372bbdc4-d323-42bb-9e6c-29ef02e3794c")


@pytest.fixture
def ctx(tmp_path, flight) -> FlightContext:
    return FlightContext(flight_id=flight.flight_id, session_id=flight.session_id,
                         temp_unit=flight.temp_unit, ground_z_m=None,
                         started_at=flight.started_at, workdir=tmp_path / "work")


def points(flight):
    """What a stage may be handed: the whole flight (what the runner gives),
    each point's view, and an empty point."""
    empty = PointData(InspectionPoint("EMPTY", None, 0, 0, 0.4), ())
    return [flight.whole, *flight.points, empty]


def imports_of(cls) -> set[str]:
    module = inspect.getmodule(cls)
    assert module is not None
    code = (f"import sys, {module.__name__}; "
            f"print('\\n'.join(m for m in sys.modules if m.startswith('dronedeck')))")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True,
                         check=True)
    return set(out.stdout.split())


ALL = [*CLEANERS, *CLASSIFIERS]


@pytest.mark.parametrize("stage", ALL, ids=lambda c: c.__name__)
def test_rule_1_self_contained(stage):
    loaded = imports_of(stage)
    assert not [m for m in loaded if m.startswith(FORBIDDEN)]


@pytest.mark.parametrize("stage", ALL, ids=lambda c: c.__name__)
def test_every_stage_names_and_versions_itself(stage):
    instance = stage()
    assert isinstance(instance.name, str) and instance.name
    assert isinstance(instance.version, str) and instance.version


@pytest.mark.parametrize("cleaner", CLEANERS, ids=lambda c: c.__name__)
class TestCleaners:
    def test_readings_come_back_unchanged_and_flags_point_at_real_rows(self, cleaner,
                                                                       flight, ctx):
        for data in points(flight):
            result = cleaner().clean(data, ctx)
            assert result.readings is data.readings or result.readings == data.readings
            indexes = {r.index for r in data.readings}
            columns = {c for r in data.readings for c in r.values}
            for flag in result.flags:
                assert flag.index in indexes
                assert flag.column is None or flag.column in columns
                assert flag.reason

    def test_the_same_input_gives_the_same_flags(self, cleaner, flight, ctx):
        data = flight.whole
        assert cleaner().clean(data, ctx).flags == cleaner().clean(data, ctx).flags


@pytest.mark.parametrize("classifier", CLASSIFIERS, ids=lambda c: c.__name__)
class TestClassifiers:
    def labels(self, classifier, data, ctx):
        clean = CleanResult(data.readings, ())
        return classifier().classify(data, clean, ctx)

    def test_the_sensor_label_is_valid_and_says_why_when_unknown(self, classifier, flight,
                                                                 ctx):
        for data in points(flight):
            label = self.labels(classifier, data, ctx).sensors
            assert label.value in ("normal", "faulty", "unknown")
            assert label.value != "unknown" or label.reason
            assert "@" in label.model

    def test_the_same_input_gives_the_same_labels(self, classifier, flight, ctx):
        data = flight.whole
        assert self.labels(classifier, data, ctx) == self.labels(classifier, data, ctx)

    def test_feature_names_carry_units(self, classifier, flight, ctx):
        result = self.labels(classifier, flight.whole, ctx)
        for name in result.features:
            assert "_" in name, f"feature {name!r} should name its unit"


def test_the_fixture_is_where_the_tests_think_it_is():
    assert (FIXTURE / "fixture.json").exists()
