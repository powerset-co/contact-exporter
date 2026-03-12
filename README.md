# contact-exporter

Extract iMessage & WhatsApp contacts locally and upload to [Powerset](https://powerset.dev).

> **Alpha software.** CLI flags and output format may change without notice.

## Why?

Your contacts are scattered across messaging apps, locked behind proprietary databases. `contact-exporter` reads your local message history, counts how often you talk to each person, and uploads the metadata (never message content) to Powerset so you can search your real network — not just LinkedIn connections.

Everything runs on your machine. The only data that leaves is what you explicitly upload.

## Features

- **iMessage extraction** — reads `~/Library/Messages/chat.db` via [imsg](https://github.com/steipete/imsg) (read-only, no message content)
- **WhatsApp extraction** — runs a local [WAHA](https://github.com/devlikeapro/waha) Docker container, authenticates via QR code
- **Contact name resolution** — matches phone numbers to names via macOS Contacts.app
- **Group chat detection** — flags contacts seen in group chats, extracts participants
- **Message frequency** — counts messages per contact over the last 90 days (capped at 200)
- **Smart merging** — re-running merges new data with existing exports; most recent data wins
- **Top-250 output** — sorted by message count so your most active contacts come first
- **Privacy-first auth** — Auth0 PKCE flow (no secrets stored, browser-based login)
- **Zero-config permissions** — automatically opens System Settings when Full Disk Access or Contacts access is needed

## Installation

### One-liner (recommended)

```bash
curl -fsSL https://raw.githubusercontent.com/powerset-co/contact-exporter/main/install.sh -o /tmp/ce-install.sh && bash /tmp/ce-install.sh
```

This downloads the installer to a temp file (never pipes directly to shell), then walks you through setup: Homebrew, Docker Desktop, and `contact-exporter` itself.

> **Why not `curl | bash`?** Piping directly to a shell is vulnerable to partial-download attacks -- if the connection drops mid-transfer, your shell executes a truncated script. Downloading first ensures you run the complete, unmodified script over HTTPS.

### Manual install

If you prefer to do it yourself, here are the three steps:

**1. Install Homebrew** (if you don't have it)

```bash
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
```

**2. Install Docker Desktop** (required for WhatsApp extraction)

```bash
brew install --cask docker
```

After installing, open Docker Desktop from Applications at least once to complete setup. If you only need iMessage extraction, you can skip this step.

**3. Install contact-exporter**

```bash
brew install powerset-co/powerset/contact-exporter
```

This installs Python, all dependencies, and the `contact-exporter` CLI into an isolated Homebrew environment. No need to manage Python versions or virtual environments yourself.

## Quick start

```bash
contact-exporter login        # opens browser for Powerset OAuth
contact-exporter imessage     # extract iMessage contacts → contacts.csv
contact-exporter whatsapp     # extract WhatsApp contacts (needs Docker)
contact-exporter upload       # upload contacts.csv to Powerset
```

On first run, `imessage` will prompt for macOS permissions (Full Disk Access + Contacts) and open System Settings automatically.

## Commands

| Command | Description |
|---------|-------------|
| `login` | Authenticate with Powerset via browser (Auth0 PKCE) |
| `logout` | Clear stored credentials from `~/.powerset/` |
| `whoami` | Show current authenticated user and token status |
| `imessage` | Extract iMessage contacts to CSV |
| `whatsapp` | Extract WhatsApp contacts via local Docker container |
| `review` | Interactively review contacts (mark to skip) before upload |
| `upload` | Upload `contacts.csv` to Powerset API |

### Flags

| Flag | Commands | Description |
|------|----------|-------------|
| `--output, -o` | `imessage`, `whatsapp` | Output file path (default: `contacts.csv`) |
| `--include-small-groups` | `imessage` | Include message counts from small group chats (≤7 people) |
| `--file, -f` | `review`, `upload` | CSV file to review/upload (default: `contacts.csv`) |

## Output format

Each row in `contacts.csv`:

| Field | Type | Description |
|-------|------|-------------|
| `phone` | string | E.164 phone number |
| `name` | string | Contact name (from Contacts.app or WhatsApp) |
| `source` | string | `"imessage"`, `"whatsapp"`, or `"imessage,whatsapp"` |
| `is_in_group_chats` | bool | Whether this contact appears in any group chat |
| `message_count` | int \| empty | Messages in the last 90 days (1:1 only, capped at 200) |
| `last_message` | string \| empty | ISO 8601 timestamp of most recent message |
| `skip` | string | `"yes"` if marked to exclude from upload |

## Configuration

Credentials are stored in `~/.powerset/credentials.json` with `0600` file permissions and `0700` directory permissions.

Override the API endpoint for local development:

```bash
POWERSET_API_URL=http://localhost:8000 contact-exporter upload
```

## How it works

### iMessage

1. Reads `~/Library/Messages/chat.db` via `imsg` CLI (read-only SQLite access)
2. Queries macOS Contacts.app via AppleScript to resolve phone numbers to names
3. Counts messages per 1:1 conversation in the last 90 days
4. Scans group chats to identify participants (no message counting for groups by default)
5. Merges with any existing `contacts.csv`, sorts by message count, outputs top 250

### WhatsApp

1. Starts a local WAHA Docker container (binds to `127.0.0.1` only)
2. Authenticates via QR code scan on your phone
3. Fetches contacts and chat list from the WAHA API
4. Counts messages per 1:1 chat
5. Keeps the container running for re-runs without re-authentication
6. Merges with existing data, sorts by count, outputs top 250

### Privacy

- **No message content** is ever read, stored, or transmitted — only metadata (phone, name, count, timestamp)
- **Local-only processing** — extraction runs entirely on your machine
- **You choose what to upload** — review `contacts.csv` before running `upload`
- **Open source** — audit the code yourself

## Security

See [SECURITY.md](SECURITY.md) for the full threat model, security controls, and vulnerability reporting.

## Development

Requires Python 3.10+ and [imsg](https://github.com/steipete/imsg).

```bash
git clone https://github.com/powerset-co/contact-exporter.git
cd contact-exporter
pip install -e .
```

## License

[MIT](LICENSE)
