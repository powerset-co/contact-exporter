"""Upload contacts.csv to the Powerset API."""

from __future__ import annotations

import csv
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn

from contact_exporter.auth.credentials import get_auth_header
from contact_exporter.config import API_BASE_URL, UPLOAD_CHUNK_SIZE
from contact_exporter.models import Contact

console = Console()


def upload_contacts(file_path: str = "contacts.csv") -> None:
    """Upload contacts from a CSV file, skipping rows marked with skip=yes."""
    path = Path(file_path)
    if not path.exists():
        console.print(f"[red]File not found: {file_path}[/red]")
        console.print("[dim]Run 'contact-exporter imessage' or 'contact-exporter whatsapp' first[/dim]")
        raise SystemExit(1)

    headers = get_auth_header()
    headers["Content-Type"] = "application/json"

    # Read CSV, skip contacts marked for exclusion
    contacts = []
    skipped = 0
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            try:
                contact = Contact.from_csv_row(row)
            except (KeyError, ValueError):
                continue
            if contact.skip:
                skipped += 1
                continue
            contacts.append({
                "phone": contact.phone,
                "name": contact.name,
                "source": contact.source,
                "is_in_group_chats": contact.is_in_group_chats,
                "message_count": contact.message_count or 0,
                "last_message": contact.last_message,
            })

    if not contacts:
        console.print("[yellow]No contacts to upload (all skipped or file empty)[/yellow]")
        return

    # Group by source for the API
    by_source: dict[str, list[dict]] = {}
    for contact in contacts:
        by_source.setdefault(contact["source"], []).append(contact)

    console.print(f"[bold]Uploading {len(contacts)} contacts to Powerset[/bold]")
    if skipped:
        console.print(f"  [dim]{skipped} contacts skipped[/dim]")
    for source, batch in by_source.items():
        console.print(f"  {source}: {len(batch)} contacts")
    console.print()

    total_uploaded = 0
    total_matched = 0
    total_errors = 0

    for source, batch in by_source.items():
        chunks = [batch[i : i + UPLOAD_CHUNK_SIZE] for i in range(0, len(batch), UPLOAD_CHUNK_SIZE)]

        with Progress(
            SpinnerColumn(),
            TextColumn(f"[progress.description]Uploading {source}"),
            BarColumn(),
            TextColumn("{task.completed}/{task.total}"),
            console=console,
        ) as progress:
            task = progress.add_task(f"Uploading {source}", total=len(batch))

            for chunk in chunks:
                try:
                    resp = requests.post(
                        f"{API_BASE_URL}/v2/contacts/import",
                        headers=headers,
                        json={"contacts": chunk, "source_channel": source},
                        timeout=60,
                    )

                    if resp.status_code == 401:
                        console.print("[red]Authentication expired. Run: contact-exporter login[/red]")
                        raise SystemExit(1)

                    if resp.status_code not in (200, 201):
                        console.print(f"[red]Upload failed ({resp.status_code}): {resp.text}[/red]")
                        total_errors += len(chunk)
                        progress.advance(task, len(chunk))
                        continue

                    result = resp.json()
                    total_uploaded += result.get("imported", 0)
                    total_matched += result.get("matched", 0)
                    total_errors += result.get("errors", 0)

                except requests.RequestException as e:
                    console.print(f"[red]Request error: {e}[/red]")
                    total_errors += len(chunk)

                progress.advance(task, len(chunk))

    console.print()
    console.print("[green bold]✅ Upload complete[/green bold]")
    console.print(f"  Imported: {total_uploaded}")
    if total_matched:
        console.print(f"  Matched to existing people: {total_matched}")
    if total_errors:
        console.print(f"  [yellow]Errors: {total_errors}[/yellow]")
