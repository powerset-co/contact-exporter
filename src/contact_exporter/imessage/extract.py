"""iMessage contact extraction via direct SQLite reads.

Reads ~/Library/Messages/chat.db directly to extract contact metadata.
Only counts messages -- never reads or exports message content.

Works on both Intel and Apple Silicon (no external binary dependencies).
"""

from __future__ import annotations

import glob
import re
import sqlite3
import subprocess
import time
import webbrowser
from datetime import datetime, timedelta, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from contact_exporter.config import MAX_CONTACTS_OUTPUT, MESSAGE_COUNT_CAP, SMALL_GROUP_MAX_MEMBERS
from contact_exporter.merge import load_existing_contacts, merge_contact, write_contacts
from contact_exporter.models import Contact

console = Console()

# ---------------------------------------------------------------------------
# Paths & constants
# ---------------------------------------------------------------------------

_CHAT_DB = Path.home() / "Library" / "Messages" / "chat.db"

# Apple epoch: Jan 1, 2001 00:00:00 UTC (timestamps are nanoseconds since this)
_APPLE_EPOCH_OFFSET = 978_307_200
_NS_PER_SEC = 1_000_000_000

# AddressBook SQLite databases — one per sync source (iCloud, local, Exchange, etc.)
_ADDRESSBOOK_GLOB = str(
    Path.home() / "Library" / "Application Support" / "AddressBook"
    / "Sources" / "*" / "AddressBook-v22.abcddb"
)

_CONTACTS_QUERY = """
    SELECT p.ZFULLNUMBER, r.ZFIRSTNAME, r.ZLASTNAME
    FROM ZABCDPHONENUMBER p
    JOIN ZABCDRECORD r ON p.ZOWNER = r.Z_PK
    WHERE p.ZFULLNUMBER IS NOT NULL AND p.ZFULLNUMBER <> ''
"""

# Fallback: AppleScript (slow but works if SQLite is locked/inaccessible)
_CONTACTS_APPLESCRIPT = '''
tell application "Contacts"
    set firstNames to first name of every person
    set lastNames to last name of every person
    set allPhones to value of phones of every person
    set output to {}
    repeat with i from 1 to count of firstNames
        set phoneList to item i of allPhones
        if (count of phoneList) > 0 then
            set fn to item i of firstNames
            set ln to item i of lastNames
            if fn is missing value then set fn to ""
            if ln is missing value then set ln to ""
            set theName to fn & " " & ln
            repeat with ph in phoneList
                set end of output to (ph & tab & theName)
            end repeat
        end if
    end repeat
    set AppleScript's text item delimiters to linefeed
    return output as text
end tell
'''


# ---------------------------------------------------------------------------
# Timestamp helpers
# ---------------------------------------------------------------------------

def _apple_ns_to_unix(ns: int | None) -> float | None:
    """Convert Apple nanosecond timestamp to Unix epoch seconds."""
    if not ns:
        return None
    return (ns / _NS_PER_SEC) + _APPLE_EPOCH_OFFSET


def _apple_ns_to_iso(ns: int | None) -> str | None:
    """Convert Apple nanosecond timestamp to ISO 8601 string."""
    unix_ts = _apple_ns_to_unix(ns)
    if unix_ts is None:
        return None
    return datetime.fromtimestamp(unix_ts, tz=timezone.utc).isoformat()


def _datetime_to_apple_ns(dt: datetime) -> int:
    """Convert a datetime to Apple nanosecond timestamp."""
    return int((dt.timestamp() - _APPLE_EPOCH_OFFSET) * _NS_PER_SEC)


# ---------------------------------------------------------------------------
# Phone number helpers
# ---------------------------------------------------------------------------

def _normalize_phone(raw: str) -> str:
    """Normalize a phone number to 10 digits for lookup matching.

    Contacts.app stores numbers inconsistently -- (408) 835-4285, +14085354285,
    4085354285, etc. We strip to digits and remove leading country code 1 for
    US/CA numbers so all formats map to the same key.
    """
    digits = re.sub(r"[^\d]", "", raw)
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _is_phone_identifier(identifier: str) -> bool:
    """Check if a chat identifier looks like a phone number (not email/URN/group)."""
    if not identifier or "@" in identifier or identifier.startswith("urn:"):
        return False
    if identifier.startswith("chat"):
        return False
    digits = re.sub(r"[^\d]", "", identifier)
    return len(digits) >= 7


def _is_group_identifier(identifier: str) -> bool:
    """Check if a chat identifier is a group chat."""
    return identifier.startswith("chat")


