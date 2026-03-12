"""iMessage contact extraction via the `imsg` CLI.

Wraps steipete/imsg (https://github.com/steipete/imsg) to read
~/Library/Messages/chat.db and extract contact metadata.

Only counts messages -- never reads or exports message content.
"""

from __future__ import annotations

import json
import re
import shutil
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

# AppleScript to bulk-export phone -> name from Contacts.app.
# We launch the app first because AppleScript can't query it unless it's running.
_CONTACTS_APPLESCRIPT = '''
tell application "Contacts"
    set output to ""
    repeat with p in every person
        set phoneList to value of phones of p
        if (count of phoneList) > 0 then
            set fn to first name of p
            set ln to last name of p
            if fn is missing value then set fn to ""
            if ln is missing value then set ln to ""
            set theName to fn & " " & ln
            repeat with ph in phoneList
                set output to output & ph & (ASCII character 9) & theName & linefeed
            end repeat
        end if
    end repeat
    return output
end tell
'''


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


def _query_contacts_app() -> dict[str, str]:
    """Run the AppleScript query against Contacts.app and parse the results."""
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
        name = name.strip()
        if not name or "missing value" in name:
            continue
        digits = re.sub(r"[^\d]", "", phone_raw)
        if len(digits) < 7:
            continue
        # Store under normalized 10-digit key for US numbers
        normalized = _normalize_phone(phone_raw)
        lookup[normalized] = name
        # Also store full digits for international numbers
        if digits != normalized:
            lookup[digits] = name

    return lookup


def _restart_contacts_app():
    """Quit and relaunch Contacts.app to clear stale state."""
    console.print("[dim]Restarting Contacts.app...[/dim]")
    subprocess.run(
        ["osascript", "-e", 'tell application "Contacts" to quit'],
        capture_output=True, text=True, timeout=10,
    )
    time.sleep(3)
    subprocess.run(
        ["osascript", "-e", 'tell application "Contacts" to launch'],
        capture_output=True, text=True, timeout=10,
    )
    time.sleep(3)


def _build_contact_name_lookup() -> dict[str, str]:
    """Build phone -> name mapping from macOS Contacts.app via AppleScript.

    If the first attempt returns 0 contacts (stale app state), automatically
    restarts Contacts.app and retries once.
    """
    lookup = _query_contacts_app()
    if lookup:
        return lookup

    console.print("[yellow]Warning: Contacts.app returned 0 contacts -- app may be in a bad state[/yellow]")
    _restart_contacts_app()
    console.print("[dim]Retrying contact lookup...[/dim]")
    lookup = _query_contacts_app()

    if not lookup:
        console.print("[yellow]Still got 0 contacts after restart. Names will be missing from export.[/yellow]")
        console.print("[dim]Try closing Contacts.app manually, reopening it, and running again.[/dim]")

    return lookup


def _check_imsg_installed() -> str:
    """Check that the imsg CLI is on PATH. Returns path or exits."""
    path = shutil.which("imsg")
    if not path:
        console.print("[red bold]imsg CLI not found[/red bold]")
        console.print()
        console.print("Install imsg to extract iMessage contacts:")
        console.print("  [cyan]brew install steipete/tap/imsg[/cyan]")
        console.print("  or build from source: [dim]https://github.com/steipete/imsg[/dim]")
        raise SystemExit(1)
    return path


def _check_full_disk_access() -> bool:
    """Check if we can read chat.db (requires Full Disk Access)."""
    try:
        result = subprocess.run(
            ["imsg", "chats", "--limit", "1", "--json"],
            capture_output=True, text=True, timeout=10,
        )
        error_output = (result.stderr.strip() or result.stdout.strip()).lower()
        if result.returncode != 0 and (
            "permissiondenied" in error_output.replace(" ", "")
            or "authorization denied" in error_output
        ):
            return False
        return result.returncode == 0
    except subprocess.TimeoutExpired:
        return True  # Slow but not a permission issue


def _check_contacts_access() -> bool:
    """Check if we have Contacts.app access via a minimal AppleScript query."""
    try:
        result = subprocess.run(
            ["osascript", "-e",
             'tell application "Contacts" to launch\n'
             'delay 1\n'
             'tell application "Contacts" to count every person'],
            capture_output=True, text=True, timeout=15,
        )
        if result.returncode != 0:
            stderr = result.stderr.lower()
            if "not authorized" in stderr or "denied" in stderr:
                return False
            # Other errors (app slow to start) -- assume OK, will fail later with a clear message
            return True
        return result.stdout.strip().isdigit()
    except subprocess.TimeoutExpired:
        return True  # Don't block on timeout


