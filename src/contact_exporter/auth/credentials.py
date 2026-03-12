"""Token storage and refresh for ~/.powerset/credentials.json."""

from __future__ import annotations

import json
import os
import time
from pathlib import Path

import requests

from contact_exporter.config import (
    AUTH0_CLIENT_ID,
    AUTH0_DOMAIN,
    CREDENTIALS_DIR,
    CREDENTIALS_FILE,
)


def _credentials_path() -> Path:
    return Path.home() / CREDENTIALS_DIR / CREDENTIALS_FILE


def save_credentials(
    access_token: str,
    refresh_token: str | None,
    expires_in: int,
    email: str | None,
) -> None:
    """Save OAuth tokens to disk with restricted file permissions."""
    creds_dir = Path.home() / CREDENTIALS_DIR
    creds_dir.mkdir(exist_ok=True)
    os.chmod(creds_dir, 0o700)

    _write_raw({
        "access_token": access_token,
        "refresh_token": refresh_token,
        "expires_at": time.time() + expires_in,
        "email": email,
    })


def load_credentials() -> dict:
    """Load credentials, auto-refreshing if expired. Exits if not logged in."""
    path = _credentials_path()
    if not path.exists():
        raise SystemExit("Not logged in. Run: contact-exporter login")

    try:
        creds = json.loads(path.read_text())
    except json.JSONDecodeError:
        path.unlink()
        raise SystemExit("Credentials file corrupted. Run: contact-exporter login")

    # Refresh if expiring within 60s
    if time.time() > creds.get("expires_at", 0) - 60:
        if not creds.get("refresh_token"):
            raise SystemExit("Session expired and no refresh token. Run: contact-exporter login")
        creds = _refresh_token(creds)
        _write_raw(creds)

    return creds


def _refresh_token(creds: dict) -> dict:
    resp = requests.post(
        f"https://{AUTH0_DOMAIN}/oauth/token",
        json={
            "grant_type": "refresh_token",
            "client_id": AUTH0_CLIENT_ID,
            "refresh_token": creds["refresh_token"],
        },
        timeout=30,
    )
    if resp.status_code != 200:
        raise SystemExit(f"Token refresh failed ({resp.status_code}). Run: contact-exporter login")

    data = resp.json()
    return {
        **creds,
        "access_token": data["access_token"],
        "expires_at": time.time() + data.get("expires_in", 86400),
        # Auth0 may rotate refresh tokens on each use
        "refresh_token": data.get("refresh_token", creds["refresh_token"]),
    }


def _write_raw(creds: dict) -> None:
    """Write credentials dict to disk with 0600 permissions."""
    path = _credentials_path()
    path.parent.mkdir(exist_ok=True)
    os.chmod(path.parent, 0o700)
    path.write_text(json.dumps(creds, indent=2))
    os.chmod(path, 0o600)


def clear_credentials() -> None:
    path = _credentials_path()
    if path.exists():
        path.unlink()


def get_auth_header() -> dict:
    """Return an Authorization header, auto-refreshing the token if needed."""
    creds = load_credentials()
    return {"Authorization": f"Bearer {creds['access_token']}"}


def get_credentials_info() -> dict | None:
    """Read stored credentials without refreshing. Returns None if absent or corrupt."""
    path = _credentials_path()
    if not path.exists():
        return None
    try:
        return json.loads(path.read_text())
    except json.JSONDecodeError:
        return None
