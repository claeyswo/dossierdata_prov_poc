"""
Shared signing utilities for file upload/download tokens.

Both the Dossier API and the File Service use this module to
generate and verify signed URLs. The signing key is the only
shared secret between the two services.
"""

from __future__ import annotations

import hashlib
import hmac
import time
from urllib.parse import urlencode, parse_qs


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

    Returns dict with: file_id, action, user_id, dossier_id, expires, signature.
    """
    expires = int(time.time()) + expiry_seconds
    payload = f"{file_id}:{action}:{user_id}:{dossier_id}:{expires}"
    signature = hmac.new(
        signing_key.encode(), payload.encode(), hashlib.sha256
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
    """Verify a signed token. Returns (valid, error_message)."""
    # Check expiry
    try:
        exp = int(expires)
    except ValueError:
        return False, "Invalid expiry"

    if time.time() > exp:
        return False, "Token expired"

    # Verify signature
    payload = f"{file_id}:{action}:{user_id}:{dossier_id}:{exp}"
    expected = hmac.new(
        signing_key.encode(), payload.encode(), hashlib.sha256
    ).hexdigest()

    if not hmac.compare_digest(signature, expected):
        return False, "Invalid signature"

    return True, ""


def token_to_query_string(token: dict) -> str:
    """Convert a token dict to a URL query string."""
    return urlencode(token)


def query_string_to_token(qs: dict) -> dict:
    """Extract token fields from query string params."""
    return {
        "file_id": qs.get("file_id", ""),
        "action": qs.get("action", ""),
        "user_id": qs.get("user_id", ""),
        "dossier_id": qs.get("dossier_id", ""),
        "expires": qs.get("expires", ""),
        "signature": qs.get("signature", ""),
    }
