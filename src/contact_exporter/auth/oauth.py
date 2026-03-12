"""Auth0 PKCE OAuth flow for CLI authentication.

Opens the user's browser to Auth0 Universal Login, captures the callback
on a local HTTP server, and exchanges the auth code for tokens.
"""

from __future__ import annotations

import base64
import hashlib
import html
import http.server
import json
import secrets
import string
import threading
import urllib.parse
import webbrowser

import requests
from rich.console import Console

from contact_exporter.auth.credentials import save_credentials
from contact_exporter.config import (
    AUTH0_AUDIENCE,
    AUTH0_CLIENT_ID,
    AUTH0_DOMAIN,
    AUTH0_SCOPES,
    CALLBACK_PORT,
    CALLBACK_URL,
)

console = Console()

_PAGE_STYLE = """
<style>
  * { margin: 0; padding: 0; box-sizing: border-box; }
  body {
    font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, sans-serif;
    min-height: 100vh;
    display: flex;
    align-items: center;
    justify-content: center;
    background: #0a0a0a;
    color: #fafafa;
  }
  .card {
    text-align: center;
    padding: 3rem 4rem;
    border-radius: 16px;
    background: #141414;
    border: 1px solid #262626;
    max-width: 420px;
  }
  .icon { font-size: 3rem; margin-bottom: 1rem; }
  h1 { font-size: 1.5rem; font-weight: 600; margin-bottom: 0.5rem; }
  p { color: #a1a1aa; font-size: 0.95rem; line-height: 1.5; }
  .subtle { margin-top: 1.5rem; font-size: 0.8rem; color: #52525b; }
</style>
"""

SUCCESS_HTML = f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Powerset</title>{_PAGE_STYLE}</head>
<body>
  <div class="card">
    <div class="icon">✅</div>
    <h1>You're in!</h1>
    <p>Authentication complete. Head back to your terminal.</p>
    <p class="subtle">You can close this tab.</p>
  </div>
</body></html>
"""

_ERROR_TEMPLATE = string.Template(f"""<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Powerset</title>{_PAGE_STYLE}</head>
<body>
  <div class="card">
    <div class="icon">❌</div>
    <h1>Login failed</h1>
    <p>$ERROR</p>
    <p class="subtle">Try running <code>contact-exporter login</code> again.</p>
  </div>
</body></html>
""")


def _generate_pkce() -> tuple[str, str]:
    """Generate PKCE code_verifier and code_challenge (S256)."""
    code_verifier = secrets.token_urlsafe(64)[:128]
    digest = hashlib.sha256(code_verifier.encode("ascii")).digest()
    code_challenge = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return code_verifier, code_challenge


class _CallbackHandler(http.server.BaseHTTPRequestHandler):
    """Captures the OAuth redirect on localhost."""

    auth_code: str | None = None
    error: str | None = None
    expected_state: str | None = None

    def do_GET(self):
        params = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)

        # CSRF check
        received_state = params.get("state", [None])[0]
        if self.expected_state and received_state != self.expected_state:
            _CallbackHandler.error = "Invalid state parameter — possible CSRF attack"
            self._respond(400, _ERROR_TEMPLATE.substitute(ERROR=_CallbackHandler.error))
            threading.Thread(target=self.server.shutdown, daemon=True).start()
            return

        if "code" in params:
            _CallbackHandler.auth_code = params["code"][0]
            self._respond(200, SUCCESS_HTML)
        else:
            _CallbackHandler.error = params.get("error_description", ["Unknown error"])[0]
            self._respond(400, _ERROR_TEMPLATE.substitute(ERROR=html.escape(_CallbackHandler.error)))

        threading.Thread(target=self.server.shutdown, daemon=True).start()

    def _respond(self, status: int, body: str):
        self.send_response(status)
        self.send_header("Content-Type", "text/html")
        self.end_headers()
        self.wfile.write(body.encode())

    def log_message(self, format, *args):
        pass  # Suppress default HTTP request logging


def _decode_jwt_email(token: str) -> str | None:
    """Extract email from a JWT payload without signature verification.

    Safe here because the token was just received from Auth0 over HTTPS --
    we're only reading our own fresh token for display purposes.
    """
    try:
        payload_b64 = token.split(".")[1]
        # Pad to valid base64 length
        payload_b64 += "=" * (4 - len(payload_b64) % 4)
        payload = json.loads(base64.urlsafe_b64decode(payload_b64))
        return payload.get("email") or payload.get("https://api.powerset.dev/email")
    except Exception:
        return None


def login():
    """Run the full Auth0 PKCE login flow.

    1. Generate PKCE verifier + challenge
    2. Open browser to Auth0 Universal Login
    3. Capture callback on localhost
    4. Exchange auth code for tokens
    5. Save credentials to disk
    """
    code_verifier, code_challenge = _generate_pkce()
    oauth_state = secrets.token_urlsafe(32)

    # Reset class-level handler state
    _CallbackHandler.auth_code = None
    _CallbackHandler.error = None
    _CallbackHandler.expected_state = oauth_state

    authorize_url = f"https://{AUTH0_DOMAIN}/authorize?" + urllib.parse.urlencode({
        "response_type": "code",
        "client_id": AUTH0_CLIENT_ID,
        "redirect_uri": CALLBACK_URL,
        "scope": AUTH0_SCOPES,
        "audience": AUTH0_AUDIENCE,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "state": oauth_state,
    })

    try:
        server = http.server.HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
    except OSError as e:
        console.print(f"[red]Could not start callback server on port {CALLBACK_PORT}: {e}[/red]")
        console.print("[dim]Another process may be using this port.[/dim]")
        raise SystemExit(1)

    console.print("\n[bold]Opening browser for Powerset login...[/bold]")
    console.print(f"[dim]If it doesn't open, visit: {authorize_url}[/dim]\n")
    webbrowser.open(authorize_url)

    server_thread = threading.Thread(target=server.serve_forever, daemon=True)
    server_thread.start()
    server_thread.join(timeout=120)

    if server_thread.is_alive():
        server.shutdown()
        server.server_close()
        console.print("[red]Login timed out. Try again.[/red]")
        raise SystemExit(1)

    server.server_close()

    if _CallbackHandler.error:
        console.print(f"[red]Login failed: {_CallbackHandler.error}[/red]")
        raise SystemExit(1)

    if not _CallbackHandler.auth_code:
        console.print("[red]Login failed: no authorization code received[/red]")
        raise SystemExit(1)

    # Exchange auth code for tokens
    console.print("[dim]Exchanging auth code for tokens...[/dim]")
    token_resp = requests.post(
        f"https://{AUTH0_DOMAIN}/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": AUTH0_CLIENT_ID,
            "code": _CallbackHandler.auth_code,
            "code_verifier": code_verifier,
            "redirect_uri": CALLBACK_URL,
        },
        timeout=30,
    )

    if token_resp.status_code != 200:
        console.print(f"[red]Token exchange failed: {token_resp.text}[/red]")
        raise SystemExit(1)

    token_data = token_resp.json()
    access_token = token_data["access_token"]

    save_credentials(
        access_token=access_token,
        refresh_token=token_data.get("refresh_token"),
        expires_in=token_data.get("expires_in", 86400),
        email=_decode_jwt_email(access_token),
    )

    email = _decode_jwt_email(access_token) or "unknown"
    console.print(f"\n[green bold]✅ Logged in as {email}[/green bold]")
