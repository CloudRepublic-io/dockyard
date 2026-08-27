import os
import secrets

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPBasic, HTTPBasicCredentials

security = HTTPBasic(auto_error=False)

AUTH_USER = os.environ.get("DOCKYARD_AUTH_USER")
AUTH_PASS = os.environ.get("DOCKYARD_AUTH_PASS")
INGEST_TOKEN = os.environ.get("DOCKYARD_INGEST_TOKEN")


def _bearer_token(request: Request) -> str:
    header = request.headers.get("authorization", "")
    if header.startswith("Bearer "):
        return header[len("Bearer "):].strip()
    return ""


def require_auth(request: Request, credentials: HTTPBasicCredentials = Depends(security)):
    """No-op if DOCKYARD_AUTH_USER / DOCKYARD_AUTH_PASS aren't both set.
    A valid DOCKYARD_INGEST_TOKEN bearer token also satisfies this globally,
    so host agents can push compose data even when interactive Basic Auth
    is enabled for the browser UI."""
    if INGEST_TOKEN and secrets.compare_digest(_bearer_token(request), INGEST_TOKEN):
        return

    if not AUTH_USER or not AUTH_PASS:
        return

    valid = (
        credentials is not None
        and secrets.compare_digest(credentials.username, AUTH_USER)
        and secrets.compare_digest(credentials.password, AUTH_PASS)
    )
    if not valid:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unauthorized",
            headers={"WWW-Authenticate": "Basic"},
        )


def require_ingest_token(request: Request):
    """Independent of require_auth: the compose-ingest endpoint always needs
    a correct token, regardless of whether Basic Auth is configured at all -
    it's a write endpoint and shouldn't be open by default the way the
    read-mostly UI is."""
    if not INGEST_TOKEN:
        raise HTTPException(status_code=403, detail="Compose ingest is disabled. Set DOCKYARD_INGEST_TOKEN to enable it.")
    token = _bearer_token(request)
    if not token or not secrets.compare_digest(token, INGEST_TOKEN):
        raise HTTPException(status_code=401, detail="Missing or invalid ingest token")