def _check_permissions():
    """Verify Full Disk Access and Contacts permissions. Opens System Settings if missing."""
    console.print("[dim]Checking permissions...[/dim]")

    has_fda = _check_full_disk_access()
    has_contacts = _check_contacts_access()

    if has_fda and has_contacts:
        return

    if not has_fda:
        console.print("[red]❌ Full Disk Access — required to read iMessage history[/red]")
    if not has_contacts:
        console.print("[red]❌ Contacts — required to resolve contact names[/red]")

    console.print()
    console.print("[bold]Opening System Settings for you...[/bold]")
    console.print("Enable your terminal app, then restart your terminal and retry.\n")

    if not has_fda:
        webbrowser.open("x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles")
    if not has_contacts:
        webbrowser.open("x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts")

    raise SystemExit(1)


def _run_imsg(args: list[str], timeout: int = 60) -> subprocess.CompletedProcess:
    return subprocess.run(["imsg"] + args, capture_output=True, text=True, timeout=timeout)


def _count_jsonl_lines(raw: str) -> int:
    if not raw.strip():
        return 0
    return sum(1 for line in raw.strip().split("\n") if line.strip())


def _count_messages(chat_id: int, start_iso: str) -> int:
    """Count messages in a chat since start_iso, capped at MESSAGE_COUNT_CAP."""
    args = [
        "history", "--chat-id", str(chat_id),
        "--start", start_iso,
        "--limit", str(MESSAGE_COUNT_CAP + 1),
        "--json",
    ]
    try:
        result = _run_imsg(args, timeout=15)
        if result.returncode != 0:
            return 0
        return min(_count_jsonl_lines(result.stdout), MESSAGE_COUNT_CAP)
    except subprocess.TimeoutExpired:
        return 0


def _parse_messages(stdout: str):
    """Yield parsed JSON objects from JSONL output, skipping malformed lines."""
    for line in stdout.strip().split("\n"):
        line = line.strip()
        if not line:
            continue
        try:
            yield json.loads(line)
        except json.JSONDecodeError:
            continue


def _extract_group_participants(chat_id: int, start_iso: str) -> dict[str, dict]:
    """Extract per-sender stats from a group chat.

    Returns {sender_phone: {"count": int, "last_message": str | None}}.
    """
    args = [
        "history", "--chat-id", str(chat_id),
        "--start", start_iso,
        # Fetch more messages for groups since they have multiple senders
        "--limit", str(MESSAGE_COUNT_CAP * 5),
        "--json",
    ]
    try:
        result = _run_imsg(args, timeout=30)
        if result.returncode != 0:
            return {}
    except subprocess.TimeoutExpired:
        return {}

    participants: dict[str, dict] = {}
    for msg in _parse_messages(result.stdout):
        if msg.get("is_from_me"):
            continue

        sender = msg.get("sender", "")
        if not sender or "@" in sender or sender.startswith("urn:"):
            continue

        if sender not in participants:
            participants[sender] = {"count": 0, "last_message": None}

        p = participants[sender]
        p["count"] = min(p["count"] + 1, MESSAGE_COUNT_CAP)

        date_str = msg.get("created_at") or ""
        if date_str and (p["last_message"] is None or date_str > p["last_message"]):
            p["last_message"] = date_str

    return participants


def _get_group_participant_phones(chat_id: int, start_iso: str) -> list[str]:
    """Get unique participant phone numbers from a group chat (no counting)."""
    args = [
        "history", "--chat-id", str(chat_id),
        "--start", start_iso, "--limit", "500", "--json",
    ]
    try:
        result = _run_imsg(args, timeout=20)
        if result.returncode != 0:
            return []
    except subprocess.TimeoutExpired:
        return []

    phones = set()
    for msg in _parse_messages(result.stdout):
        if msg.get("is_from_me"):
            continue
        sender = msg.get("sender", "")
        if sender and "@" not in sender and not sender.startswith("urn:"):
            phones.add(sender)
    return list(phones)


