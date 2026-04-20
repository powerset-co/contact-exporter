"""Merge contacts from multiple sources into a deduplicated CSV file."""

from __future__ import annotations

import csv
from pathlib import Path

from contact_exporter.models import (
    CSV_HEADERS,
    Contact,
    canonicalize_phone,
    is_emoji_only_name,
    should_auto_skip,
)


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

            canonical_phone = canonicalize_phone(contact.phone)
            if not canonical_phone:
                continue
            contact.phone = canonical_phone

            if canonical_phone in contacts:
                contacts[canonical_phone] = merge_contact(contacts[canonical_phone], contact)
            else:
                contacts[canonical_phone] = contact

    return contacts


def _prefer_name(existing_name: str, new_name: str) -> str:
    """Prefer a stable human-readable name when merging duplicate phones."""
    existing = (existing_name or "").strip()
    new = (new_name or "").strip()
    if not existing:
        return new
    if not new:
        return existing
    if is_emoji_only_name(existing) and not is_emoji_only_name(new):
        return new
    return existing


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

    # Preserve richer local match metadata if available.
    match_status = existing.match_status or new.match_status
    matched_person_id = existing.matched_person_id or new.matched_person_id
    matched_name = existing.matched_name or new.matched_name
    matched_linkedin_url = existing.matched_linkedin_url or new.matched_linkedin_url
    match_confidence = existing.match_confidence
    if new.match_confidence is not None and (
        match_confidence is None or new.match_confidence > match_confidence
    ):
        match_confidence = new.match_confidence
    match_method = existing.match_method or new.match_method
    match_reason = existing.match_reason or new.match_reason

    return Contact(
        phone=existing.phone,
        name=_prefer_name(existing.name, new.name),
        source=source,
        is_in_group_chats=existing.is_in_group_chats or new.is_in_group_chats,
        message_count=message_count,
        last_message=last_message,
        skip=existing.skip,  # Preserve skip from previous run
        match_status=match_status,
        matched_person_id=matched_person_id,
        matched_name=matched_name,
        matched_linkedin_url=matched_linkedin_url,
        match_confidence=match_confidence,
        match_method=match_method,
        match_reason=match_reason,
    )


def write_contacts(
    contacts: dict[str, Contact],
    path: str,
    limit: int | None = None,
) -> int:
    """Write contacts to CSV, sorted by message count desc."""
    # Normalize keys up front so old rows like "1415..." and "+1415..." merge.
    deduped: dict[str, Contact] = {}
    for contact in contacts.values():
        canonical_phone = canonicalize_phone(contact.phone)
        if not canonical_phone:
            continue
        contact.phone = canonical_phone
        if canonical_phone in deduped:
            deduped[canonical_phone] = merge_contact(deduped[canonical_phone], contact)
        else:
            deduped[canonical_phone] = contact

    # Apply deterministic auto-skip rules (preserves explicit manual skips).
    for contact in deduped.values():
        if should_auto_skip(contact):
            contact.skip = True

    sorted_contacts = sorted(
        deduped.values(),
        key=lambda c: (c.message_count or 0),
        reverse=True,
    )
    if limit is not None:
        sorted_contacts = sorted_contacts[:limit]

    with Path(path).open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(CSV_HEADERS)
        for contact in sorted_contacts:
            writer.writerow(contact.to_csv_row())

    return len(sorted_contacts)
