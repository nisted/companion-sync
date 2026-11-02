# Companion Sync

Shares one Bitfocus Companion config between all your rigs (Windows, Mac and Raspberry Pi) through the storage
you choose: Google Drive, GitHub, Dropbox, OneDrive, a network folder / NAS, or iCloud Drive (Windows and Mac).

One rig has the config at a time. It saves every 15 minutes without stopping Companion, and returns the config
when Companion is quit (on a Pi: `companion-sync return`). Other rigs take it when it's free. Every take keeps
a safety copy of the rig's own old config, so nothing is thrown away.

## Download

From the [latest release](https://github.com/nisted/companion-sync/releases/latest):

| File | For |
| --- | --- |
| `Companion-Sync-Setup.exe` | Windows: installer for the tray app (just for you, no admin needed) |
| `Companion-Sync-Windows.exe` | Windows: the same app on its own, without installing |
| `Companion-Sync-Mac.zip` | Mac (Apple silicon): a menu-bar app |
| `Companion-Sync-Pi.tar.gz` | Raspberry Pi: a tray app on a Pi with a desktop, or headless (command and services) |

## Using it

Like Companion's own tray app, in three steps:

- **Tray icon:** a left click shows or hides the **pop-up window** (Windows and Mac; on the Pi a click opens the
  menu, unless `tray_left_click` is turned on in Settings). A right click opens the mini menu: Show/Hide window,
  Open Companion, Take config, Launch GUI, Quit.
- **Pop-up window:** who has the config, last save, storage, sign-in and encryption, with Open Companion, Take
  config, Save now, Return config, Launch GUI and Start at login.
- **Web GUI** (Launch GUI, or `http://<this rig>:8090` from any device on the network): Status, Storage &
  sign-in, Encryption, Settings, Log and Update. Settings has the port, network access and an optional password
  (off by default). Questions and sign-in windows still appear on the rig's own screen.

## Updating

Choose **Update Companion Sync…** in the menu. It checks for a newer build, downloads it, installs it and
opens it again, on all three:

- **Windows:** runs the installer quietly. The first update from a loose `Companion Sync.exe` moves the app
  into the installed copy (Start menu, uninstaller); the old exe can then be deleted. Start at login follows it.
- **Mac:** replaces the app where it is (it needs to be somewhere you can write to, e.g. Applications).
- **Pi:** runs `pi/install.sh --from-update`, asking for the password if sudo needs one.

## Set up

**Windows / Mac:** open the app, then choose **Storage…** in its menu, pick a storage and sign in. On the rig
whose config you want to keep, choose **Open Companion** and say yes to putting its config there. Do the same on
every other rig with the same storage and account. From then on, open Companion from the Companion Sync menu.

**Raspberry Pi:** unpack, install, then choose the storage. No screen is needed.
On a Pi with a desktop, the installer also adds the tray app (same menu as Windows / Mac) and starts it at
login; `--headless` leaves it out.

```
tar xzf Companion-Sync-Pi.tar.gz
sudo ./companion-sync/pi/install.sh              # or: --headless, --tray, --panel
companion-sync setup
```

Companion on the Pi can be CompanionPi's service, the desktop app or a Docker container (with its config folder
bind-mounted from the Pi); it's detected, and `companion-sync status` says which. To set it, or anything else
for this rig, copy `~/.config/companion-sync/settings.json.example` to `settings.json` and keep only what
you change. `login_action` decides whether the tray app takes the config and opens Companion at login
(`take_and_open`, the Pi's default) or just shows the icon (`tray_only`). With a service or container, Open
Companion opens its admin page, and the config isn't returned when Companion stops: use **Return config**.

Run `setup` as your normal user, without sudo. GitHub signs in with a code typed on your phone; a network folder
just needs mounting; Google Drive, Dropbox and OneDrive give a command to run on any computer with a browser,
whose result you paste back. The user needs passwordless sudo (the Pi's normal first user has it).
Pi commands: `companion-sync status | take | save | return | setup | unlock | encryption on|off`.

## Features

- **Choice of storage**, changeable later: from the rig that has the config, **Storage…** moves it (and its
  modules) into a new, empty storage. The old one is marked as moved so no rig carries on with it.
- **Optional encryption** (off by default): a passphrase you choose; the storage only ever holds scrambled copies.
- **Modules travel** with the config, custom ones included.
- **Companion versions:** an older Companion refuses a newer config and offers the download page; a newer one
  upgrades the config when it starts.
- **Lost rigs:** a lock not refreshed for 24 hours can be taken over, after asking. Unsaved edits from that rig go
  to `stranded/` when it next comes online, never over the main copy.
- **History:** the last 30 saves are kept (on GitHub, the repository's own history).

## For developers

- `engine.py`: all the sync logic and storage back-ends; on the Pi it's also the `companion-sync` command.
- `app.py`: the Windows / Mac / Pi desktop tray app (on the Pi: zenity dialogs, AppIndicator icon).
- `popup.py`: the pop-up window (tkinter, its own process); `web.py`: the web GUI and its local API.
- `panel/`: an optional 2.4" SPI touchscreen panel for the Pi (`install.sh --panel`).
- `pi/`: the Pi services (start-up take before Companion or Docker, 15-minute saves), `install.sh` and
  `settings.example.json`.
- `tests/`: scenario tests against a local folder, rclone and a fake GitHub: `python3 tests/test_engine.py`.
- `.github/workflows/build.yml`: builds and publishes a release on every push to `main`.
  Google sign-in details come from the repository secrets `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

Panel extras: touch calibration
`sudo systemctl stop companion-panel; python3 /opt/companion-sync/panel/calibrate.py; sudo systemctl start companion-panel`.
Settings: copy `~/.config/companion-sync/panel.json.example` to `panel.json` and keep only what you change.
The panel's **UPDATE CS** button reads a GitHub token from `~/.config/companion-sync/github-token`.
