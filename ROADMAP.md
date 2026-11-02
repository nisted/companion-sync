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
- [x] Windows installer; one-click update (download, install, reopen) on Windows, Mac and Pi
- [x] Companion-style tray: mini menu, pop-up window, web GUI (port 8090), Take config (build 21)
- [x] Public repo (free)
- [x] "Get Companion" download page offered when a rig's Companion is too old
- [x] Left-click on the tray icon opens the menu (Windows)
- [x] Module sync: installed module versions (incl. custom imports) go to Drive on save/return and are copied onto a rig on take; `modules/modules.json` lists them
- [x] BD-1 on the new engine (old script kept as `companion-sync.old-script`)
- [x] BD-1 touchscreen panel: system / network / Companion / sync tabs, flashing alerts, tap-again confirm, start-up screen with Nige's logo, touch calibration
- [x] Pi start-up check before Companion: takes the config if free, never blocks (about 6 s)
- [x] Panel RESTART / SHUTDOWN return the config first; UPDATE COMPANION saves first, then installs the latest stable
- [x] Pi install script; Pi package in each release; UPDATE CS on the panel
- [x] c3po on Companion 5.1; GitHub key on BD-1 (UPDATE CS works, 2 Nov)

## To test

- [ ] One-click update: Windows (K-2SO), Mac (c3po), Pi (BD-1 done 2 Nov via UPDATE CS)
- [ ] Pop-up window and left click: Windows, Mac; Pi with `tray_left_click` on
- [ ] Web GUI from another device on the network
- [ ] GitHub storage (limited and quick)
- [ ] Dropbox
- [ ] OneDrive
- [ ] Network folder / NAS
- [ ] iCloud Drive (Windows, Mac)
- [ ] Move between storages
- [ ] Encryption on / off

## Next up

1. [ ] **Stream Deck button: return then shut down** (the panel does this already; a Companion button could call the same).
2. [ ] **Fix BD-1's low-voltage warning.** Power supply or cable (the panel's SYSTEM tab shows it).

## Parked: making it available to others

- [ ] Choice of storage on first run (built; see To test)
- [ ] Google sign-in for other people: verification, own client ID, or rclone's built-in
- [ ] Setup documentation for each storage option
- [ ] Code signing (Apple about £80/yr, Windows more)
- [ ] Licence
- [ ] Naming: label as an unofficial community tool, or check with Bitfocus
- [ ] Decide the support position ("use at your own risk" or otherwise)
