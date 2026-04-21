"""
File Service — standalone FastAPI app for file upload/download.

Proxies to S3 (MinIO). Verifies signed tokens from the Dossier API.
No knowledge of dossiers, workflows, or access control.

For the POC, uses local filesystem instead of S3.

Usage:
    uvicorn file_service.app:app --port 8001
"""

from __future__ import annotations

import json
import logging
import os
import shutil
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from dossier_common.signing import verify_token

logger = logging.getLogger("file_service")

app = FastAPI(
    title="File Service",
    description="S3 proxy for dossier file management",
    version="0.1.0",
)


def _default_config_path() -> str:
    """Resolve the default config path via the installed `dossier_app`
    package location. This lets the file service launch from any cwd —
    the config and its relative paths (like `storage_root`) end up
    anchored to the dossier_app package directory, which is where the
    dossier engine also looks for them. Override via the
    `FILE_SERVICE_CONFIG` env var if you need a different config."""
    try:
        import dossier_app
        pkg_file = getattr(dossier_app, "__file__", None)
        if pkg_file is not None:
            return str(Path(pkg_file).parent / "config.yaml")
        # Namespace package or editable install where __file__ is None —
        # fall back to __path__ (a list of directories).
        pkg_path = getattr(dossier_app, "__path__", None)
        if pkg_path:
            return str(Path(pkg_path[0]) / "config.yaml")
    except ImportError:
        # dossier_app isn't on the import path — running standalone
        # (e.g. in a container that only ships file_service). Fall
        # through to the cwd-relative default.
        logger.debug(
            "dossier_app not importable; using cwd-relative "
            "config.yaml as the default path",
        )
    return "config.yaml"


CONFIG_PATH = os.environ.get("FILE_SERVICE_CONFIG", _default_config_path())

# Warn once at module load if the configured path doesn't exist. Without
# this, a typo in ``FILE_SERVICE_CONFIG`` silently falls back to the POC
# signing key (``poc-signing-key-change-in-production``) — an
# operational footgun that's hard to spot after deploy. One startup
# line in the log is enough for ops to catch it; per-request logging
# (``get_config`` is called many times per request) would spam.
if not Path(CONFIG_PATH).exists():
    logger.warning(
        "Config path %r does not exist. file_service will fall back to "
        "built-in defaults, including the POC signing key. Set "
        "FILE_SERVICE_CONFIG to the real config path, or ensure the "
        "default resolves to an existing file.",
        CONFIG_PATH,
    )


def get_config():
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f) or {}
    except FileNotFoundError:
        # Silent here because the module-load warning above already
        # fired once; spamming per-request is noise.
        config = {}
    return config.get("file_service", {})


def get_signing_key() -> str:
    config = get_config()
    return config.get("signing_key", "poc-signing-key-change-in-production")


def get_storage_root() -> Path:
    """Return the storage root as an absolute path.

    If the config's `storage_root` is relative, it's resolved against
    the config file's parent directory (same rule the dossier engine
    uses in `load_config_and_registry`). This keeps the two services
    in agreement on where files live regardless of launch cwd."""
    config = get_config()
    raw = config.get("storage_root", "./file_storage")
    root = Path(raw)
    if not root.is_absolute():
        config_dir = Path(CONFIG_PATH).parent
        root = (config_dir / root).resolve()
    root.mkdir(parents=True, exist_ok=True)
    return root


# --- Upload endpoint ---

