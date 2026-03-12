"""CLI entry point for contact-exporter.

Usage:
    contact-exporter login       # Authenticate via browser
    contact-exporter logout      # Clear stored credentials
    contact-exporter whoami      # Show current user
    contact-exporter imessage    # Extract iMessage contacts
    contact-exporter whatsapp    # Extract WhatsApp contacts via Docker
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
from contact_exporter.imessage.extract import extract_imessage
from contact_exporter.review import review_contacts
from contact_exporter.upload import upload_contacts
from contact_exporter.whatsapp.extract import extract_whatsapp

console = Console()


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
    extract_imessage(output_path=args.output, include_small_groups=args.include_small_groups)


def cmd_whatsapp(args):
    extract_whatsapp(output_path=args.output)


def cmd_review(args):
    review_contacts(file_path=args.file)


def cmd_upload(args):
    upload_contacts(file_path=args.file)


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
    imsg_parser.add_argument("--output", "-o", default="contacts.csv", help="Output file path")
    imsg_parser.add_argument(
        "--include-small-groups",
        action="store_true",
        help="Count messages from small group chats (<=7 people)",
    )

    wa_parser = subparsers.add_parser("whatsapp", help="Extract WhatsApp contacts via Docker")
    wa_parser.add_argument("--output", "-o", default="contacts.csv", help="Output file path")

    review_parser = subparsers.add_parser("review", help="Review contacts interactively before upload")
    review_parser.add_argument("--file", "-f", default="contacts.csv", help="CSV file to review")

    upload_parser = subparsers.add_parser("upload", help="Upload contacts to Powerset")
    upload_parser.add_argument("--file", "-f", default="contacts.csv", help="CSV file to upload")

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
        "review": cmd_review,
        "upload": cmd_upload,
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
