#!/bin/bash
# Contact Exporter -- installer
#
# SECURE USAGE (recommended -- download, inspect, then run):
#   curl -fsSL https://raw.githubusercontent.com/powerset-co/contact-exporter/main/install.sh -o install.sh
#   less install.sh
#   bash install.sh
#
# ONE-LINER (downloads to temp file first -- never pipes directly to shell):
#   curl -fsSL https://raw.githubusercontent.com/powerset-co/contact-exporter/main/install.sh -o /tmp/ce-install.sh && bash /tmp/ce-install.sh
#
set -euo pipefail

BOLD='\033[1m'
DIM='\033[2m'
GREEN='\033[0;32m'
YELLOW='\033[0;33m'
RED='\033[0;31m'
NC='\033[0m'

# ---------------------------------------------------------------------------
# Safety checks
# ---------------------------------------------------------------------------

# Detect if script is being piped directly to shell (curl | bash)
# This is a security risk -- the download could be truncated mid-stream,
# causing partial script execution with unpredictable results.
if [ ! -t 0 ] && [ -z "${CONTACT_EXPORTER_ALLOW_PIPE:-}" ]; then
    echo ""
    echo -e "${RED}${BOLD}ERROR: Do not pipe this script directly to a shell.${NC}"
    echo ""
    echo "This protects you from truncated-download attacks (MITM / partial transfer)."
    echo ""
    echo "Instead, download first and then run:"
    echo ""
    echo -e "  ${BOLD}curl -fsSL https://raw.githubusercontent.com/powerset-co/contact-exporter/main/install.sh -o /tmp/ce-install.sh${NC}"
    echo -e "  ${BOLD}bash /tmp/ce-install.sh${NC}"
    echo ""
    exit 1
fi

# macOS only
if [[ "$(uname -s)" != "Darwin" ]]; then
    echo -e "${RED}This installer is for macOS only.${NC}"
    echo "contact-exporter reads iMessage and macOS Contacts, which are macOS-specific."
    exit 1
fi

echo ""
echo -e "${BOLD}Contact Exporter Installer${NC}"
echo -e "${DIM}Extract iMessage & WhatsApp contacts locally${NC}"
echo ""

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

# 1. Check/install Homebrew
if ! command -v brew &>/dev/null; then
    echo -e "${YELLOW}Homebrew is required but not installed.${NC}"
    echo ""
    echo "Install it first:"
    echo -e "  ${BOLD}/bin/bash -c \"\$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)\"${NC}"
    echo ""
    echo "Then re-run this installer."
    exit 1
else
    echo -e "${GREEN}ok${NC} Homebrew"
fi

# 2. Check Docker (required for WhatsApp, manual install only)
if command -v docker &>/dev/null; then
    echo -e "${GREEN}ok${NC} Docker"
else
    echo -e "${YELLOW}!!${NC} Docker not found (needed for WhatsApp extraction)"
    echo -e "  ${DIM}Install manually: brew install --cask docker${NC}"
    echo -e "  ${DIM}iMessage extraction works without Docker${NC}"
fi

# ---------------------------------------------------------------------------
# Install contact-exporter
# ---------------------------------------------------------------------------

echo ""
echo -e "${DIM}Installing contact-exporter via Homebrew...${NC}"
brew install powerset-co/powerset/contact-exporter

echo -e "${GREEN}ok${NC} contact-exporter + imsg"

echo ""
echo -e "${GREEN}${BOLD}Installed!${NC}"
echo ""
echo -e "  ${BOLD}contact-exporter login${NC}      Authenticate with Powerset"
echo -e "  ${BOLD}contact-exporter imessage${NC}   Extract iMessage contacts"
echo -e "  ${BOLD}contact-exporter whatsapp${NC}   Extract WhatsApp contacts"
echo -e "  ${BOLD}contact-exporter upload${NC}     Upload to Powerset"
echo ""
echo -e "  ${DIM}Docs: https://github.com/powerset-co/contact-exporter${NC}"
echo ""
