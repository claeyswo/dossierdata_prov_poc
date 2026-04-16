"""
Tests for `dossier_common.signing`.

Signing tokens are a security boundary between the dossier API and
the file service — two processes that share only the signing key.
Unit tests here lock in the exact shape of the token, the
round-trip invariants, and the rejection behavior for each failure
mode. Anything that bypasses these checks is a security regression.
"""
from __future__ import annotations

import time
from unittest.mock import patch

from dossier_common.signing import (
    DEFAULT_EXPIRY,
    sign_token,
    verify_token,
    token_to_query_string,
    query_string_to_token,
)


KEY = "test-signing-key"
OTHER_KEY = "different-key"


# --------------------------------------------------------------------
# sign_token — shape and content
# --------------------------------------------------------------------

class TestSignToken:

    def test_download_token_has_all_fields(self):
        tok = sign_token(
            file_id="f1",
            action="download",
            signing_key=KEY,
            user_id="alice",
            dossier_id="d1",
        )
        assert tok["file_id"] == "f1"
        assert tok["action"] == "download"
        assert tok["user_id"] == "alice"
        assert tok["dossier_id"] == "d1"
        assert "expires" in tok
        assert "signature" in tok
        # expires is a string (ready for URL-encoding); the value is
        # the unix-seconds integer stringified.
        assert tok["expires"].isdigit()
        # signature is sha256 hex → 64 chars.
        assert len(tok["signature"]) == 64

    def test_upload_token_has_empty_dossier(self):
        """Upload tokens are user-scoped but not dossier-scoped —
        a fresh upload isn't attached to a dossier yet. The field
        is empty string, not missing, so downstream formatting is
        uniform."""
        tok = sign_token(
            file_id="f1",
            action="upload",
            signing_key=KEY,
            user_id="alice",
            # dossier_id omitted → defaults to ""
        )
        assert tok["dossier_id"] == ""

    def test_default_expiry_is_one_hour(self):
        with patch("dossier_common.signing.time.time", return_value=1_000_000):
            tok = sign_token("f1", "download", KEY, "alice", "d1")
            assert int(tok["expires"]) == 1_000_000 + DEFAULT_EXPIRY
            assert DEFAULT_EXPIRY == 3600

    def test_custom_expiry(self):
        with patch("dossier_common.signing.time.time", return_value=1_000_000):
            tok = sign_token(
                "f1", "download", KEY, "alice", "d1",
                expiry_seconds=60,
            )
            assert int(tok["expires"]) == 1_000_060

    def test_different_keys_produce_different_signatures(self):
        """Two tokens with identical payload but different signing
        keys must produce different signatures. Otherwise the key
        isn't actually affecting the HMAC."""
        with patch("dossier_common.signing.time.time", return_value=1_000_000):
            tok_a = sign_token("f1", "download", KEY, "alice", "d1")
            tok_b = sign_token("f1", "download", OTHER_KEY, "alice", "d1")
        assert tok_a["signature"] != tok_b["signature"]

    def test_different_users_produce_different_signatures(self):
        """Signing includes user_id in the payload, so two tokens
        for different users (same file, same dossier, same key)
        must differ in signature. This is what prevents Alice's
        token from being replayed by Bob."""
        with patch("dossier_common.signing.time.time", return_value=1_000_000):
            tok_a = sign_token("f1", "download", KEY, "alice", "d1")
            tok_b = sign_token("f1", "download", KEY, "bob", "d1")
        assert tok_a["signature"] != tok_b["signature"]


# --------------------------------------------------------------------
# verify_token — accept path
# --------------------------------------------------------------------

class TestVerifyTokenAccept:

    def test_fresh_token_verifies(self):
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        ok, err = verify_token(
            tok["file_id"], tok["action"], tok["user_id"], tok["dossier_id"],
            tok["expires"], tok["signature"], KEY,
        )
        assert ok is True
        assert err == ""

    def test_upload_token_with_empty_dossier_verifies(self):
        tok = sign_token("f1", "upload", KEY, "alice")
        ok, err = verify_token(
            tok["file_id"], tok["action"], tok["user_id"], tok["dossier_id"],
            tok["expires"], tok["signature"], KEY,
        )
        assert ok is True


# --------------------------------------------------------------------
# verify_token — reject paths (every failure mode)
# --------------------------------------------------------------------

