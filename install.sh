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
    echo -e "${YELLOW}Homebrew not found. Installing...${NC}"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for this session
    if [[ -f /opt/homebrew/bin/brew ]]; then
        eval "$(/opt/homebrew/bin/brew shellenv)"
    elif [[ -f /usr/local/bin/brew ]]; then
        eval "$(/usr/local/bin/brew shellenv)"
    fi
    if ! command -v brew &>/dev/null; then
        echo -e "${RED}Homebrew installation failed. Please install manually and re-run.${NC}"
        exit 1
    fi
    echo -e "${GREEN}ok${NC} Homebrew installed"
else
    echo -e "${GREEN}ok${NC} Homebrew"
fi

# 2. Check/install Docker (required for WhatsApp)
if command -v docker &>/dev/null; then
    echo -e "${GREEN}ok${NC} Docker"
else
    echo ""
    echo -e "${YELLOW}Docker is not installed.${NC}"
    echo "Docker is required for WhatsApp extraction (iMessage works without it)."
    echo ""
    read -p "Install Docker Desktop via Homebrew? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${DIM}Installing Docker Desktop...${NC}"
        brew install --cask docker
        echo -e "${GREEN}ok${NC} Docker Desktop installed"
        echo -e "${DIM}Open Docker Desktop from Applications to complete setup before using WhatsApp extraction.${NC}"
    else
        echo -e "${DIM}Skipped. Install later with: brew install --cask docker${NC}"
    fi
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
