PIPELINE FIXTURE — what is real here, and what was made up
=========================================================

data/ is laid out exactly like the agent's data folder, so LocalFlightSource
reads it with no special case.

  REAL      flights/2026-09-24/flight_372bbdc4_…csv — the first 400 rows (about
            40 s at 10 Hz) of flight 372bbdc4, flown by hand in the lab on
            2026-09-24. Real temperature, pressure, position and battery.

  ADDED     a point_id column: "P1" on rows 100–159, "P2" on rows 250–309,
            empty elsewhere. That flight was manual and had no inspection
            points; the tags are what the mission controller writes while
            HOLDING, placed so the pipeline's grouping can be tested.

  MADE UP   sessions/<id>/meta.json — a trimmed session naming that flight.
            sessions/<id>/missions/<flight>.json — a three-point plan; P3 was
            never reached, so it has no readings.
            fixture.json — which flight and session this is.

What the fixture proves: readings group by point; transit readings belong to
no point; a point never reached is insufficient_data. (Until 2026-11-15 it
also held seven generated camera frames; the drone carries no camera now.)

`dronedeck process --fixture` runs the whole pipeline on it and writes to
fixtures/results/, which git ignores.