class TestVerifyTokenReject:

    def test_expired_token(self):
        """Expiry check: if now > expires, reject with
        "Token expired". The `>` is strict, so == now is still
        valid (barely). We test the strict-rejection case."""
        with patch("dossier_common.signing.time.time", return_value=1_000_000):
            tok = sign_token("f1", "download", KEY, "alice", "d1",
                             expiry_seconds=60)
        # Jump forward past the expiry.
        with patch("dossier_common.signing.time.time", return_value=1_000_061):
            ok, err = verify_token(
                tok["file_id"], tok["action"], tok["user_id"],
                tok["dossier_id"], tok["expires"], tok["signature"], KEY,
            )
        assert ok is False
        assert err == "Token expired"

    def test_invalid_expires(self):
        """Unparseable `expires` → "Invalid expiry". This catches
        tampering attempts that swap a number for a string and also
        any accidental bug where expires isn't serialized correctly."""
        ok, err = verify_token(
            "f1", "download", "alice", "d1",
            "not-a-number", "a" * 64, KEY,
        )
        assert ok is False
        assert err == "Invalid expiry"

    def test_tampered_signature(self):
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        # Flip one character of the signature.
        tampered_sig = tok["signature"][:-1] + (
            "0" if tok["signature"][-1] != "0" else "1"
        )
        ok, err = verify_token(
            tok["file_id"], tok["action"], tok["user_id"], tok["dossier_id"],
            tok["expires"], tampered_sig, KEY,
        )
        assert ok is False
        assert err == "Invalid signature"

    def test_tampered_file_id(self):
        """Signature is over the full payload including file_id, so
        swapping the file_id after signing must fail verification.
        This is what prevents "Alice got a download URL for file A,
        let me swap the file_id to file B."""
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        ok, err = verify_token(
            "f2", tok["action"], tok["user_id"], tok["dossier_id"],
            tok["expires"], tok["signature"], KEY,
        )
        assert ok is False
        assert err == "Invalid signature"

    def test_tampered_user_id(self):
        """Payload includes user_id. Swapping it (the Bob-replays-
        Alice's-token attack) must fail."""
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        ok, err = verify_token(
            tok["file_id"], tok["action"], "bob", tok["dossier_id"],
            tok["expires"], tok["signature"], KEY,
        )
        assert ok is False
        assert err == "Invalid signature"

    def test_tampered_dossier_id(self):
        """Payload includes dossier_id. Swapping it must fail —
        otherwise a download token for dossier A could be replayed
        against dossier B."""
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        ok, err = verify_token(
            tok["file_id"], tok["action"], tok["user_id"], "d2",
            tok["expires"], tok["signature"], KEY,
        )
        assert ok is False
        assert err == "Invalid signature"

    def test_wrong_signing_key(self):
        """Verifying with a different key than the one used to sign
        must fail. This is the "attacker doesn't have the key"
        baseline."""
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        ok, err = verify_token(
            tok["file_id"], tok["action"], tok["user_id"], tok["dossier_id"],
            tok["expires"], tok["signature"], OTHER_KEY,
        )
        assert ok is False
        assert err == "Invalid signature"


# --------------------------------------------------------------------
# Query string serialization — round trip
# --------------------------------------------------------------------

class TestQueryStringRoundTrip:

    def test_round_trip_preserves_all_fields(self):
        """sign → token_to_query_string → query_string_to_token
        (via urlparse simulation) → verify should all succeed."""
        tok = sign_token("f1", "download", KEY, "alice", "d1")
        qs = token_to_query_string(tok)

        # Simulate what FastAPI would give us: a dict of query params.
        # We parse the urlencoded string ourselves.
        from urllib.parse import parse_qs
        parsed = parse_qs(qs)
        # parse_qs produces lists; flatten to single values to match
        # the dict shape query_string_to_token expects.
        flat = {k: v[0] for k, v in parsed.items()}

        extracted = query_string_to_token(flat)
        assert extracted == tok

        # And the extracted token still verifies.
        ok, err = verify_token(
            extracted["file_id"], extracted["action"],
            extracted["user_id"], extracted["dossier_id"],
            extracted["expires"], extracted["signature"], KEY,
        )
        assert ok is True

    def test_query_string_to_token_defaults_empty(self):
        """Missing fields become empty strings rather than raising
        KeyError. verify_token then produces a specific
        error message (Invalid expiry or Invalid signature)
        instead of a 500."""
        extracted = query_string_to_token({})
        assert extracted == {
            "file_id": "",
            "action": "",
            "user_id": "",
            "dossier_id": "",
            "expires": "",
            "signature": "",
        }

    def test_query_string_to_token_partial(self):
        """Only some fields present. Missing ones still get empty
        defaults; present ones come through verbatim."""
        extracted = query_string_to_token({
            "file_id": "f1",
            "action": "download",
            # others missing
        })
        assert extracted["file_id"] == "f1"
        assert extracted["action"] == "download"
        assert extracted["user_id"] == ""
        assert extracted["expires"] == ""
        assert extracted["signature"] == ""
