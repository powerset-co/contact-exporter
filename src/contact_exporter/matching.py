"""Local matching against operator contacts from Powerset backend.

Downloads the caller's contact catalog from /v2/contacts, stores it in
powerset_contacts.csv, and applies backend-equivalent name matching logic
locally to contacts.csv rows.
"""

from __future__ import annotations

import csv
import re
from dataclasses import dataclass
from difflib import SequenceMatcher
from pathlib import Path
from typing import Dict, List, Optional

import requests
from rich.console import Console

from contact_exporter.auth.credentials import get_auth_header
from contact_exporter.config import get_api_base_url
from contact_exporter.models import Contact

console = Console()

_CATALOG_HEADERS = [
    "id",
    "name",
    "linkedin_url",
    "phone_number",
    "emails",
    "public_identifier",
]

_CONTACTS_INCLUDE_FIELDS = ",".join(
    [
        "id",
        "display_name",
        "first_name",
        "last_name",
        "confirmed_linkedin_url",
        "public_profile_url",
        "phone_number",
        "emails",
        "public_identifier",
    ]
)


@dataclass
class PowersetCandidate:
    id: str
    name: str
    linkedin_url: Optional[str] = None
    phone_number: Optional[str] = None
    public_identifier: Optional[str] = None
    emails: Optional[List[str]] = None

    @property
    def normalized_name(self) -> str:
        return _normalize_name(self.name)


def _normalize_name(raw: Optional[str]) -> str:
    s = re.sub(r"[^a-z0-9 ]+", " ", (raw or "").strip().lower())
    return re.sub(r"\s+", " ", s).strip()


def _normalize_phone(raw: Optional[str]) -> str:
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits


def _candidate_name(row: dict) -> str:
    display = (row.get("display_name") or "").strip()
    if display:
        return display
    first = (row.get("first_name") or "").strip()
    last = (row.get("last_name") or "").strip()
    full = " ".join(part for part in [first, last] if part).strip()
    if full:
        return full
    return (row.get("public_identifier") or "").strip()


def fetch_operator_candidates(
    *,
    page_size: int = 200,
) -> List[PowersetCandidate]:
    """Download all candidate contacts visible to the authenticated operator."""
    headers = get_auth_header()
    api_base_url = get_api_base_url()

    out: Dict[str, PowersetCandidate] = {}
    page = 0
    total_count = None

    while True:
        resp = requests.get(
            f"{api_base_url}/v2/contacts",
            headers=headers,
            params={
                "page": page,
                "page_size": page_size,
                "sort_field": "first_name",
                "sort_dir": "asc",
                "include_fields": _CONTACTS_INCLUDE_FIELDS,
            },
            timeout=60,
        )

        if resp.status_code == 401:
            raise SystemExit("Authentication expired. Run: contact-exporter login")
        if resp.status_code != 200:
            raise RuntimeError(f"Failed to fetch contacts ({resp.status_code}): {resp.text[:200]}")

        payload = resp.json()
        rows = payload.get("data") or []
        total_count = payload.get("total_count", total_count)

        for row in rows:
            cid = str(row.get("id") or "").strip()
            if not cid:
                continue
            name = _candidate_name(row)
            if not name:
                continue

            incoming = PowersetCandidate(
                id=cid,
                name=name,
                linkedin_url=(row.get("confirmed_linkedin_url") or row.get("public_profile_url") or "").strip() or None,
                phone_number=(row.get("phone_number") or "").strip() or None,
                public_identifier=(row.get("public_identifier") or "").strip() or None,
                emails=[e for e in (row.get("emails") or []) if e],
            )
            existing = out.get(cid)
            if not existing:
                out[cid] = incoming
            else:
                # Keep richest row when duplicates appear across pages.
                if len(incoming.name) > len(existing.name):
                    existing.name = incoming.name
                existing.linkedin_url = existing.linkedin_url or incoming.linkedin_url
                existing.phone_number = existing.phone_number or incoming.phone_number
                existing.public_identifier = existing.public_identifier or incoming.public_identifier
                existing.emails = existing.emails or incoming.emails

        if not rows:
            break
        seen = (page + 1) * page_size
        if total_count is not None and seen >= int(total_count):
            break
        page += 1

    return sorted(out.values(), key=lambda c: c.id)


def save_candidates_csv(candidates: List[PowersetCandidate], path: str) -> None:
    p = Path(path)
    with p.open("w", newline="") as f:
        writer = csv.writer(f)
        writer.writerow(_CATALOG_HEADERS)
        for c in candidates:
            writer.writerow(
                [
                    c.id,
                    c.name,
                    c.linkedin_url or "",
                    c.phone_number or "",
                    ";".join(c.emails or []),
                    c.public_identifier or "",
                ]
            )


def load_candidates_csv(path: str) -> List[PowersetCandidate]:
    p = Path(path)
    if not p.exists():
        return []
    out: List[PowersetCandidate] = []
    with p.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            cid = (row.get("id") or "").strip()
            name = (row.get("name") or "").strip()
            if not cid or not name:
                continue
            emails = [e for e in (row.get("emails") or "").split(";") if e]
            out.append(
                PowersetCandidate(
                    id=cid,
                    name=name,
                    linkedin_url=(row.get("linkedin_url") or "").strip() or None,
                    phone_number=(row.get("phone_number") or "").strip() or None,
                    public_identifier=(row.get("public_identifier") or "").strip() or None,
                    emails=emails,
                )
            )
    return out


