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
MACOS_VER=$(sw_vers -productVersion)
MACOS_MAJOR=$(echo "$MACOS_VER" | cut -d. -f1)
USE_BREW=true

if ! command -v brew &>/dev/null; then
    # macOS < 13 (Ventura): Homebrew may refuse to install
    if [[ "$MACOS_MAJOR" -lt 13 ]]; then
        echo -e "${YELLOW}macOS ${MACOS_VER} detected — Homebrew requires macOS 13+${NC}"
        echo -e "${DIM}Will install via pip instead${NC}"
        USE_BREW=false
    else
        echo -e "${YELLOW}Homebrew not found. Installing...${NC}"
        /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
        # Add brew to PATH for this session (Apple Silicon vs Intel)
        if [[ -f /opt/homebrew/bin/brew ]]; then
            eval "$(/opt/homebrew/bin/brew shellenv)"
        elif [[ -f /usr/local/bin/brew ]]; then
            eval "$(/usr/local/bin/brew shellenv)"
        fi
        if ! command -v brew &>/dev/null; then
            echo -e "${YELLOW}Homebrew installation failed — falling back to pip${NC}"
            USE_BREW=false
        else
            echo -e "${GREEN}ok${NC} Homebrew installed"
        fi
    fi
else
    echo -e "${GREEN}ok${NC} Homebrew"
fi

# 2. Check/install Docker runtime (required for WhatsApp)
if command -v docker &>/dev/null && docker info &>/dev/null 2>&1; then
    echo -e "${GREEN}ok${NC} Docker"
elif command -v colima &>/dev/null; then
    echo -e "${DIM}Starting Colima...${NC}"
    if [[ "$ARCH" == "arm64" ]]; then
        colima start --memory 2 --vm-type vz --vz-rosetta 2>/dev/null || colima start --memory 2 2>/dev/null
    else
        colima start --memory 2 2>/dev/null
    fi
    echo -e "${GREEN}ok${NC} Colima"
elif [[ "$USE_BREW" == true ]]; then
    echo ""
    echo -e "${YELLOW}Docker runtime not found.${NC}"
    echo "Required for WhatsApp extraction (iMessage works without it)."
    echo ""
    read -p "Install Colima (lightweight Docker runtime)? [y/N] " -n 1 -r
    echo ""
    if [[ $REPLY =~ ^[Yy]$ ]]; then
        echo -e "${DIM}Installing Colima + Docker CLI...${NC}"
        brew install colima docker
        echo -e "${DIM}Starting Colima...${NC}"
        if [[ "$ARCH" == "arm64" ]]; then
            colima start --memory 2 --vm-type vz --vz-rosetta 2>/dev/null || colima start --memory 2 2>/dev/null
        else
            colima start --memory 2 2>/dev/null
        fi
        echo -e "${GREEN}ok${NC} Colima + Docker"
    else
        echo -e "${DIM}Skipped. Install later with: brew install colima docker && colima start${NC}"
    fi
else
    echo -e "${DIM}Docker unavailable — WhatsApp extraction requires: brew install colima docker${NC}"
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
# Install / upgrade contact-exporter
# ---------------------------------------------------------------------------

echo ""

if [[ "$USE_BREW" == true ]]; then
    # --- Homebrew install path ---

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

    # imsg no longer needed — we read chat.db directly via SQLite

else
    # --- Pip fallback for old macOS ---

    if ! command -v python3 &>/dev/null; then
        echo -e "${RED}Python 3 not found. Install from https://www.python.org/downloads/${NC}"
        exit 1
    fi

    PYTHON_VER=$(python3 -c "import sys; print(f'{sys.version_info.major}.{sys.version_info.minor}')")
    echo -e "${GREEN}ok${NC} Python ${PYTHON_VER}"

    echo -e "${DIM}Installing contact-exporter via pip...${NC}"
    python3 -m pip install --upgrade pip 2>/dev/null || true
    python3 -m pip install "git+https://github.com/powerset-co/contact-exporter.git" 2>&1

    if command -v contact-exporter &>/dev/null; then
        echo -e "${GREEN}ok${NC} contact-exporter ($(contact-exporter --version 2>&1 | tail -1))"
    else
        # pip --user installs to ~/Library/Python/X.Y/bin which may not be on PATH
        PIP_BIN="$HOME/Library/Python/${PYTHON_VER}/bin"
        if [[ -f "$PIP_BIN/contact-exporter" ]]; then
            echo -e "${GREEN}ok${NC} contact-exporter"
            echo -e "${YELLOW}Add this to your shell profile to put it on PATH:${NC}"
            echo -e "  export PATH=\"${PIP_BIN}:\$PATH\""
        else
            echo -e "${RED}contact-exporter install failed${NC}"
            exit 1
        fi
    fi

    # No external dependencies needed — chat.db and AddressBook are read directly via SQLite
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
