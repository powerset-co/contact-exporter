"""Unit tests for contact-exporter core logic.

Tests phone normalization, contact name cleaning, JID parsing,
timestamp handling, merge logic, CSV round-trips, QR rendering,
version consistency, and install script syntax.
"""

import csv
import io
import os
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

# ---------------------------------------------------------------------------
# Setup: ensure src/ is importable
# ---------------------------------------------------------------------------
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from contact_exporter import __version__
from contact_exporter.models import (
    CSV_HEADERS,
    Contact,
    canonicalize_phone,
    is_emoji_only_name,
    should_auto_skip,
)
from contact_exporter.merge import merge_contact, write_contacts, load_existing_contacts
from contact_exporter.imessage.extract import (
    _normalize_phone,
    _clean_contact_name,
    _add_phone_to_lookup,
)
from contact_exporter.whatsapp.extract import (
    _extract_jid,
    _jid_to_phone,
    _parse_timestamp,
    _render_qr_to_terminal,
)
from contact_exporter.matching import _candidate_name, apply_local_name_matching, PowersetCandidate
from contact_exporter.llm_review import _load_contacts_for_review

passed = 0
failed = 0


def check(name: str, condition: bool, detail: str = ""):
    global passed, failed
    if condition:
        passed += 1
        print(f"  ✅ {name}")
    else:
        failed += 1
        print(f"  ❌ {name}{f' — {detail}' if detail else ''}")


# ===================================================================
# 1. Phone normalization (iMessage)
# ===================================================================
print("\n📞 Phone normalization")

check("US 10-digit", _normalize_phone("(408) 835-4285") == "4088354285")
check("US +1 prefix", _normalize_phone("+14085354285") == "4085354285")
check("US 11-digit", _normalize_phone("14085354285") == "4085354285")
check("already 10", _normalize_phone("4085354285") == "4085354285")
check("intl keeps digits", _normalize_phone("+447911123456") == "447911123456")
check("strips dashes/spaces", _normalize_phone("408-535-4285") == "4085354285")
check("empty string", _normalize_phone("") == "")
check("short number", _normalize_phone("911") == "911")

check("canonicalize: US 10-digit", canonicalize_phone("4085354285") == "+14085354285")
check("canonicalize: keeps +", canonicalize_phone("+447911123456") == "+447911123456")
check("canonicalize: intl digits => +", canonicalize_phone("447911123456") == "+447911123456")
check("canonicalize: too short empty", canonicalize_phone("123") == "")


# ===================================================================
# 2. Contact name cleaning
# ===================================================================
print("\n👤 Contact name cleaning")

check("first + last", _clean_contact_name("John", "Doe") == "John Doe")
check("first only", _clean_contact_name("John", "") == "John")
check("last only", _clean_contact_name("", "Doe") == "Doe")
check("both empty", _clean_contact_name("", "") == "")
check("sync suffix /1", _clean_contact_name("Joy/1", "Chen") == "Joy Chen")
check("sync suffix /23", _clean_contact_name("Joy/23", "") == "Joy")
check("Last;First format", _clean_contact_name("Li;David", "") == "David Li")
check("semicolon in full", _clean_contact_name("", "Chao;Joy") == "Joy Chao")
check("whitespace trim", _clean_contact_name("  John  ", "  Doe  ") == "John Doe")


# ===================================================================
# 3. Phone-to-lookup helper
# ===================================================================
print("\n🔗 Phone-to-lookup mapping")

lookup: dict[str, str] = {}
_add_phone_to_lookup(lookup, "+14085354285", "Jake Z")
check("normalized key exists", "4085354285" in lookup)
check("full digits key exists", "14085354285" in lookup)
check("value correct", lookup.get("4085354285") == "Jake Z")

lookup2: dict[str, str] = {}
_add_phone_to_lookup(lookup2, "123", "Short")
check("short number rejected", len(lookup2) == 0)

lookup3: dict[str, str] = {}
_add_phone_to_lookup(lookup3, "+14085354285", "   ")
check("blank name rejected", len(lookup3) == 0)


# ===================================================================
# 3b. Candidate name preference
# ===================================================================
print("\n🧾 Candidate name preference")

