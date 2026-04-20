"""Download or upload messages research review artifacts."""

from __future__ import annotations

import json
import shutil
import tempfile
import zipfile
from pathlib import Path

import requests
from rich.console import Console

from contact_exporter.auth.credentials import get_auth_header
from contact_exporter.config import get_api_base_url

console = Console()


def _request(method: str, path: str, **kwargs) -> requests.Response:
    base = get_api_base_url().rstrip("/")
    headers = kwargs.pop("headers", {})
    merged = {**get_auth_header(), **headers}
    resp = requests.request(
        method,
        f"{base}{path}",
        headers=merged,
        timeout=120,
        **kwargs,
    )
    if resp.status_code == 401:
        raise SystemExit("Authentication expired. Run: contact-exporter login")
    return resp


def upload_research_review(csv_path: str) -> None:
    path = Path(csv_path)
    if not path.exists():
        raise SystemExit(f"File not found: {csv_path}")

    with path.open("rb") as f:
        resp = _request(
            "POST",
            "/v2/messages-research/artifacts",
            files={"file": (path.name, f, "text/csv")},
        )

    if resp.status_code != 200:
        raise SystemExit(f"Upload failed ({resp.status_code}): {resp.text[:300]}")

    data = resp.json()
    console.print("[green bold]✅ Messages review uploaded[/green bold]")
    console.print(f"  artifact_id: {data['artifact_id']}")
    console.print(f"  total: {data['total_count']}")
    console.print(f"  yes: {data['yes_count']}")
    console.print(f"  maybe: {data['maybe_count']}")
    console.print(f"  no: {data['no_count']}")


def download_research_review(
    artifact_id: str | None = None,
    output_dir: str | None = None,
) -> None:
    if artifact_id:
        meta_resp = _request("GET", f"/v2/messages-research/artifacts/{artifact_id}")
    else:
        meta_resp = _request("GET", "/v2/messages-research/artifacts/latest")

    if meta_resp.status_code == 404:
        console.print("[yellow]Nothing to review.[/yellow]")
        return
    if meta_resp.status_code != 200:
        raise SystemExit(f"Review lookup failed ({meta_resp.status_code}): {meta_resp.text[:300]}")

    meta = meta_resp.json()
    artifact_id = meta["id"]
    download_resp = _request("GET", f"/v2/messages-research/artifacts/{artifact_id}/download")
    if download_resp.status_code == 404:
        console.print("[yellow]Nothing to review.[/yellow]")
        return
    if download_resp.status_code != 200:
        raise SystemExit(f"Download failed ({download_resp.status_code}): {download_resp.text[:300]}")

    target_dir = Path(output_dir) if output_dir else Path.cwd()
    target_dir.mkdir(parents=True, exist_ok=True)

    with tempfile.NamedTemporaryFile(suffix=".zip", delete=False) as tmp:
        tmp.write(download_resp.content)
        tmp_path = Path(tmp.name)

    try:
        with zipfile.ZipFile(tmp_path) as zf:
            for member in zf.infolist():
                if member.is_dir():
                    continue
                destination = target_dir / member.filename
                destination.parent.mkdir(parents=True, exist_ok=True)
                with zf.open(member) as src, destination.open("wb") as dst:
                    shutil.copyfileobj(src, dst)
    finally:
        tmp_path.unlink(missing_ok=True)

    console.print("[green bold]✅ Messages review ready[/green bold]")
    console.print(f"  artifact_id: {artifact_id}")
    console.print(f"  output_dir: {target_dir}")
    console.print(f"  total: {meta['total_count']}")
    console.print(f"  yes: {meta['yes_count']}")
    console.print(f"  maybe: {meta['maybe_count']}")
    console.print(f"  no: {meta['no_count']}")
