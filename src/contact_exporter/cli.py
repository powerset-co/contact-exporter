"""CLI entry point for contact-exporter.

Usage:
    contact-exporter login       # Authenticate via browser
    contact-exporter logout      # Clear stored credentials
    contact-exporter whoami      # Show current user
    contact-exporter imessage    # Extract iMessage contacts
    contact-exporter whatsapp    # Extract WhatsApp contacts via Docker
    contact-exporter full        # iMessage -> WhatsApp -> LLM review in one run
    contact-exporter sync-candidates  # Download operator candidate catalog
    contact-exporter match-local      # Apply local matching to contacts CSV
    contact-exporter review      # Interactively review contacts before upload
    contact-exporter upload      # Upload contacts.csv to Powerset
"""

import argparse
import time

import requests
from rich.console import Console

from contact_exporter import __version__
from contact_exporter.auth.credentials import clear_credentials, get_credentials_info
from contact_exporter.auth.oauth import login
from contact_exporter.config import LOCAL_API_BASE_URL, get_api_base_url, set_api_base_url
from contact_exporter.imessage.extract import extract_imessage
from contact_exporter.llm_review import review_contacts_llm
from contact_exporter.matching import apply_local_name_matching, sync_candidate_catalog
from contact_exporter.merge import load_existing_contacts, write_contacts
from contact_exporter.research_review import download_research_review, upload_research_review
from contact_exporter.review import review_contacts
from contact_exporter.upload import upload_contacts
from contact_exporter.whatsapp.extract import extract_whatsapp

console = Console()


def _apply_api_override(args) -> None:
    local = bool(getattr(args, "local", False))
    custom = getattr(args, "api_base_url", None)
    if local and custom:
        console.print("[red]Use either --local or --api-base-url, not both.[/red]")
        raise SystemExit(2)

    if local:
        set_api_base_url(LOCAL_API_BASE_URL)
    elif custom:
        set_api_base_url(custom)
    else:
        return

    console.print(f"[dim]Using API endpoint: {get_api_base_url()}[/dim]")


def _add_api_override_flags(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--local",
        action="store_true",
        help=f"Use local API endpoint ({LOCAL_API_BASE_URL})",
    )
    p.add_argument(
        "--api-base-url",
        help="Override API endpoint for this run (e.g. http://localhost:8000)",
    )


def cmd_login(_args):
    login()


def cmd_logout(_args):
    clear_credentials()
    console.print("[green]Logged out. Credentials cleared.[/green]")


def cmd_whoami(_args):
    info = get_credentials_info()
    if not info:
        console.print("[yellow]Not logged in. Run: contact-exporter login[/yellow]")
        raise SystemExit(1)

    email = info.get("email", "unknown")
    expires_at = info.get("expires_at", 0)

    console.print(f"[bold]Email:[/bold] {email}")
    if time.time() > expires_at:
        console.print("[yellow]Token expired — will auto-refresh on next command[/yellow]")
    else:
        remaining = int(expires_at - time.time())
        console.print(f"[dim]Token expires in {remaining // 3600}h {(remaining % 3600) // 60}m[/dim]")


def cmd_imessage(args):
    _apply_api_override(args)
    extract_imessage(
        output_path=args.output,
        include_small_groups=args.include_small_groups,
        operator_id=args.operator_id,
    )


def cmd_whatsapp(args):
    _apply_api_override(args)
    extract_whatsapp(output_path=args.output, reset=args.reset, operator_id=args.operator_id)


def cmd_llm_review(args):
    review_contacts_llm(
        csv_path=args.file,
        api_key=args.api_key,
        model=args.model,
        dry_run=args.dry_run,
        include_matched=args.all,
    )


def cmd_full(args):
    _apply_api_override(args)

    console.print("[bold]Running full pipeline[/bold]")
    console.print("[dim]1/3 iMessage extraction[/dim]")
    extract_imessage(
        output_path=args.output,
        include_small_groups=args.include_small_groups,
        operator_id=args.operator_id,
    )

    console.print("[dim]2/3 WhatsApp extraction[/dim]")
    extract_whatsapp(
        output_path=args.output,
        reset=args.reset_whatsapp,
        operator_id=args.operator_id,
    )

    console.print("[dim]3/3 LLM review (unmatched/suggested by default)[/dim]")
    review_contacts_llm(
        csv_path=args.output,
        api_key=args.api_key,
        model=args.model,
        dry_run=args.dry_run,
        include_matched=args.all,
    )

    console.print(f"[green bold]✅ Full pipeline complete: {args.output}[/green bold]")


