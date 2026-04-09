"""WhatsApp contact extraction via a local WAHA Docker container.

Starts an ephemeral WAHA container, authenticates via QR code,
extracts contacts and chat metadata, then keeps the container running
for subsequent runs without re-scanning.

No message content is read -- only contact info and message counts.
"""

from __future__ import annotations

import re
import shutil
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path

import qrcode as qrcode_lib
import requests
from rich.console import Console
from rich.live import Live
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.text import Text

from contact_exporter.config import (
    MAX_CONTACTS_OUTPUT,
    MESSAGE_COUNT_CAP,
    WAHA_API_KEY,
    WAHA_CONTAINER_NAME,
    WAHA_PORT,
    WAHA_SESSION_NAME,
)
from contact_exporter.merge import load_existing_contacts, merge_contact, write_contacts
from contact_exporter.models import Contact

console = Console()

# Throttle WAHA API requests to avoid hammering the local container
_MIN_REQUEST_INTERVAL = 0.5

_WAHA_HEADERS = {"X-Api-Key": WAHA_API_KEY}
_WAHA_BASE = f"http://127.0.0.1:{WAHA_PORT}"

# Persistent session storage — survives container restarts
_SESSIONS_DIR = Path.home() / ".powerset" / "waha-sessions"

# E.164 phone numbers: 7-15 digits
_MIN_PHONE_DIGITS = 7
_MAX_PHONE_DIGITS = 15


# ---------------------------------------------------------------------------
# Retry helper
# ---------------------------------------------------------------------------

def _waha_get(url: str, retries: int = 3, backoff: float = 2.0, **kwargs) -> requests.Response:
    """GET with retries and exponential backoff for slow Rosetta responses."""
    kwargs.setdefault("headers", _WAHA_HEADERS)
    kwargs.setdefault("timeout", 60)
    last_exc: requests.RequestException | None = None
    for attempt in range(retries):
        try:
            time.sleep(_MIN_REQUEST_INTERVAL)
            resp = requests.get(url, **kwargs)
            if resp.status_code < 500:
                return resp
            # 5xx → retry
            console.print(f"[dim]Server error {resp.status_code}, retrying...[/dim]")
        except requests.RequestException as e:
            last_exc = e
            if attempt < retries - 1:
                wait = backoff ** attempt
                console.print(f"[dim]{type(e).__name__}, retrying in {wait:.0f}s...[/dim]")
                time.sleep(wait)
    if last_exc:
        raise last_exc
    return resp  # type: ignore[possibly-undefined]


# ---------------------------------------------------------------------------
# Docker lifecycle
# ---------------------------------------------------------------------------

def _check_docker_installed():
    """Verify Docker is installed and running, starting Colima/Docker Desktop if needed."""
    if not shutil.which("docker"):
        console.print("[red bold]Docker not found[/red bold]")
        console.print()
        console.print("Docker is required for WhatsApp extraction.")
        console.print("  Install: [cyan]brew install colima docker[/cyan]")
        raise SystemExit(1)

    result = subprocess.run(["docker", "info"], capture_output=True, text=True, timeout=10)
    if result.returncode == 0:
        return

    # Try Colima first (lightweight, no EULA)
    if shutil.which("colima"):
        console.print("[dim]Starting Colima...[/dim]")
        colima_args = ["colima", "start", "--memory", "2"]
        # Use Rosetta for faster x86 emulation on Apple Silicon
        arch_result = subprocess.run(["uname", "-m"], capture_output=True, text=True, timeout=5)
        if arch_result.stdout.strip() == "arm64":
            colima_args.extend(["--vm-type", "vz", "--vz-rosetta"])
        subprocess.run(colima_args, timeout=120)
        check = subprocess.run(["docker", "info"], capture_output=True, timeout=10)
        if check.returncode == 0:
            console.print("[dim]Colima is ready[/dim]")
            return

    # Fallback: try Docker Desktop
    console.print("[dim]Starting Docker Desktop...[/dim]")
    subprocess.run(["open", "-a", "Docker"], capture_output=True, timeout=10)
    deadline = time.time() + 60
    while time.time() < deadline:
        check = subprocess.run(["docker", "info"], capture_output=True, timeout=5)
        if check.returncode == 0:
            console.print("[dim]Docker is ready[/dim]")
            return
        time.sleep(2)

    console.print("[red bold]Docker runtime not available[/red bold]")
    console.print("Install Colima: [cyan]brew install colima docker && colima start[/cyan]")
    raise SystemExit(1)


