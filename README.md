# Companion Sync

Shares one Bitfocus Companion config between rigs (Pi, Windows, Mac) through Google Drive.

- **engine.py**: all the sync logic. On the Pi it's also the command-line tool (`companion-sync status | take | save | return`).
- **app.py**: the tray / menu-bar app for Windows and Mac.
- **panel/**: the BD-1 touchscreen panel (2.4" ILI9341/XPT2046 on SPI): system, network, Companion and sync tabs.
- **pi/**: the Pi's services (start-up sync check before Companion, the panel, the 15-minute save) and `install.sh`.
- **.github/workflows/build.yml**: builds the Windows and Mac apps on every push. Download them from the Actions tab.

How it works: one rig holds the config at a time (a LOCK file on Drive). The holder saves every 15 minutes
without stopping Companion. A lock not refreshed for 24 hours can be taken over after asking. Edits from a
rig whose lock was taken over go to `stranded/` instead of overwriting. Drive keeps the last 30 copies in `history/`.

Google sign-in details come from the repository secrets `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

## On the Pi

Download `Companion-Sync-Pi.tar.gz` from the latest release, unpack it and run `sudo ./companion-sync/pi/install.sh`
(add `--panel` for the 2.4" touchscreen panel). Then, as the same user and without sudo, run `companion-sync setup`
to choose where the config is stored and sign in. No screen is needed: GitHub signs in with a code typed on your
phone, a network folder just needs mounting, and Google Drive / Dropbox / OneDrive give a command to run on any
computer with a browser, whose result is pasted back. The user needs to be able to use sudo without a password
(the Pi's normal first user can).

Other commands: `companion-sync status | take | save | return | unlock | encryption on|off`.
After that, the panel's **UPDATE CS** button installs newer builds itself. It needs a GitHub fine-grained token
with read-only *Contents* access to this repository, saved on the Pi in `~/.config/companion-sync/github-token`.

- Touch calibration: `sudo systemctl stop companion-panel; python3 /opt/companion-sync/panel/calibrate.py; sudo systemctl start companion-panel`
- Panel settings: copy `~/.config/companion-sync/panel.json.example` to `panel.json` and keep only what you change.
