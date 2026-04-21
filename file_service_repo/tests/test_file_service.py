"""
HTTP tests for the file_service standalone FastAPI app:

* `PUT /upload/{file_id}` — upload with signed token
* `GET /download/{file_id}` — download with signed token
* `POST /internal/move` — move file from temp to permanent
* `GET /health` — health check

The file service has real security logic: every upload and
download call verifies an HMAC-signed token produced by
`dossier_common.signing`. These tests exercise the full
signature verification path plus the filesystem round-trip.

Uses `httpx.AsyncClient` with `ASGITransport` against the
file_service app (no cross-loop issue — it's a standalone
FastAPI app that doesn't use asyncpg).

The tests patch `get_config` to use a temp directory for
storage and a known signing key, so the filesystem operations
don't touch the real file_storage directory.
"""
from __future__ import annotations

import io
import json
import tempfile
from pathlib import Path
from uuid import uuid4

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from dossier_common.signing import sign_token, token_to_query_string

# Import the app but we'll patch its config for tests.
import file_service.app as fs_module


SIGNING_KEY = "test-file-signing-key"


@pytest_asyncio.fixture
async def file_client(tmp_path):
    """Yield an AsyncClient wired to the file_service app with
    config patched to use a temp directory + known signing key."""
    original_get_config = fs_module.get_config

    def _test_config():
        return {
            "signing_key": SIGNING_KEY,
            "storage_root": str(tmp_path / "storage"),
        }

    fs_module.get_config = _test_config
    try:
        transport = ASGITransport(app=fs_module.app)
        async with AsyncClient(
            transport=transport, base_url="http://test",
        ) as c:
            yield c
    finally:
        fs_module.get_config = original_get_config


def _sign(file_id: str, action: str = "upload", user_id: str = "u1"):
    """Produce a signed token dict for the given file_id + action."""
    return sign_token(
        file_id=file_id,
        action=action,
        signing_key=SIGNING_KEY,
        user_id=user_id,
    )


def _qs(token: dict) -> dict:
    """Convert a signed token to query parameters dict."""
    qs = token_to_query_string(token)
    return dict(pair.split("=", 1) for pair in qs.split("&"))


async def _upload(client, file_id: str, content: bytes = b"test data"):
    """Upload a file with a valid signature. Returns the response."""
    token = _sign(file_id, "upload")
    params = _qs(token)
    return await client.put(
        f"/upload/{file_id}",
        params=params,
        files={"file": ("test.txt", io.BytesIO(content), "text/plain")},
    )


# --------------------------------------------------------------------
# Health check
# --------------------------------------------------------------------


class TestHealth:

    async def test_health_returns_ok(self, file_client):
        r = await file_client.get("/health")
        assert r.status_code == 200
        assert r.json() == {"status": "ok"}


# --------------------------------------------------------------------
# Upload
# --------------------------------------------------------------------


class TestUpload:

    async def test_valid_upload(self, file_client):
        """Valid signed upload token + file data → 200 with
        stored=True and the correct file_id and size."""
        fid = str(uuid4())
        r = await _upload(file_client, fid, b"hello world")
        assert r.status_code == 200
        body = r.json()
        assert body["stored"] is True
        assert body["file_id"] == fid
        assert body["size"] == 11

    async def test_invalid_signature_returns_403(self, file_client):
        """Tampered signature → 403 'Invalid upload token'."""
        fid = str(uuid4())
        token = _sign(fid, "upload")
        params = _qs(token)
        params["signature"] = "tampered"

        r = await file_client.put(
            f"/upload/{fid}",
            params=params,
            files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
        )
        assert r.status_code == 403
        assert "Invalid" in r.json()["detail"]

    async def test_download_token_rejected_for_upload(self, file_client):
        """A valid token signed for action=download can't be
        used to upload. 403 'not an upload token'."""
        fid = str(uuid4())
        token = _sign(fid, "download")  # wrong action
        params = _qs(token)

        r = await file_client.put(
            f"/upload/{fid}",
            params=params,
            files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
        )
        assert r.status_code == 403
        assert "not an upload token" in r.json()["detail"]

    async def test_expired_token_rejected(self, file_client):
        """An expired token (expires in the past) → 403."""
        fid = str(uuid4())
        token = sign_token(
            file_id=fid, action="upload", signing_key=SIGNING_KEY,
            user_id="u1", expiry_seconds=0,  # expires immediately
        )
        params = _qs(token)

        import time; time.sleep(1.1)  # wait for expiry

        r = await file_client.put(
            f"/upload/{fid}",
            params=params,
            files={"file": ("test.txt", io.BytesIO(b"data"), "text/plain")},
        )
        assert r.status_code == 403