def extract_imessage(output_path: str = "contacts.csv", include_small_groups: bool = False) -> int:
    """Extract iMessage contacts and write to JSONL.

    Reads all conversations from chat.db, counts messages for 1:1 chats,
    flags group chat participants, and outputs the top contacts sorted by
    message count.

    Returns the number of contacts written.
    """
    _check_imsg_installed()
    _check_permissions()

    console.print("[bold]Extracting iMessage contacts...[/bold]")
    console.print("[dim]Reading ~/Library/Messages/chat.db (read-only)[/dim]\n")

    # List all conversations
    console.print("[dim]Listing conversations...[/dim]")
    try:
        result = _run_imsg(["chats", "--json", "--limit", "10000"], timeout=60)
    except subprocess.TimeoutExpired:
        console.print("[red]Timed out listing conversations[/red]")
        raise SystemExit(1)

    if result.returncode != 0:
        console.print(f"[red]imsg error: {result.stderr.strip() or result.stdout.strip()}[/red]")
        raise SystemExit(1)

    conversations = list(_parse_messages(result.stdout))
    if not conversations:
        console.print("[yellow]No conversations found[/yellow]")
        return 0

    # Separate 1:1 vs group chats, skip email-based iMessage identifiers
    direct_chats = []
    group_chats = []
    for conv in conversations:
        identifier = conv.get("identifier") or ""
        # Skip non-phone identifiers: emails, Apple Business Chat URNs, short codes
        if not identifier or "@" in identifier or identifier.startswith("urn:"):
            continue
        elif identifier.startswith("chat"):
            group_chats.append(conv)
        else:
            digits_only = re.sub(r"[^\d]", "", identifier)
            if len(digits_only) < 7:
                continue
            direct_chats.append(conv)

    console.print(f"[dim]Found {len(direct_chats)} direct + {len(group_chats)} group conversations[/dim]")

    # Build phone -> name lookup from Contacts.app
    console.print("[dim]Resolving contact names...[/dim]")
    name_lookup = _build_contact_name_lookup()
    console.print(f"[dim]Loaded {len(name_lookup)} contacts from address book[/dim]\n")

    now = datetime.now(timezone.utc)
    start_90 = (now - timedelta(days=90)).strftime("%Y-%m-%dT00:00:00Z")
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
            chat_id = conv.get("id")
            if chat_id is None:
                continue

            identifier = conv.get("identifier") or ""
            last_message_at = conv.get("last_message_at")
            name = name_lookup.get(_normalize_phone(identifier), "")

            # Only count messages if the chat had activity in the last 90 days
            count = 0
            if last_message_at:
                try:
                    last_dt = datetime.fromisoformat(last_message_at.replace("Z", "+00:00"))
                    if last_dt >= now - timedelta(days=90):
                        count = _count_messages(chat_id, start_90)
                except (ValueError, TypeError):
                    count = _count_messages(chat_id, start_90)
            else:
                count = _count_messages(chat_id, start_90)

            # Same phone can have multiple chats (iMessage, SMS, iMessageLite) — merge them
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
            chat_id = conv.get("id")
            if chat_id is None:
                continue

            # Skip groups with no activity in 90 days
            last_message_at = conv.get("last_message_at")
            if last_message_at:
                try:
                    last_dt = datetime.fromisoformat(last_message_at.replace("Z", "+00:00"))
                    if last_dt < now - timedelta(days=90):
                        continue
                except (ValueError, TypeError):
                    pass

            if include_small_groups:
                # --include-small-groups: count per-sender messages in groups <= 7 people
                participants = _extract_group_participants(chat_id, start_90)
                is_small = len(participants) <= SMALL_GROUP_MAX_MEMBERS

                for sender_phone, stats in participants.items():
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
                phones = _get_group_participant_phones(chat_id, start_90)
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

    # Merge with existing contacts.csv (preserves WhatsApp data)
    existing = load_existing_contacts(output_path)
    for phone, new_contact in contacts_by_phone.items():
        if phone in existing:
            existing[phone] = merge_contact(existing[phone], new_contact)
        else:
            existing[phone] = new_contact

    total_written = write_contacts(existing, output_path, limit=MAX_CONTACTS_OUTPUT)

    console.print(f"\n[green bold]✅ Extracted top {total_written} contacts (of {len(existing)}) to {output_path}[/green bold]")
    console.print(f"[dim]Review the file before uploading: cat {output_path}[/dim]")

    return total_written
