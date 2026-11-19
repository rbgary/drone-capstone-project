"""The Set up page's engine, against a fake drone — no radio, no flashing.

Two parts since 2026-11-15: the drone firmware (main) and the positioning deck
(lighthouse). The AI deck's camera and Wi-Fi are gone with the camera, and a
drone without an AI deck is set up like any other."""

from __future__ import annotations

import shutil
from dataclasses import replace

import pytest

from dronedeck import drone_setup
from dronedeck.drone_setup import (
    BUNDLE_DIR,
    DroneSetup,
    Facts,
    SetupError,
    load_manifest,
    needed_parts,
    reinstall_set,
)

MANIFEST = load_manifest()
FRESH = Facts(battery_v=4.1, fw_version=None, lighthouse_deck=True, lighthouse_needs_fw=True)
DONE = Facts(battery_v=4.1, fw_version=MANIFEST["drone_wifi"], lighthouse_deck=True)


class Rig:
    def __init__(self, facts: Facts | None, *, flash_ok=True, deck_takes_fw=True,
                 main_asks_for_deck_fw=False) -> None:
        self.facts = facts
        self.deck_takes_fw = deck_takes_fw          # the write is accepted
        self.main_asks_for_deck_fw = main_asks_for_deck_fw
        self.flashed: list[str] = []
        self.radio: list[str] = []
        self.flash_ok = flash_ok
        self.setup = DroneSetup(
            pause_radio=lambda: self.radio.append("pause"),
            resume_radio=lambda: self.radio.append("resume"),
            probe=self.probe, flash=self.flash, sleep=lambda _s: None,
        )

    def probe(self) -> Facts | None:
        return self.facts

    def flash(self, part, path, on_progress) -> bool:
        on_progress(0.5)
        on_progress(1.0)
        self.flashed.append(part)
        if self.flash_ok and self.facts is not None:
            if part == "main":
                self.facts = replace(self.facts, fw_version=MANIFEST["drone_wifi"])
                if self.main_asks_for_deck_fw:
                    # a new main firmware can name a different deck image
                    self.facts = replace(self.facts, lighthouse_needs_fw=True)
            if part == "lighthouse" and self.deck_takes_fw:
                self.facts = replace(self.facts, lighthouse_needs_fw=False)
        return self.flash_ok

    def run(self, action: str, *args) -> dict:
        getattr(self.setup, action)(*args)
        self.setup.wait(10)
        return self.setup.state()


class TestNeededParts:
    def test_a_new_drone_needs_both(self):
        assert needed_parts(FRESH, MANIFEST) == {"main": "needed", "lighthouse": "needed"}

    def test_a_set_up_drone_needs_nothing(self):
        assert set(needed_parts(DONE, MANIFEST).values()) == {"installed"}

    def test_old_drone_software_alone_is_updated(self):
        old_main = replace(DONE, fw_version=MANIFEST["drone_wifi"] - 1)
        assert needed_parts(old_main, MANIFEST) == {"main": "needed", "lighthouse": "installed"}

    def test_the_positioning_deck_reports_its_own_state(self):
        assert needed_parts(replace(DONE, lighthouse_needs_fw=True), MANIFEST)["lighthouse"] \
            == "needed"
        assert needed_parts(replace(DONE, lighthouse_deck=False), MANIFEST)["lighthouse"] \
            == "absent"

    def test_only_the_two_parts_are_offered(self):
        assert drone_setup.PART_ORDER == ("main", "lighthouse")
        assert set(MANIFEST["parts"]) == {"main", "lighthouse"}


class TestCheck:
    def test_it_reports_without_touching_the_drone(self):
        rig = Rig(FRESH)
        state = rig.run("check")
        assert state["phase"] == "ready"
        assert rig.flashed == []

    def test_an_already_set_up_drone_is_said_so(self):
        state = Rig(DONE).run("check")
        assert state["phase"] == "done" and "already" in state["message"]

    def test_no_drone_says_what_to_do(self):
        state = Rig(None).run("check")
        assert state["phase"] == "failed" and "switch the drone on" in state["message"]

    def test_a_drone_without_an_ai_deck_is_not_refused(self):
        """Until 2026-11-15 a drone with no AI deck was refused before main or
        the positioning deck were written. There is no camera now."""
        state = Rig(FRESH).run("check")
        assert state["phase"] == "ready" and "AI deck" not in (state["message"] or "")

    def test_the_radio_is_given_back_afterwards(self):
        rig = Rig(FRESH)
        rig.run("check")
        assert rig.radio == ["pause", "resume"]


