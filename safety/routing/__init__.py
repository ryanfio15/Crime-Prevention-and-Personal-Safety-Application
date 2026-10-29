"""Directions: provider clients and the shared route schema (design doc S13).

Pure on purpose -- nothing here imports FastAPI -- so every normaliser can be
unit-tested against the fixtures captured from the live services
(tests/fixtures/routing/) with httpx.MockTransport and no network.

    polyline    encoded-polyline decoding, precision per call
    schema      Point, Step, TransitLeg, Route, Place: what every provider is
                normalised to, and the JSON the API returns
    errors      the failures a provider call can end in, with their HTTP status
    ttlcache    in-memory TTL + LRU cache with async single flight
    budget      DailyBudget, the per-UTC-day upstream call allowance
    breaker     CircuitBreaker, 3 failures -> provider unavailable for 2 min
    cities      server-side city_at(), the port of web/app.js cityAt
    providers   ValhallaClient, TransitousClient, PhotonClient
    scoring     the reported activity along a route, against the city average,
                and the "lower reported activity" pick
    detours     via points for walking and cycling alternatives
    service     Routing, the per-process bundle the API holds in app.state

Privacy rule for the whole package: coordinates and query text are never
logged, and never put in an exception message. Logs carry the provider, the
operation, the upstream status and the latency.
"""
