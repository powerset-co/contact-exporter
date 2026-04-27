"""Shared data models for contact extraction."""

from __future__ import annotations

from dataclasses import dataclass
import re
import unicodedata
from typing import Iterable

CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
    "group_names",
    "message_count",
    "last_message",
    "skip",
    "match_status",
    "matched_person_id",
    "matched_name",
    "matched_linkedin_url",
    "match_confidence",
    "match_method",
    "match_reason",
]

_GROUP_NAME_SEPARATOR = " | "


def canonicalize_phone(raw: str) -> str:
    """Canonicalize a phone-ish identifier for stable CSV merge keys."""
    value = (raw or "").strip()
    digits = re.sub(r"[^\d]", "", value)
    if len(digits) < 7:
        return ""
    if value.startswith("+"):
        return f"+{digits}"
    if len(digits) == 10:
        return f"+1{digits}"
    if len(digits) == 11 and digits.startswith("1"):
        return f"+{digits}"
    if len(digits) <= 15:
        return f"+{digits}"
    return digits


def is_emoji_only_name(name: str) -> bool:
    """True when a name contains only emoji/symbol glyphs and separators."""
    raw = (name or "").strip()
    if not raw:
        return False
    compact = "".join(ch for ch in raw if not ch.isspace())
    if not compact:
        return False

    has_symbol = False
    for ch in compact:
        if ch in ("\u200d", "\ufe0f"):
            continue
        if ch.isalnum():
            return False
        cat = unicodedata.category(ch)
        if cat.startswith(("L", "N")):
            return False
        if cat.startswith("P"):
            continue
        if cat.startswith("S"):
            has_symbol = True
            continue
        return False
    return has_symbol


def should_auto_skip(contact: "Contact") -> bool:
    """Default non-controversial skip rules applied before upload/write."""
    name = (contact.name or "").strip()
    msg_count = int(contact.message_count or 0)
    if not name and msg_count == 0:
        return True
    if name and is_emoji_only_name(name):
        return True
    return False


def serialize_group_names(names: Iterable[str]) -> str | None:
    """Normalize, dedupe, and serialize group names for CSV storage."""
    cleaned = {
        re.sub(r"\s+", " ", (name or "").strip())
        for name in names
        if (name or "").strip()
    }
    if not cleaned:
        return None
    return _GROUP_NAME_SEPARATOR.join(sorted(cleaned, key=str.casefold))


def parse_group_names(raw: str | None) -> list[str]:
    """Parse serialized group names from CSV."""
    if not raw:
        return []
    return [
        part.strip()
        for part in raw.split(_GROUP_NAME_SEPARATOR)
        if part.strip()
    ]


def merge_group_names(*values: str | None) -> str | None:
    """Merge multiple serialized group-name values into one canonical string."""
    names: list[str] = []
    for value in values:
        names.extend(parse_group_names(value))
    return serialize_group_names(names)


@dataclass
class Contact:
    """A single extracted contact. One row in contacts.csv."""

    phone: str  # E.164 format: "+14155551234"
    name: str
    source: str  # "imessage" or "whatsapp"
    is_in_group_chats: bool = False
    group_names: str | None = None  # Named group chats containing this contact
    message_count: int | None = None
    last_message: str | None = None  # ISO 8601 timestamp
    skip: bool = False  # Set to True to exclude from upload
    match_status: str | None = None  # matched | suggested | unmatched
    matched_person_id: str | None = None
    matched_name: str | None = None
    matched_linkedin_url: str | None = None
    match_confidence: float | None = None
    match_method: str | None = None
    match_reason: str | None = None

    def to_csv_row(self) -> list[str]:
        """Convert to a list of strings for csv.writer."""
        return [
            self.phone,
            self.name,
            self.source,
            str(self.is_in_group_chats).lower(),
            self.group_names or "",
            str(self.message_count) if self.message_count is not None else "",
            self.last_message or "",
            "yes" if self.skip else "",
            self.match_status or "",
            self.matched_person_id or "",
            self.matched_name or "",
            self.matched_linkedin_url or "",
            f"{self.match_confidence:.3f}" if self.match_confidence is not None else "",
            self.match_method or "",
            self.match_reason or "",
        ]

    @classmethod
    def from_csv_row(cls, row: dict) -> Contact:
        """Create from a csv.DictReader row."""
        count = row.get("message_count", "").strip()
        confidence_raw = (row.get("match_confidence", "") or "").strip()
        try:
            confidence = float(confidence_raw) if confidence_raw else None
        except ValueError:
            confidence = None
        return cls(
            phone=row["phone"],
            name=row.get("name", ""),
            source=row.get("source", ""),
            is_in_group_chats=row.get("is_in_group_chats", "").lower() == "true",
            group_names=row.get("group_names", "") or None,
            message_count=int(count) if count else None,
            last_message=row.get("last_message", "") or None,
            skip=row.get("skip", "").strip().lower() in ("yes", "true", "1"),
            match_status=row.get("match_status", "") or None,
            matched_person_id=row.get("matched_person_id", "") or None,
            matched_name=row.get("matched_name", "") or None,
            matched_linkedin_url=row.get("matched_linkedin_url", "") or None,
            match_confidence=confidence,
            match_method=row.get("match_method", "") or None,
            match_reason=row.get("match_reason", "") or None,
        )
