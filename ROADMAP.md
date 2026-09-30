# Companion Sync: Roadmap

Tick things off by editing this file.

## Done

- [x] Google Cloud project "Companion Sync" and OAuth desktop client (Internal)
- [x] GitHub repo and Actions builds; each build on `main` is published as a release
- [x] Engine: take, save (15 min, live SQLite snapshot), return, 24-hour stale takeover, stranded edits, Drive history (last 30), Companion version handling, saved rig names
- [x] 18 scenario tests for the engine
- [x] Windows and Mac tray apps (build 6): status icon, menu, auto-return on quit, start at login
- [x] Nige's CS logo
- [x] "Update Companion Sync" menu item and build number
- [x] "Get Companion" download page offered when a rig's Companion is too old
- [x] Left-click on the tray icon opens the menu (Windows)

## Next up

1. [ ] **Mac: update Companion to 5.0.** Open once so it moves the config across, quit, then check Status reads `c3po` and 5.0 on both sides.
2. [ ] **BD-1: switch to the new engine,** replacing the old command-line script. *Needs the Pi on.*
3. [ ] **Pi tray app.** Test the icon shows on BD-1's desktop, then a one-command install. *Needs the Pi on.*
4. [ ] **Pi boot-time take,** before Companion starts. Must never block the show when offline or when another rig holds the config.
5. [ ] **Companion button: return then shut down.** Small service file plus a permission rule, so it survives Companion stopping.
6. [ ] **Fix BD-1's low-voltage warning.** Power supply or cable.

## Parked: making it available to others

- [ ] Choice of storage (Google Drive, Dropbox, OneDrive, NAS…) on first run
- [ ] Google sign-in for other people: verification, own client ID, or rclone's built-in
- [ ] Setup documentation for each storage option
- [ ] Code signing (Apple about £80/yr, Windows more)
- [ ] Public repo and licence
- [ ] Naming: label as an unofficial community tool, or check with Bitfocus
- [ ] Decide the support position ("use at your own risk" or otherwise)
