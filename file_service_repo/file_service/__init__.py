"""
File service — standalone FastAPI app for upload/download with signed URLs.

Runs as a separate process on a separate port from the dossier API.
The two communicate only through HTTP (via signed URLs) and share
exactly one secret (the signing key). All token signing/verification
lives in `dossier_common.signing`.
"""
