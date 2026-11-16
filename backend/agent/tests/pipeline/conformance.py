"""The contract checks EVERY stage implementation must pass.

To plug a stage in, add your class to the list for its stage below. Every check
in tests/pipeline/test_conformance.py then runs against it on the fixture — a
real flight's readings — plus an empty point.

These are the rules marked [checked] in docs/handoffs/sprint-1/done/dpp-contract.txt:
  1  self-contained (no flight, session, api, sync or camera imports)
  3  the input is never changed (a cleaner only flags)
  5  the same input gives the same output
  6  an empty point is a result, not an exception
  (Rules 2 and 4 were about frames; contract v3 has none.)
"""

from __future__ import annotations

from dronedeck.pipeline.stages.classify.blocks import BlocksClassifier
from dronedeck.pipeline.stages.classify.ground import GroundClassifier
from dronedeck.pipeline.stages.classify.stub import StubClassifier
from dronedeck.pipeline.stages.clean.robust import RobustCleaner
from dronedeck.pipeline.stages.clean.stub import StubCleaner

#: Every cleaner, checked against the contract.
CLEANERS = [StubCleaner, RobustCleaner]
#: Every classifier, checked against the contract.
CLASSIFIERS = [StubClassifier, BlocksClassifier, GroundClassifier]
