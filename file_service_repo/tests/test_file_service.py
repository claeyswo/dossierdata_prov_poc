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

    async def test_valid_download_after_upload(self, file_client):
        """Upload a file, then download it with a valid download
        token. The response should return the file bytes."""
        fid = str(uuid4())
        await _upload(file_client, fid, b"download me")

        token = _sign(fid, "download")
        params = _qs(token)
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
