"""
File upload signing endpoint.

The dossier API itself never receives file bytes — uploads go directly
to the file_service running on a separate port. This endpoint
mints a signed upload URL the client can POST a file to.

Flow:

1. Client calls `POST /files/upload/request` with a JSON body
   carrying at least `filename`.
2. This endpoint generates a fresh `file_id` (UUID4), signs an
   upload token over `(file_id, action="upload", user_id)`, and
   returns the file service URL with the signature embedded as
   query parameters.
3. Client uploads the file bytes to the returned URL. The file
   service verifies the token and rejects unsigned/expired requests.
4. Client references the `file_id` in subsequent activity content
   (e.g. `bijlage.file_id`). The dossier reader endpoints later
   inject signed *download* URLs for the same file_id, scoped to
   the reader's user + dossier.

The user must be authenticated to mint upload tokens, but tokens
themselves don't carry dossier scope — a freshly uploaded file isn't
yet attached to any dossier. The download tokens (issued by the
dossier read path) are dossier-scoped.
"""

from __future__ import annotations

from uuid import uuid4

from fastapi import Depends, FastAPI

from dossier_common.signing import sign_token, token_to_query_string

from ..auth import User


def register(app: FastAPI, *, get_user) -> None:
    """Register file-related endpoints on the FastAPI app."""

    @app.post(
        "/files/upload/request",
        tags=["files"],
        summary="Request a signed upload URL",
    )
    async def request_upload(
        request_body: dict,
        user: User = Depends(get_user),
    ):
        """Request a signed URL for file upload. User must be
        authenticated. Returns a `file_id` and `upload_url` for the
        client to POST the file bytes to."""
        file_config = app.state.config.get("file_service", {})
        signing_key = file_config.get("signing_key")
        if not signing_key:
            from fastapi import HTTPException
            raise HTTPException(
                500,
                detail="file_service.signing_key is not configured",
            )
        file_service_url = file_config.get("url", "http://localhost:8001")

        file_id = str(uuid4())
        token = sign_token(
            file_id=file_id,
            action="upload",
            signing_key=signing_key,
            user_id=user.id,
        )
        upload_url = (
            f"{file_service_url}/upload/{file_id}"
            f"?{token_to_query_string(token)}"
        )

        return {
            "file_id": file_id,
            "upload_url": upload_url,
            "filename": request_body.get("filename", ""),
        }
