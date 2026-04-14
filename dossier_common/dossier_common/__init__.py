"""
Shared utilities for the dossier platform.

This package holds code that is genuinely cross-cutting — used by
more than one of the sibling projects (dossier_engine,
dossier_toelatingen, file_service, dossier_app) and would create
unwanted coupling if it lived in any one of them.

Current contents:
* `signing` — HMAC-signed token minting and verification for file
  upload/download URLs, shared between the engine (which mints
  download URLs) and the file service (which verifies them).

Keep this package small and dependency-light. It must not import
from any of the sibling projects — it's at the bottom of the
dependency graph and every sibling can depend on it. If something
belongs in dossier_common, it should be broadly useful; if it only
serves one sibling, put it there instead.
"""