def _is_container_running() -> bool:
    result = subprocess.run(
        ["docker", "inspect", "--format", "{{.State.Running}}", WAHA_CONTAINER_NAME],
        capture_output=True, text=True, timeout=10,
    )
    return result.returncode == 0 and "true" in result.stdout.lower()


_WAHA_IMAGE = "devlikeapro/waha:noweb-2026.3.4"


def _start_container():
    """Start a fresh WAHA Docker container, removing any existing one.

    Mounts ~/.powerset/waha-sessions to /app/.sessions so WhatsApp credentials
    persist across container restarts (no re-scanning QR every time).
    """
    subprocess.run(["docker", "rm", "-f", WAHA_CONTAINER_NAME], capture_output=True, timeout=10)

    # Ensure session storage directory exists on host
    _SESSIONS_DIR.mkdir(parents=True, exist_ok=True)

    # Pull image separately so the user sees download progress
    check = subprocess.run(
        ["docker", "image", "inspect", _WAHA_IMAGE],
        capture_output=True, timeout=10,
    )
    if check.returncode != 0:
        console.print(f"[dim]Pulling {_WAHA_IMAGE}...[/dim]")
        pull = subprocess.run(
            ["docker", "pull", "--platform", "linux/amd64", _WAHA_IMAGE],
            timeout=600,
        )
        if pull.returncode != 0:
            console.print("[red]Failed to pull WAHA image[/red]")
            raise SystemExit(1)

    console.print("[dim]Starting WAHA container...[/dim]")
    result = subprocess.run(
        [
            "docker", "run", "-d",
            "--platform", "linux/amd64",
            "--name", WAHA_CONTAINER_NAME,
            "-p", f"127.0.0.1:{WAHA_PORT}:3000",
            "-v", f"{_SESSIONS_DIR}:/app/.sessions",
            "-e", "WAHA_DEFAULT_ENGINE=NOWEB",
            "-e", "WHATSAPP_DEFAULT_ENGINE=NOWEB",
            "-e", "WHATSAPP_RESTART_ALL_SESSIONS=true",
            "-e", f"WAHA_API_KEY={WAHA_API_KEY}",
            _WAHA_IMAGE,
        ],
        capture_output=True, text=True, timeout=300,
    )
    if result.returncode != 0:
        console.print(f"[red]Failed to start WAHA: {result.stderr.strip()}[/red]")
        raise SystemExit(1)

    console.print("[dim]WAHA container started[/dim]")


def _stop_container():
    console.print("[dim]Stopping WAHA container...[/dim]")
    subprocess.run(["docker", "rm", "-f", WAHA_CONTAINER_NAME], capture_output=True, timeout=15)


# ---------------------------------------------------------------------------
# QR code rendering
# ---------------------------------------------------------------------------

def _render_qr_to_terminal(data: str) -> Text:
    """Render a QR code using Rich styled backgrounds.

    Uses explicit black/white background colors on spaces — no half-block
    Unicode chars that break with terminal line spacing.
    Returns a Text object for use with Rich Live display.
    """
    qr = qrcode_lib.QRCode(
        border=2,
        error_correction=qrcode_lib.constants.ERROR_CORRECT_L,
    )
    qr.add_data(data)
    qr.make(fit=True)

    output = Text()
    matrix = qr.get_matrix()
    for i, row in enumerate(matrix):
        for cell in row:
            # 2 spaces per module — terminal chars are ~2:1 height:width,
            # so 2 spaces makes each module roughly square (scannable)
            output.append("  ", style="on black" if cell else "on white")
        if i < len(matrix) - 1:
            output.append("\n")
    return output


# ---------------------------------------------------------------------------
# WAHA session management
# ---------------------------------------------------------------------------

def _is_session_authenticated() -> bool:
    try:
        resp = requests.get(
            f"{_WAHA_BASE}/api/sessions/{WAHA_SESSION_NAME}",
            headers=_WAHA_HEADERS, timeout=5,
        )
        return resp.status_code == 200 and resp.json().get("status") == "WORKING"
    except (requests.ConnectionError, requests.Timeout):
        return False


def _wait_for_healthy(timeout: int = 180):
    """Block until WAHA is ready to accept API requests."""
    deadline = time.time() + timeout
    with console.status("[bold]Waiting for WAHA to start..."):
        while time.time() < deadline:
            try:
                resp = requests.get(f"{_WAHA_BASE}/api/sessions", headers=_WAHA_HEADERS, timeout=15)
                if resp.status_code == 200:
                    return
            except requests.RequestException:
                pass
            time.sleep(1)

    console.print("[red]WAHA did not become healthy within timeout[/red]")
    raise SystemExit(1)


