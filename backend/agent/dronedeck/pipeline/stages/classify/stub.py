"""The placeholder classifier: every label is "unknown", and says why.

Never "normal": a stub that called everything normal would write confident
nonsense into every result — the failure that retired the greenhouse model
(docs/features/architecture.txt, crop-health predictions). Replaced by
blocks@1 (stages/classify/blocks.py; docs/handoffs/sprint-1/done/dpp-classify.txt).
"""

from __future__ import annotations

from dronedeck.pipeline.contracts import (
    ClassifyResult,
    CleanResult,
    FlightContext,
    Label,
    PointData,
)

NO_MODEL = "no classifier yet"


class StubClassifier:
    name = "stub"
    version = "0"

    def classify(self, data: PointData, clean: CleanResult,
                 ctx: FlightContext) -> ClassifyResult:
        model = f"{self.name}@{self.version}"
        return ClassifyResult(sensors=Label("unknown", None, model, NO_MODEL), features={})
