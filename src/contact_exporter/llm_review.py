"""LLM-powered contact review via OpenRouter.

Sends contacts in batches to a cheap/fast LLM to decide which ones are
worth enriching (real professional relationships) vs which should be
skipped (service providers, nicknames, placeholder names, etc.).

Privacy: OpenRouter does not log prompts/completions by default.
"""

from __future__ import annotations

import csv
import json
import time
from datetime import datetime, timezone
from pathlib import Path

import requests
from rich.console import Console
from rich.progress import BarColumn, Progress, SpinnerColumn, TextColumn
from rich.table import Table

from contact_exporter.models import CSV_HEADERS, Contact

console = Console()

OPENROUTER_BASE = "https://openrouter.ai/api/v1"
DEFAULT_MODEL = "openai/gpt-4.1-mini"
BATCH_SIZE = 40

# Pricing per 1M tokens (USD) — for cost estimates
_MODEL_PRICING = {
    "openai/gpt-4.1-mini": {"input": 0.40, "output": 1.60},
    "openai/gpt-4.1-nano": {"input": 0.10, "output": 0.40},
    "anthropic/claude-haiku-4-5": {"input": 0.80, "output": 4.00},
}

_REVIEW_PROMPT = """\
You are evaluating phone contacts to determine which ones represent real \
professional relationships worth looking up on LinkedIn.

For each contact, decide: ENRICH or SKIP.

Consider these factors (in priority order):
- **Name quality & notability**: Use your training data and world knowledge \
to actively identify whether a name matches or resembles known public figures, \
business leaders, prominent families, or well-known professionals. If you \
recognize the name or surname from business, tech, politics, entertainment, \
finance, or any professional domain — say so in the reason and ENRICH. Even \
partial recognition counts — if the name *could* belong to someone notable, \
ENRICH and explain the possible association. The user's phone contacts are \
already filtered to people they actually know.
- **Message volume**: Higher message counts suggest a real relationship \
(max is 200 = very active)
- **Recency**: Recently contacted people are more valuable, but a recognizable \
full name can override low recency
- **Skip patterns**: Skip entries that are:
  - Service providers, businesses, or roles rather than people \
(e.g., "Plumber", "HVAC", "Vet", "Groomer")
  - Clearly placeholder, coded, or non-standard naming patterns that wouldn't \
map to a real LinkedIn profile (e.g., numbered labels, pet names, inside jokes, \
private aliases for informal relationships)
  - Just a single first name with no last name AND zero message count \
(impossible to look up)
- **Group chat only**: If someone ONLY appears in group chats with low \
individual message count, they're less valuable

Be optimistic — these are real phone contacts, not random leads. When in doubt \
about a full name, ALWAYS lean ENRICH. Only SKIP names that clearly cannot map \
to a LinkedIn profile.

Contacts to evaluate:
{contacts_json}

Respond with a JSON object containing a "results" array, one entry per contact, \
in the same order:
{{"results": [{{"idx": 0, "name": "...", "verdict": "ENRICH" or "SKIP", \
"reason": "15 words max explaining why"}}]}}

Return ONLY the JSON object, no other text."""


def _load_contacts_for_review(csv_path: str) -> list[dict]:
    """Load contacts from CSV, returning only those with names."""
    contacts = []
    path = Path(csv_path)
    if not path.exists():
        console.print(f"[red]File not found: {csv_path}[/red]")
        raise SystemExit(1)

    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("name") or "").strip()
            if not name:
                continue
            contacts.append({
                "phone": row.get("phone", ""),
                "name": name,
                "source": row.get("source", ""),
                "is_in_group_chats": row.get("is_in_group_chats", "false"),
                "message_count": int(row["message_count"]) if row.get("message_count") else 0,
                "last_message": row.get("last_message", ""),
                "skip": row.get("skip", ""),
            })
    return contacts


def _format_recency(last_message: str) -> str:
    """Convert ISO timestamp to human-readable recency."""
    if not last_message:
        return "unknown"
    try:
        dt = datetime.fromisoformat(last_message.replace("Z", "+00:00"))
        days_ago = (datetime.now(timezone.utc) - dt).days
        if days_ago == 0:
            return "today"
        if days_ago == 1:
            return "yesterday"
        if days_ago < 30:
            return f"{days_ago} days ago"
        if days_ago < 365:
            return f"{days_ago // 30} months ago"
        return f"{days_ago // 365}y ago"
    except (ValueError, TypeError):
        return "unknown"