# --------------------------------------------------------------------
# Download
# --------------------------------------------------------------------


class TestDownload:

    async def test_valid_download_after_upload_and_move(self, file_client):
        """Upload a file, move it to a dossier's permanent location,
        then download it with a valid download token for that
        dossier. The response should return the file bytes.

        Updated from the pre-Bug-44-fix behavior: downloads from
        temp are no longer served. The realistic flow is
        upload → /internal/move → download, which mirrors what the
        engine + worker do in production."""
        fid = str(uuid4())
        did = str(uuid4())
        await _upload(file_client, fid, b"download me")

        # Move to permanent before attempting download.
        move_r = await file_client.post(
            "/internal/move",
            params={"file_id": fid, "dossier_id": did},
        )
        assert move_r.status_code == 200

        # Sign a download token for this specific dossier_id so the
        # signature validates against the same triple we'll query.
        from dossier_common.signing import sign_token, token_to_query_string
        token = sign_token(
            file_id=fid, action="download",
            signing_key=SIGNING_KEY, user_id="u1",
            dossier_id=did,
        )
        params = dict(
            pair.split("=", 1)
            for pair in token_to_query_string(token).split("&")
        )
        r = await file_client.get(f"/download/{fid}", params=params)
        assert r.status_code == 200
        assert r.content == b"download me"

    async def test_invalid_signature_returns_403(self, file_client):
        fid = str(uuid4())
        await _upload(file_client, fid, b"data")

        token = _sign(fid, "download")
        params = _qs(token)
        params["signature"] = "bad"
        r = await file_client.get(f"/download/{fid}", params=params)
        assert r.status_code == 403

    async def test_upload_token_rejected_for_download(self, file_client):
        """A valid upload token can't be used to download."""
        fid = str(uuid4())
        await _upload(file_client, fid, b"data")

        token = _sign(fid, "upload")  # wrong action
        params = _qs(token)
        r = await file_client.get(f"/download/{fid}", params=params)
        assert r.status_code == 403
        assert "not a download token" in r.json()["detail"]

    async def test_file_not_found_returns_404(self, file_client):
        """Download a file_id that was never uploaded → 404."""
        fid = str(uuid4())
        token = _sign(fid, "download")
        params = _qs(token)
        r = await file_client.get(f"/download/{fid}", params=params)
        assert r.status_code == 404


# --------------------------------------------------------------------
# Internal move
# --------------------------------------------------------------------


class TestInternalMove:

    async def test_move_from_temp_to_permanent(self, file_client, tmp_path):
        """Upload a file (goes to temp), then move it to the
        permanent location under dossier_id/bijlagen/."""
        fid = str(uuid4())
        did = str(uuid4())
        await _upload(file_client, fid, b"permanent data")

        r = await file_client.post(
            "/internal/move",
            params={"file_id": fid, "dossier_id": did},
        )
        assert r.status_code == 200
        assert r.json()["moved"] is True

        # The file should now be downloadable from the permanent
        # location. Verify on disk:
        perm_path = tmp_path / "storage" / did / "bijlagen" / fid
        assert perm_path.exists()
        assert perm_path.read_bytes() == b"permanent data"

    async def test_already_moved_is_idempotent(self, file_client):
        """Moving the same file twice → second call returns
        `already_permanent: True` without error."""
        fid = str(uuid4())
        did = str(uuid4())
        await _upload(file_client, fid, b"data")

        await file_client.post(
            "/internal/move",
            params={"file_id": fid, "dossier_id": did},
        )
        r = await file_client.post(
            "/internal/move",
            params={"file_id": fid, "dossier_id": did},
        )
        assert r.status_code == 200
        assert r.json()["already_permanent"] is True

    async def test_file_not_found_returns_404(self, file_client):
        """File never uploaded → 404."""
        r = await file_client.post(
            "/internal/move",
            params={"file_id": str(uuid4()), "dossier_id": str(uuid4())},
        )
        assert r.status_code == 404


