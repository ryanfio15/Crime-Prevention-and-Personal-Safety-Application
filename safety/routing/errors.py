"""The ways a provider call can fail, each with the HTTP status the API answers.

The message on every exception is written here, for the visitor, and is the
only text that reaches a response. A provider's own error body is never echoed:
it can carry the coordinates we sent, internal hostnames or stack traces, and
it is not ours to vouch for. What the provider said goes into `reason` for the
log -- an error code or an HTTP status, never a body and never a coordinate.

    NoRoute               422  the provider answered: there is no path
    OutOfCoverage         422  a point is off the routing graph, or too far
    BadRequest            400  the provider could not parse what we sent
    ProviderUnavailable   503  down, 5xx, unreadable answer, circuit open
    ProviderTimeout       504  no answer within routing_timeout_seconds
    BudgetExhausted       503  today's upstream allowance is spent
    TooSoon               429  this client asked for transit moments ago
"""

from __future__ import annotations

# Which service, in the visitor's words. Keyed by the provider name the
# clients use (providers.py).
SERVICE_LABEL = {
    "valhalla": "Street routing (driving, walking and cycling)",
    "transitous": "Transit routing",
    "photon": "Address search",
}


class RoutingError(Exception):
    status = 500
    code = "routing_error"
    retry_after: int | None = None

    def __init__(self, message: str, *, provider: str | None = None, reason: str | None = None):
        super().__init__(message)
        self.message = message
        self.provider = provider
        # For the log only: an error code or status, never a body.
        self.reason = reason

    def body(self) -> dict[str, str]:
        return {"detail": self.message, "code": self.code}


def _label(provider: str | None) -> str:
    return SERVICE_LABEL.get(provider or "", "The directions service")


class NoRoute(RoutingError):
    status = 422
    code = "no_route"

    def __init__(self, *, provider: str | None = None, reason: str | None = None):
        super().__init__(
            "No route was found between those two points.", provider=provider, reason=reason
        )


class OutOfCoverage(RoutingError):
    """A point the router has no road or path near (Valhalla 171), or a trip
    longer than the router accepts (154, kind="distance")."""

    status = 422
    code = "out_of_coverage"

    def __init__(
        self, kind: str = "location", *, provider: str | None = None, reason: str | None = None
    ):
        self.kind = kind
        if kind == "distance":
            self.code = "too_far"
            message = "Those two points are too far apart to route."
        else:
            message = (
                "One of the points is not near a road or path that directions cover. "
                "Try a point closer to a street."
            )
        super().__init__(message, provider=provider, reason=reason)


class BadRequest(RoutingError):
    status = 400
    code = "bad_request"

    def __init__(self, *, provider: str | None = None, reason: str | None = None):
        super().__init__(
            f"{_label(provider)} could not use that request.", provider=provider, reason=reason
        )


class ProviderUnavailable(RoutingError):
    status = 503
    code = "provider_unavailable"
    retry_after = 30

    def __init__(self, *, provider: str | None = None, reason: str | None = None):
        super().__init__(
            f"{_label(provider)} is temporarily unavailable; try again shortly.",
            provider=provider,
            reason=reason,
        )


class ProviderTimeout(RoutingError):
    status = 504
    code = "provider_timeout"
    retry_after = 5

    def __init__(self, *, provider: str | None = None, reason: str | None = None):
        super().__init__(
            f"{_label(provider)} took too long to answer; try again shortly.",
            provider=provider,
            reason=reason,
        )


class BudgetExhausted(RoutingError):
    """The day's upstream allowance is used up; it comes back at UTC midnight.

    Retry-After says when, so a client (or a person reading the message) is
    not told "shortly" about something hours away.
    """

    status = 503
    code = "budget_exhausted"

    def __init__(self, *, provider: str | None = None, retry_after: int | None = None):
        super().__init__(
            f"{_label(provider)} has reached today's limit for this site and will be "
            "back after midnight UTC.",
            provider=provider,
            reason="daily budget",
        )
        self.retry_after = retry_after


class TooSoon(RoutingError):
    """One client asking for transit plans faster than one per
    TRANSIT_CLIENT_INTERVAL_S (safety/routing/service.py): the day's
    Transitous budget is shared by everyone, so no one visitor may spend it in
    a burst."""

    status = 429
    code = "too_soon"

    def __init__(self, retry_after: int):
        super().__init__(
            "Transit directions were asked for a moment ago; try again in a few seconds.",
            provider="transitous",
            reason="client interval",
        )
        self.retry_after = retry_after
