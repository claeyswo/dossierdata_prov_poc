"""
File Service — standalone FastAPI app for file upload/download.

Proxies to S3 (MinIO). Verifies signed tokens from the Dossier API.
No knowledge of dossiers, workflows, or access control.

For the POC, uses local filesystem instead of S3.

Usage:
    uvicorn file_service.app:app --port 8001
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path
from uuid import uuid4

import yaml
from fastapi import FastAPI, UploadFile, File, HTTPException, Query
from fastapi.responses import FileResponse, JSONResponse

from dossier_common.signing import verify_token

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
        pass
    return "config.yaml"


CONFIG_PATH = os.environ.get("FILE_SERVICE_CONFIG", _default_config_path())


def get_config():
    try:
        with open(CONFIG_PATH) as f:
            config = yaml.safe_load(f)
    except FileNotFoundError:
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

    # Store metadata
    meta_path = temp_dir / f"{file_id}.meta"
    import json
    meta = {
        "filename": file.filename or file_id,
        "content_type": file.content_type or "application/octet-stream",
        "size": len(content),
        "uploaded_by": user_id,
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

    # Try permanent location first, then temp
    permanent_path = root / dossier_id / "bijlagen" / file_id
    temp_path = root / "temp" / file_id

    if permanent_path.exists():
        file_path = permanent_path
    elif temp_path.exists():
        file_path = temp_path
    else:
        raise HTTPException(404, detail="File not found")

    # Read metadata for filename and content_type
    import json
    meta = {}
    for meta_candidate in [
        permanent_path.parent / f"{file_id}.meta",
        root / "temp" / f"{file_id}.meta",
    ]:
        if meta_candidate.exists():
            with open(meta_candidate) as f:
                meta = json.load(f)
            break

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
