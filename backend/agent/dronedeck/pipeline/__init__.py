"""The data pipeline: a recorded flight → findings, and a verdict for every
inspection point.

    load ─▶ clean ─▶ classify ─▶ interpret ─▶ save
             (4.2)     (4.6)        (4.6)

    contracts.py   the types and interfaces every stage builds against — THE
                   agreement (docs/handoffs/sprint-1/done/dpp-contract.txt)
    sources.py     where a flight comes from   (a port; LocalFlightSource)
    sinks.py       where a result goes         (a port; LocalResultSink)
    compose.py     which implementation each stage uses — the one line a
                   stage owner changes to plug in
    runner.py      runs each stage once over the whole flight (contract v3)
    stages/        clean (Kevin), classify (Reagan), interpret (Samuel)

Ports and adapters: the core never knows whether a flight came off this
laptop's disk or a cloud bucket. A cloud worker later is a new source and a
new sink, not a rewrite.

NOTHING HERE IMPORTS THE FLIGHT CODE (tests/pipeline holds that line): the
pipeline must run with no radio, and in a process of its own beside a flight.
"""

#: Bumped whenever the shape of a result changes. Recorded in every result.
#: "4" (2026-11-15): no frames, no enhance stage, no image fields on a finding.
PIPELINE_VERSION = "4"
