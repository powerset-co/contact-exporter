"""Interactive TUI for reviewing contacts before upload.

Uses curses (stdlib) for a fullscreen checkbox list. Arrow keys to
navigate, space to toggle, mouse click to toggle when supported, and
enter to save. No external dependencies.
"""

from __future__ import annotations

import curses
import csv
import textwrap
from dataclasses import dataclass
from pathlib import Path

from rich.console import Console

from contact_exporter.merge import load_existing_contacts, write_contacts
from contact_exporter.models import Contact


_MOUSE_LEFT_MASK = (
    getattr(curses, "BUTTON1_CLICKED", 0)
    | getattr(curses, "BUTTON1_PRESSED", 0)
    | getattr(curses, "BUTTON1_RELEASED", 0)
    | getattr(curses, "BUTTON1_DOUBLE_CLICKED", 0)
)

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
    stdscr.keypad(True)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error:
        pass

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
        footer = "  ↑/↓ navigate  SPACE/click toggle  a all  n none  ENTER save  q cancel"
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
        elif key == curses.KEY_MOUSE and _MOUSE_LEFT_MASK:
            try:
                _id, _mx, my, _mz, bstate = curses.getmouse()
            except curses.error:
                continue
            if not (bstate & _MOUSE_LEFT_MASK):
                continue
            idx = scroll_offset + (my - 2)
            if 0 <= idx < total:
                cursor = idx
                selected[idx] = not selected[idx]


@dataclass
class _ResearchRow:
    data: dict[str, str]
    original_bucket: str


def _bucket_group(bucket: str) -> int:
    b = (bucket or "").strip().lower()
    if b in {"yes", "confident"}:
        return 0
    if b in {"maybe", "medium"}:
        return 1
    return 2


def _bucket_label(bucket: str) -> str:
    b = (bucket or "").strip().lower()
    if b in {"yes", "confident"}:
        return "yes"
    if b in {"maybe", "medium"}:
        return "maybe"
    return "no"


def _trunc(text: str, max_len: int) -> str:
    if len(text) <= max_len:
        return text
    if max_len <= 1:
        return text[:max_len]
    return text[: max_len - 1] + "…"


def _research_sort_key(row: _ResearchRow) -> tuple[int, int, str]:
    count_raw = (row.data.get("total_messages") or "0").strip()
    try:
        count = int(count_raw)
    except ValueError:
        count = 0
    return (
        _bucket_group(row.data.get("bucket", "")),
        -count,
        (row.data.get("full_name") or "").lower(),
    )


def _wrap_plain(text: str, width: int) -> list[str]:
    clean = (text or "").strip() or "—"
    lines = textwrap.wrap(
        clean,
        width=max(1, width),
        break_long_words=True,
        break_on_hyphens=False,
    )
    return lines or ["—"]


def _wrap_prefixed(prefix: str, value: str, width: int) -> list[str]:
    clean = (value or "").strip() or "—"
    body_width = max(1, width - len(prefix))
    parts = textwrap.wrap(
        clean,
        width=body_width,
        break_long_words=True,
        break_on_hyphens=False,
    ) or ["—"]
    lines = [f"{prefix}{parts[0]}"]
    continuation = " " * len(prefix)
    for part in parts[1:]:
        lines.append(f"{continuation}{part}")
    return lines


