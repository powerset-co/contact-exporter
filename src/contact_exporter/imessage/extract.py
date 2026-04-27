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
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from contact_exporter.matching import apply_local_name_matching, sync_candidate_catalog
from contact_exporter.merge import load_existing_contacts, merge_contact, write_contacts
from contact_exporter.models import Contact, canonicalize_phone, serialize_group_names

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


def _resolve_group_chat_name(
    chat_identifier: str,
    display_name: str | None,
    room_name: str | None,
) -> str:
    """Return a human-readable group name when chat.db exposes one."""
    for candidate in (display_name, room_name):
        cleaned = re.sub(r"\s+", " ", (candidate or "").strip())
        if not cleaned:
            continue
        if cleaned == chat_identifier:
            continue
        return cleaned
    return ""


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


def _canonical_contact_phone(phone_raw: str) -> str:
    """Canonicalize contact phone for CSV rows.

    Prefer E.164-like output for US/CA numbers so iMessage and WhatsApp rows
    converge more often on the same key.
    """
    return canonicalize_phone(phone_raw)


def _add_contact_entry(contacts: dict[str, str], phone_raw: str, name: str) -> None:
    """Add canonical phone->name entry to contact inventory."""
    canonical = _canonical_contact_phone(phone_raw)
    if not canonical:
        return
    name = (name or "").strip()
    existing = contacts.get(canonical, "")
    if name and (not existing or len(name) > len(existing)):
        contacts[canonical] = name
    elif canonical not in contacts:
        contacts[canonical] = name


def _query_contacts_sqlite() -> tuple[dict[str, str], dict[str, str]]:
    """Read contacts and phone→name lookup from AddressBook SQLite databases."""
    db_paths = glob.glob(_ADDRESSBOOK_GLOB)
    if not db_paths:
        return {}, {}

    contacts: dict[str, str] = {}
    lookup: dict[str, str] = {}
    for db_path in db_paths:
        try:
            conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
            for phone_raw, first, last in conn.execute(_CONTACTS_QUERY):
                name = _clean_contact_name(first or "", last or "")
                _add_contact_entry(contacts, phone_raw, name)
                _add_phone_to_lookup(lookup, phone_raw, name)
            conn.close()
        except sqlite3.Error:
            continue

    return contacts, lookup


def _query_contacts_applescript() -> tuple[dict[str, str], dict[str, str]]:
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
            return {}, {}
    except subprocess.TimeoutExpired:
        return {}, {}

    contacts: dict[str, str] = {}
    lookup: dict[str, str] = {}
    for line in result.stdout.strip().split("\n"):
        if "\t" not in line:
            continue
        phone_raw, name = line.split("\t", 1)
        _add_contact_entry(contacts, phone_raw, name)
        _add_phone_to_lookup(lookup, phone_raw, name)

    return contacts, lookup


def _build_contacts_index() -> tuple[dict[str, str], dict[str, str]]:
    """Build contact inventory + lookup map from macOS Contacts.

    Tries direct SQLite read first (fast), falls back to AppleScript.
    """
    contacts, lookup = _query_contacts_sqlite()
    if contacts:
        console.print(f"[dim]Loaded {len(contacts)} contacts from AddressBook database[/dim]")
        return contacts, lookup

    console.print("[dim]SQLite read failed, falling back to AppleScript...[/dim]")
    contacts, lookup = _query_contacts_applescript()
    if contacts:
        return contacts, lookup

    console.print("[yellow]Warning: could not load local Contacts.app data[/yellow]")
    console.print("[dim]Ensure Full Disk Access is granted to your terminal app[/dim]")
    return contacts, lookup


def _build_contact_name_lookup() -> dict[str, str]:
    """Backward-compatible helper used by tests and legacy call sites."""
    _, lookup = _build_contacts_index()
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


def _aggregate_handle_message_stats() -> dict[str, dict]:
    """Aggregate full-history message counts and recency by handle in one query."""
    conn = _open_chat_db()
    try:
        rows = conn.execute("""
            SELECT
                h.id AS identifier,
                COUNT(*) AS msg_count,
                MAX(m.date) AS last_date_ns
            FROM message m
            JOIN handle h ON h.ROWID = m.handle_id
            WHERE h.id IS NOT NULL
              AND h.id <> ''
              AND (m.associated_message_type IS NULL
                   OR m.associated_message_type < 2000
                   OR m.associated_message_type > 3006)
            GROUP BY h.id
        """).fetchall()
    finally:
        conn.close()

    stats: dict[str, dict] = {}
    for row in rows:
        identifier = row["identifier"] or ""
        if not _is_phone_identifier(identifier):
            continue
        stats[identifier] = {
            "count": int(row["msg_count"] or 0),
            "last_message": _apple_ns_to_iso(row["last_date_ns"]),
        }
    return stats


