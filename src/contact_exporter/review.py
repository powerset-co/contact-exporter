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


_RESEARCH_TABS: tuple[str, str, str] = ("yes", "maybe", "no")


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


def _normalize_tab(tab: str | None) -> str:
    raw = (tab or "").strip().lower()
    if raw in {"y", "yes"}:
        return "yes"
    if raw in {"m", "maybe"}:
        return "maybe"
    if raw in {"n", "no"}:
        return "no"
    return "yes"


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
    group_names = " | ".join(
        part.strip() for part in (data.get("group_names") or "").split("|") if part.strip()
    ) or "—"
    companies = " | ".join(part.strip() for part in (data.get("top_companies") or "").split("|") if part.strip()) or "—"
    raw_pairs = (data.get("top_title_company_pairs") or "").strip()
    title_company_pairs = " | ".join(part.strip() for part in raw_pairs.split("|") if part.strip()) or "—"
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
    body_lines.extend(_wrap_prefixed("groups: ", group_names, inner_w))
    body_lines.extend(_wrap_prefixed("title@company: ", title_company_pairs, inner_w))
    body_lines.extend(_wrap_prefixed("companies: ", companies, inner_w))
    body_lines.extend(_wrap_prefixed("education: ", education, inner_w))
    body_lines.extend(_wrap_prefixed("reason: ", reason, inner_w))

    top = "╭" + ("─" * inner_w) + "╮"
    bottom = "╰" + ("─" * inner_w) + "╯"
    card = [top]
    for line in body_lines:
        card.append(f"│{line.ljust(inner_w)}│")
    card.append(bottom)
    return card


