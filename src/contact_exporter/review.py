"""Interactive TUI for reviewing contacts before upload.

Uses curses (stdlib) for a fullscreen checkbox list. Arrow keys to
navigate, space to toggle, enter to save. No external dependencies.
"""

from __future__ import annotations

import curses

from rich.console import Console

from contact_exporter.merge import load_existing_contacts, write_contacts
from contact_exporter.models import Contact

console = Console()


def _format_row(contact: Contact, width: int) -> str:
    """Format a contact as a fixed-width row for the TUI."""
    name = (contact.name or "—")[:25]
    phone = contact.phone[:16]
    source = contact.source[:8]
    count = str(contact.message_count) if contact.message_count else ""
    # Show last contacted as a short date like "Mar 05" or "2024-07"
    last = ""
    if contact.last_message:
        last = contact.last_message[:10]  # YYYY-MM-DD
    return f"{name:<25s}  {phone:<16s}  {source:<8s}  {count:>4s}  {last}"


def _run_tui(stdscr, contacts: list[Contact]) -> bool | None:
    """Curses main loop. Returns True if saved, False if cancelled."""
    curses.curs_set(0)  # Hide cursor
    curses.use_default_colors()

    # Color pairs
    curses.init_pair(1, curses.COLOR_GREEN, -1)   # Selected checkbox
    curses.init_pair(2, curses.COLOR_RED, -1)      # Deselected
    curses.init_pair(3, curses.COLOR_CYAN, -1)     # Header/footer
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE)  # Current row highlight

    selected = [not c.skip for c in contacts]
    cursor = 0
    scroll_offset = 0

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        list_height = height - 4  # Reserve lines for header + footer

        # Header
        total = len(contacts)
        num_selected = sum(selected)
        header = f"  Review Contacts — {num_selected} of {total} selected"
        stdscr.addnstr(0, 0, header, width - 1, curses.color_pair(3) | curses.A_BOLD)
        stdscr.addnstr(1, 0, "─" * min(width - 1, 80), width - 1, curses.color_pair(3))

        # Scrolling
        if cursor < scroll_offset:
            scroll_offset = cursor
        elif cursor >= scroll_offset + list_height:
            scroll_offset = cursor - list_height + 1

        # Contact list
        for i in range(list_height):
            idx = scroll_offset + i
            if idx >= total:
                break

            row_y = i + 2
            checkbox = "[x]" if selected[idx] else "[ ]"
            label = _format_row(contacts[idx], width)
            line = f"  {checkbox} {label}"

            if idx == cursor:
                attr = curses.color_pair(4)
            elif selected[idx]:
                attr = curses.color_pair(1)
            else:
                attr = curses.color_pair(2)

            stdscr.addnstr(row_y, 0, line, width - 1, attr)

        # Footer
        footer = "  ↑/↓ navigate  SPACE toggle  a all  n none  ENTER save  q cancel"
        stdscr.addnstr(height - 1, 0, footer, width - 1, curses.color_pair(3))

        stdscr.refresh()

        # Handle input
        key = stdscr.getch()

        if key == curses.KEY_UP and cursor > 0:
            cursor -= 1
        elif key == curses.KEY_DOWN and cursor < total - 1:
            cursor += 1
        elif key == ord(" "):
            selected[cursor] = not selected[cursor]
        elif key == ord("a"):
            selected = [True] * total
        elif key == ord("n"):
            selected = [False] * total
        elif key in (curses.KEY_ENTER, 10, 13):
            # Apply selections back to contacts
            for i, contact in enumerate(contacts):
                contact.skip = not selected[i]
            return True
        elif key in (ord("q"), 27):  # q or ESC
            return False
        elif key == curses.KEY_PPAGE:  # Page Up
            cursor = max(0, cursor - list_height)
        elif key == curses.KEY_NPAGE:  # Page Down
            cursor = min(total - 1, cursor + list_height)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = total - 1


def review_contacts(file_path: str = "contacts.csv") -> None:
    """Open the interactive review TUI and update the CSV with skip flags."""
    contacts_dict = load_existing_contacts(file_path)
    if not contacts_dict:
        console.print(f"[yellow]No contacts found in {file_path}[/yellow]")
        return

    # Sort by message count first, then by last contacted
    contacts_list = sorted(
        contacts_dict.values(),
        key=lambda c: (c.message_count or 0, c.last_message or ""),
        reverse=True,
    )

    saved = curses.wrapper(_run_tui, contacts_list)

    if saved:
        # Update the dict with modified skip flags
        for contact in contacts_list:
            contacts_dict[contact.phone] = contact

        total = len(contacts_list)
        skipped = sum(1 for c in contacts_list if c.skip)
        write_contacts(contacts_dict, file_path, limit=total)

        console.print(f"\n[green bold]✅ Saved {total - skipped} contacts ({skipped} skipped)[/green bold]")
    else:
        console.print("[dim]Cancelled — no changes made[/dim]")