def sync_candidate_catalog(
    *,
    catalog_path: str = "powerset_contacts.csv",
    refresh: bool = True,
) -> List[PowersetCandidate]:
    """Refresh candidate catalog from API.

    Fails closed for HTTP/app-level failures (server reachable but bad response),
    and only uses cached fallback for auth expiry or network reachability issues.
    """
    cache_path = Path(catalog_path)
    if not refresh:
        return load_candidates_csv(catalog_path)

    try:
        candidates = fetch_operator_candidates()
        save_candidates_csv(candidates, catalog_path)
        console.print(f"[dim]Downloaded {len(candidates)} operator candidates → {cache_path}[/dim]")
        return candidates
    except SystemExit:
        if cache_path.exists():
            cached = load_candidates_csv(catalog_path)
            console.print(f"[yellow]Auth missing/expired. Using cached {cache_path} ({len(cached)} rows).[/yellow]")
            return cached
        console.print("[yellow]Not logged in. Run: contact-exporter login[/yellow]")
        return []
    except requests.RequestException as exc:
        if cache_path.exists():
            cached = load_candidates_csv(catalog_path)
            console.print(
                f"[yellow]Candidate download failed ({exc}). Using cached {cache_path} ({len(cached)} rows).[/yellow]"
            )
            return cached
        raise SystemExit(f"Candidate download failed (network): {exc}") from exc
    except Exception as exc:
        # Server was reachable but returned an invalid/unexpected response.
        # Fail fast so route/path/config errors don't silently degrade matching quality.
        raise SystemExit(f"Candidate download failed (server response): {exc}") from exc


def _set_unmatched(contact: Contact, reason: str) -> None:
    contact.match_status = "unmatched"
    contact.matched_person_id = None
    contact.matched_name = None
    contact.matched_linkedin_url = None
    contact.match_confidence = None
    contact.match_method = "unmatched"
    contact.match_reason = reason


def _set_match(
    contact: Contact,
    *,
    status: str,
    candidate: PowersetCandidate,
    confidence: float,
    method: str,
    reason: str,
) -> None:
    contact.match_status = status
    contact.matched_person_id = candidate.id
    contact.matched_name = candidate.name
    contact.matched_linkedin_url = candidate.linkedin_url
    contact.match_confidence = confidence
    contact.match_method = method
    contact.match_reason = reason


def apply_local_name_matching(
    contacts: Dict[str, Contact],
    candidates: List[PowersetCandidate],
) -> Dict[str, int]:
    """Apply backend-equivalent name matcher against contacts in memory."""
    if not contacts:
        return {"total": 0, "matched": 0, "suggested": 0, "unmatched": 0}
    if not candidates:
        for contact in contacts.values():
            _set_unmatched(contact, "No local candidate catalog available")
        return {"total": len(contacts), "matched": 0, "suggested": 0, "unmatched": len(contacts)}

    exact_index: Dict[str, List[PowersetCandidate]] = {}
    last_name_index: Dict[str, List[PowersetCandidate]] = {}

    for c in candidates:
        norm = c.normalized_name
        if not norm:
            continue
        exact_index.setdefault(norm, []).append(c)
        parts = norm.split(" ")
        if len(parts) >= 2:
            last_name_index.setdefault(parts[-1], []).append(c)

    for bucket in exact_index.values():
        bucket.sort(key=lambda c: c.id)
    for bucket in last_name_index.values():
        bucket.sort(key=lambda c: c.id)

    matched = 0
    suggested = 0
    unmatched = 0

    for contact in contacts.values():
        contact_name = contact.name or ""
        normalized_contact = _normalize_name(contact_name)
        if not normalized_contact:
            unmatched += 1
            _set_unmatched(contact, "Missing contact name")
            continue

        if normalized_contact == _normalize_name(contact.phone):
            unmatched += 1
            _set_unmatched(contact, "Name is identical to phone")
            continue

        exact_matches = list(exact_index.get(normalized_contact, []))
        if len(exact_matches) == 1:
            matched += 1
            _set_match(
                contact,
                status="matched",
                candidate=exact_matches[0],
                confidence=1.0,
                method="name_exact_linkedin",
                reason="Unique exact name match",
            )
            continue

        if len(exact_matches) > 1:
            suggested += 1
            _set_match(
                contact,
                status="suggested",
                candidate=exact_matches[0],
                confidence=0.80,
                method="name_exact_ambiguous",
                reason=f"{len(exact_matches)} exact-name candidates",
            )
            continue

        tokens = normalized_contact.split(" ")
        if len(tokens) < 2:
            unmatched += 1
            _set_unmatched(contact, "Single-token name with no exact candidate")
            continue

        pool = list(last_name_index.get(tokens[-1], []))
        if not pool:
            unmatched += 1
            _set_unmatched(contact, "No same-last-name candidates")
            continue

        scored = sorted(
            [(SequenceMatcher(None, normalized_contact, cand.normalized_name).ratio(), cand) for cand in pool],
            key=lambda item: item[0],
            reverse=True,
        )
        best_score, best_candidate = scored[0]
        second_score = scored[1][0] if len(scored) > 1 else 0.0
        confidence = round(float(best_score), 3)

        if best_score >= 0.94 and (best_score - second_score) >= 0.05:
            matched += 1
            _set_match(
                contact,
                status="matched",
                candidate=best_candidate,
                confidence=confidence,
                method="name_fuzzy_linkedin",
                reason="High-confidence fuzzy last-name match",
            )
            continue

        if best_score >= 0.80:
            suggested += 1
            _set_match(
                contact,
                status="suggested",
                candidate=best_candidate,
                confidence=confidence,
                method="name_fuzzy_suggested",
                reason="High-confidence fuzzy last-name candidate",
            )
            continue

        unmatched += 1
        _set_unmatched(contact, "Low-confidence fuzzy candidate")

    return {
        "total": len(contacts),
        "matched": matched,
        "suggested": suggested,
        "unmatched": unmatched,
    }