def _render_research_card(
    row: _ResearchRow,
    selected: bool,
    ordinal: int,
    card_width: int,
) -> list[str]:
    inner_w = max(12, card_width - 2)
    data = row.data

    name = (data.get("full_name") or "Unknown").strip()
    bucket = _bucket_label(data.get("bucket", "")).upper()
    msgs = (data.get("total_messages") or "0").strip() or "0"
    src = (data.get("message_source") or "—").strip()
    phone = (data.get("phone_e164") or "—").strip()
    city = (data.get("location_city") or "").strip()
    country = (data.get("location_country") or "").strip()
    location = ", ".join(part for part in [city, country] if part) or "—"
    company = ((data.get("top_companies") or "").split("|")[0]).strip() or "—"
    education = ((data.get("schools") or "").split("|")[0]).strip() or "—"
    reason = (data.get("short_reason") or "").strip() or "—"

    marker = "✓" if selected else " "
    action = "KEEP" if selected else "EXCLUDE"
    head = f"{marker} #{ordinal:02d} {name}"
    head_meta = f"[{bucket}] [{action}] msgs={msgs} src={src}"

    body_lines: list[str] = []
    body_lines.extend(_wrap_plain(f"{head}  {head_meta}", inner_w))
    body_lines.extend(_wrap_prefixed("phone: ", phone, inner_w))
    body_lines.extend(_wrap_prefixed("location: ", location, inner_w))
    body_lines.extend(_wrap_prefixed("company: ", company, inner_w))
    body_lines.extend(_wrap_prefixed("education: ", education, inner_w))
    body_lines.extend(_wrap_prefixed("reason: ", reason, inner_w))

    top = "╭" + ("─" * inner_w) + "╮"
    bottom = "╰" + ("─" * inner_w) + "╯"
    card = [top]
    for line in body_lines:
        card.append(f"│{line.ljust(inner_w)}│")
    card.append(bottom)
    return card


def _run_research_tui(stdscr, rows: list[_ResearchRow], batch_size: int) -> bool | None:
    """Review messages-research rows in fixed-size batches (yes-first order)."""
    curses.curs_set(0)
    curses.use_default_colors()
    stdscr.keypad(True)
    try:
        curses.mousemask(curses.ALL_MOUSE_EVENTS)
    except curses.error:
        pass
    curses.init_pair(1, curses.COLOR_GREEN, -1)  # kept/included
    curses.init_pair(2, curses.COLOR_RED, -1)  # excluded
    curses.init_pair(3, curses.COLOR_CYAN, -1)  # header/footer
    curses.init_pair(4, curses.COLOR_BLACK, curses.COLOR_WHITE)  # current row

    total = len(rows)
    selected = [True] * total  # selected=True => keep/include, False => exclude
    cursor = 0
    # Cached from render pass; used by mouse hit-testing in this frame.
    line_to_idx: dict[int, int] = {}

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()

        total_batches = (total + batch_size - 1) // batch_size
        batch_idx = cursor // batch_size
        batch_start = batch_idx * batch_size
        batch_end = min(total, batch_start + batch_size)

        kept = sum(selected)
        excluded = total - kept
        header = (
            f"  Review Messages CSV — batch {batch_idx + 1}/{total_batches} "
            f"(yes first)  keep={kept} exclude={excluded}"
        )
        stdscr.addnstr(0, 0, header, width - 1, curses.color_pair(3) | curses.A_BOLD)
        stdscr.addnstr(1, 0, "─" * min(width - 1, 120), width - 1, curses.color_pair(3))

        # Full-width "packet" cards with wrapped fields.
        # Cursor-centered viewport inside current batch.
        cards_top_y = 2
        footer_y = height - 1
        available_lines = max(1, footer_y - cards_top_y)
        card_width = max(20, width - 1)

        cards_by_idx: dict[int, list[str]] = {}
        card_heights: dict[int, int] = {}
        for idx in range(batch_start, batch_end):
            ordinal = idx - batch_start + 1
            card_lines = _render_research_card(
                rows[idx],
                selected=selected[idx],
                ordinal=ordinal,
                card_width=card_width,
            )
            cards_by_idx[idx] = card_lines
            card_heights[idx] = len(card_lines)

        # Keep cursor visible by expanding around it.
        visible_start = cursor
        used = card_heights[cursor]
        while visible_start > batch_start and used + card_heights[visible_start - 1] <= available_lines:
            visible_start -= 1
            used += card_heights[visible_start]
        visible_end = cursor + 1
        while visible_end < batch_end and used + card_heights[visible_end] <= available_lines:
            used += card_heights[visible_end]
            visible_end += 1

        line_to_idx = {}
        row_y = cards_top_y
        for idx in range(visible_start, visible_end):
            if row_y >= footer_y:
                break
            if idx == cursor:
                attr = curses.color_pair(4)
            elif selected[idx]:
                attr = curses.color_pair(1)
            else:
                attr = curses.color_pair(2)

            for line in cards_by_idx[idx]:
                if row_y >= footer_y:
                    break
                stdscr.addnstr(row_y, 0, line, width - 1, attr)
                line_to_idx[row_y] = idx
                row_y += 1

        footer = (
            "  ↑/↓ navigate  SPACE/click toggle  n/p next/prev batch  "
            "a all  x exclude batch  ENTER save  q cancel"
        )
        stdscr.addnstr(height - 1, 0, footer, width - 1, curses.color_pair(3))
        stdscr.refresh()

        key = stdscr.getch()
        if key == curses.KEY_UP and cursor > 0:
            cursor -= 1
        elif key == curses.KEY_DOWN and cursor < total - 1:
            cursor += 1
        elif key == ord(" "):
            selected[cursor] = not selected[cursor]
        elif key == ord("a"):
            selected = [True] * total
        elif key == ord("x"):
            for i in range(batch_start, batch_end):
                selected[i] = False
        elif key == ord("n"):
            if batch_idx < total_batches - 1:
                cursor = min(total - 1, (batch_idx + 1) * batch_size)
        elif key == ord("p"):
            if batch_idx > 0:
                cursor = (batch_idx - 1) * batch_size
        elif key in (curses.KEY_ENTER, 10, 13):
            for i, row in enumerate(rows):
                if selected[i]:
                    row.data["exclude"] = ""
                else:
                    row.data["exclude"] = "yes"
                    # Excluded rows should not stay in positive buckets.
                    row.data["bucket"] = "no"
            return True
        elif key in (ord("q"), 27):
            return False
        elif key == curses.KEY_PPAGE:
            cursor = max(0, batch_start - batch_size)
        elif key == curses.KEY_NPAGE:
            cursor = min(total - 1, batch_start + batch_size)
        elif key == curses.KEY_HOME:
            cursor = 0
        elif key == curses.KEY_END:
            cursor = total - 1
        elif key == curses.KEY_MOUSE and _MOUSE_LEFT_MASK:
            try:
                _id, _mx, my, _mz, bstate = curses.getmouse()
            except curses.error:
                continue
            if not (bstate & _MOUSE_LEFT_MASK):
                continue
            idx = line_to_idx.get(my)
            if idx is not None:
                cursor = idx
                selected[idx] = not selected[idx]