def _build_batch_payload(batch: list[dict]) -> list[dict]:
    """Build the contact list for the LLM prompt."""
    payload = []
    for idx, c in enumerate(batch):
        payload.append({
            "idx": idx,
            "name": c["name"],
            "source": c["source"],
            "message_count": c["message_count"],
            "last_contacted": _format_recency(c["last_message"]),
            "in_group_chats": c.get("is_in_group_chats", "false"),
        })
    return payload


def _call_openrouter(
    api_key: str,
    contacts_json: str,
    model: str = DEFAULT_MODEL,
) -> tuple[list[dict], int, int]:
    """Call OpenRouter and return (results, input_tokens, output_tokens)."""
    prompt = _REVIEW_PROMPT.format(contacts_json=contacts_json)

    resp = requests.post(
        f"{OPENROUTER_BASE}/chat/completions",
        headers={
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
        },
        json={
            "model": model,
            "messages": [{"role": "user", "content": prompt}],
            "response_format": {"type": "json_object"},
        },
        timeout=120,
    )

    if resp.status_code == 402:
        console.print("[red]API key has insufficient credits or hit spending limit.[/red]")
        raise SystemExit(1)
    if resp.status_code == 401:
        console.print("[red]Invalid API key. Check your OpenRouter key.[/red]")
        raise SystemExit(1)
    if resp.status_code != 200:
        console.print(f"[red]OpenRouter error: {resp.status_code} {resp.text[:200]}[/red]")
        raise SystemExit(1)

    data = resp.json()
    usage = data.get("usage", {})
    input_tokens = usage.get("prompt_tokens", 0)
    output_tokens = usage.get("completion_tokens", 0)

    raw_content = data["choices"][0]["message"]["content"].strip()
    try:
        parsed = json.loads(raw_content)
        if isinstance(parsed, dict):
            for key in ("results", "contacts", "evaluations", "data"):
                if key in parsed and isinstance(parsed[key], list):
                    return parsed[key], input_tokens, output_tokens
            # Grab first list value
            for v in parsed.values():
                if isinstance(v, list):
                    return v, input_tokens, output_tokens
        elif isinstance(parsed, list):
            return parsed, input_tokens, output_tokens
    except json.JSONDecodeError as e:
        console.print(f"[yellow]Warning: JSON parse error: {e}[/yellow]")
        console.print(f"[dim]Raw response: {raw_content[:300]}[/dim]")

    return [], input_tokens, output_tokens


def _estimate_cost(contacts: list[dict], model: str = DEFAULT_MODEL) -> float:
    """Estimate total cost in USD for reviewing contacts."""
    pricing = _MODEL_PRICING.get(model, {"input": 2.0, "output": 8.0})
    total_input_tokens = 0
    total_output_tokens = 0

    for i in range(0, len(contacts), BATCH_SIZE):
        batch = contacts[i:i + BATCH_SIZE]
        payload = _build_batch_payload(batch)
        prompt = _REVIEW_PROMPT.format(contacts_json=json.dumps(payload, indent=2))
        # ~4 chars per token
        total_input_tokens += len(prompt) // 4
        # Output estimate: ~50 tokens per contact
        total_output_tokens += len(batch) * 50

    input_cost = (total_input_tokens / 1_000_000) * pricing["input"]
    output_cost = (total_output_tokens / 1_000_000) * pricing["output"]
    return input_cost + output_cost


