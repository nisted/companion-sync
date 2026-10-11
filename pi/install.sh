#!/bin/bash
# Companion Sync on the Pi: the engine, the touchscreen panel, the start-up
# check before Companion, and the 15-minute saves.
#
#   sudo ./pi/install.sh        install or update from this unpacked folder
#
# The panel's UPDATE CS button runs this from a downloaded release.
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/companion-sync
say() { echo "[companion-sync] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: sudo $0"; exit 1; }

# Whose Drive sign-in and home folder to use
USER_NAME="${COMPANION_SYNC_USER:-${SUDO_USER:-}}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
    [ -f "$DEST/USER" ] && USER_NAME="$(cat "$DEST/USER")"
fi
[ -n "$USER_NAME" ] && [ "$USER_NAME" != root ] || { echo "Can't tell which user to install for. Run it with sudo from your own account."; exit 1; }
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
say "Installing for $USER_NAME"

# Packages
need=()
for p in python3-spidev python3-libgpiod python3-pil python3-numpy python3-cryptography rsync; do
    dpkg -s "$p" >/dev/null 2>&1 || need+=("$p")
done
command -v rclone >/dev/null || need+=(rclone)
if [ ${#need[@]} -gt 0 ]; then
    say "Installing ${need[*]}"
    apt-get install -y "${need[@]}"
fi

# SPI for the touchscreen
REBOOT=""
if ! grep -q '^dtparam=spi=on' /boot/firmware/config.txt 2>/dev/null; then
    raspi-config nonint do_spi 0
    REBOOT=yes
fi
usermod -aG spi,gpio "$USER_NAME"

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
install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.config/companion-sync"
install -o "$USER_NAME" -g "$USER_NAME" -m 644 "$DEST/panel/panel-settings.example.json" \
    "$HOME_DIR/.config/companion-sync/panel.json.example"

# Services
for u in companion-sync-save.service companion-sync-save.timer companion-sync-boot.service companion-panel.service; do
    sed "s/@USER@/$USER_NAME/g" "$DEST/pi/$u" > "/etc/systemd/system/$u"
done
systemctl daemon-reload
systemctl enable companion-sync-boot.service          # runs at the next start-up, before Companion
systemctl enable --now companion-sync-save.timer
systemctl enable companion-panel.service
if [ -n "$REBOOT" ]; then
    say "SPI has just been turned on: restart the Pi to start the panel."
else
    systemctl restart companion-panel.service
fi
say "Done: build $(cat "$DEST/BUILD" 2>/dev/null || echo dev)"
