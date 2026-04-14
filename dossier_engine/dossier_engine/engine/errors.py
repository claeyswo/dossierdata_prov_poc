"""
Engine exception types.

Two distinct error classes:

* `ActivityError` — raised by validators, handlers, and pipeline phases
  when an activity is rejected for a reason the client should know about
  (validation failure, authorization, conflict, etc). Carries an HTTP
  status code, a human-readable detail string, and an optional structured
  payload that the route layer merges into the JSON response body.

* `CardinalityError` — raised when engine or handler code tries to look
  up a singleton entity of a type the plugin has declared as `multiple`.
  Indicates a programming bug, not a client error.
"""

from __future__ import annotations

from typing import Any


class ActivityError(Exception):
    """Activity rejected. Maps to an HTTP error response.

    The route layer turns this into an HTTPException via
    `routes._activity_error_to_http`. If `payload` is set it is merged
    into the response body alongside `detail`, so clients get a single
    JSON object with both human prose and machine-readable fields.
    """

    def __init__(self, status_code: int, detail: Any, payload: dict | None = None):
        self.status_code = status_code
        self.detail = detail
        self.payload = payload


class CardinalityError(Exception):
    """Singleton lookup attempted on a multi-cardinality entity type.

    Always a bug in engine or handler code — the caller should be
    iterating entities by type (`get_entities_latest`) instead of
    assuming a unique one. Never reaches the client; surfaces as a 500.
    """
    pass