def _list_group_participant_metadata() -> tuple[set[str], dict[str, set[str]]]:
    """Return group participants plus any named iMessage groups they appear in."""
    conn = _open_chat_db()
    try:
        rows = conn.execute("""
            SELECT
                h.id AS identifier,
                c.chat_identifier,
                c.display_name,
                c.room_name
            FROM chat c
            JOIN chat_handle_join chj ON chj.chat_id = c.ROWID
            JOIN handle h ON h.ROWID = chj.handle_id
            WHERE c.chat_identifier LIKE 'chat%'
        """).fetchall()
    finally:
        conn.close()

    participants: set[str] = set()
    group_names_by_identifier: dict[str, set[str]] = {}
    for row in rows:
        identifier = row["identifier"] or ""
        if _is_phone_identifier(identifier):
            participants.add(identifier)
            group_name = _resolve_group_chat_name(
                row["chat_identifier"] or "",
                row["display_name"],
                row["room_name"],
            )
            if group_name:
                group_names_by_identifier.setdefault(identifier, set()).add(group_name)
    return participants, group_names_by_identifier


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------

def extract_imessage(
    output_path: str = "contacts.csv",
    include_small_groups: bool = False,
    operator_id: str | None = None,
) -> int:
    """Extract iMessage contacts via direct SQLite reads of chat.db.

    Starts from local Contacts.app phone records, then enriches each contact
    with message counts and recency when iMessage history exists.

    Returns the number of contacts written.
    """
    _check_permissions()

    console.print("[bold]Extracting iMessage contacts...[/bold]")
    console.print(f"[dim]Reading {_CHAT_DB} (read-only)[/dim]\n")

    if include_small_groups:
        console.print("[dim]Note: --include-small-groups is now a no-op; full-history counts are always used.[/dim]")

    console.print("[dim]Loading contacts + full message history...[/dim]")
    with ThreadPoolExecutor(max_workers=3) as pool:
        fut_contacts = pool.submit(_build_contacts_index)
        fut_stats = pool.submit(_aggregate_handle_message_stats)
        fut_groups = pool.submit(_list_group_participant_metadata)

        contacts_inventory, _ = fut_contacts.result()
        stats_by_identifier = fut_stats.result()
        group_participants, group_names_by_identifier = fut_groups.result()

    if not contacts_inventory:
        console.print("[yellow]No local Contacts found[/yellow]")
        return 0

    # Normalize message stats to phone digits so different identifier formats
    # collapse to one contact.
    stats_by_normalized: dict[str, dict] = {}
    for identifier, stat in stats_by_identifier.items():
        normalized = _normalize_phone(identifier)
        if len(normalized) < 7:
            continue
        current = stats_by_normalized.get(normalized)
        count = int(stat.get("count", 0) or 0)
        last_message = stat.get("last_message")
        if not current:
            stats_by_normalized[normalized] = {"count": count, "last_message": last_message}
            continue
        current["count"] = int(current.get("count", 0) or 0) + count
        if last_message and (
            not current.get("last_message") or last_message > current["last_message"]
        ):
            current["last_message"] = last_message

    group_normalized = {
        _normalize_phone(identifier)
        for identifier in group_participants
        if len(_normalize_phone(identifier)) >= 7
    }
    group_names_by_normalized: dict[str, set[str]] = {}
    for identifier, group_names in group_names_by_identifier.items():
        normalized = _normalize_phone(identifier)
        if len(normalized) < 7:
            continue
        group_names_by_normalized.setdefault(normalized, set()).update(group_names)

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Building contact rows", total=len(contacts_inventory))
        contacts_by_phone: dict[str, Contact] = {}
        for phone, name in contacts_inventory.items():
            progress.advance(task)
            normalized = _normalize_phone(phone)
            stat = stats_by_normalized.get(normalized, {})
            msg_count = int(stat.get("count", 0) or 0)
            contacts_by_phone[phone] = Contact(
                phone=phone,
                name=name,
                source="imessage",
                is_in_group_chats=normalized in group_normalized,
                group_names=serialize_group_names(group_names_by_normalized.get(normalized, set())),
                message_count=msg_count or None,
                last_message=stat.get("last_message"),
            )

    with_messages = sum(1 for c in contacts_by_phone.values() if c.message_count)
    in_groups = sum(1 for c in contacts_by_phone.values() if c.is_in_group_chats)
    console.print(
        f"[dim]Found {len(contacts_by_phone)} local contacts "
        f"({with_messages} with iMessage history, "
        f"{in_groups} in groups)[/dim]"
    )

    # Merge with existing contacts.csv (preserves WhatsApp data)
    existing = load_existing_contacts(output_path)
    for phone, new_contact in contacts_by_phone.items():
        canonical_phone = canonicalize_phone(phone)
        if not canonical_phone:
            continue
        new_contact.phone = canonical_phone
        if canonical_phone in existing:
            existing[canonical_phone] = merge_contact(existing[canonical_phone], new_contact)
        else:
            existing[canonical_phone] = new_contact

    candidates = sync_candidate_catalog(operator_id=operator_id)
    match_stats = apply_local_name_matching(existing, candidates)

    total_written = write_contacts(existing, output_path)

    console.print(f"\n[green bold]✅ Extracted {total_written} contacts to {output_path}[/green bold]")
    console.print(
        f"[dim]Local match results: matched={match_stats['matched']} "
        f"suggested={match_stats['suggested']} unmatched={match_stats['unmatched']}[/dim]"
    )
    console.print()
    console.print("[bold]Next steps:[/bold]")
    console.print(f"  [cyan]contact-exporter llm-review[/cyan]  Analyze unmatched contacts")
    console.print(f"  [cyan]contact-exporter upload[/cyan]      Upload to Powerset")
    console.print("[dim]  Use --model to change LLM (default: claude-sonnet-4-6)[/dim]")

    return total_written
