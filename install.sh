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
# Architecture detection
# ---------------------------------------------------------------------------

ARCH=$(uname -m)
if [[ "$ARCH" == "arm64" ]]; then
    echo -e "${DIM}Detected Apple Silicon (arm64)${NC}"

    # Rosetta 2 is needed for the WhatsApp Docker container (WAHA is x86_64 only)
    if ! arch -x86_64 /usr/bin/true 2>/dev/null; then
        echo -e "${YELLOW}Installing Rosetta 2 (needed for WhatsApp Docker container)...${NC}"
        softwareupdate --install-rosetta --agree-to-license 2>/dev/null || true
        if arch -x86_64 /usr/bin/true 2>/dev/null; then
            echo -e "${GREEN}ok${NC} Rosetta 2"
        else
            echo -e "${YELLOW}Rosetta 2 install may have failed — WhatsApp extraction might not work${NC}"
            echo -e "${DIM}iMessage extraction will still work fine${NC}"
        fi
    else
        echo -e "${GREEN}ok${NC} Rosetta 2"
    fi
else
    echo -e "${DIM}Detected Intel (x86_64)${NC}"
fi

# ---------------------------------------------------------------------------
# Prerequisites
# ---------------------------------------------------------------------------

# 0. Xcode Command Line Tools (required for Homebrew and git)
if ! xcode-select -p &>/dev/null; then
    echo -e "${YELLOW}Xcode Command Line Tools not found. Installing...${NC}"
    echo -e "${DIM}This provides git, clang, and other dev tools that Homebrew needs.${NC}"

    # Trigger the install — this shows a macOS system dialog
    xcode-select --install 2>/dev/null || true

    echo ""
    echo -e "${BOLD}⏳ Waiting for Xcode CLT install to finish...${NC}"
    echo -e "${DIM}If you see a system dialog, click 'Install' and wait for it to complete.${NC}"
    echo ""

    # Wait up to 10 minutes for the install to complete
    DEADLINE=$((SECONDS + 600))
    while ! xcode-select -p &>/dev/null && [[ $SECONDS -lt $DEADLINE ]]; do
        sleep 5
    done

    if xcode-select -p &>/dev/null; then
        echo -e "${GREEN}ok${NC} Xcode Command Line Tools"
    else
        echo -e "${RED}Xcode CLT install timed out or was cancelled.${NC}"
        echo "Install manually with: xcode-select --install"
        echo "Then re-run this installer."
        exit 1
    fi
else
    echo -e "${GREEN}ok${NC} Xcode Command Line Tools"
fi

# 1. Check/install Homebrew
if ! command -v brew &>/dev/null; then
    echo -e "${YELLOW}Homebrew not found. Installing...${NC}"
    /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
    # Add brew to PATH for this session (Apple Silicon vs Intel)
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

        # Docker Desktop must be opened once to accept EULA and finish setup
        echo ""
        echo -e "${BOLD}Opening Docker Desktop for first-time setup...${NC}"
        echo -e "${DIM}Accept the terms and wait for Docker to start (whale icon in menu bar).${NC}"
        open -a Docker

        # Wait up to 2 minutes for Docker daemon to be ready
        echo -e "${DIM}Waiting for Docker to be ready...${NC}"
        DEADLINE=$((SECONDS + 120))
        while [[ $SECONDS -lt $DEADLINE ]]; do
            if docker info &>/dev/null 2>&1; then
                echo -e "${GREEN}ok${NC} Docker is running"
                break
            fi
            sleep 3
        done

        if ! docker info &>/dev/null 2>&1; then
            echo -e "${YELLOW}Docker isn't ready yet — it may still be starting.${NC}"
            echo -e "${DIM}Wait for the whale icon in your menu bar, then run: contact-exporter whatsapp${NC}"
        fi
    else
        echo -e "${DIM}Skipped. Install later with: brew install --cask docker${NC}"
    fi
fi

# ---------------------------------------------------------------------------
# Clean up conflicting installs (pipx, pip, old name)
# ---------------------------------------------------------------------------

# If contact-exporter or the old powerset-contacts was installed via pipx/pip,
# it shadows the Homebrew version on PATH and causes version confusion.
for OLD_PKG in contact-exporter powerset-contacts; do
    if command -v pipx &>/dev/null && pipx list 2>/dev/null | grep -q "$OLD_PKG"; then
        echo -e "${DIM}Removing old pipx install of ${OLD_PKG}...${NC}"
        pipx uninstall "$OLD_PKG" 2>/dev/null || true
    fi
done

# Also check if a non-Homebrew contact-exporter is on PATH
CE_EXISTING=$(command -v contact-exporter 2>/dev/null || true)
if [[ -n "$CE_EXISTING" ]] && [[ "$CE_EXISTING" != *"/homebrew/"* ]] && [[ "$CE_EXISTING" != *"/Cellar/"* ]] && [[ "$CE_EXISTING" != *"/usr/local/bin/"* ]]; then
    echo -e "${YELLOW}⚠️  Found non-Homebrew contact-exporter at: ${CE_EXISTING}${NC}"
    echo -e "${DIM}This may shadow the Homebrew version. Consider removing it.${NC}"