def cmd_review(args):
    review_contacts(file_path=args.file, batch_size=args.batch_size)


def cmd_upload(args):
    _apply_api_override(args)
    upload_contacts(file_path=args.file)


def cmd_research_review(args):
    _apply_api_override(args)
    if args.upload:
        upload_research_review(args.upload)
        return
    if args.review is not None:
        artifact_id = None if args.review == "__LATEST__" else args.review
        download_research_review(artifact_id=artifact_id, output_dir=args.output_dir)
        return
    raise SystemExit("Use --upload <csv> or --review [artifact_id]")


def cmd_sync_candidates(args):
    _apply_api_override(args)
    candidates = sync_candidate_catalog(
        catalog_path=args.output,
        refresh=not args.use_cached,
        operator_id=args.operator_id,
    )
    console.print(f"[green]Candidate catalog ready: {len(candidates)} rows[/green]")


def cmd_match_local(args):
    _apply_api_override(args)
    contacts = load_existing_contacts(args.file)
    if not contacts:
        console.print(f"[yellow]No contacts found in {args.file}[/yellow]")
        raise SystemExit(1)

    candidates = sync_candidate_catalog(
        catalog_path=args.candidates,
        refresh=not args.use_cached,
        operator_id=args.operator_id,
    )
    stats = apply_local_name_matching(contacts, candidates)
    written = write_contacts(contacts, args.file)

    console.print(f"[green bold]✅ Updated {written} contacts with local match metadata[/green bold]")
    console.print(
        f"[dim]matched={stats['matched']} suggested={stats['suggested']} "
        f"unmatched={stats['unmatched']}[/dim]"
    )