@app.put(
    "/upload/{file_id}",
    tags=["files"],
    summary="Upload a file with a signed token",
)
async def upload_file(
    file_id: str,
    file: UploadFile = File(...),
    action: str = Query(...),
    user_id: str = Query(...),
    dossier_id: str = Query(""),
    expires: str = Query(...),
    signature: str = Query(...),
):
    """Upload a file to temp storage. Requires a valid signed upload token."""
    signing_key = get_signing_key()

    valid, error = verify_token(
        file_id=file_id,
        action=action,
        user_id=user_id,
        dossier_id=dossier_id,
        expires=expires,
        signature=signature,
        signing_key=signing_key,
    )

    if not valid:
        raise HTTPException(403, detail=f"Invalid upload token: {error}")

    if action != "upload":
        raise HTTPException(403, detail="Token is not an upload token")

    # Store in temp folder
    root = get_storage_root()
    temp_dir = root / "temp"
    temp_dir.mkdir(parents=True, exist_ok=True)

    file_path = temp_dir / file_id
    with open(file_path, "wb") as f:
        content = await file.read()
        f.write(content)

    # Store metadata. `intended_dossier_id` carries the dossier this
    # upload was signed for — the /internal/move endpoint compares
    # that binding against the move's target dossier and 403s on
    # mismatch. This is what prevents Bob from grafting Alice's
    # file_id onto his own dossier (Bug 47): Alice's .meta says
    # d=D1, Bob's activity would schedule a move to d=D2, mismatch
    # rejected before any bytes cross dossier boundaries.
    meta_path = temp_dir / f"{file_id}.meta"
    meta = {
        "filename": file.filename or file_id,
        "content_type": file.content_type or "application/octet-stream",
        "size": len(content),
        "uploaded_by": user_id,
        "intended_dossier_id": dossier_id,
    }
    with open(meta_path, "w") as f:
        json.dump(meta, f)

    return {"stored": True, "file_id": file_id, "size": len(content)}


# --- Download endpoint ---

@app.get(
    "/download/{file_id}",
    tags=["files"],
    summary="Download a file with a signed token",
)
async def download_file(
    file_id: str,
    action: str = Query(...),
    user_id: str = Query(...),
    dossier_id: str = Query(""),
    expires: str = Query(...),
    signature: str = Query(...),
):
    """Download a file. Requires a valid signed download token."""
    signing_key = get_signing_key()

    valid, error = verify_token(
        file_id=file_id,
        action=action,
        user_id=user_id,
        dossier_id=dossier_id,
        expires=expires,
        signature=signature,
        signing_key=signing_key,
    )

    if not valid:
        raise HTTPException(403, detail=f"Invalid download token: {error}")

    if action != "download":
        raise HTTPException(403, detail="Token is not a download token")

    root = get_storage_root()

    # Permanent location only. Files in temp belong to an upload
    # in-flight; they're not yet attached to a dossier and must not
    # be downloadable until the move task has run. Removing the
    # previous temp-fallback closes Bug 44 (cross-tenant exfiltration
    # via temp) — the move task is the single legitimate reader of
    # temp, and it runs as systeemgebruiker with per-file provenance.
    file_path = root / dossier_id / "bijlagen" / file_id
    if not file_path.exists():
        raise HTTPException(404, detail="File not found")

    # Read metadata for filename and content_type. Only the permanent
    # location's .meta is consulted — temp metadata is irrelevant
    # here (we no longer serve from temp) and reading it would be a
    # second way to leak the uploader_user_id across tenants.
    meta = {}
    meta_path = file_path.parent / f"{file_id}.meta"
    if meta_path.exists():
        with open(meta_path) as f:
            meta = json.load(f)

    return FileResponse(
        path=str(file_path),
        filename=meta.get("filename", file_id),
        media_type=meta.get("content_type", "application/octet-stream"),
    )


# --- Internal: move file from temp to permanent ---

