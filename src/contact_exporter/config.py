"""Configuration constants for contact-exporter."""

import os

# Auth0 PKCE public client (no secret needed)
AUTH0_DOMAIN = "aleph-mvp.us.auth0.com"
AUTH0_CLIENT_ID = "U7p09NWeJ0jy9M4GiaWa4cz0YVCdDVBl"
AUTH0_AUDIENCE = "https://api.powerset.dev"
AUTH0_SCOPES = "openid profile email offline_access"

# Local callback server for OAuth
CALLBACK_PORT = 9876
CALLBACK_URL = f"http://localhost:{CALLBACK_PORT}/callback"

# Powerset API
DEFAULT_API_BASE_URL = "https://search-api-7wk4uhe77q-uw.a.run.app"
LOCAL_API_BASE_URL = "http://localhost:8000"
API_BASE_URL = os.environ.get("POWERSET_API_URL", DEFAULT_API_BASE_URL)


def get_api_base_url() -> str:
    """Resolve API base URL from env (runtime) with sensible default."""
    return os.environ.get("POWERSET_API_URL", API_BASE_URL)


def set_api_base_url(url: str) -> str:
    """Set API base URL override for the current process."""
    cleaned = (url or "").strip().rstrip("/")
    if not cleaned:
        raise ValueError("API base URL cannot be empty")
    os.environ["POWERSET_API_URL"] = cleaned
    return cleaned

# Credentials storage (~/.powerset/credentials.json)
CREDENTIALS_DIR = ".powerset"
CREDENTIALS_FILE = "credentials.json"

# Upload
UPLOAD_CHUNK_SIZE = 500

# Extraction
SMALL_GROUP_MAX_MEMBERS = 7

# WAHA (local WhatsApp bridge via Docker)
WAHA_CONTAINER_NAME = "powerset-waha"
WAHA_PORT = 3000
WAHA_SESSION_NAME = "default"
WAHA_API_KEY = "powerset-local"
