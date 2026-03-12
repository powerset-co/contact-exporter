"""Merge contacts from multiple sources into a deduplicated CSV file."""

from __future__ import annotations

import csv
from pathlib import Path

from contact_exporter.config import MAX_CONTACTS_OUTPUT
from contact_exporter.models import CSV_HEADERS, Contact


def load_existing_contacts(path: str) -> dict[str, Contact]:
    """Load contacts.csv into a phone -> Contact dict, merging duplicates.

    Preserves the skip column from previous runs so users don't lose
    their manual exclusions when re-running extraction.
    """
    output = Path(path)
    if not output.exists():
        return {}

    contacts: dict[str, Contact] = {}
    with output.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                contact = Contact.from_csv_row(row)
            except (KeyError, ValueError):
                continue

            if contact.phone in contacts:
                contacts[contact.phone] = merge_contact(contacts[contact.phone], contact)
            else:
                contacts[contact.phone] = contact

    return contacts


def merge_contact(existing: Contact, new: Contact) -> Contact:
    """Merge two contacts for the same phone number. Richer/newer data wins."""
    last_message = existing.last_message
    if new.last_message:
        if not last_message or new.last_message > last_message:
            last_message = new.last_message

    # `max(...) or None` converts 0 back to None so zero-message contacts
    # don't get a misleading message_count=0
    message_count = max(existing.message_count or 0, new.message_count or 0) or None

    # Source: combine both sources (e.g. "imessage,whatsapp")
    existing_sources = {s for s in existing.source.split(",") if s}
    new_sources = {s for s in new.source.split(",") if s}
    combined = sorted(existing_sources | new_sources)
    source = ",".join(combined) or new.source

    return Contact(
        phone=existing.phone,
        name=existing.name or new.name,
        source=source,
        is_in_group_chats=existing.is_in_group_chats or new.is_in_group_chats,
        message_count=message_count,
        last_message=last_message,
        skip=existing.skip,  # Preserve skip from previous run
    )


def write_contacts(
    contacts: dict[str, Contact],
    path: str,
    limit: int = MAX_CONTACTS_OUTPUT,
) -> int:
    """Write contacts to CSV, sorted by message count desc, capped at limit."""
    sorted_contacts = sorted(
        contacts.values(),
        key=lambda c: (c.message_count or 0),
        reverse=True,
    )[:limit]

    with Path(path).open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for contact in sorted_contacts:
            writer.writerow(contact.to_csv_row())

    return len(sorted_contacts)