def _stop_session():
    """Stop and delete the WAHA session to clear stale state."""
    try:
        requests.put(
            f"{_WAHA_BASE}/api/sessions/{WAHA_SESSION_NAME}/stop",
            headers=_WAHA_HEADERS, timeout=10,
        )
    except requests.RequestException:
        pass
    try:
        requests.delete(
            f"{_WAHA_BASE}/api/sessions/{WAHA_SESSION_NAME}",
            headers=_WAHA_HEADERS, timeout=10,
        )
    except requests.RequestException:
        pass


def _create_session():
    """Create a fresh WAHA session, cleaning up any stale one first."""
    # Clean up any existing session to avoid 422 / stale state
    _stop_session()
    time.sleep(1)

    # NOWEB store must be enabled to fetch chats/contacts (required since WAHA 2026.3+)
    session_config = {
        "name": WAHA_SESSION_NAME,
        "config": {
            "noweb": {
                "store": {
                    "enabled": True,
                    "full_sync": True,
                }
            }
        },
    }

    resp = requests.post(
        f"{_WAHA_BASE}/api/sessions/start",
        json=session_config,
        headers=_WAHA_HEADERS, timeout=15,
    )
    if resp.status_code == 422:
        # Session still lingering — force delete and retry
        console.print("[dim]Clearing stale session...[/dim]")
        _stop_session()
        time.sleep(2)
        resp = requests.post(
            f"{_WAHA_BASE}/api/sessions/start",
            json=session_config,
            headers=_WAHA_HEADERS, timeout=15,
        )

    if resp.status_code not in (200, 201):
        console.print(f"[red]Failed to create session: {resp.status_code} {resp.text}[/red]")
        raise SystemExit(1)


def _build_qr_display(qr_text: Text | None, status: str, remaining: int) -> Text:
    """Build the full QR display panel for Rich Live."""
    output = Text()
    output.append("Scan the QR code with WhatsApp on your phone\n", style="bold")
    output.append("WhatsApp > Settings > Linked Devices > Link a Device\n\n", style="dim")

    if qr_text:
        output.append_text(qr_text)
        output.append("\n")
    else:
        output.append("Waiting for QR code...\n", style="dim")

    output.append(f"\nStatus: {status}  ({remaining}s remaining)", style="dim")
    return output


def _wait_for_qr_auth(timeout: int = 120):
    """Display QR code and wait for the user to scan it.

    Uses Rich Live to update the display in-place — no scrolling.
    """
    deadline = time.time() + timeout
    last_qr_value = None
    last_qr_time = 0.0
    qr_text: Text | None = None

    with Live(
        _build_qr_display(None, "starting", timeout),
        console=console,
        refresh_per_second=1,
    ) as live:
        while time.time() < deadline:
            remaining = int(deadline - time.time())
            try:
                resp = requests.get(
                    f"{_WAHA_BASE}/api/sessions/{WAHA_SESSION_NAME}",
                    headers=_WAHA_HEADERS, timeout=30,
                )
                if resp.status_code != 200:
                    live.update(_build_qr_display(qr_text, f"HTTP {resp.status_code}", remaining))
                    time.sleep(2)
                    continue

                session_data = resp.json()
                status = session_data.get("status", "")

                if status == "WORKING":
                    live.update(Text("✅ WhatsApp authenticated!", style="green bold"))
                    time.sleep(1)
                    return

                if status == "FAILED":
                    live.update(Text("❌ WhatsApp session failed.", style="red bold"))
                    raise SystemExit(1)

                # Fetch QR on any non-terminal status
                needs_refresh = (time.time() - last_qr_time) > 15
                if needs_refresh and status not in ("WORKING", "FAILED"):
                    qr_resp = requests.get(
                        f"{_WAHA_BASE}/api/{WAHA_SESSION_NAME}/auth/qr",
                        params={"format": "raw"},
                        headers=_WAHA_HEADERS, timeout=30,
                    )
                    if qr_resp.status_code == 200:
                        qr_data = qr_resp.json()
                        qr_value = qr_data.get("value") or qr_data.get("qr", "")
                        if qr_value and qr_value != last_qr_value:
                            qr_text = _render_qr_to_terminal(qr_value)
                            last_qr_value = qr_value
                            last_qr_time = time.time()

                live.update(_build_qr_display(qr_text, status, remaining))

            except requests.RequestException as e:
                live.update(_build_qr_display(qr_text, f"waiting ({type(e).__name__})", remaining))

            time.sleep(3)

    console.print("[red]QR scan timed out. Try again.[/red]")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# JID / phone helpers
# ---------------------------------------------------------------------------