check(
    "display suffix prefers first+last",
    _candidate_name(
        {
            "display_name": "Alex (via Google Sheets)",
            "first_name": "Alex",
            "last_name": "Oppenheimer",
            "public_identifier": "alex-oppenheimer",
        }
    ) == "Alex Oppenheimer",
)
check(
    "display differs but first+last wins",
    _candidate_name(
        {
            "display_name": "Alex (Some Source Label)",
            "first_name": "Alex",
            "last_name": "Oppenheimer",
            "public_identifier": "alex-oppenheimer",
        }
    ) == "Alex Oppenheimer",
)
check(
    "normal display name preserved",
    _candidate_name(
        {
            "display_name": "Alex Oppenheimer",
            "first_name": "Alex",
            "last_name": "Oppenheimer",
            "public_identifier": "alex-oppenheimer",
        }
    ) == "Alex Oppenheimer",
)

# Rows already matched to a person should not enter default review queue.
with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f_review:
    review_csv = f_review.name
try:
    with open(review_csv, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=CSV_HEADERS)
        w.writeheader()
        w.writerow({
            "phone": "+14155550000",
            "name": "Matched Person",
            "source": "imessage",
            "is_in_group_chats": "false",
            "message_count": "3",
            "last_message": "",
            "skip": "",
            "match_status": "unmatched",  # stale/incorrect status
            "matched_person_id": "11111111-1111-1111-1111-111111111111",
            "matched_name": "Matched Person",
            "matched_linkedin_url": "",
            "match_confidence": "1.0",
            "match_method": "name_exact_linkedin",
            "match_reason": "Unique exact match",
        })
        w.writerow({
            "phone": "+14155550001",
            "name": "Needs Review",
            "source": "imessage",
            "is_in_group_chats": "false",
            "message_count": "1",
            "last_message": "",
            "skip": "",
            "match_status": "unmatched",
            "matched_person_id": "",
            "matched_name": "",
            "matched_linkedin_url": "",
            "match_confidence": "",
            "match_method": "unmatched",
            "match_reason": "No candidate",
        })
    queued = _load_contacts_for_review(review_csv, include_matched=False)
    check("review excludes rows with matched_person_id", len(queued) == 1, f"got {len(queued)}")
    check("review keeps unresolved rows", queued and queued[0]["name"] == "Needs Review")
finally:
    os.unlink(review_csv)

# Prefix + last-name match should beat generic fuzzy same-last-name.
prefix_contacts = {
    "+14150000000": Contact(phone="+14150000000", name="Amir Moazami", source="imessage"),
}
prefix_candidates = [
    PowersetCandidate(id="amirteymour", name="Amirteymour Moazami"),
    PowersetCandidate(id="yasmine", name="Yasmine Moazami"),
    PowersetCandidate(id="mohsen", name="Mohsen Moazami"),
]
prefix_stats = apply_local_name_matching(prefix_contacts, prefix_candidates)
pref = prefix_contacts["+14150000000"]
check("prefix+lastname promotes matched", pref.match_status == "matched", pref.match_status or "")
check("prefix+lastname picks Amirteymour", pref.matched_person_id == "amirteymour", pref.matched_person_id or "")
check(
    "prefix+lastname method tagged",
    pref.match_method == "name_prefix_lastname_linkedin",
    pref.match_method or "",
)
check("prefix+lastname counted matched", prefix_stats.get("matched", 0) == 1, str(prefix_stats))


# ===================================================================
# 4. WhatsApp JID parsing
# ===================================================================
print("\n💬 WhatsApp JID parsing")

check("string JID", _extract_jid("14155551234@c.us") == "14155551234@c.us")
check("dict JID serialized", _extract_jid({"_serialized": "14155551234@c.us"}) == "14155551234@c.us")
check("dict JID user fallback", _extract_jid({"user": "14155551234"}) == "14155551234")
check("empty dict", _extract_jid({}) == "")

check("c.us -> phone", _jid_to_phone("14155551234@c.us") == "+14155551234")
check("group JID -> None", _jid_to_phone("1234567890@g.us") is None)
check("lid JID -> None", _jid_to_phone("abcdef@lid") is None)
check("empty -> None", _jid_to_phone("") is None)
check("no @ -> None", _jid_to_phone("14155551234") is None)
check("too short -> None", _jid_to_phone("123@c.us") is None)
check("too long -> None", _jid_to_phone("1234567890123456@c.us") is None)
check("7 digits OK", _jid_to_phone("1234567@c.us") == "+1234567")
check("15 digits OK", _jid_to_phone("123456789012345@c.us") == "+123456789012345")


