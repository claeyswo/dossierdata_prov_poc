"""
Error mapping from engine to HTTP.

`ActivityError` is the engine's structured exception type. It carries
a status code, a human message, and an optional `payload` dict with
machine-readable diagnostics (an `error` discriminator string plus
context fields like `stale.intervening_versions`). FastAPI's
`HTTPException` only takes a status code and a `detail`, so we
forward by merging the payload into the detail body.

The merge produces a single JSON object with `detail` (the message)
plus every payload key flattened in alongside it. Clients can switch
on the `error` discriminator for programmatic handling and read the
context fields for richer error UI.
"""

from __future__ import annotations

from fastapi import HTTPException

from ..engine import ActivityError


def activity_error_to_http(e: ActivityError) -> HTTPException:
    """Forward an ActivityError to an HTTPException, merging any
    structured payload so the client gets a single JSON body."""
    if e.payload:
        body = {"detail": e.detail, **e.payload}
        return HTTPException(e.status_code, detail=body)
    return HTTPException(e.status_code, detail=e.detail)