# ---------------------------------------------------------------------------
# Contact name lookup
# ---------------------------------------------------------------------------

def _clean_contact_name(first: str, last: str) -> str:
    """Clean up a contact name from AddressBook fields.

    Handles sync artifacts:
      - "/N" disambiguation suffixes (e.g. "Joy/1" → "Joy")
      - "Last;First" ordering (e.g. "Chao;Joy" → "Joy Chao")
    """
    first = re.sub(r"/\d+$", "", first).strip()
    last = re.sub(r"/\d+$", "", last).strip()

    if first and last:
        name = f"{first} {last}"
    elif first:
        name = first
    elif last:
        name = last
    else:
        return ""

    if ";" in name:
        parts = name.split(";", 1)
        name = f"{parts[1]} {parts[0]}"

    return name.strip()


def _add_phone_to_lookup(lookup: dict[str, str], phone_raw: str, name: str) -> None:
    """Add a phone→name mapping with both normalized and full-digit keys."""
    name = name.strip()
    if not name:
        return
    digits = re.sub(r"[^\d]", "", phone_raw)
    if len(digits) < 7:
        return
    normalized = _normalize_phone(phone_raw)
    lookup[normalized] = name
    if digits != normalized:
        lookup[digits] = name


def _query_contacts_sqlite() -> dict[str, str]:
    """Read phone→name mappings directly from AddressBook SQLite databases."""
    db_paths = glob.glob(_ADDRESSBOOK_GLOB)
    if not db_paths:
        return {}

    lookup: dict[str, str] = {}
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for phone_raw, first, last in conn.execute(_CONTACTS_QUERY):
                name = _clean_contact_name(first or "", last or "")
                _add_phone_to_lookup(lookup, phone_raw, name)
            conn.close()
        except sqlite3.Error:
            continue

    return lookup


