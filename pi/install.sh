#!/bin/bash
# Companion Sync on the Pi: the engine, the start-up check before Companion,
# the 15-minute saves, the tray icon (on a Pi with a desktop) and (optionally)
# the BD-1 style touchscreen panel.
#
#   sudo ./pi/install.sh              install or update from this unpacked folder
#   sudo ./pi/install.sh --headless   no tray icon, even on a Pi with a desktop
#   sudo ./pi/install.sh --tray       add the tray icon (e.g. to undo --headless)
#   sudo ./pi/install.sh --panel      also set up the 2.4" SPI touchscreen panel
#
# Companion can be CompanionPi's service, the desktop app or a Docker
# container: it's detected, or set in ~/.config/companion-sync/settings.json
# (see settings.json.example there).
#
# An update keeps the panel and the tray/headless choice as they were. The
# panel's UPDATE CS button and the tray app's "Update Companion Sync…" run
# this from a downloaded release (with --from-update).
# Afterwards, choose where the config is stored:  companion-sync setup
set -euo pipefail

SRC="$(cd "$(dirname "$0")/.." && pwd)"
DEST=/opt/companion-sync
say() { echo "[companion-sync] $*"; }

[ "$(id -u)" -eq 0 ] || { echo "Run it with sudo: sudo $0"; exit 1; }

PANEL=""
TRAY_ARG=""
for a in "$@"; do
    case "$a" in
        --panel) PANEL=yes ;;
        --headless) TRAY_ARG=no ;;
        --tray) TRAY_ARG=yes ;;
        --from-update) ;;      # run by the tray app's / panel's update: same as a plain update
        *) echo "Unknown option: $a"; exit 1 ;;
    esac
done
[ -f /etc/systemd/system/companion-panel.service ] && PANEL=yes

# Whose sign-in and home folder to use (the services run as this user)
USER_NAME="${COMPANION_SYNC_USER:-${SUDO_USER:-}}"
if [ -z "$USER_NAME" ] || [ "$USER_NAME" = root ]; then
    [ -f "$DEST/USER" ] && USER_NAME="$(cat "$DEST/USER")"
fi
[ -n "$USER_NAME" ] && [ "$USER_NAME" != root ] || { echo "Can't tell which user to install for. Run it with sudo from your own account."; exit 1; }
HOME_DIR="$(getent passwd "$USER_NAME" | cut -d: -f6)"
say "Installing for $USER_NAME"

# Tray icon or headless: as asked, else as before, else tray if this Pi has a desktop
AUTOSTART="$HOME_DIR/.config/autostart/companion-sync-tray.desktop"
if [ -n "$TRAY_ARG" ]; then
    TRAY="$TRAY_ARG"
elif [ -f "$DEST/HEADLESS" ]; then
    TRAY=no
elif [ -f "$AUTOSTART" ]; then
    TRAY=yes
elif [ "$(systemctl get-default 2>/dev/null)" = graphical.target ] \
     || ls /usr/share/xsessions/*.desktop /usr/share/wayland-sessions/*.desktop >/dev/null 2>&1; then
    TRAY=yes
else
    TRAY=no
fi
[ "$TRAY" = yes ] && say "With the tray icon" || say "Headless (no tray icon)"

# Packages
need=()
pkgs=(python3-cryptography rsync)
[ -n "$PANEL" ] && pkgs+=(python3-spidev python3-libgpiod python3-pil python3-numpy)
[ "$TRAY" = yes ] && pkgs+=(python3-venv python3-gi gir1.2-ayatanaappindicator3-0.1 python3-pil python3-pil.imagetk python3-tk zenity libnotify-bin)
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
rsync -a --delete --exclude USER --exclude HEADLESS --exclude venv --exclude icon.png --exclude __pycache__ "$SRC/" "$DEST/"
echo "$USER_NAME" > "$DEST/USER"
chmod 755 "$DEST/engine.py" "$DEST/panel/panel.py" "$DEST/pi/install.sh"
[ -f "$DEST/app.py" ] && chmod 755 "$DEST/app.py"

# Docker: let the user check and restart Companion's container without sudo
if getent group docker >/dev/null; then
    usermod -aG docker "$USER_NAME"
fi

# This rig's settings: an example to copy from (settings.json itself is only ever yours)
install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.config/companion-sync"
install -o "$USER_NAME" -g "$USER_NAME" -m 644 "$DEST/pi/settings.example.json" \
    "$HOME_DIR/.config/companion-sync/settings.json.example"

# Tray icon: its own Python (for pystray), started at login, and in the menu
if [ "$TRAY" = yes ]; then
    rm -f "$DEST/HEADLESS"
    if [ ! -x "$DEST/venv/bin/python" ]; then
        say "Setting up the tray app's Python"
        python3 -m venv --system-site-packages "$DEST/venv"
    fi
    "$DEST/venv/bin/pip" install --quiet --upgrade pystray
    (cd "$DEST" && "$DEST/venv/bin/python" -c "from icon import draw_cs; draw_cs(256, rounded=True).save('icon.png')")
    entry() {   # $1 = extra argument
        printf '[Desktop Entry]\nType=Application\nName=Companion Sync\nComment=Share one Companion config between rigs\nExec=%s %s %s\nIcon=%s\nTerminal=false\nCategories=AudioVideo;Utility;\n' \
            "$DEST/venv/bin/python" "$DEST/app.py" "$1" "$DEST/icon.png"
    }
    install -d -o "$USER_NAME" -g "$USER_NAME" "$HOME_DIR/.config/autostart"
    entry --login > "$AUTOSTART"
    chown "$USER_NAME:$USER_NAME" "$AUTOSTART"
    entry "" > /usr/share/applications/companion-sync.desktop
else
    touch "$DEST/HEADLESS"
    rm -f "$AUTOSTART" /usr/share/applications/companion-sync.desktop
fi

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
say "Companion on this Pi: $(sudo -u "$USER_NAME" -H /usr/bin/python3 -c 'import sys; sys.path.insert(0, "/opt/companion-sync"); import engine as E; print(E.describe_companion(E.default_settings(log=lambda m: None)))' 2>/dev/null || echo 'not detected yet')"
[ "$TRAY" = yes ] && say "The tray icon starts at the next login (or open Companion Sync from the menu)."
if [ ! -f "$HOME_DIR/.companion-sync-storage.json" ] && [ ! -f "$HOME_DIR/.config/rclone/rclone.conf" ]; then
    say "Next, choose where the config is stored (as $USER_NAME, without sudo):"
    say "    companion-sync setup"
fi