class TestInstall:
    def test_a_new_drone_gets_main_then_the_positioning_deck(self):
        rig = Rig(FRESH)
        state = rig.run("install")
        assert rig.flashed == ["main", "lighthouse"]
        assert state["phase"] == "done"
        assert state["parts"] == {"main": "installed", "lighthouse": "installed"}

    def test_a_drone_with_no_positioning_deck_is_set_up_without_it(self):
        rig = Rig(replace(FRESH, lighthouse_deck=False, lighthouse_needs_fw=False))
        state = rig.run("install")
        assert rig.flashed == ["main"]
        assert state["phase"] == "done" and state["parts"]["lighthouse"] == "absent"

    def test_the_deck_is_asked_again_after_main_because_main_names_its_image(self):
        rig = Rig(replace(DONE, fw_version=None), main_asks_for_deck_fw=True)
        state = rig.run("install")
        assert rig.flashed == ["main", "lighthouse"]
        assert state["phase"] == "done"

    def test_a_deck_that_still_asks_after_the_write_is_reported(self):
        rig = Rig(FRESH, deck_takes_fw=False)
        state = rig.run("install")
        assert state["phase"] == "failed" and "still asks for its firmware" in state["message"]
        assert rig.flashed == ["main", "lighthouse"]

    def test_only_what_is_missing_is_flashed(self):
        rig = Rig(replace(DONE, fw_version=None))
        rig.run("install")
        assert rig.flashed == ["main"]

    def test_a_low_battery_is_refused_before_anything_is_written(self):
        rig = Rig(replace(FRESH, battery_v=3.7))
        state = rig.run("install")
        assert state["phase"] == "failed" and "3.70 V" in state["message"]
        assert rig.flashed == []

    def test_a_failed_flash_stops_and_says_it_resumes(self):
        rig = Rig(FRESH, flash_ok=False)
        state = rig.run("install")
        assert state["phase"] == "failed" and "continues from here" in state["message"]
        assert rig.flashed == ["main"]

    def test_a_damaged_bundle_file_is_refused(self, tmp_path):
        bundle = tmp_path / "bundle"
        shutil.copytree(BUNDLE_DIR, bundle)
        (bundle / "cf2.bin").write_bytes(b"not the tested file")
        rig = Rig(FRESH)
        rig.setup._bundle = bundle
        state = rig.run("install")
        assert state["phase"] == "failed" and "damaged" in state["message"]
        assert rig.flashed == []

    def test_the_bundle_matches_its_manifest(self):
        for part in drone_setup.PART_ORDER:
            assert drone_setup.verified_file(BUNDLE_DIR, part, MANIFEST).exists()

    def test_the_bundle_carries_no_ai_deck_image(self):
        assert not (BUNDLE_DIR / "aideck_gap8.bin").exists()
        assert not (BUNDLE_DIR / "aideck_esp.bin").exists()

    def test_a_second_install_while_one_runs_is_refused(self):
        rig = Rig(FRESH)
        rig.setup._worker = type("Busy", (), {"is_alive": lambda self: True})()
        with pytest.raises(drone_setup.SetupError):
            rig.setup.install()


class TestReinstallAnyway:
    def test_the_parts_asked_for_come_back(self):
        assert reinstall_set(["main"]) == {"main"}
        assert reinstall_set(["lighthouse", "main"]) == {"main", "lighthouse"}
        assert reinstall_set([]) == frozenset()

    def test_an_unknown_part_is_refused_by_name(self):
        with pytest.raises(SetupError, match="gap8"):
            reinstall_set(["gap8"])

    def test_the_ai_decks_old_parts_say_why_they_are_gone(self):
        with pytest.raises(SetupError, match="no camera"):
            reinstall_set(["camera", "wifi"])

    def test_nothing_asked_on_a_set_up_drone_changes_nothing(self):
        rig = Rig(DONE)
        state = rig.run("install")
        assert rig.flashed == [] and state["phase"] == "done"

    def test_the_drone_firmware_again(self):
        rig = Rig(DONE)
        state = rig.run("install", ["main"])
        assert rig.flashed == ["main"]
        assert state["phase"] == "done"

    def test_a_positioning_deck_asked_for_again_is_not_overruled_after_main(self):
        # After main the deck is normally asked again — and answers it is fine.
        rig = Rig(DONE)
        rig.run("install", ["main", "lighthouse"])
        assert rig.flashed == ["main", "lighthouse"]

    def test_a_deck_that_is_not_fitted_is_not_written(self):
        rig = Rig(replace(DONE, lighthouse_deck=False))
        state = rig.run("install", ["lighthouse"])
        assert rig.flashed == [] and state["parts"]["lighthouse"] == "absent"

    def test_reinstalling_still_refuses_a_low_battery(self):
        rig = Rig(replace(DONE, battery_v=3.7))
        state = rig.run("install", ["main"])
        assert state["phase"] == "failed" and rig.flashed == []

    def test_an_unknown_part_is_refused_before_the_radio_is_taken(self):
        rig = Rig(DONE)
        with pytest.raises(SetupError):
            rig.setup.install(["gap8"])
        assert rig.radio == []
