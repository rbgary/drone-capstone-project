# This file was modified on 11/4/2026 with the assistance of Claude Code in VS Code
# (Claude Opus 5.5, Anthropic, 2026). Adds tests that a second, unmeasured station no
# longer blocks a placed drone unless the room requires two stations, and moves the
# "names its channel" test to a drone with no measured station.

"""The first stage between base-station light and a position that is not done
(flight/position_stages.py) — one rule for the map, the stages and ② Position."""

from __future__ import annotations

from dronedeck.flight.position_stages import first_blocker
from dronedeck.safety.flight_guard import PositioningStatus

SETTLED = (0.0004, 0.0004, 0.0004)


def status(*, received=(), geometry=(0, 1), light=4, variance=SETTLED):
    return PositioningStatus(received=tuple(received), calibrated=(0, 1),
                             with_geometry=tuple(geometry), stored=(0, 1, 2, 3),
                             variance_m2=variance, light_sensors=light)


def stage(st, deck=True, fw=False):
    found = first_blocker(st, deck_detected=deck, deck_needs_fw=fw)
    return None if found is None else found.stage


class TestTheOrder:
    def test_no_deck_comes_before_everything(self):
        assert stage(status(light=0), deck=False) == "deck"

    def test_deck_firmware_before_light(self):
        assert stage(status(light=0), fw=True) == "firmware"

    def test_no_light_at_all_is_its_own_stage(self):
        """2026-10-24: a whole session read 0 of 4 sensors — not a channel or a
        geometry problem, and the fix is the stations or the deck."""
        found = first_blocker(status(light=0), deck_detected=True, deck_needs_fw=False)
        assert found is not None and found.stage == "light"
        assert "0 of 4" in found.text and "solid green" in found.fix

    def test_light_but_nothing_decoded_points_at_the_channels(self):
        found = first_blocker(status(light=3), deck_detected=True, deck_needs_fw=False)
        assert found is not None and found.stage == "decoded" and "Ch 1 and Ch 2" in found.fix

    def test_a_decoded_station_without_a_place_names_its_channel(self):
        found = first_blocker(status(received=(0, 1), geometry=()),
                              deck_detected=True, deck_needs_fw=False)
        assert found is not None and found.stage == "stored" and "Ch 1 and Ch 2" in found.text

    def test_a_second_station_without_a_place_does_not_block_a_placed_drone(self):
        """2026-11-04: Ch 1 measured and settled; Ch 2 in view, not measured.
        One usable station is enough (MIN_USABLE_STATIONS), so it is placed."""
        assert stage(status(received=(0, 1), geometry=(0,))) is None

    def test_a_room_that_insists_on_two_names_the_unmeasured_one(self, monkeypatch):
        monkeypatch.setenv("DRONEDECK_MIN_STATIONS", "2")
        found = first_blocker(status(received=(0, 1), geometry=(0,)),
                              deck_detected=True, deck_needs_fw=False)
        assert found is not None and found.stage == "stored" and "Ch 2" in found.text

    def test_not_settled_says_how_far(self):
        assert stage(status(received=(0, 1), variance=(1.0, 1.0, 1.0))) == "settled"

    def test_placed_is_none(self):
        assert stage(status(received=(0, 1))) is None

    def test_unknown_deck_facts_do_not_block(self):
        assert first_blocker(status(received=(0, 1)), deck_detected=None,
                             deck_needs_fw=None) is None


class TestTheReasonAtSessionStart:
    def test_no_light_says_so_not_no_signal(self):
        problems = " ".join(status(light=0).problems())
        assert "No light reaches the positioning deck" in problems

    def test_light_but_undecoded_says_channels(self):
        assert "own" in " ".join(status(light=2).problems())

    def test_without_the_sensors_in_the_stream_the_old_words_stand(self):
        assert "No base station signal" in " ".join(status(light=None).problems())