def _is_research_csv(fieldnames: list[str]) -> bool:
    required = {"bucket", "full_name", "phone_e164"}
    return required.issubset(set(fieldnames or []))


def _review_research_csv(file_path: str, batch_size: int) -> None:
    path = Path(file_path)
    if not path.exists():
        console.print(f"[yellow]File not found: {file_path}[/yellow]")
        return

    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        fieldnames = list(reader.fieldnames or [])
        rows = [_ResearchRow(data={k: (v or "") for k, v in row.items()}, original_bucket=(row.get("bucket") or "")) for row in reader]

    if not rows:
        console.print(f"[yellow]No rows found in {file_path}[/yellow]")
        return

    if "exclude" not in fieldnames:
        fieldnames.append("exclude")

    rows.sort(key=_research_sort_key)
    saved = curses.wrapper(_run_research_tui, rows, max(1, batch_size))

    if not saved:
        console.print("[dim]Cancelled — no changes made[/dim]")
        return

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            out = {k: r.data.get(k, "") for k in fieldnames}
            writer.writerow(out)

    excluded = sum(1 for r in rows if (r.data.get("exclude") or "").strip().lower() in {"yes", "true", "1"})
    console.print(
        f"\n[green bold]✅ Saved research review: kept {len(rows) - excluded}, excluded {excluded}[/green bold]"
    )


def review_contacts(file_path: str = "contacts.csv", batch_size: int = 10) -> None:
    """Open the interactive review TUI and update the CSV with skip flags."""
    path = Path(file_path)
    if path.exists():
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            if _is_research_csv(list(reader.fieldnames or [])):
                _review_research_csv(file_path, batch_size=batch_size)
                return

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