def _extract_jid(raw_id) -> str:
    """Extract serialized JID string from a WAHA id (may be string or dict)."""
    if isinstance(raw_id, dict):
        return raw_id.get("_serialized", "") or raw_id.get("user", "")
    return str(raw_id)


def _jid_to_phone(jid: str) -> str | None:
    """Convert a WhatsApp JID like '14155551234@c.us' to '+14155551234'.

    Returns None for group JIDs (@g.us), linked IDs (@lid), and invalid numbers.
    @lid contacts are WhatsApp internal IDs, not real phone numbers — they always
    have a corresponding @c.us entry with the same name and a real number.
    """
    if not jid or "@g.us" in jid or "@lid" in jid:
        return None
    match = re.match(r"(\d+)@", jid)
    if not match:
        return None
    digits = match.group(1)
    if not (_MIN_PHONE_DIGITS <= len(digits) <= _MAX_PHONE_DIGITS):
        return None
    return f"+{digits}"


# ---------------------------------------------------------------------------
# Data extraction
# ---------------------------------------------------------------------------

def _get_chat_message_count(chat_id: str) -> int:
    """Count messages in a 1:1 chat, capped at MESSAGE_COUNT_CAP."""
    try:
        resp = _waha_get(
            f"{_WAHA_BASE}/api/{WAHA_SESSION_NAME}/chats/{chat_id}/messages",
            params={"limit": MESSAGE_COUNT_CAP + 1, "downloadMedia": "false"},
        )
        if resp.status_code == 200:
            messages = resp.json()
            if isinstance(messages, list):
                count = min(len(messages), MESSAGE_COUNT_CAP)
                if count == 0:
                    console.print(f"[dim yellow]  ⚠ {chat_id}: API returned 0 messages[/dim yellow]")
                return count
            else:
                console.print(f"[dim red]  ✗ {chat_id}: unexpected response type: {type(messages).__name__}[/dim red]")
        else:
            console.print(f"[dim red]  ✗ {chat_id}: HTTP {resp.status_code}[/dim red]")
    except requests.RequestException as e:
        console.print(f"[dim red]  ✗ {chat_id}: request error: {e}[/dim red]")
    return 0


def _parse_timestamp(ts) -> str | None:
    """Convert a WAHA timestamp (seconds, milliseconds, or string) to ISO 8601."""
    if not ts:
        return None
    try:
        if isinstance(ts, (int, float)):
            # WAHA sometimes returns milliseconds instead of seconds
            if ts > 1e12:
                ts = ts / 1000
            return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat()
        return str(ts)
    except (ValueError, TypeError, OSError):
        return None