# ===================================================================
# 5. Timestamp parsing
# ===================================================================
print("\n⏰ Timestamp parsing")

check("unix seconds", _parse_timestamp(1700000000) is not None)
check("unix millis", _parse_timestamp(1700000000000) is not None)
ts_sec = _parse_timestamp(1700000000)
ts_ms = _parse_timestamp(1700000000000)
check("sec == ms", ts_sec == ts_ms, f"{ts_sec} vs {ts_ms}")
check("None input", _parse_timestamp(None) is None)
check("zero input", _parse_timestamp(0) is None)
check("string passthrough", _parse_timestamp("2024-01-01T00:00:00Z") == "2024-01-01T00:00:00Z")


# ===================================================================
# 6. Contact model CSV round-trip
# ===================================================================
print("\n📄 Contact CSV round-trip")

c1 = Contact(
    phone="+14155551234",
    name="Test User",
    source="imessage",
    is_in_group_chats=True,
    message_count=42,
    last_message="2024-01-15T12:00:00Z",
    skip=False,
)
row = c1.to_csv_row()
check("row length", len(row) == len(CSV_HEADERS), f"{len(row)} vs {len(CSV_HEADERS)}")

# Round-trip through DictReader
buf = io.StringIO()
writer = csv.writer(buf)
writer.writerow(CSV_HEADERS)
writer.writerow(row)
buf.seek(0)
reader = csv.DictReader(buf)
c2 = Contact.from_csv_row(next(reader))
check("phone preserved", c2.phone == c1.phone)
check("name preserved", c2.name == c1.name)
check("source preserved", c2.source == c1.source)
check("group flag preserved", c2.is_in_group_chats == c1.is_in_group_chats)
check("msg count preserved", c2.message_count == c1.message_count)
check("last_message preserved", c2.last_message == c1.last_message)
check("skip preserved", c2.skip == c1.skip)

# Edge: empty optional fields
c3 = Contact(phone="+1", name="", source="whatsapp")
row3 = c3.to_csv_row()
buf3 = io.StringIO()
w3 = csv.writer(buf3)
w3.writerow(CSV_HEADERS)
w3.writerow(row3)
buf3.seek(0)
c4 = Contact.from_csv_row(next(csv.DictReader(buf3)))
check("None msg_count round-trips", c4.message_count is None)
check("None last_message round-trips", c4.last_message is None)
check("skip=False round-trips", c4.skip is False)


# ===================================================================
# 6b. Auto-skip rules
# ===================================================================
print("\n🚫 Auto-skip rules")

check("emoji-only name detected", is_emoji_only_name("❤️") is True)
check("letters + emoji is not emoji-only", is_emoji_only_name("Bob ❤️") is False)
check("empty + 0 messages auto-skip", should_auto_skip(Contact(phone="+1", name="", source="imessage", message_count=None)) is True)
check("named + 0 messages not auto-skip", should_auto_skip(Contact(phone="+1", name="Bob", source="imessage", message_count=0)) is False)
check("emoji-only auto-skip", should_auto_skip(Contact(phone="+1", name="😀", source="imessage", message_count=50)) is True)


# ===================================================================
# 7. Merge logic
# ===================================================================
print("\n🔀 Contact merge")

a = Contact(phone="+1", name="Alice", source="imessage", message_count=10,
            last_message="2024-01-01T00:00:00Z", is_in_group_chats=False, skip=True)
b = Contact(phone="+1", name="", source="whatsapp", message_count=20,
            last_message="2024-06-01T00:00:00Z", is_in_group_chats=True)

merged = merge_contact(a, b)
check("name: first non-empty wins", merged.name == "Alice")
check("source: combined", merged.source == "imessage,whatsapp")
check("msg_count: max wins", merged.message_count == 20)
check("last_message: newer wins", merged.last_message == "2024-06-01T00:00:00Z")
check("group: OR", merged.is_in_group_chats is True)
check("skip: preserved from existing", merged.skip is True)

# Merge with None message counts
c_none = Contact(phone="+2", name="Bob", source="imessage", message_count=None)
d_none = Contact(phone="+2", name="Bob", source="whatsapp", message_count=None)
merged_none = merge_contact(c_none, d_none)
check("None + None = None", merged_none.message_count is None)

c_zero = Contact(phone="+3", name="", source="imessage", message_count=0)
d_five = Contact(phone="+3", name="", source="whatsapp", message_count=5)
merged_zero = merge_contact(c_zero, d_five)
check("0 + 5 = 5", merged_zero.message_count == 5)


