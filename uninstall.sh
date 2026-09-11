#!/usr/bin/env bash
# Remove kiosk-manager. Config and the Firefox profile are kept unless --purge.
set -euo pipefail

PREFIX="${PREFIX:-$HOME/.local}"
LIB_DIR="$PREFIX/lib/kiosk-manager"
BIN="$PREFIX/bin/kiosk-manager"
UNIT_DIR="$HOME/.config/systemd/user"
DATA_DIR="${XDG_DATA_HOME:-$HOME/.local/share}/kiosk-manager"
CONFIG_DIR="${XDG_CONFIG_HOME:-$HOME/.config}/kiosk-manager"

PURGE=0
[ "${1:-}" = "--purge" ] && PURGE=1

# The kiosk browser and settings window run in their own systemd scopes, so
# stopping the service alone would leave them on screen.
"$BIN" stop >/dev/null 2>&1 || true
pkill -f "kiosk_manager gui" 2>/dev/null || true

systemctl --user disable --now kiosk-manager.service 2>/dev/null || true
rm -f "$UNIT_DIR/kiosk-manager.service"
rm -f "$UNIT_DIR"/graphical-session.target.wants/kiosk-manager.service
rm -f "$UNIT_DIR"/default.target.wants/kiosk-manager.service
systemctl --user daemon-reload

rm -f "$HOME/.config/autostart/kiosk-manager.desktop"
rm -f "$PREFIX/share/applications/kiosk-manager.desktop"
rm -f "$BIN"
rm -rf "$LIB_DIR"

if [ "$PURGE" = "1" ]; then
    rm -rf "$DATA_DIR" "$CONFIG_DIR"
    echo "Removed config and the kiosk Firefox profile too."
else
    echo "Kept $CONFIG_DIR and $DATA_DIR (pass --purge to delete them)."
fi

echo "kiosk-manager removed."
