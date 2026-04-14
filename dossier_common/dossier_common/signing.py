"""
Signed tokens for file upload and download URLs.

The dossier API and the file service are separate processes that
share one secret (the signing key) and no other state. This module
mints signed tokens the engine attaches to file URLs and verifies
them on the file service side.

Token shape: a flat dict with `file_id`, `action` (`"upload"` or
`"download"`), `user_id`, `dossier_id`, `expires` (Unix seconds as
string), and `signature` (HMAC-SHA256 hex of the payload string).
The payload format is `"{file_id}:{action}:{user_id}:{dossier_id}:{expires}"`.

Scope conventions:
* **Upload tokens** are user-scoped but NOT dossier-scoped, because
  a freshly uploaded file isn't yet attached to any dossier. The
  `dossier_id` field is empty on upload tokens.
* **Download tokens** are both user-scoped and dossier-scoped. The
  engine mints them when rendering entity content that contains
  `FileId` fields, binding each URL to the reader's user identity
  and the dossier they're reading.

Expiry defaults to 1 hour. `verify_token` rejects expired tokens
and signature mismatches, using `hmac.compare_digest` for the
constant-time comparison.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode


DEFAULT_EXPIRY = 3600  # 1 hour


def sign_token(
    file_id: str,
    action: str,  # "upload" or "download"
    signing_key: str,
    user_id: str = "",
    dossier_id: str = "",
    expiry_seconds: int = DEFAULT_EXPIRY,
) -> dict:
    """Generate a signed token for file upload or download.

    Returns a dict with `file_id`, `action`, `user_id`, `dossier_id`,
    `expires`, and `signature`. The caller serializes this via
    `token_to_query_string` before appending to a URL."""
    expires = int(time.time()) + expiry_seconds
    payload = f"{file_id}:{action}:{user_id}:{dossier_id}:{expires}"
    signature = hmac.new(
        signing_key.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()

    return {
        "file_id": file_id,
        "action": action,
        "user_id": user_id,
        "dossier_id": dossier_id,
        "expires": str(expires),
        "signature": signature,
    }


def verify_token(
    file_id: str,
    action: str,
    user_id: str,
    dossier_id: str,
    expires: str,
    signature: str,
    signing_key: str,
) -> tuple[bool, str]:
    """Verify a signed token. Returns `(valid, error_message)`.

    Rejects on: unparseable `expires`, expired tokens, or signature
    mismatch. The signature check uses `hmac.compare_digest` to avoid
    a timing side channel."""
    try:
        exp = int(expires)
    except ValueError:
        return False, "Invalid expiry"

    if time.time() > exp:
        return False, "Token expired"

    payload = f"{file_id}:{action}:{user_id}:{dossier_id}:{exp}"
    expected = hmac.new(
        signing_key.encode(), payload.encode(), hashlib.sha256,
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return False, "Invalid signature"

    return True, ""


def token_to_query_string(token: dict) -> str:
    """Serialize a token dict into a URL query string (no leading `?`)."""
    return urlencode(token)


def query_string_to_token(qs: dict) -> dict:
    """Extract token fields from a query string dict. Fields default
    to empty strings when absent so `verify_token` can produce a
    specific "missing field" error rather than a KeyError."""
    return {
        "file_id": qs.get("file_id", ""),
        "action": qs.get("action", ""),
        "user_id": qs.get("user_id", ""),
        "dossier_id": qs.get("dossier_id", ""),
        "expires": qs.get("expires", ""),
        "signature": qs.get("signature", ""),
    }
