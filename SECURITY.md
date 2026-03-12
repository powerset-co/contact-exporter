# Security Policy

## Reporting a vulnerability

If you discover a security vulnerability, please email **security@powerset.dev** instead of opening a public issue. We aim to respond within 48 hours.

## Installation security

The install script (`install.sh`) is designed to prevent common attack vectors:

- **No `curl | bash`** — the script detects when piped directly to a shell and exits with an error. This prevents partial-download / truncated-stream attacks where a MITM or network interruption causes a half-downloaded script to execute.
- **HTTPS only** — the script is served from GitHub over TLS. Combined with `curl -f` (fail on HTTP errors), this prevents serving malicious content via error pages.
- **Download-first pattern** — users download the script to a file, can inspect it, then run it. This ensures the complete script is present before execution.
- **No auto-install of Docker** — Docker Desktop requires manual installation to prevent privilege escalation via an automated installer.

## Threat model

| Asset | Storage | Risk | Mitigation |
|-------|---------|------|------------|
| OAuth tokens | `~/.powerset/credentials.json` | Local file access | File permissions `0600`, directory `0700` |
| iMessage database | `~/Library/Messages/chat.db` | Read-only access via `imsg` | No write operations; requires Full Disk Access |
| Contact names | macOS Contacts.app | Read-only AppleScript query | No modification; requires Contacts permission |
| WhatsApp session | Local Docker container | Local process access to WAHA API | Binds to `127.0.0.1`; fixed API key (see below) |
| Extracted contacts | `contacts.csv` | Local file | User reviews before upload; no auto-upload |

## Security controls

- **OAuth PKCE with state parameter**: No client secrets stored; CSRF protection via `state` parameter
- **Localhost-only callback**: OAuth callback server binds to `localhost:9876`
- **Docker network isolation**: WAHA container binds to `127.0.0.1`, not `0.0.0.0`
- **Credential file permissions**: `~/.powerset/` directory is `0700`, `credentials.json` is `0600`
- **No message content**: Only metadata (phone, name, count, timestamp) is extracted — never message text
- **No subprocess injection**: All subprocess arguments are passed as lists, never shell-interpolated

## Known limitations

- **No encryption at rest**: `credentials.json` and `contacts.csv` are stored as plaintext
- **macOS Contacts.app**: AppleScript queries are hardcoded strings (no user input interpolation), but the Contacts permission grant is broad
- **WAHA container**: Uses the WAHA Core free tier which runs a headless Chromium instance; WhatsApp sees this as "Google Chrome (Linux)"
- **WAHA API key is fixed**: The key `powerset-local` is hardcoded and identical for all users. While the container binds to `127.0.0.1`, any local process on the machine can access the WAHA API at `http://localhost:3000` while the container is running. Stop the container after use with `docker rm -f powerset-waha` to close this window.
- **ARM64 emulation**: The WAHA Docker image is `linux/amd64` only. On Apple Silicon Macs it runs via Rosetta 2 emulation, which is slower but functional.
