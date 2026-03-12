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
API_BASE_URL = os.environ.get("POWERSET_API_URL", "https://search-api-7wk4uhe77q-uw.a.run.app")

# Credentials storage (~/.powerset/credentials.json)
CREDENTIALS_DIR = ".powerset"
CREDENTIALS_FILE = "credentials.json"

# Upload
UPLOAD_CHUNK_SIZE = 500

# Extraction limits
MAX_CONTACTS_OUTPUT = 250
MESSAGE_COUNT_CAP = 200
SMALL_GROUP_MAX_MEMBERS = 7

# WAHA (local WhatsApp bridge via Docker)
WAHA_CONTAINER_NAME = "powerset-waha"
WAHA_PORT = 3000
WAHA_SESSION_NAME = "default"
WAHA_API_KEY = "powerset-local"
