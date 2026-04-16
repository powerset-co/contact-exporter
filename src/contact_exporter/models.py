"""Shared data models for contact extraction."""

from __future__ import annotations

from dataclasses import dataclass

CSV_HEADERS = [
    "phone",
    "name",
    "source",
    "is_in_group_chats",
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