def main():
    parser = argparse.ArgumentParser(
        prog="contact-exporter",
        description="Extract iMessage & WhatsApp contacts locally for Powerset",
    )
    parser.add_argument("--version", action="version", version=f"contact-exporter {__version__}")
    subparsers = parser.add_subparsers(dest="command")

    subparsers.add_parser("login", help="Authenticate with Powerset via browser")
    subparsers.add_parser("logout", help="Clear stored credentials")
    subparsers.add_parser("whoami", help="Show current authenticated user")

    imsg_parser = subparsers.add_parser("imessage", help="Extract iMessage contacts")
    _add_api_override_flags(imsg_parser)
    imsg_parser.add_argument("--operator-id", help="Fetch candidate catalog for this operator_id (admin only)")
    imsg_parser.add_argument("--output", "-o", default="contacts.csv", help="Output file path")
    imsg_parser.add_argument(
        "--include-small-groups",
        action="store_true",
        help="Count messages from small group chats (<=7 people)",
    )

    wa_parser = subparsers.add_parser("whatsapp", help="Extract WhatsApp contacts via Docker")
    _add_api_override_flags(wa_parser)
    wa_parser.add_argument("--operator-id", help="Fetch candidate catalog for this operator_id (admin only)")
    wa_parser.add_argument("--output", "-o", default="contacts.csv", help="Output file path")
    wa_parser.add_argument("--reset", action="store_true", help="Clear existing session and start fresh")

    full_parser = subparsers.add_parser(
        "full",
        help="Run iMessage + WhatsApp extraction, then LLM review",
    )
    _add_api_override_flags(full_parser)
    full_parser.add_argument("--operator-id", help="Fetch candidate catalog for this operator_id (admin only)")
    full_parser.add_argument("--output", "-o", default="contacts.csv", help="Unified CSV output path")
    full_parser.add_argument(
        "--include-small-groups",
        action="store_true",
        help="Passed to iMessage extractor (no-op compatibility flag)",
    )
    full_parser.add_argument(
        "--reset-whatsapp",
        action="store_true",
        help="Reset WhatsApp session before extraction (forces fresh QR auth)",
    )
    full_parser.add_argument("--api-key", help="OpenRouter API key (or set OPENROUTER_API_KEY)")
    full_parser.add_argument("--model", default="anthropic/claude-sonnet-4-6", help="Model to use for LLM review")
    full_parser.add_argument("--dry-run", action="store_true", help="Estimate LLM review cost without API calls")
    full_parser.add_argument("--all", action="store_true", help="Review all named contacts (default: only unmatched/suggested)")

    llm_parser = subparsers.add_parser("llm-review", help="LLM-powered contact review (ENRICH/SKIP)")
    llm_parser.add_argument("--file", "-f", default="contacts.csv", help="CSV file to review")
    llm_parser.add_argument("--api-key", help="OpenRouter API key (or set OPENROUTER_API_KEY)")
    llm_parser.add_argument("--model", default="anthropic/claude-sonnet-4-6", help="Model to use")
    llm_parser.add_argument("--dry-run", action="store_true", help="Estimate cost without calling API")
    llm_parser.add_argument("--all", action="store_true", help="Review all named contacts (default: only unmatched/suggested)")

    review_parser = subparsers.add_parser("review", help="Review contacts interactively before upload")
    review_parser.add_argument("--file", "-f", default="contacts.csv", help="CSV file to review")
    review_parser.add_argument(
        "--batch-size",
        type=int,
        default=10,
        help="Rows per page for research-review CSVs (default: 10)",
    )

    upload_parser = subparsers.add_parser("upload", help="Upload contacts to Powerset")
    _add_api_override_flags(upload_parser)
    upload_parser.add_argument("--file", "-f", default="contacts.csv", help="CSV file to upload")

    rr_parser = subparsers.add_parser("research-review", help="Upload or download messages research review artifacts")
    _add_api_override_flags(rr_parser)
    rr_group = rr_parser.add_mutually_exclusive_group(required=True)
    rr_group.add_argument("--upload", metavar="CSV", help="Upload a reviewed messages CSV")
    rr_group.add_argument(
        "--review",
        nargs="?",
        const="__LATEST__",
        metavar="ARTIFACT_ID",
        help="Download latest review artifact or a specific artifact id",
    )
    rr_parser.add_argument(
        "--output-dir",
        help="Directory to extract downloaded review ZIP into (default: ./messages_research_reviews)",
    )

    sync_parser = subparsers.add_parser("sync-candidates", help="Download operator candidate catalog to local CSV")
    _add_api_override_flags(sync_parser)
    sync_parser.add_argument("--operator-id", help="Fetch candidates for this operator_id (admin only)")
    sync_parser.add_argument("--output", "-o", default="powerset_contacts.csv", help="Candidate CSV output path")
    sync_parser.add_argument("--use-cached", action="store_true", help="Use existing local candidate CSV without refreshing")

    match_parser = subparsers.add_parser("match-local", help="Run local name matching against candidate catalog")
    _add_api_override_flags(match_parser)
    match_parser.add_argument("--operator-id", help="Fetch candidates for this operator_id (admin only)")
    match_parser.add_argument("--file", "-f", default="contacts.csv", help="Contacts CSV to update")
    match_parser.add_argument("--candidates", default="powerset_contacts.csv", help="Candidate CSV path")
    match_parser.add_argument("--use-cached", action="store_true", help="Use existing local candidate CSV without refreshing")

    args = parser.parse_args()
    if not args.command:
        parser.print_help()
        raise SystemExit(1)

    handlers = {
        "login": cmd_login,
        "logout": cmd_logout,
        "whoami": cmd_whoami,
        "imessage": cmd_imessage,
        "whatsapp": cmd_whatsapp,
        "full": cmd_full,
        "llm-review": cmd_llm_review,
        "review": cmd_review,
        "upload": cmd_upload,
        "research-review": cmd_research_review,
        "sync-candidates": cmd_sync_candidates,
        "match-local": cmd_match_local,
    }

    try:
        handlers[args.command](args)
    except KeyboardInterrupt:
        console.print("\n[dim]Interrupted.[/dim]")
        raise SystemExit(130)
    except requests.ConnectionError:
        console.print("[red]Connection error. Check your internet and try again.[/red]")
        raise SystemExit(1)
    except requests.Timeout:
        console.print("[red]Request timed out. Try again.[/red]")
        raise SystemExit(1)