def _update_csv_with_verdicts(csv_path: str, verdicts: dict[str, str]) -> int:
    """Update the skip column in the CSV based on LLM verdicts.

    Returns the number of contacts marked as skip.
    """
    path = Path(csv_path)
    rows = []
    with path.open(newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            phone = row.get("phone", "")
            if phone in verdicts and verdicts[phone] == "SKIP":
                row["skip"] = "yes"
            rows.append(row)

    with path.open("w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        writer.writeheader()
        writer.writerows(rows)

    return sum(1 for v in verdicts.values() if v == "SKIP")


def review_contacts_llm(
    csv_path: str = "contacts.csv",
    api_key: str | None = None,
    model: str = DEFAULT_MODEL,
    dry_run: bool = False,
) -> None:
    """Run LLM review on contacts CSV.

    Marks contacts as skip=yes if the LLM determines they're not worth
    enriching (service providers, nicknames, placeholder names, etc.).
    """
    import os
    api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
    if not api_key and not dry_run:
        console.print("[red]No API key provided.[/red]")
        console.print()
        console.print("Provide a key via one of:")
        console.print("  [cyan]contact-exporter llm-review --api-key sk-or-...[/cyan]")
        console.print("  [cyan]export OPENROUTER_API_KEY=sk-or-...[/cyan]")
        raise SystemExit(1)

    contacts = _load_contacts_for_review(csv_path)
    if not contacts:
        console.print("[yellow]No named contacts found in CSV.[/yellow]")
        return

    total_batches = (len(contacts) + BATCH_SIZE - 1) // BATCH_SIZE
    est_cost = _estimate_cost(contacts, model)

    console.print(f"[bold]LLM Contact Review[/bold]")
    console.print(f"[dim]Model: {model}[/dim]")
    console.print(f"[dim]Contacts with names: {len(contacts)}[/dim]")
    console.print(f"[dim]Batches: {total_batches}[/dim]")
    console.print(f"[dim]Estimated cost: ${est_cost:.4f}[/dim]")
    console.print()

    if dry_run:
        console.print("[yellow]Dry run — no API calls made.[/yellow]")
        return

    # Process batches
    all_verdicts: dict[str, str] = {}
    all_reasons: dict[str, str] = {}
    total_input = 0
    total_output = 0

    with Progress(
        SpinnerColumn(),
        TextColumn("[progress.description]{task.description}"),
        BarColumn(),
        TextColumn("{task.completed}/{task.total}"),
        console=console,
    ) as progress:
        task = progress.add_task("Reviewing contacts", total=total_batches)

        for i in range(0, len(contacts), BATCH_SIZE):
            batch = contacts[i:i + BATCH_SIZE]
            payload = _build_batch_payload(batch)

            results, inp_tokens, out_tokens = _call_openrouter(
                api_key,
                json.dumps(payload, indent=2),
                model=model,
            )
            total_input += inp_tokens
            total_output += out_tokens

            for result in results:
                idx = result.get("idx", -1)
                if 0 <= idx < len(batch):
                    phone = batch[idx]["phone"]
                    verdict = result.get("verdict", "UNKNOWN")
                    all_verdicts[phone] = verdict
                    all_reasons[phone] = result.get("reason", "")

            progress.advance(task)

    # Update CSV
    skipped = _update_csv_with_verdicts(csv_path, all_verdicts)

    # Summary
    enrich_count = sum(1 for v in all_verdicts.values() if v == "ENRICH")
    skip_count = sum(1 for v in all_verdicts.values() if v == "SKIP")

    console.print()

    # Show results table
    table = Table(title="Review Results", show_lines=False)
    table.add_column("Name", style="bold", max_width=30)
    table.add_column("Verdict", justify="center")
    table.add_column("Msgs", justify="right")
    table.add_column("Reason", style="dim", max_width=40)

    for c in sorted(contacts, key=lambda x: (all_verdicts.get(x["phone"], ""), -x["message_count"])):
        phone = c["phone"]
        verdict = all_verdicts.get(phone, "?")
        style = "green" if verdict == "ENRICH" else "red" if verdict == "SKIP" else "yellow"
        table.add_row(
            c["name"],
            f"[{style}]{verdict}[/{style}]",
            str(c["message_count"] or ""),
            all_reasons.get(phone, ""),
        )

    console.print(table)

    # Cost summary
    pricing = _MODEL_PRICING.get(model, {"input": 2.0, "output": 8.0})
    input_cost = (total_input / 1_000_000) * pricing["input"]
    output_cost = (total_output / 1_000_000) * pricing["output"]
    total_cost = input_cost + output_cost

    console.print()
    console.print(f"[green bold]✅ {enrich_count} contacts to enrich, {skip_count} skipped[/green bold]")
    console.print(f"[dim]Updated {csv_path} — skipped contacts marked with skip=yes[/dim]")
    console.print(f"[dim]Cost: ${total_cost:.4f} ({total_input + total_output:,} tokens)[/dim]")