fi

# ---------------------------------------------------------------------------
# Install / upgrade contact-exporter + imsg
# ---------------------------------------------------------------------------

echo ""

# Ensure the tap is fresh
brew tap powerset-co/powerset 2>/dev/null || true
brew update --quiet 2>/dev/null || true

# Install or upgrade contact-exporter
if brew list powerset-co/powerset/contact-exporter &>/dev/null; then
    INSTALLED_VERSION=$(brew info --json=v2 powerset-co/powerset/contact-exporter 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(d['formulae'][0]['installed'][0]['version'])" 2>/dev/null || echo "unknown")
    LATEST_VERSION=$(brew info --json=v2 powerset-co/powerset/contact-exporter 2>/dev/null | \
        python3 -c "import sys,json; d=json.load(sys.stdin); print(d['formulae'][0]['versions']['stable'])" 2>/dev/null || echo "unknown")

    if [[ "$INSTALLED_VERSION" != "$LATEST_VERSION" ]]; then
        echo -e "${YELLOW}Upgrading contact-exporter ${INSTALLED_VERSION} → ${LATEST_VERSION}...${NC}"
        brew upgrade powerset-co/powerset/contact-exporter 2>/dev/null || brew reinstall powerset-co/powerset/contact-exporter
    else
        echo -e "${GREEN}ok${NC} contact-exporter ${INSTALLED_VERSION} (up to date)"
    fi
else
    echo -e "${DIM}Installing contact-exporter via Homebrew...${NC}"
    brew install powerset-co/powerset/contact-exporter
fi
echo -e "${GREEN}ok${NC} contact-exporter"

# Verify the Homebrew binary is what's on PATH
CE_ON_PATH=$(command -v contact-exporter 2>/dev/null || true)
if [[ -n "$CE_ON_PATH" ]] && [[ "$CE_ON_PATH" != *"/homebrew/"* ]] && [[ "$CE_ON_PATH" != *"/Cellar/"* ]] && [[ "$CE_ON_PATH" != *"/usr/local/bin/"* ]]; then
    echo ""
    echo -e "${YELLOW}⚠️  PATH conflict: ${CE_ON_PATH} is shadowing the Homebrew install${NC}"
    echo -e "  Expected: $(brew --prefix)/bin/contact-exporter"
    echo -e "  ${DIM}Fix: remove the old binary or adjust your shell PATH to put Homebrew first${NC}"
    echo ""
fi

# imsg: iMessage extraction CLI (reads ~/Library/Messages/chat.db)
if command -v imsg &>/dev/null; then
    echo -e "${GREEN}ok${NC} imsg"
else
    echo -e "${DIM}Installing imsg (iMessage extraction)...${NC}"
    brew install steipete/tap/imsg
    echo -e "${GREEN}ok${NC} imsg"
fi

# ---------------------------------------------------------------------------
# macOS permissions setup
# ---------------------------------------------------------------------------

echo ""
echo -e "${BOLD}📋 Permissions setup${NC}"
echo ""
echo "iMessage extraction needs two macOS permissions for your terminal app:"
echo ""
echo -e "  1. ${BOLD}Full Disk Access${NC} — to read ~/Library/Messages/chat.db"
echo -e "  2. ${BOLD}Contacts${NC}         — to resolve phone numbers to names"
echo ""
echo "These will be prompted automatically on first run, or you can grant them now."
echo ""

read -p "Open System Settings to grant permissions now? [y/N] " -n 1 -r
echo ""
if [[ $REPLY =~ ^[Yy]$ ]]; then
    echo -e "${DIM}Opening Full Disk Access settings...${NC}"
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_AllFiles"
    echo -e "${DIM}Opening Contacts settings...${NC}"
    open "x-apple.systempreferences:com.apple.preference.security?Privacy_Contacts"
    echo ""
    echo -e "${YELLOW}Add your terminal app (Terminal, iTerm2, Ghostty, etc.) to both lists.${NC}"
    echo -e "${DIM}You may need to restart your terminal after granting permissions.${NC}"
else
    echo -e "${DIM}Skipped. Permissions will be prompted on first run.${NC}"
fi

echo ""
echo -e "${GREEN}${BOLD}✅ Installed!${NC}"
echo ""
echo -e "  ${BOLD}contact-exporter login${NC}      Authenticate with Powerset"
echo -e "  ${BOLD}contact-exporter imessage${NC}   Extract iMessage contacts"
echo -e "  ${BOLD}contact-exporter whatsapp${NC}   Extract WhatsApp contacts"
echo -e "  ${BOLD}contact-exporter upload${NC}     Upload to Powerset"
echo ""
echo -e "  ${DIM}Docs: https://github.com/powerset-co/contact-exporter${NC}"
echo ""
