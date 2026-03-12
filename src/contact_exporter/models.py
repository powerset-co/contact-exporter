"""Shared data models for contact extraction."""

from __future__ import annotations

from dataclasses import dataclass

CSV_HEADERS = ["phone", "name", "source", "is_in_group_chats", "message_count", "last_message", "skip"]


@dataclass
class Contact:
    """A single extracted contact. One row in contacts.csv."""

    phone: str  # E.164 format: "+14155551234"
    name: str
    source: str  # "imessage" or "whatsapp"
    is_in_group_chats: bool = False
    message_count: int | None = None
    last_message: str | None = None  # ISO 8601 timestamp
    skip: bool = False  # Set to True to exclude from upload

    def to_csv_row(self) -> list[str]:
        """Convert to a list of strings for csv.writer."""
        return [
            self.phone,
            self.name,
            self.source,
            str(self.is_in_group_chats).lower(),
            str(self.message_count) if self.message_count is not None else "",
            self.last_message or "",
            "yes" if self.skip else "",
        ]

    @classmethod
    def from_csv_row(cls, row: dict) -> Contact:
        """Create from a csv.DictReader row."""
        count = row.get("message_count", "").strip()
        return cls(
            phone=row["phone"],
            name=row.get("name", ""),
            source=row.get("source", ""),
            is_in_group_chats=row.get("is_in_group_chats", "").lower() == "true",
            message_count=int(count) if count else None,
            last_message=row.get("last_message", "") or None,
            skip=row.get("skip", "").strip().lower() in ("yes", "true", "1"),
        )
