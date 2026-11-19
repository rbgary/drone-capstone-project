"""Which implementation each stage uses — the composition root.

THIS IS THE LINE A STAGE OWNER CHANGES TO PLUG IN: replace a stub with your
class, add your class to tests/pipeline/conformance.py, and every contract
check runs against it (docs/handoffs/sprint-1/done/dpp-contract.txt, "How you plug in").
"""

from __future__ import annotations

from dataclasses import dataclass

from dronedeck.pipeline.contracts import Classifier, Cleaner, Interpreter
from dronedeck.pipeline.stages.classify.blocks import BlocksClassifier
from dronedeck.pipeline.stages.classify.ground import GroundClassifier
from dronedeck.pipeline.stages.clean.robust import RobustCleaner
from dronedeck.pipeline.stages.interpret.findings import FindingInterpreter


@dataclass(frozen=True)
class Stages:
    cleaner: Cleaner
    classifier: Classifier
    interpreter: Interpreter

    def names(self) -> dict[str, str]:
        return {
            "clean": f"{self.cleaner.name}@{self.cleaner.version}",
            "classify": f"{self.classifier.name}@{self.classifier.version}",
            "interpret": f"{self.interpreter.name}@{self.interpreter.version}",
        }


def default_stages() -> Stages:
    return Stages(
        cleaner=RobustCleaner(),          # replaced hampel@1 (Kevin) on 2026-10-09
        classifier=BlocksClassifier(),    # blocks against expected: ml/anomaly-eval/
        interpreter=FindingInterpreter(), # events → findings and verdicts
    )


def session_stages() -> Stages:
    """A session around its flights: the same cleaner and interpreter; the
    ground classifier in place of the flight's (stages/classify/ground.py)."""
    stages = default_stages()
    return Stages(cleaner=stages.cleaner, classifier=GroundClassifier(),
                  interpreter=stages.interpreter)
