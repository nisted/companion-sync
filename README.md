# Companion Sync

Shares one Bitfocus Companion config between rigs (Pi, Windows, Mac) through Google Drive.

- **engine.py**: all the sync logic. On the Pi it's also the command-line tool (`companion-sync status | take | save | return`).
- **app.py**: the tray / menu-bar app for Windows and Mac.
- **pi/**: the 15-minute save timer for the Pi.
- **.github/workflows/build.yml**: builds the Windows and Mac apps on every push. Download them from the Actions tab.

How it works: one rig holds the config at a time (a LOCK file on Drive). The holder saves every 15 minutes
without stopping Companion. A lock not refreshed for 24 hours can be taken over after asking. Edits from a
rig whose lock was taken over go to `stranded/` instead of overwriting. Drive keeps the last 30 copies in `history/`.

Google sign-in details come from the repository secrets `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET`.
