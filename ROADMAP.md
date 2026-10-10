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
- [x] Module sync: installed module versions (incl. custom imports) go to Drive on save/return and are copied onto a rig on take; `modules/modules.json` lists them
- [x] BD-1 on the new engine (old script kept as `companion-sync.old-script`)
- [x] BD-1 touchscreen panel: system / network / Companion / sync tabs, flashing alerts, tap-again confirm, start-up screen with Nige's logo, touch calibration
- [x] Pi start-up check before Companion: takes the config if free, never blocks (about 6 s)
- [x] Panel RESTART / SHUTDOWN return the config first; UPDATE COMPANION saves first, then installs the latest stable
- [x] Pi install script; Pi package in each release; UPDATE CS on the panel

## Next up

1. [ ] **Mac: update Companion to 5.0.** Open once so it moves the config across, quit, then check Status reads `c3po` and 5.0 on both sides.
2. [ ] **GitHub read-only key on BD-1** so UPDATE CS works.
3. [ ] **Stream Deck button: return then shut down** (the panel does this already; a Companion button could call the same).
4. [ ] **Fix BD-1's low-voltage warning.** Power supply or cable (the panel's SYSTEM tab shows it).

## Parked: making it available to others

- [ ] Choice of storage (Google Drive, Dropbox, OneDrive, NAS…) on first run
- [ ] Google sign-in for other people: verification, own client ID, or rclone's built-in
- [ ] Setup documentation for each storage option
- [ ] Code signing (Apple about £80/yr, Windows more)
- [ ] Public repo and licence
- [ ] Naming: label as an unofficial community tool, or check with Bitfocus
- [ ] Decide the support position ("use at your own risk" or otherwise)