# ===================================================================
# 8. Write contacts (sorting + limit)
# ===================================================================
print("\n📝 Write contacts (sort + limit)")

import tempfile
import os

contacts = {
    f"+14155550{i:02d}": Contact(phone=f"+14155550{i:02d}", name=f"User{i}", source="imessage",
                      message_count=i)
    for i in range(1, 11)
}
with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
    tmp_path = f.name

try:
    written = write_contacts(contacts, tmp_path, limit=5)
    check("limit respected", written == 5)

    loaded = load_existing_contacts(tmp_path)
    check("loaded count", len(loaded) == 5)

    counts = [c.message_count for c in loaded.values()]
    check("top 5 by msg_count", all(c >= 6 for c in counts if c is not None),
          f"got counts {counts}")
finally:
    os.unlink(tmp_path)

# Dedup and auto-skip during write
with tempfile.NamedTemporaryFile(mode="w", suffix=".csv", delete=False) as f:
    tmp_path2 = f.name

try:
    mixed = {
        "a": Contact(phone="+14155551234", name="Candela", source="imessage", message_count=10),
        "b": Contact(phone="14155551234", name="Candela Whatsapp", source="whatsapp", message_count=5),
        "c": Contact(phone="+14155550000", name="", source="imessage", message_count=0),
        "d": Contact(phone="+14155550001", name="❤️", source="whatsapp", message_count=2),
    }
    write_contacts(mixed, tmp_path2)
    loaded2 = load_existing_contacts(tmp_path2)
    check("dedupe +/non+ phone variants", len(loaded2) == 3, f"got {len(loaded2)} rows")
    merged_candela = loaded2.get("+14155551234")
    check("merged row exists", merged_candela is not None)
    check("sources merged after dedupe", merged_candela.source == "imessage,whatsapp", merged_candela.source if merged_candela else "")
    check("empty+0 marked skip", loaded2["+14155550000"].skip is True)
    check("emoji-only marked skip", loaded2["+14155550001"].skip is True)
finally:
    os.unlink(tmp_path2)


# ===================================================================
# 9. QR code rendering
# ===================================================================
print("\n📱 QR code rendering")

qr_text = _render_qr_to_terminal("https://example.com")
check("returns Text object", type(qr_text).__name__ == "Text")
plain = qr_text.plain
check("has content", len(plain) > 0)
check("has newlines (multiline)", "\n" in plain)
lines = plain.split("\n")
check("all lines same length", len(set(len(l) for l in lines)) == 1,
      f"line lengths: {set(len(l) for l in lines)}")
allowed_qr_chars = {" ", "█", "▀", "▄"}
check(
    "only QR block chars",
    all(c in allowed_qr_chars for c in plain.replace("\n", "")),
)


# ===================================================================
# 10. Version consistency
# ===================================================================
print("\n🏷️  Version consistency")

pyproject = ROOT / "pyproject.toml"
pyproject_text = pyproject.read_text()
check("pyproject.toml has version",
      f'version = "{__version__}"' in pyproject_text,
      f"__version__={__version__}")


# ===================================================================
# 11. Install script syntax check
# ===================================================================
print("\n🛠️  Install script")

result = subprocess.run(
    ["bash", "-n", str(ROOT / "install.sh")],
    capture_output=True, text=True,
)
check("install.sh valid bash syntax", result.returncode == 0,
      result.stderr.strip() if result.stderr else "")

# Check for CRLF line endings (known past issue)
install_bytes = (ROOT / "install.sh").read_bytes()
check("no CRLF line endings", b"\r\n" not in install_bytes)

# Check key sections exist
install_text = (ROOT / "install.sh").read_text()
check("has Xcode CLT check", "xcode-select" in install_text)
check("has Homebrew install", "brew install" in install_text)
check("has Docker install", "docker" in install_text)
check("has Rosetta check", "rosetta" in install_text.lower() or "arch -x86_64" in install_text)
check("has imsg install", "imsg" in install_text)
check("has permissions setup", "Privacy_AllFiles" in install_text)


# ===================================================================
# Summary
# ===================================================================
print(f"\n{'='*50}")
total = passed + failed
if failed == 0:
    print(f"🎉 All {passed} tests passed!")
else:
    print(f"💥 {failed}/{total} tests FAILED")
sys.exit(1 if failed else 0)