def _extract_contacts_from_waha() -> dict[str, Contact]:
    """Pull contacts and chat metadata from the WAHA API."""
    session = WAHA_SESSION_NAME

    # Fetch all chats
    console.print("[dim]Fetching chats...[/dim]")
    chats_resp = _waha_get(f"{_WAHA_BASE}/api/{session}/chats")
    chats = chats_resp.json() if chats_resp.status_code == 200 else []

    # Fetch contacts for name resolution + @lid -> phone mapping
    console.print("[dim]Fetching contacts...[/dim]")
    contacts_resp = _waha_get(
        f"{_WAHA_BASE}/api/contacts/all",
        params={"session": session},
    )
    raw_contacts = contacts_resp.json() if contacts_resp.status_code == 200 else []

    # Build lookups from contacts in a single pass:
    # - jid_to_name: any JID -> display name
    # - phone_to_name: phone -> display name (for group-only contacts)
    # - lid_to_phones: @lid JID -> list of E.164 phones (matched via contact name)
    #   Usually 1 phone, but ambiguous names (e.g. "Chrissy Hu" with 2 numbers)
    #   produce multiple entries — we create a Contact for each.
    jid_to_name: dict[str, str] = {}
    phone_to_name: dict[str, str] = {}
    lid_to_phones: dict[str, list[str]] = {}

    # Pass 1: index @c.us contacts by name, build jid_to_name for all
    name_to_phones: dict[str, list[str]] = {}
    lid_jids_by_name: dict[str, list[str]] = {}
    for raw in raw_contacts:
        raw_jid = _extract_jid(raw.get("id", ""))
        name = raw.get("name") or raw.get("pushname") or raw.get("shortName") or ""
        if raw_jid and name:
            jid_to_name[raw_jid] = name
        phone = _jid_to_phone(raw_jid)
        if phone:
            if name:
                phone_to_name[phone] = name
                name_to_phones.setdefault(name, []).append(phone)
        elif "@lid" in raw_jid and name:
            lid_jids_by_name.setdefault(name, []).append(raw_jid)

    # Pass 2: map @lid -> phones (all matching numbers for that name)
    for name, lid_jids in lid_jids_by_name.items():
        phones = name_to_phones.get(name, [])
        if phones:
            for lid_jid in lid_jids:
                lid_to_phones[lid_jid] = phones

    if lid_to_phones:
        console.print(f"[dim]Resolved {len(lid_to_phones)} @lid contacts to phone numbers[/dim]")

    direct_chats: dict[str, dict] = {}
    group_member_phones: set[str] = set()

    for chat in chats:
        chat_id = _extract_jid(chat.get("id", ""))
        if "@g.us" in chat_id:
            participants = chat.get("participants") or chat.get("groupMetadata", {}).get("participants", [])
            for p in participants:
                p_jid = _extract_jid(p.get("id", ""))
                phone = _jid_to_phone(p_jid)
                if phone:
                    group_member_phones.add(phone)
                else:
                    for ph in lid_to_phones.get(p_jid, []):
                        group_member_phones.add(ph)
        else:
            direct_chats[chat_id] = chat

    console.print(f"[dim]Found {len(raw_contacts)} contacts, {len(direct_chats)} direct chats[/dim]\n")

    contacts_by_phone: dict[str, Contact] = {}
    direct_jids = [
        jid for jid in direct_chats
        if _jid_to_phone(jid) or lid_to_phones.get(jid)
    ]

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Counting messages", total=len(direct_jids))

        for jid in direct_jids:
            progress.advance(task)

            # Resolve JID to phone(s): @c.us → 1 phone, @lid → possibly multiple
            direct_phone = _jid_to_phone(jid)
            phones = [direct_phone] if direct_phone else lid_to_phones.get(jid, [])
            if not phones:
                continue

            count = _get_chat_message_count(jid)

            chat = direct_chats[jid]
            last_message = _parse_timestamp(
                chat.get("timestamp") or chat.get("last_message_timestamp")
            )

            for phone in phones:
                contacts_by_phone[phone] = Contact(
                    phone=phone,
                    name=jid_to_name.get(jid, ""),
                    source="whatsapp",
                    is_in_group_chats=phone in group_member_phones,
                    message_count=count,
                    last_message=last_message,
                )

    # Add group-only contacts (seen in groups but no direct chat)
    for phone in group_member_phones:
        if phone not in contacts_by_phone:
            contacts_by_phone[phone] = Contact(
                phone=phone,
                name=phone_to_name.get(phone, ""),
                source="whatsapp",
                is_in_group_chats=True,
            )

    return contacts_by_phone


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_whatsapp(output_path: str = "contacts.csv", reset: bool = False) -> int:
    """Extract WhatsApp contacts via a local WAHA Docker container.

    Reuses an existing container if already running and authenticated.
    Merges results with any existing contacts.csv.

    Args:
        reset: If True, tear down any existing container and start fresh.

    Returns the number of contacts written.
    """
    _check_docker_installed()
    console.print("[bold]Extracting WhatsApp contacts...[/bold]")

    if reset:
        console.print("[yellow]Resetting WhatsApp session...[/yellow]")
        _stop_container()
        # Clear persisted session credentials so we get a fresh QR
        if _SESSIONS_DIR.exists():
            shutil.rmtree(_SESSIONS_DIR)
            console.print("[dim]Cleared saved session data[/dim]")
        time.sleep(1)

    started_new = False
    if not reset and _is_container_running() and _is_session_authenticated():
        console.print("[dim]Reusing existing WAHA session[/dim]\n")
    else:
        console.print("[dim]Starting local WAHA container (Docker)[/dim]\n")
        started_new = True
        _start_container()
        _wait_for_healthy()
        _create_session()
        _wait_for_qr_auth()

    try:
        wa_contacts = _extract_contacts_from_waha()

        existing = load_existing_contacts(output_path)
        for phone, new_contact in wa_contacts.items():
            if phone in existing:
                existing[phone] = merge_contact(existing[phone], new_contact)
            else:
                existing[phone] = new_contact

        total_written = write_contacts(existing, output_path, limit=MAX_CONTACTS_OUTPUT)

        console.print(
            f"\n[green bold]✅ {len(wa_contacts)} WhatsApp contacts merged"
            f" → {total_written} total in {output_path}[/green bold]"
        )
        console.print("[dim]WAHA container kept running — re-run without QR scan[/dim]")
        console.print(f"[dim]To stop: docker rm -f {WAHA_CONTAINER_NAME}[/dim]")

        return total_written

    except (Exception, KeyboardInterrupt):
        if started_new:
            _stop_container()
        raise
