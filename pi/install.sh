#!/bin/bash
# Companion Sync on the Pi: the engine, the start-up check before Companion,
# the 15-minute saves and (optionally) the BD-1 style touchscreen panel.
#
#   sudo ./pi/install.sh           install or update from this unpacked folder
#   sudo ./pi/install.sh --panel   also set up the 2.4" SPI touchscreen panel
#
# An update keeps the panel if it's already installed. The panel's UPDATE CS
# button runs this from a downloaded release.
# Afterwards, choose where the config is stored:  companion-sync setup
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/companion-sync
say() { echo "[companion-sync] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: sudo $0"; exit 1; }

PANEL=""
[ "${1:-}" = "--panel" ] && PANEL=yes
[ -f /etc/systemd/system/companion-panel.service ] && PANEL=yes

# Whose sign-in and home folder to use (the services run as this user)
USER_NAME="${COMPANION_SYNC_USER:-${SUDO_USER:-}}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
    [ -f "$DEST/USER" ] && USER_NAME="$(cat "$DEST/USER")"
fi
[ -n "$USER_NAME" ] && [ "$USER_NAME" != root ] || { echo "Can't tell which user to install for. Run it with sudo from your own account."; exit 1; }
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
say "Installing for $USER_NAME"

# Packages
need=()
pkgs=(python3-cryptography rsync)
[ -n "$PANEL" ] && pkgs+=(python3-spidev python3-libgpiod python3-pil python3-numpy)
for p in "${pkgs[@]}"; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
command -v rclone >/dev/null || need+=(rclone)
if [ ${#need[@]} -gt 0 ]; then
    say "Installing ${need[*]}"
    apt-get install -y "${need[@]}"
fi

# SPI for the touchscreen
REBOOT=""
if [ -n "$PANEL" ]; then
    if ! grep -q '^dtparam=spi=on' /boot/firmware/config.txt 2>/dev/null; then
        raspi-config nonint do_spi 0
        REBOOT=yes
    fi
    usermod -aG spi,gpio "$USER_NAME"
fi

# Files
mkdir -p "$DEST"
rsync -a --delete --exclude USER --exclude __pycache__ "$SRC/" "$DEST/"
echo "$USER_NAME" > "$DEST/USER"
chmod 755 "$DEST/engine.py" "$DEST/panel/panel.py" "$DEST/pi/install.sh"

# The companion-sync command (replaces the old stand-alone script)
if [ -f /usr/local/bin/companion-sync ] && ! grep -q /opt/companion-sync /usr/local/bin/companion-sync; then
    mv /usr/local/bin/companion-sync /usr/local/bin/companion-sync.old-script
    say "Old script kept as /usr/local/bin/companion-sync.old-script"
fi
cat > /usr/local/bin/companion-sync <<'CMD'
#!/bin/sh
exec /usr/bin/python3 /opt/companion-sync/engine.py "$@"
CMD
chmod 755 /usr/local/bin/companion-sync

# Panel settings: an example to copy from (panel.json itself is only ever yours)
if [ -n "$PANEL" ]; then
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.config/companion-sync"
    install -o "$USER_NAME" -g "$USER_NAME" -m 644 "$DEST/panel/panel-settings.example.json" \
        "$HOME_DIR/.config/companion-sync/panel.json.example"
fi

# Services
units=(companion-sync-save.service companion-sync-save.timer companion-sync-boot.service)
[ -n "$PANEL" ] && units+=(companion-panel.service)
for u in "${units[@]}"; do
    sed "s/@USER@/$USER_NAME/g" "$DEST/pi/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable companion-sync-boot.service          # runs at the next start-up, before Companion
systemctl enable --now companion-sync-save.timer
if [ -n "$PANEL" ]; then
    systemctl enable companion-panel.service
    if [ -n "$REBOOT" ]; then
        say "SPI has just been turned on: restart the Pi to start the panel."
    else
        systemctl restart companion-panel.service
    fi
fi
say "Done: build $(cat "$DEST/BUILD" 2>/dev/null || echo dev)"
if [ ! -f "$HOME_DIR/.companion-sync-storage.json" ] && [ ! -f "$HOME_DIR/.config/rclone/rclone.conf" ]; then
    say "Next, choose where the config is stored (as $USER_NAME, without sudo):"
    say "    companion-sync setup"
fi
