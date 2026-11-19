# Stickywell

Simple sticky notes for the Linux desktop.

<img width="1728" height="972" alt="stickywelscreenshotl" src="https://github.com/user-attachments/assets/cd440ba3-1a5a-4469-91da-60a76f518abf" />


Stickywell puts coloured sticky notes on your desktop and keeps out of the way.
Type a note, drag it where you want it, and it will still be there next time.

## Features

- As many notes as you like, in six colours
- Drag notes anywhere, resize them, and keep any note on top
- Everything is saved automatically and comes back where you left it, on either monitor
- Hide and show all notes with one click from the tray icon
- Optional tray icon and taskbar icon, so it can run quietly in the background with nothing showing
- Run at startup
- Import and export your notes as a backup file or as plain text
- Menus follow your desktop theme: your colour scheme on KDE Plasma, light or dark and
  your accent colour on GNOME

## Install

**Snap Store**

```
sudo snap install stickywell
```

**AppImage**

Download `Stickywell-x.y-x86_64.AppImage` from the
[Releases page](https://github.com/bobwellapps/stickywell/releases), make it
executable and run it. Nothing else needs installing.

```
chmod +x Stickywell-*-x86_64.AppImage
./Stickywell-*-x86_64.AppImage
```

**From source**

Stickywell is a single Python file and needs only PyQt6.

```
sudo apt install python3-pyqt6
python3 stickywell.py
```

Keep `stickywell.png` next to `stickywell.py` so the app picks up its icon. A sample
menu entry is in `packaging/stickywell.desktop`.

## Using it

| To do this | Do this |
| --- | --- |
| Add a note | Click `+` on any note, or press Ctrl+N |
| Change a note's colour | Click the colour square |
| Keep a note above other windows | Click the pin |
| Delete a note | Click `✕`, then Delete |
| Hide or show all notes | Click the tray icon, or press Ctrl+H to hide |
| Everything else (import, export, startup, icons, quit) | The `☰` menu on any note, or right-click the tray icon |

If you turn off both the tray icon and the application icon, start Stickywell again
from your application menu to bring hidden notes back.

Notes are stored in `~/.local/share/stickywell/notes.json` (for the snap,
`~/snap/stickywell/current/.local/share/stickywell/notes.json`).

## Good to know

Stickywell runs through XWayland on Wayland desktops. Native Wayland does not let an
app choose where its windows go or keep them on top, and sticky notes need both.

## Licence

Stickywell is free software under the
[GNU General Public License, version 3 or later](LICENSE).

The Stickywell name and icon are not covered by that licence and may not be used for
other projects or forks. See [TRADEMARKS.md](TRADEMARKS.md).