# --------------------------------------------------------------------
# Bug 44 fix — download no longer falls back to temp
# Bug 47 mitigation — /internal/move enforces uploader consistency
# --------------------------------------------------------------------
#
# These tests pin down two security-relevant behaviors that were
# added together:
#
# 1. The download endpoint no longer serves files from temp. Once a
#    legitimate file_id leaks out of its tenant's boundary (via a
#    log line, a buggy sibling endpoint, a Sentry event), the old
#    code's temp-fallback allowed any user with a valid download
#    token for any dossier to retrieve that file by naming its
#    file_id in the URL. Removing the fallback forces the retrieval
#    path through the permanent location — which is dossier-scoped
#    on disk, closing the cross-tenant exfiltration path.
#
# 2. The /internal/move endpoint now accepts an optional
#    expected_uploader_user_id and rejects with 403 when the file's
#    temp metadata reports a different uploader. Combined with the
#    worker wiring the triggering activity's attributed agent into
#    that parameter, this closes the attach-someone-else's-upload
#    variant: Bob can't reference Alice's in-flight file_id in his
#    own dienAanvraagIn and have the worker cheerfully copy Alice's
#    bytes into Bob's dossier.


class TestDownloadNoLongerFallsBackToTemp:

    async def test_download_before_move_returns_404(self, file_client):
        """Upload a file. Do NOT call /internal/move. Request
        download with a valid token. The old code would fall back
        to temp and return the bytes; the new code must 404.

        This is the primary defensive behavior: a file in temp is
        an upload-in-flight, not yet attached to any dossier, and
        must not be downloadable by its (dossier_id, file_id) token
        pair until the move has placed it in the permanent
        location."""
        fid = str(uuid4())
        did = str(uuid4())
        await _upload(file_client, fid, b"should not be downloadable from temp")

        # Sign a download token for the target dossier — the
        # attacker here has a legitimate token pair for SOME
        # dossier, and is testing whether the file_service will
        # serve a temp-located file_id against that dossier's
        # scope. Pre-Bug-44-fix: yes. Post-fix: 404.
        from dossier_common.signing import sign_token, token_to_query_string
        token = sign_token(
            file_id=fid, action="download",
            signing_key=SIGNING_KEY, user_id="u1",
            dossier_id=did,
        )
        params = dict(
            pair.split("=", 1)
            for pair in token_to_query_string(token).split("&")
        )

        r = await file_client.get(f"/download/{fid}", params=params)
        assert r.status_code == 404, (
            f"expected 404 (no temp fallback), got {r.status_code}: "
            f"{r.text}"
        )

    async def test_download_succeeds_after_move(self, file_client):
        """Happy path: upload, move, then download → 200. Confirms
        the fix doesn't break the normal flow."""
        fid = str(uuid4())
        did = str(uuid4())
        await _upload(file_client, fid, b"downloadable after move")

        move_r = await file_client.post(
            "/internal/move",
            params={"file_id": fid, "dossier_id": did},
        )
        assert move_r.status_code == 200

        token = _sign(fid, "download", user_id="u1")
        params = _qs(token)
        # Re-sign with the correct dossier_id (the one we just
        # moved into) so the signature matches.
        from dossier_common.signing import sign_token
        correct_token = sign_token(
            file_id=fid, action="download",
            signing_key=SIGNING_KEY, user_id="u1",
            dossier_id=did,
        )
        from dossier_common.signing import token_to_query_string
        correct_params = dict(
            pair.split("=", 1)
            for pair in token_to_query_string(correct_token).split("&")
        )

        r = await file_client.get(f"/download/{fid}", params=correct_params)
        assert r.status_code == 200
        assert r.content == b"downloadable after move"


