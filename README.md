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
| `Companion-Sync-Windows.exe` | Windows: a tray app |
| `Companion-Sync-Mac.zip` | Mac (Apple silicon): a menu-bar app |
| `Companion-Sync-Pi.tar.gz` | Raspberry Pi running Companion as a service (e.g. CompanionPi) |

## Set up

**Windows / Mac:** open the app, then choose **Storage…** in its menu, pick a storage and sign in. On the rig
whose config you want to keep, choose **Open Companion** and say yes to putting its config there. Do the same on
every other rig with the same storage and account. From then on, open Companion from the Companion Sync menu.

**Raspberry Pi:** unpack, install, then choose the storage. No screen is needed.

```
tar xzf Companion-Sync-Pi.tar.gz
sudo ./companion-sync/pi/install.sh
companion-sync setup
```

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
- `app.py`: the Windows / Mac tray app.
- `panel/`: an optional 2.4" SPI touchscreen panel for the Pi (`install.sh --panel`).
- `pi/`: the Pi services (start-up take before Companion, 15-minute saves) and `install.sh`.
- `tests/`: scenario tests against a local folder, rclone and a fake GitHub: `python3 tests/test_engine.py`.
- `.github/workflows/build.yml`: builds and publishes a release on every push to `main`.
  Google sign-in details come from the repository secrets `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.

Panel extras: touch calibration
`sudo systemctl stop companion-panel; python3 /opt/companion-sync/panel/calibrate.py; sudo systemctl start companion-panel`.
Settings: copy `~/.config/companion-sync/panel.json.example` to `panel.json` and keep only what you change.
The panel's **UPDATE CS** button reads a GitHub token from `~/.config/companion-sync/github-token`.
