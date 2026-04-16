# contact-exporter

Extract iMessage & WhatsApp contacts locally and upload to [Powerset](https://powerset.dev).

> **Alpha software.** CLI flags and output format may change without notice.

## Why?

Your contacts are scattered across messaging apps, locked behind proprietary databases. `contact-exporter` reads your local message history, counts how often you talk to each person, and uploads the metadata (never message content) to Powerset so you can search your real network — not just LinkedIn connections.

Everything runs on your machine. The only data that leaves is what you explicitly upload.

## Features

- **iMessage extraction** — reads `~/Library/Messages/chat.db` directly in read-only mode (no message content)
- **WhatsApp extraction** — runs a local [WAHA](https://github.com/devlikeapro/waha) Docker container, authenticates via QR code
- **Contact name resolution** — matches phone numbers to names via macOS Contacts.app
- **Group chat detection** — flags contacts seen in group chats, extracts participants
- **Full-history message frequency** — counts messages across full local history (no 90-day window, no cap)
- **Smart merging** — re-running merges new data with existing exports; most recent data wins
- **Local matching** — downloads your operator candidate catalog and matches locally by name
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
contact-exporter full --api-key sk-or-...  # iMessage + WhatsApp + LLM review
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
| `full` | Run iMessage + WhatsApp extraction, then LLM review on unmatched/suggested |
| `sync-candidates` | Download operator candidate catalog to `powerset_contacts.csv` |
| `match-local` | Apply local name matching to `contacts.csv` |
| `review` | Interactively review contacts (mark to skip) before upload |
| `upload` | Upload `contacts.csv` to Powerset API |

### Flags

| Flag | Commands | Description |
|------|----------|-------------|
| `--output, -o` | `imessage`, `whatsapp` | Output file path (default: `contacts.csv`) |
| `--output, -o` | `full` | Unified CSV output path (default: `contacts.csv`) |
| `--include-small-groups` | `imessage` | Kept for compatibility (no-op; full-history counting is always on) |
| `--include-small-groups` | `full` | Passed to iMessage extractor (same no-op compatibility behavior) |
| `--file, -f` | `review`, `upload` | CSV file to review/upload (default: `contacts.csv`) |
| `--api-key` | `llm-review`, `full` | OpenRouter API key (or set `OPENROUTER_API_KEY`) |
| `--model` | `llm-review`, `full` | LLM model for review (default: `anthropic/claude-sonnet-4-6`) |
| `--dry-run` | `llm-review`, `full` | Estimate LLM review cost without API calls |
| `--all` | `llm-review`, `full` | Review all named contacts (default: only unmatched/suggested) |
| `--local` | `imessage`, `whatsapp`, `full`, `sync-candidates`, `match-local`, `upload` | Use local API endpoint (`http://localhost:8000`) for this run |
| `--api-base-url` | `imessage`, `whatsapp`, `full`, `sync-candidates`, `match-local`, `upload` | Use a custom API endpoint for this run |

## Output format

Each row in `contacts.csv`:

| Field | Type | Description |
|-------|------|-------------|
| `phone` | string | E.164 phone number |
| `name` | string | Contact name (from Contacts.app or WhatsApp) |
| `source` | string | `"imessage"`, `"whatsapp"`, or `"imessage,whatsapp"` |
| `is_in_group_chats` | bool | Whether this contact appears in any group chat |
| `message_count` | int \| empty | Full-history message count for the contact |
| `last_message` | string \| empty | ISO 8601 timestamp of most recent message |
| `skip` | string | `"yes"` if marked to exclude from upload |
| `match_status` | string \| empty | `matched`, `suggested`, or `unmatched` |
| `matched_person_id` | string \| empty | Candidate person id for matched/suggested rows |
| `matched_name` | string \| empty | Candidate display name for matched/suggested rows |
| `matched_linkedin_url` | string \| empty | Candidate LinkedIn URL for matched/suggested rows |
| `match_confidence` | float \| empty | Local matcher confidence score |
| `match_method` | string \| empty | Matching method used |
| `match_reason` | string \| empty | Human-readable reason for match/unmatch |

## Configuration

Credentials are stored in `~/.powerset/credentials.json` with `0600` file permissions and `0700` directory permissions.

Override the API endpoint for local development:

```bash
POWERSET_API_URL=http://localhost:8000 contact-exporter upload
```

## How it works

### iMessage

1. Reads `~/Library/Messages/chat.db` directly in read-only mode
2. Queries macOS Contacts.app / AddressBook DB to resolve phone numbers to names
3. Aggregates full-history message counts per contact in SQL (single pass)
4. Flags group chat participants
5. Syncs operator candidate catalog (`powerset_contacts.csv`) and applies local name matching
6. Merges with any existing `contacts.csv` and writes all contacts

### WhatsApp

1. Starts a local WAHA Docker container (binds to `127.0.0.1` only)
2. Authenticates via QR code scan on your phone
3. Fetches contacts and chat list from the WAHA API
4. Counts full 1:1 chat history (pagination / parallel fetch as needed)
5. Keeps the container running for re-runs without re-authentication
6. Syncs candidate catalog and applies local name matching
7. Merges with existing data and writes full contact set

### Privacy

- **No message content** is ever read, stored, or transmitted — only metadata (phone, name, count, timestamp)
- **Local-only processing** — extraction runs entirely on your machine
- **You choose what to upload** — review `contacts.csv` before running `upload`
- **Open source** — audit the code yourself

## Security

See [SECURITY.md](SECURITY.md) for the full threat model, security controls, and vulnerability reporting.

## Development

Requires Python 3.10+.

```bash
git clone https://github.com/powerset-co/contact-exporter.git
cd contact-exporter
pip install -e .
```

## License

[MIT](LICENSE)