def _query_contacts_applescript() -> dict[str, str]:
    """Fallback: query Contacts.app via AppleScript (slow but reliable)."""
    try:
        subprocess.run(
            ["osascript", "-e", 'tell application "Contacts" to launch'],
            capture_output=True, text=True, timeout=10,
        )
        time.sleep(2)

        result = subprocess.run(
            ["osascript", "-e", _CONTACTS_APPLESCRIPT],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode != 0:
            return {}
    except subprocess.TimeoutExpired:
        return {}

    lookup: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        if "\t" not in line:
            continue
        phone_raw, name = line.split("\t", 1)
        _add_phone_to_lookup(lookup, phone_raw, name)

    return lookup


def _build_contact_name_lookup() -> dict[str, str]:
    """Build phone→name mapping from macOS Contacts.

    Tries direct SQLite read first (fast), falls back to AppleScript.
    """
    lookup = _query_contacts_sqlite()
    if lookup:
        console.print(f"[dim]Loaded {len(lookup)} contacts from AddressBook database[/dim]")
        return lookup

    console.print("[dim]SQLite read failed, falling back to AppleScript...[/dim]")
    lookup = _query_contacts_applescript()
    if lookup:
        return lookup

    console.print("[yellow]Warning: could not load contacts — names will be missing from export[/yellow]")
    console.print("[dim]Ensure Full Disk Access is granted to your terminal app[/dim]")
    return lookup


# ---------------------------------------------------------------------------
# Permissions check
# ---------------------------------------------------------------------------

def _check_full_disk_access() -> bool:
    """Check if we can read chat.db (requires Full Disk Access)."""
    try:
        conn = sqlite3.connect(f"file:{_CHAT_DB}?mode=ro", uri=True)
        conn.execute("SELECT 1 FROM chat LIMIT 1")
        conn.close()
        return True
    except sqlite3.Error:
        return False


def _check_permissions():
    """Verify Full Disk Access. Opens System Settings if missing."""
    console.print("[dim]Checking permissions...[/dim]")

    if _check_full_disk_access():
        return

    console.print("[red]❌ Full Disk Access — required to read iMessage history[/red]")
    console.print()
    console.print("[bold]Opening System Settings for you...[/bold]")
    console.print("Enable your terminal app, then [bold]restart your terminal[/bold] and retry.\n")

    webbrowser.open("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
    raise SystemExit(1)


# ---------------------------------------------------------------------------
# chat.db queries
# ---------------------------------------------------------------------------

def _open_chat_db() -> sqlite3.Connection:
    """Open chat.db read-only."""
    conn = sqlite3.connect(f"file:{_CHAT_DB}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _list_conversations(conn: sqlite3.Connection) -> list[dict]:
    """List all conversations with last message timestamp."""
    rows = conn.execute("""
        SELECT c.ROWID AS id,
               c.chat_identifier AS identifier,
               c.display_name AS display_name,
               MAX(m.date) AS last_date
        FROM chat c
        JOIN chat_message_join cmj ON c.ROWID = cmj.chat_id
        JOIN message m ON m.ROWID = cmj.message_id
        GROUP BY c.ROWID
        ORDER BY last_date DESC
    """).fetchall()

    return [
        {
            "id": row["id"],
            "identifier": row["identifier"] or "",
            "display_name": row["display_name"] or "",
            "last_date_ns": row["last_date"],
            "last_message_at": _apple_ns_to_iso(row["last_date"]),
        }
        for row in rows
    ]


def _count_messages_since(conn: sqlite3.Connection, chat_id: int, since_ns: int) -> int:
    """Count messages in a chat since a timestamp, capped at MESSAGE_COUNT_CAP.

    Excludes reactions/tapbacks (associated_message_type 2000-3006).
    """
    row = conn.execute("""
        SELECT COUNT(*) AS cnt
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        WHERE cmj.chat_id = ?
          AND m.date >= ?
          AND (m.associated_message_type IS NULL
               OR m.associated_message_type < 2000
               OR m.associated_message_type > 3006)
    """, (chat_id, since_ns)).fetchone()

    return min(row["cnt"], MESSAGE_COUNT_CAP) if row else 0


def _get_group_participants(conn: sqlite3.Connection, chat_id: int) -> list[str]:
    """Get phone numbers of group chat participants via chat_handle_join."""
    rows = conn.execute("""
        SELECT h.id
        FROM chat_handle_join chj
        JOIN handle h ON h.ROWID = chj.handle_id
        WHERE chj.chat_id = ?
    """, (chat_id,)).fetchall()

    phones = []
    for row in rows:
        handle_id = row["id"] or ""
        if _is_phone_identifier(handle_id):
            phones.append(handle_id)
    return phones


def _get_group_sender_stats(
    conn: sqlite3.Connection, chat_id: int, since_ns: int
) -> dict[str, dict]:
    """Get per-sender message counts and last message time for a group chat.

    Returns {sender_phone: {"count": int, "last_message": str | None}}.
    Excludes reactions/tapbacks and messages from self.
    """
    rows = conn.execute("""
        SELECT h.id AS sender, m.date AS msg_date
        FROM chat_message_join cmj
        JOIN message m ON m.ROWID = cmj.message_id
        LEFT JOIN handle h ON m.handle_id = h.ROWID
        WHERE cmj.chat_id = ?
          AND m.date >= ?
          AND m.is_from_me = 0
          AND (m.associated_message_type IS NULL
               OR m.associated_message_type < 2000
               OR m.associated_message_type > 3006)
    """, (chat_id, since_ns)).fetchall()

    participants: dict[str, dict] = {}
    for row in rows:
        sender = row["sender"] or ""
        if not sender or "@" in sender or sender.startswith("urn:"):
            continue

        if sender not in participants:
            participants[sender] = {"count": 0, "last_message": None}

        p = participants[sender]
        p["count"] = min(p["count"] + 1, MESSAGE_COUNT_CAP)

        iso = _apple_ns_to_iso(row["msg_date"])
        if iso and (p["last_message"] is None or iso > p["last_message"]):
            p["last_message"] = iso

    return participants


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_imessage(output_path: str = "contacts.csv", include_small_groups: bool = False) -> int:
    """Extract iMessage contacts via direct SQLite reads of chat.db.

    Reads all conversations, counts messages for 1:1 chats,
    flags group chat participants, and outputs the top contacts sorted by
    message count.

    Returns the number of contacts written.
    """
    _check_permissions()

    console.print("[bold]Extracting iMessage contacts...[/bold]")
    console.print(f"[dim]Reading {_CHAT_DB} (read-only)[/dim]\n")

    conn = _open_chat_db()

    # List all conversations
    console.print("[dim]Listing conversations...[/dim]")
    conversations = _list_conversations(conn)
    if not conversations:
        console.print("[yellow]No conversations found[/yellow]")
        conn.close()
        return 0

    # Separate 1:1 vs group chats, skip emails/URNs/short codes
    direct_chats = []
    group_chats = []
    for conv in conversations:
        identifier = conv["identifier"]
        if _is_group_identifier(identifier):
            group_chats.append(conv)
        elif _is_phone_identifier(identifier):
            direct_chats.append(conv)

    console.print(f"[dim]Found {len(direct_chats)} direct + {len(group_chats)} group conversations[/dim]")

    # Build phone → name lookup from Contacts
    console.print("[dim]Loading contacts...[/dim]")
    name_lookup = _build_contact_name_lookup()
    console.print()

    now = datetime.now(timezone.utc)
    cutoff_90d = now - timedelta(days=90)
    since_ns = _datetime_to_apple_ns(cutoff_90d)
    contacts_by_phone: dict[str, Contact] = {}

    # Count messages for 1:1 chats
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Counting 1:1 messages", total=len(direct_chats))

        for conv in direct_chats:
            progress.advance(task)
            chat_id = conv["id"]
            identifier = conv["identifier"]
            last_message_at = conv["last_message_at"]
            name = name_lookup.get(_normalize_phone(identifier), "")

            # Only count messages if the chat had activity in the last 90 days
            count = 0
            last_date_ns = conv["last_date_ns"]
            if last_date_ns:
                last_unix = _apple_ns_to_unix(last_date_ns)
                if last_unix and last_unix >= cutoff_90d.timestamp():
                    count = _count_messages_since(conn, chat_id, since_ns)
            else:
                count = _count_messages_since(conn, chat_id, since_ns)

            new_contact = Contact(
                phone=identifier,
                name=name,
                source="imessage",
                message_count=count or None,
                last_message=last_message_at,
            )
            if identifier in contacts_by_phone:
                contacts_by_phone[identifier] = merge_contact(contacts_by_phone[identifier], new_contact)
            else:
                contacts_by_phone[identifier] = new_contact

    # Process group chats
    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Scanning group chats", total=len(group_chats))

        for conv in group_chats:
            progress.advance(task)
            chat_id = conv["id"]

            # Skip groups with no activity in 90 days
            last_date_ns = conv["last_date_ns"]
            if last_date_ns:
                last_unix = _apple_ns_to_unix(last_date_ns)
                if last_unix and last_unix < cutoff_90d.timestamp():
                    continue

            if include_small_groups:
                participants = _get_group_sender_stats(conn, chat_id, since_ns)
                is_small = len(participants) <= SMALL_GROUP_MAX_MEMBERS

                for sender_phone, stats in participants.items():
                    if not _is_phone_identifier(sender_phone):
                        continue
                    name = name_lookup.get(_normalize_phone(sender_phone), "")
                    if sender_phone in contacts_by_phone:
                        existing = contacts_by_phone[sender_phone]
                        existing.is_in_group_chats = True
                        if is_small:
                            existing.message_count = min(
                                (existing.message_count or 0) + stats["count"],
                                MESSAGE_COUNT_CAP,
                            )
                        if stats["last_message"] and (
                            not existing.last_message or stats["last_message"] > existing.last_message
                        ):
                            existing.last_message = stats["last_message"]
                        if name and not existing.name:
                            existing.name = name
                    else:
                        contacts_by_phone[sender_phone] = Contact(
                            phone=sender_phone,
                            name=name,
                            source="imessage",
                            is_in_group_chats=True,
                            message_count=stats["count"] if is_small else None,
                            last_message=stats["last_message"],
                        )
            else:
                # Default: just flag group participants, don't count messages
                phones = _get_group_participants(conn, chat_id)
                for sender_phone in phones:
                    name = name_lookup.get(_normalize_phone(sender_phone), "")
                    if sender_phone in contacts_by_phone:
                        contacts_by_phone[sender_phone].is_in_group_chats = True
                        if name and not contacts_by_phone[sender_phone].name:
                            contacts_by_phone[sender_phone].name = name
                    else:
                        contacts_by_phone[sender_phone] = Contact(
                            phone=sender_phone,
                            name=name,
                            source="imessage",
                            is_in_group_chats=True,
                        )

    conn.close()

    # Merge with existing contacts.csv (preserves WhatsApp data)
    existing = load_existing_contacts(output_path)
    for phone, new_contact in contacts_by_phone.items():
        if phone in existing:
            existing[phone] = merge_contact(existing[phone], new_contact)
        else:
            existing[phone] = new_contact

    total_written = write_contacts(existing, output_path, limit=MAX_CONTACTS_OUTPUT)

    console.print(f"\n[green bold]✅ Extracted top {total_written} contacts (of {len(existing)}) to {output_path}[/green bold]")
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(f"  [cyan]contact-exporter llm-review[/cyan]  Auto-classify contacts worth enriching")
    console.print(f"  [cyan]contact-exporter upload[/cyan]      Upload to Powerset")
    console.print("[dim]  Use --model to change LLM (default: claude-sonnet-4-6)[/dim]")

    return total_written