def _run_research_tui(
    stdscr,
    rows: list[_ResearchRow],
    batch_size: int,
    initial_tab: str,
    initial_selected: list[bool],
) -> bool | None:
    """Review messages-research rows using YES/MAYBE/NO tabbed cards."""
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
    if len(initial_selected) == total:
        selected = list(initial_selected)
    else:
        selected = [True] * total

    # Only commit `exclude` updates for rows the user actually toggled in this
    # session. Untouched rows keep whatever bucket+exclude they had on disk so
    # opening one tab and saving never silently nukes the others.
    modified: set[int] = set()

    active_tab = _normalize_tab(initial_tab)
    cursor_pos_by_tab: dict[str, int] = {tab: 0 for tab in _RESEARCH_TABS}
    line_to_idx: dict[int, int] = {}
    tab_segments: list[tuple[int, int, str]] = []

    while True:
        stdscr.clear()
        height, width = stdscr.getmaxyx()
        width_safe = max(1, width)

        indices_by_tab: dict[str, list[int]] = {
            tab: [i for i, row in enumerate(rows) if _bucket_label(row.data.get("bucket", "")) == tab]
            for tab in _RESEARCH_TABS
        }
        tab_indices = indices_by_tab[active_tab]
        tab_total = len(tab_indices)

        cursor_pos = cursor_pos_by_tab[active_tab]
        if tab_total == 0:
            cursor_pos = 0
        else:
            cursor_pos = max(0, min(cursor_pos, tab_total - 1))
        cursor_pos_by_tab[active_tab] = cursor_pos

        total_batches = max(1, (tab_total + batch_size - 1) // batch_size)
        batch_idx = (cursor_pos // batch_size) if tab_total else 0
        batch_start_pos = batch_idx * batch_size
        batch_end_pos = min(tab_total, batch_start_pos + batch_size)

        kept = sum(selected)
        excluded = total - kept
        mode_hint = (
            "YES mode: deselect rows you want excluded"
            if active_tab == "yes"
            else f"{active_tab.upper()} mode: select rows you want included"
        )
        header = (
            f"  Review Messages CSV — {active_tab.upper()} batch {batch_idx + 1}/{total_batches}  "
            f"keep={kept} exclude={excluded}"
        )
        stdscr.addnstr(0, 0, header, width_safe - 1, curses.color_pair(3) | curses.A_BOLD)

        tab_segments = []
        tabs_count = len(_RESEARCH_TABS)
        seg_base = width_safe // tabs_count
        seg_remainder = width_safe % tabs_count
        x = 0
        for i, tab in enumerate(_RESEARCH_TABS):
            seg_w = seg_base + (1 if i < seg_remainder else 0)
            if seg_w <= 0:
                continue
            label = f" {tab.upper()} ".center(seg_w)
            count = f"{len(indices_by_tab[tab])} rows".center(seg_w)
            attr = curses.color_pair(3) | curses.A_BOLD
            if tab == active_tab:
                attr |= curses.A_REVERSE
            stdscr.addnstr(1, x, label, seg_w, attr)
            stdscr.addnstr(2, x, count, seg_w, attr)
            tab_segments.append((x, x + seg_w, tab))
            x += seg_w

        stdscr.addnstr(3, 0, f"  {mode_hint}", width_safe - 1, curses.color_pair(3))
        stdscr.addnstr(4, 0, "─" * min(width_safe - 1, 140), width_safe - 1, curses.color_pair(3))

        cards_top_y = 5
        footer_y = height - 1
        available_lines = max(1, footer_y - cards_top_y)
        card_width = max(20, width_safe - 1)

        line_to_idx = {}
        if tab_total == 0:
            empty = "  No rows in this tab. Use LEFT/RIGHT or 1/2/3 to switch tabs."
            stdscr.addnstr(cards_top_y, 0, empty, width_safe - 1, curses.color_pair(3))
        else:
            batch_indices = tab_indices[batch_start_pos:batch_end_pos]
            cards_by_idx: dict[int, list[str]] = {}
            card_heights: dict[int, int] = {}
            for ordinal, idx in enumerate(batch_indices, start=batch_start_pos + 1):
                card_lines = _render_research_card(
                    rows[idx],
                    selected=selected[idx],
                    ordinal=ordinal,
                    card_width=card_width,
                )
                cards_by_idx[idx] = card_lines
                card_heights[idx] = len(card_lines)

            visible_start_pos = cursor_pos
            cursor_idx = tab_indices[cursor_pos]
            used = card_heights[cursor_idx]

            while (
                visible_start_pos > batch_start_pos
                and used + card_heights[tab_indices[visible_start_pos - 1]] <= available_lines
            ):
                visible_start_pos -= 1
                used += card_heights[tab_indices[visible_start_pos]]

            visible_end_pos = cursor_pos + 1
            while (
                visible_end_pos < batch_end_pos
                and used + card_heights[tab_indices[visible_end_pos]] <= available_lines
            ):
                used += card_heights[tab_indices[visible_end_pos]]
                visible_end_pos += 1

            row_y = cards_top_y
            for tab_pos in range(visible_start_pos, visible_end_pos):
                if row_y >= footer_y:
                    break
                idx = tab_indices[tab_pos]
                if tab_pos == cursor_pos:
                    attr = curses.color_pair(4)
                elif selected[idx]:
                    attr = curses.color_pair(1)
                else:
                    attr = curses.color_pair(2)

                for line in cards_by_idx[idx]:
                    if row_y >= footer_y:
                        break
                    stdscr.addnstr(row_y, 0, line, width_safe - 1, attr)
                    line_to_idx[row_y] = idx
                    row_y += 1

        primary_batch = "x deselect batch" if active_tab == "yes" else "s select batch"
        footer = (
            f"  ←/→ tabs  1/2/3 tabs  ↑/↓ navigate  SPACE/click toggle  {primary_batch}  "
            "n/p next/prev batch  ENTER save  q cancel"
        )
        stdscr.addnstr(height - 1, 0, footer, width_safe - 1, curses.color_pair(3))
        stdscr.refresh()

        key = stdscr.getch()

        def _switch_tab(delta: int) -> None:
            nonlocal active_tab
            tab_i = _RESEARCH_TABS.index(active_tab)
            active_tab = _RESEARCH_TABS[(tab_i + delta) % len(_RESEARCH_TABS)]

        if key == curses.KEY_LEFT:
            _switch_tab(-1)
        elif key == curses.KEY_RIGHT:
            _switch_tab(1)
        elif key in (ord("1"), ord("2"), ord("3")):
            active_tab = _RESEARCH_TABS[int(chr(key)) - 1]
        elif key == curses.KEY_UP and tab_total > 0 and cursor_pos > 0:
            cursor_pos_by_tab[active_tab] = cursor_pos - 1
        elif key == curses.KEY_DOWN and tab_total > 0 and cursor_pos < tab_total - 1:
            cursor_pos_by_tab[active_tab] = cursor_pos + 1
        elif key == ord(" ") and tab_total > 0:
            idx = tab_indices[cursor_pos]
            selected[idx] = not selected[idx]
            modified.add(idx)
        elif key == ord("a"):
            for idx in tab_indices:
                selected[idx] = True
                modified.add(idx)
        elif key == ord("d"):
            for idx in tab_indices:
                selected[idx] = False
                modified.add(idx)
        elif key == ord("s"):
            for tab_pos in range(batch_start_pos, batch_end_pos):
                idx = tab_indices[tab_pos]
                selected[idx] = True
                modified.add(idx)
        elif key == ord("x"):
            for tab_pos in range(batch_start_pos, batch_end_pos):
                idx = tab_indices[tab_pos]
                selected[idx] = False
                modified.add(idx)
        elif key == ord("n"):
            if tab_total > 0 and batch_idx < total_batches - 1:
                cursor_pos_by_tab[active_tab] = min(tab_total - 1, (batch_idx + 1) * batch_size)
        elif key == ord("p"):
            if tab_total > 0 and batch_idx > 0:
                cursor_pos_by_tab[active_tab] = (batch_idx - 1) * batch_size
        elif key in (curses.KEY_ENTER, 10, 13):
            # Only write `exclude` for rows the user actually touched this
            # session. Untouched rows keep whatever they had on disk —
            # critical so that flipping one MAYBE row and saving does not
            # silently rewrite all the other maybes/reviews to bucket=no,
            # exclude=yes. Bucket is research-derived metadata and never
            # gets overwritten by the TUI.
            #
            # Use exclude="no" (not blank) for explicit include, so the
            # toggle round-trips through reopen: the file's reload path treats
            # blank exclude as "no decision yet" and falls back to the bucket
            # default, which for a maybe row means unselected. Writing "no"
            # is the explicit "user said include this" signal.
            for i in modified:
                if selected[i]:
                    rows[i].data["exclude"] = "no"
                else:
                    rows[i].data["exclude"] = "yes"
            return True
        elif key in (ord("q"), 27):
            return False
        elif key == curses.KEY_HOME and tab_total > 0:
            cursor_pos_by_tab[active_tab] = 0
        elif key == curses.KEY_END and tab_total > 0:
            cursor_pos_by_tab[active_tab] = tab_total - 1
        elif key == curses.KEY_PPAGE and tab_total > 0:
            cursor_pos_by_tab[active_tab] = max(0, batch_start_pos - batch_size)
        elif key == curses.KEY_NPAGE and tab_total > 0:
            cursor_pos_by_tab[active_tab] = min(tab_total - 1, batch_start_pos + batch_size)
        elif key == curses.KEY_MOUSE and _MOUSE_LEFT_MASK:
            try:
                _id, mx, my, _mz, bstate = curses.getmouse()
            except curses.error:
                continue
            if not (bstate & _MOUSE_LEFT_MASK):
                continue
            if my in (1, 2):
                for start_x, end_x, tab in tab_segments:
                    if start_x <= mx < end_x:
                        active_tab = tab
                        break
                continue
            idx = line_to_idx.get(my)
            if idx is not None:
                current_tab_indices = indices_by_tab[active_tab]
                if idx in current_tab_indices:
                    cursor_pos_by_tab[active_tab] = current_tab_indices.index(idx)
                selected[idx] = not selected[idx]
                modified.add(idx)


def _is_research_csv(fieldnames: list[str]) -> bool:
    required = {"bucket", "full_name", "phone_e164", "top_title_company_pairs"}
    return required.issubset(set(fieldnames or []))


def _review_research_csv(file_path: str, batch_size: int, initial_tab: str) -> None:
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

    had_exclude_column = "exclude" in fieldnames
    if not had_exclude_column:
        fieldnames.append("exclude")

    initial_selected: list[bool] = []
    for row in rows:
        exclude_raw = (row.data.get("exclude") or "").strip().lower()
        if exclude_raw in {"yes", "true", "1"}:
            initial_selected.append(False)
            continue
        if exclude_raw in {"no", "false", "0"}:
            initial_selected.append(True)
            continue
        # No explicit user decision (blank or missing exclude). Default by
        # bucket: confident/yes rows are pre-selected, maybe/review rows are
        # not. This used to default-select everything when the column existed,
        # which combined with the destructive save to silently rewrite untouched
        # rows to bucket=no on Enter. Since the new save logic only commits
        # rows the user actually touched, defaulting by bucket is both safer
        # and more consistent with the first-open behavior.
        initial_selected.append(_bucket_label(row.data.get("bucket", "")) == "yes")

    rows.sort(key=_research_sort_key)
    saved = curses.wrapper(
        _run_research_tui,
        rows,
        max(1, batch_size),
        _normalize_tab(initial_tab),
        initial_selected,
    )

    if not saved:
        console.print("[dim]Cancelled — no changes made[/dim]")
        return

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for r in rows:
            out = {k: r.data.get(k, "") for k in fieldnames}
            writer.writerow(out)

    # Compute what the server will see on upload, mirroring its yes/maybe/no
    # accounting:
    #   exclude=yes  → no
    #   exclude=no   → yes (explicit user include)
    #   blank        → bucket default (confident=yes, medium=maybe, review=no)
    counts = {"yes": 0, "maybe": 0, "no": 0}
    explicit_include = 0
    explicit_exclude = 0
    for r in rows:
        ex = (r.data.get("exclude") or "").strip().lower()
        if ex in {"yes", "true", "1"}:
            counts["no"] += 1
            explicit_exclude += 1
        elif ex in {"no", "false", "0"}:
            counts["yes"] += 1
            explicit_include += 1
        else:
            counts[_bucket_label(r.data.get("bucket", ""))] += 1
    console.print(
        f"\n[green bold]✅ Saved research review: yes={counts['yes']} "
        f"maybe={counts['maybe']} no={counts['no']}[/green bold]"
    )
    console.print(
        f"[dim]   ({explicit_include} explicitly included, "
        f"{explicit_exclude} explicitly excluded, "
        f"{len(rows) - explicit_include - explicit_exclude} using bucket default)[/dim]"
    )


def review_contacts(file_path: str = "contacts.csv", batch_size: int = 10, cli_tab: str = "yes") -> None:
    """Open the interactive review TUI and update the CSV with skip flags."""
    path = Path(file_path)
    if path.exists():
        with path.open(newline="") as f:
            reader = csv.DictReader(f)
            fieldnames = list(reader.fieldnames or [])
            base_research = {"bucket", "full_name", "phone_e164"}
            fields = set(fieldnames)
            if base_research.issubset(fields) and "top_title_company_pairs" not in fields:
                console.print(
                    "[red]Research CSV missing required column: top_title_company_pairs. "
                    "Regenerate from the latest pipeline output.[/red]"
                )
                return
            if _is_research_csv(fieldnames):
                _review_research_csv(file_path, batch_size=batch_size, initial_tab=cli_tab)
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