@app.post(
    "/internal/move",
    tags=["internal"],
    summary="Move a file from temp to permanent location (internal only)",
)
async def move_file(
    file_id: str = Query(...),
    dossier_id: str = Query(...),
):
    """Move file from temp to permanent dossier location.
    Internal endpoint — should only be accessible from the worker on the internal network.

    Enforces the dossier-binding invariant: a file uploaded for
    dossier X can only be moved into dossier X. The binding is
    established at upload time — the engine's /files/upload/request
    endpoint requires ``dossier_id``, signs it into the token, and
    the upload handler records it as ``intended_dossier_id`` in the
    .meta file. If the move target disagrees, the bytes never
    cross dossier boundaries (Bug 47).
    """
    root = get_storage_root()
    temp_path = root / "temp" / file_id
    temp_meta = root / "temp" / f"{file_id}.meta"

    if not temp_path.exists():
        # Already moved or doesn't exist — idempotent
        permanent_path = root / dossier_id / "bijlagen" / file_id
        if permanent_path.exists():
            return {"moved": True, "already_permanent": True}
        raise HTTPException(404, detail="File not found in temp or permanent storage")

    # Dossier-binding check. Compare the dossier the file was
    # uploaded for against the dossier being moved into. Mismatch
    # is an attempted cross-dossier graft — reject.
    #
    # Policy for the three possible ``.meta`` states:
    #
    # * **Missing entirely** — file was uploaded before this check was
    #   introduced (Bug 47). Legacy path: allow through; the
    #   back-compat door closes naturally as old temp files drain.
    #   This is the ``if temp_meta.exists():`` guard below.
    # * **Present and valid, with ``intended_dossier_id``** — the
    #   normal case. Compare strict; mismatch → 403.
    # * **Present and valid but missing ``intended_dossier_id``** —
    #   legacy-content path (a stub ``.meta`` from before the field
    #   was added). Allow through, same as "missing entirely."
    # * **Present but corrupted** (Bug 76) — the file exists, so it
    #   wasn't written before the check was introduced, so the binding
    #   *should* be there and isn't. Unsafe to fall back; reject.
    #   Corrupted ``.meta`` is the kind of anomaly that precedes an
    #   attack (tampering to bypass the binding) or that signals disk
    #   corruption — either way, rejecting is strictly safer than the
    #   silent bypass the old code permitted.
    if temp_meta.exists():
        try:
            with open(temp_meta) as f:
                meta = json.load(f)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            # Bug 76: log and reject. The legacy back-compat door is
            # "no .meta file at all" (handled by the outer
            # ``if temp_meta.exists():``), not "corrupted .meta" —
            # this case falls through only when the file is present
            # and unreadable, which shouldn't happen in normal
            # operation.
            #
            # Three catch shapes, all same policy:
            # * ``OSError`` — disk read failed (permission, I/O error).
            # * ``json.JSONDecodeError`` — truncated / malformed JSON.
            # * ``UnicodeDecodeError`` — bytes that aren't valid UTF-8,
            #   raised by ``open()`` default text mode before JSON sees
            #   them. Subclass of ``ValueError``, not of
            #   ``JSONDecodeError`` — easy to miss when writing the
            #   catch; regression-guarded by
            #   ``test_move_rejects_when_meta_is_non_json_garbage``.
            logger.error(
                "Corrupted .meta for file %s: %s. Rejecting move.",
                file_id, exc,
            )
            raise HTTPException(
                500,
                detail=(
                    "File metadata is unreadable; move refused to "
                    "preserve the dossier-binding invariant. "
                    "Investigate and either repair or re-upload."
                ),
            )
        intended = meta.get("intended_dossier_id", "")
        if intended and intended != dossier_id:
            raise HTTPException(
                403,
                detail=(
                    f"Dossier mismatch: file {file_id} was uploaded for "
                    f"dossier '{intended}' but the move targets "
                    f"'{dossier_id}'. A file is bound to one dossier at "
                    f"upload time and cannot be grafted onto another."
                ),
            )

    # Create permanent directory
    perm_dir = root / dossier_id / "bijlagen"
    perm_dir.mkdir(parents=True, exist_ok=True)

    # Move file and metadata
    shutil.move(str(temp_path), str(perm_dir / file_id))
    if temp_meta.exists():
        shutil.move(str(temp_meta), str(perm_dir / f"{file_id}.meta"))

    return {"moved": True, "file_id": file_id, "dossier_id": dossier_id}


# --- Health check ---

@app.get("/health", tags=["system"])
async def health():
    return {"status": "ok"}