class TestMoveRejectsCrossUserAttach:

    async def test_move_rejects_when_expected_uploader_mismatches(
        self, file_client,
    ):
        """Alice uploads. Worker calls /internal/move with
        expected_uploader_user_id=bob (simulating Bob's activity
        trying to attach Alice's file). The file_service reads the
        temp meta, sees uploaded_by=alice, rejects with 403.

        This is the core cross-user attach defense."""
        fid = str(uuid4())
        did = str(uuid4())
        # Sign the upload token as alice — that's what the file
        # service stores as uploaded_by in the .meta file.
        from dossier_common.signing import sign_token, token_to_query_string
        import io
        alice_token = sign_token(
            file_id=fid, action="upload",
            signing_key=SIGNING_KEY, user_id="alice",
        )
        alice_params = dict(
            pair.split("=", 1)
            for pair in token_to_query_string(alice_token).split("&")
        )
        await file_client.put(
            f"/upload/{fid}",
            params=alice_params,
            files={"file": ("a.txt", io.BytesIO(b"alice's bytes"), "text/plain")},
        )

        # Worker attempts the move on behalf of Bob.
        r = await file_client.post(
            "/internal/move",
            params={
                "file_id": fid,
                "dossier_id": did,
                "expected_uploader_user_id": "bob",
            },
        )
        assert r.status_code == 403
        body = r.json()
        assert "uploader mismatch" in body["detail"].lower()
        assert "alice" in body["detail"]
        assert "bob" in body["detail"]

    async def test_move_succeeds_when_expected_uploader_matches(
        self, file_client,
    ):
        """Alice uploads, worker moves with
        expected_uploader_user_id=alice → 200. Confirms the check
        doesn't reject the legitimate same-user path."""
        fid = str(uuid4())
        did = str(uuid4())
        from dossier_common.signing import sign_token, token_to_query_string
        import io
        alice_token = sign_token(
            file_id=fid, action="upload",
            signing_key=SIGNING_KEY, user_id="alice",
        )
        alice_params = dict(
            pair.split("=", 1)
            for pair in token_to_query_string(alice_token).split("&")
        )
        await file_client.put(
            f"/upload/{fid}",
            params=alice_params,
            files={"file": ("a.txt", io.BytesIO(b"alice's bytes"), "text/plain")},
        )

        r = await file_client.post(
            "/internal/move",
            params={
                "file_id": fid,
                "dossier_id": did,
                "expected_uploader_user_id": "alice",
            },
        )
        assert r.status_code == 200
        assert r.json()["moved"] is True

    async def test_move_allows_missing_expected_uploader_for_backcompat(
        self, file_client,
    ):
        """When expected_uploader_user_id is omitted (empty string),
        the check is skipped — preserves back-compat for any caller
        that hasn't been updated to pass it yet. The toelatingen
        worker DOES pass it; this path exists so a future caller
        that genuinely doesn't know the user (e.g. an admin
        bulk-move tool) isn't broken."""
        fid = str(uuid4())
        did = str(uuid4())
        await _upload(file_client, fid, b"data")  # uploaded_by = u1 (the default)

        # No expected_uploader_user_id passed.
        r = await file_client.post(
            "/internal/move",
            params={"file_id": fid, "dossier_id": did},
        )
        assert r.status_code == 200

    async def test_move_allows_when_meta_missing(self, file_client, tmp_path):
        """A file in temp with no .meta file (legacy data, manual
        placement, edge case) skips the uploader check rather than
        erroring. The check is opportunistic: if we can't determine
        the uploader, we fall back to the old permissive behavior
        rather than blocking a legitimate operation."""
        # Put a file directly in temp with no .meta companion.
        fid = str(uuid4())
        did = str(uuid4())
        temp_dir = tmp_path / "storage" / "temp"
        temp_dir.mkdir(parents=True, exist_ok=True)
        (temp_dir / fid).write_bytes(b"legacy orphan")

        r = await file_client.post(
            "/internal/move",
            params={
                "file_id": fid,
                "dossier_id": did,
                "expected_uploader_user_id": "anybody",
            },
        )
        assert r.status_code == 200
