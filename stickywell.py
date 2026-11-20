#!/usr/bin/env python3
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 bobwellapps
#
# Stickywell is free software: you can redistribute it and/or modify it under
# the terms of the GNU General Public License as published by the Free Software
# Foundation, either version 3 of the License, or (at your option) any later
# version. It is distributed WITHOUT ANY WARRANTY; see the LICENSE file.
# The Stickywell name and icon are not covered by that licence: see TRADEMARKS.md.
"""Stickywell - simple sticky notes for the Linux desktop.

Run with:  python3 stickywell.py
Needs:     PyQt6  (sudo apt install python3-pyqt6)
"""
import ctypes
import ctypes.util
import html
import json
import os
import sys
import uuid
from pathlib import Path

# Wayland does not let apps place their own windows, so notes could not
# reopen where you left them. Prefer X11 (XWayland), fall back to Wayland.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb;wayland")

from PyQt6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer, pyqtSlot
from PyQt6.QtDBus import QDBus, QDBusConnection, QDBusMessage, QDBusVariant
from PyQt6.QtGui import (
    QAction, QColor, QCursor, QFont, QFontMetrics, QGuiApplication, QIcon, QPainter, QPainterPath,
    QPalette, QPen, QPixmap, QTextDocument,
)
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QSizeGrip,
    QProxyStyle, QStyle, QStyleFactory, QSystemTrayIcon, QTextEdit, QToolButton, QVBoxLayout,
    QWidget,
)

APP_NAME = "Stickywell"
APP_ID = "stickywell"
VERSION = "1.2"

COLOURS = {
    "Yellow": "#fff3a3",
    "Pink": "#ffc9de",
    "Green": "#c9f2b6",
    "Blue": "#bfe3ff",
    "Purple": "#dfcdfb",
    "Orange": "#ffd6a5",
}
DEFAULT_COLOUR = "Yellow"
DEFAULT_SIZE = (260, 240)

IN_SNAP = bool(os.environ.get("SNAP") and os.environ.get("SNAP_USER_DATA"))

# Where the notes are kept.
_LOCAL_DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP_ID
if IN_SNAP and os.environ.get("SNAP_USER_COMMON"):
    # A snap's normal data folder is kept separately for each version, and is
    # copied across when the snap updates. If an update happened while
    # Stickywell was still running (snapd does this after waiting two weeks),
    # anything typed afterwards would go into the old version's folder and be
    # missing next time. The snap's "common" folder is the same for every
    # version, so the notes live there.
    DATA_DIR = Path(os.environ["SNAP_USER_COMMON"])
else:
    DATA_DIR = _LOCAL_DATA_DIR
DATA_FILE = DATA_DIR / "notes.json"

if IN_SNAP and DATA_DIR != _LOCAL_DATA_DIR and not DATA_FILE.exists():
    # Bring the notes across from where earlier versions of the snap kept them
    # (copied, so the old file stays as it was).
    _old_file = _LOCAL_DATA_DIR / "notes.json"
    try:
        if _old_file.is_file():
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            _tmp = DATA_FILE.with_suffix(".json.tmp")
            _tmp.write_bytes(_old_file.read_bytes())
            os.replace(_tmp, DATA_FILE)
    except OSError as _err:
        print(f"{APP_NAME}: could not bring earlier notes across: {_err}", file=sys.stderr)
elif not IN_SNAP:
    # Carry over notes from before the app was renamed.
    _OLD_DIR = DATA_DIR.parent / "stickwell"
    if _OLD_DIR.is_dir() and not DATA_DIR.exists():
        try:
            _OLD_DIR.rename(DATA_DIR)
        except OSError:
            pass

if IN_SNAP:
    # Snaps start at login from a file in their own private config folder.
    AUTOSTART_FILE = Path(os.environ["SNAP_USER_DATA"]) / ".config/autostart" / f"{APP_ID}.desktop"
else:
    AUTOSTART_FILE = (Path(os.environ.get("XDG_CONFIG_HOME") or Path.home() / ".config")
                      / "autostart" / f"{APP_ID}.desktop")
# Inside a snap, HOME is the snap's private folder; this is the real one.
USER_HOME = Path(os.environ.get("SNAP_REAL_HOME") or Path.home())


def launch_command():
    """The command that starts this copy of the app, quoted for a .desktop file."""
    if IN_SNAP:
        return APP_ID
    appimage = os.environ.get("APPIMAGE")
    if appimage:
        parts = [appimage]
    elif getattr(sys, "frozen", False):
        parts = [sys.executable]
    else:
        parts = [sys.executable, os.path.abspath(__file__)]
    quoted = []
    for part in parts:
        for ch in ("\\", '"', "`", "$"):
            part = part.replace(ch, "\\" + ch)
        quoted.append('"' + part.replace("%", "%%") + '"')
    return " ".join(quoted)


def autostart_entry():
    return ("[Desktop Entry]\n"
            "Type=Application\n"
            f"Name={APP_NAME}\n"
            "Comment=Simple sticky notes for your desktop\n"
            f"Exec={launch_command()}\n"
            f"Icon={APP_ID}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n")


class _XEvent(ctypes.Union):
    class _Client(ctypes.Structure):
        _fields_ = [("type", ctypes.c_int), ("serial", ctypes.c_ulong),
                    ("send_event", ctypes.c_int), ("display", ctypes.c_void_p),
                    ("window", ctypes.c_ulong), ("message_type", ctypes.c_ulong),
                    ("format", ctypes.c_int), ("data", ctypes.c_long * 5)]
    _fields_ = [("client", _Client), ("pad", ctypes.c_long * 24)]


_x11 = None


def set_skip_taskbar(window_id, skip):
    """Ask the window manager to keep a window out of (or put it back in)
    the taskbar and pager. X11 only; quietly does nothing elsewhere."""
    global _x11
    if QGuiApplication.platformName() != "xcb":
        return
    try:
        if _x11 is None:
            try:
                lib = ctypes.CDLL("libX11.so.6")
            except OSError:
                lib = ctypes.CDLL(ctypes.util.find_library("X11"))
            lib.XOpenDisplay.restype = ctypes.c_void_p
            lib.XOpenDisplay.argtypes = [ctypes.c_char_p]
            lib.XInternAtom.restype = ctypes.c_ulong
            lib.XInternAtom.argtypes = [ctypes.c_void_p, ctypes.c_char_p, ctypes.c_int]
            lib.XDefaultRootWindow.restype = ctypes.c_ulong
            lib.XDefaultRootWindow.argtypes = [ctypes.c_void_p]
            lib.XSendEvent.argtypes = [ctypes.c_void_p, ctypes.c_ulong, ctypes.c_int,
                                       ctypes.c_long, ctypes.POINTER(_XEvent)]
            lib.XFlush.argtypes = [ctypes.c_void_p]
            display = lib.XOpenDisplay(None)
            if not display:
                return
            _x11 = (lib, display)
        lib, display = _x11
        event = _XEvent()
        msg = event.client
        msg.type = 33   # ClientMessage
        msg.send_event = 1
        msg.window = window_id
        msg.message_type = lib.XInternAtom(display, b"_NET_WM_STATE", 0)
        msg.format = 32
        msg.data[0] = 1 if skip else 0
        msg.data[1] = lib.XInternAtom(display, b"_NET_WM_STATE_SKIP_TASKBAR", 0)
        msg.data[2] = lib.XInternAtom(display, b"_NET_WM_STATE_SKIP_PAGER", 0)
        msg.data[3] = 1
        mask = (1 << 20) | (1 << 19)   # SubstructureRedirect | SubstructureNotify
        lib.XSendEvent(display, lib.XDefaultRootWindow(display), 0, mask, ctypes.byref(event))
        lib.XFlush(display)
    except Exception:
        pass


def swatch(hex_colour, size=16):
    pix = QPixmap(size, size)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setBrush(QColor(hex_colour))
    p.setPen(QColor(0, 0, 0, 90))
    p.drawRoundedRect(1, 1, size - 2, size - 2, 3, 3)
    p.end()
    return QIcon(pix)


def app_icon():
    # Use stickywell.png (bundled in the AppImage, or sitting next to this
    # script); fall back to a simple drawn note if it is missing.
    for folder in (getattr(sys, "_MEIPASS", None), os.path.dirname(os.path.abspath(__file__))):
        if folder:
            pix = QPixmap(os.path.join(folder, f"{APP_ID}.png"))
            if not pix.isNull():
                return QIcon(pix)
    pix = QPixmap(64, 64)
    pix.fill(Qt.GlobalColor.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.RenderHint.Antialiasing)
    p.setPen(QColor(0, 0, 0, 70))
    p.setBrush(QColor(COLOURS["Yellow"]))
    p.drawRoundedRect(6, 6, 52, 52, 6, 6)
    p.setPen(Qt.PenStyle.NoPen)
    p.setBrush(QColor("#e8d76a"))
    p.drawRoundedRect(6, 6, 52, 12, 6, 6)
    p.setBrush(QColor(0, 0, 0, 90))
    for y in (28, 37, 46):
        p.drawRect(15, y, 34 if y != 46 else 22, 3)
    p.end()
    return QIcon(pix)


# ---------------------------------------------------------------- desktop theme
# The notes keep their own paper colours, but menus, tooltips and dialogs follow
# the desktop. The copy of Qt bundled with Stickywell cannot load the desktop's
# own style, so the colours are worked out here:
#   - KDE Plasma: the full colour scheme and fonts from kdeglobals, read from the
#     file itself when possible, or through the desktop portal inside the snap
#     (which is not allowed to read the file).
#   - GNOME and other desktops with a portal: the light or dark choice and the
#     accent colour (and GNOME's interface font), turned into Adwaita colours.
#   - Anything else: Qt's standard look, as before.

PORTAL = ("org.freedesktop.portal.Desktop", "/org/freedesktop/portal/desktop",
          "org.freedesktop.portal.Settings")

KDE_COLOUR_KEYS = {
    "Window": ("BackgroundNormal", "ForegroundNormal", "ForegroundInactive"),
    "View": ("BackgroundNormal", "BackgroundAlternate", "ForegroundNormal",
             "ForegroundInactive", "ForegroundLink", "ForegroundVisited"),
    "Button": ("BackgroundNormal", "ForegroundNormal"),
    "Selection": ("BackgroundNormal", "ForegroundNormal"),
    "Tooltip": ("BackgroundNormal", "ForegroundNormal"),
}

# KDE's own defaults, used for anything the colour scheme leaves out.
BREEZE_LIGHT = {
    "Window": {"BackgroundNormal": "239,240,241", "ForegroundNormal": "35,38,41",
               "ForegroundInactive": "112,125,138"},
    "View": {"BackgroundNormal": "255,255,255", "BackgroundAlternate": "247,247,247",
             "ForegroundNormal": "35,38,41", "ForegroundInactive": "112,125,138",
             "ForegroundLink": "41,128,185", "ForegroundVisited": "155,89,182"},
    "Button": {"BackgroundNormal": "252,252,252", "ForegroundNormal": "35,38,41"},
    "Selection": {"BackgroundNormal": "61,174,233", "ForegroundNormal": "255,255,255"},
    "Tooltip": {"BackgroundNormal": "247,247,247", "ForegroundNormal": "35,38,41"},
}
BREEZE_DARK = {
    "Window": {"BackgroundNormal": "32,35,38", "ForegroundNormal": "252,252,252",
               "ForegroundInactive": "161,169,177"},
    "View": {"BackgroundNormal": "20,22,24", "BackgroundAlternate": "29,31,34",
             "ForegroundNormal": "252,252,252", "ForegroundInactive": "161,169,177",
             "ForegroundLink": "29,153,243", "ForegroundVisited": "155,89,182"},
    "Button": {"BackgroundNormal": "41,44,48", "ForegroundNormal": "252,252,252"},
    "Selection": {"BackgroundNormal": "61,174,233", "ForegroundNormal": "252,252,252"},
    "Tooltip": {"BackgroundNormal": "41,44,48", "ForegroundNormal": "252,252,252"},
}


# GNOME's own colours (libadwaita), in the same layout as a KDE colour scheme.
ADWAITA_LIGHT = {
    "Window": {"BackgroundNormal": "250,250,251", "ForegroundNormal": "51,51,54",
               "ForegroundInactive": "128,128,132"},
    "View": {"BackgroundNormal": "255,255,255", "BackgroundAlternate": "246,246,247",
             "ForegroundNormal": "51,51,54", "ForegroundInactive": "128,128,132",
             "ForegroundLink": "28,113,216", "ForegroundVisited": "129,61,156"},
    "Button": {"BackgroundNormal": "235,235,237", "ForegroundNormal": "51,51,54"},
    "Selection": {"BackgroundNormal": "53,132,228", "ForegroundNormal": "255,255,255"},
    "Tooltip": {"BackgroundNormal": "46,46,50", "ForegroundNormal": "255,255,255"},
}
ADWAITA_DARK = {
    "Window": {"BackgroundNormal": "34,34,38", "ForegroundNormal": "255,255,255",
               "ForegroundInactive": "154,154,157"},
    "View": {"BackgroundNormal": "29,29,32", "BackgroundAlternate": "36,36,40",
             "ForegroundNormal": "255,255,255", "ForegroundInactive": "154,154,157",
             "ForegroundLink": "120,174,237", "ForegroundVisited": "220,138,221"},
    "Button": {"BackgroundNormal": "56,56,61", "ForegroundNormal": "255,255,255"},
    "Selection": {"BackgroundNormal": "53,132,228", "ForegroundNormal": "255,255,255"},
    "Tooltip": {"BackgroundNormal": "54,54,58", "ForegroundNormal": "255,255,255"},
}
# GNOME's named accent colours (Settings > Appearance).
GNOME_ACCENTS = {
    "blue": "53,132,228", "teal": "33,144,164", "green": "58,148,74", "yellow": "200,136,0",
    "orange": "237,91,0", "red": "230,45,66", "pink": "213,97,153", "purple": "145,65,172",
    "slate": "111,131,150",
}


def portal_accent(value):
    """The portal's accent colour: three numbers from 0 to 1, or anything else for none."""
    if isinstance(value, (list, tuple)) and len(value) == 3:
        try:
            parts = [float(v) for v in value]
        except (TypeError, ValueError):
            return None
        if all(0.0 <= v <= 1.0 for v in parts):
            return QColor(*(round(v * 255) for v in parts))
    return None


def pango_font(text):
    """A GNOME font setting such as "Cantarell 11" or "Noto Sans Bold 10"."""
    if not isinstance(text, str) or " " not in text.strip():
        return None
    words = text.split()
    try:
        size = float(words[-1])
    except ValueError:
        return None
    words = words[:-1]
    weights = {"thin": QFont.Weight.Thin, "light": QFont.Weight.Light,
               "medium": QFont.Weight.Medium, "semi-bold": QFont.Weight.DemiBold,
               "semibold": QFont.Weight.DemiBold, "bold": QFont.Weight.Bold,
               "heavy": QFont.Weight.Black, "black": QFont.Weight.Black}
    weight, italic = QFont.Weight.Normal, False
    while len(words) > 1 and words[-1].lower() in (*weights, "regular", "book", "italic", "oblique"):
        word = words.pop().lower()
        if word in ("italic", "oblique"):
            italic = True
        elif word in weights:
            weight = weights[word]
    if not words or not 4 <= size <= 72:
        return None
    font = QFont(" ".join(words))
    font.setPointSizeF(size)
    font.setWeight(weight)
    font.setItalic(italic)
    return font


def on_kde():
    desktops = os.environ.get("XDG_CURRENT_DESKTOP", "").upper().split(":")
    return "KDE" in desktops


def read_kdeglobals_file():
    """kdeglobals as {group: {key: value}}; empty inside the snap, which cannot read it."""
    if IN_SNAP:
        return {}
    config_home = os.environ.get("XDG_CONFIG_HOME") or str(Path.home() / ".config")
    data = {}
    for path in ("/etc/xdg/kdeglobals", os.path.join(config_home, "kdeglobals")):
        try:
            with open(path, encoding="utf-8", errors="replace") as f:
                lines = f.read().splitlines()
        except OSError:
            continue
        group = None
        for line in lines:
            line = line.strip()
            if not line or line.startswith(("#", ";")):
                continue
            if line.startswith("["):
                group = line[1:-1] if line.endswith("]") else None
                continue
            if group is None or "=" not in line:
                continue
            key, value = (part.strip() for part in line.split("=", 1))
            if key.endswith("]"):
                if "[$" not in key:
                    continue              # a translated value, such as Name[de]
                key = key[:key.index("[$")]
            data.setdefault(group, {})[key] = value
    return data


class PortalReader:
    """Reads single settings from the desktop portal's Settings interface."""

    def __init__(self):
        self.bus = QDBusConnection.sessionBus()
        self.broken = not self.bus.isConnected()

    def read(self, namespace, key, timeout=1000):
        if self.broken:
            return None
        for method in ("ReadOne", "Read"):
            msg = QDBusMessage.createMethodCall(*PORTAL, method)
            msg.setArguments([namespace, key])
            reply = self.bus.call(msg, QDBus.CallMode.Block, timeout)
            if reply.type() == QDBusMessage.MessageType.ReplyMessage:
                args = reply.arguments()
                value = args[0] if args else None
                while isinstance(value, QDBusVariant):   # Read wraps the value twice
                    value = value.variant()
                return value
            error = reply.errorName()
            if method == "ReadOne" and error == "org.freedesktop.DBus.Error.UnknownMethod":
                continue                  # an older portal: try Read instead
            if not error.startswith("org.freedesktop.portal.Error"):
                self.broken = True        # no portal at all: stop asking
            return None
        return None


def read_kdeglobals_portal():
    """The colour and font settings, and the light/dark preference, via the portal."""
    reader = PortalReader()
    scheme = reader.read("org.freedesktop.appearance", "color-scheme", timeout=2000)
    data = {}
    for group, keys in KDE_COLOUR_KEYS.items():
        for key in keys:
            value = reader.read(f"org.kde.kdeglobals.Colors:{group}", key)
            if value is not None:
                data.setdefault(f"Colors:{group}", {})[key] = value
    for key in ("font", "menuFont"):
        value = reader.read("org.kde.kdeglobals.General", key)
        if value:
            data.setdefault("General", {})[key] = str(value)
    return data, scheme


def parse_kde_colour(value):
    if isinstance(value, (list, tuple)):
        parts = list(value)
    else:
        text = str(value).strip()
        if text.startswith("#"):
            colour = QColor(text)
            return colour if colour.isValid() else None
        parts = text.split(",")
    try:
        r, g, b = (int(float(p)) for p in parts[:3])
    except (TypeError, ValueError):
        return None
    if not all(0 <= v <= 255 for v in (r, g, b)):
        return None
    return QColor(r, g, b)


def mix(a, b, amount):
    return QColor(round(a.red() + (b.red() - a.red()) * amount),
                  round(a.green() + (b.green() - a.green()) * amount),
                  round(a.blue() + (b.blue() - a.blue()) * amount))


def kde_palette(colours):
    """Turn KDE colour scheme entries into a Qt palette, the way KDE apps do."""
    c = colours
    window, button = c["Window", "BackgroundNormal"], c["Button", "BackgroundNormal"]
    pal = QPalette(button, window)        # works out the light and dark shades
    roles = {
        QPalette.ColorRole.Window: window,
        QPalette.ColorRole.WindowText: c["Window", "ForegroundNormal"],
        QPalette.ColorRole.Base: c["View", "BackgroundNormal"],
        QPalette.ColorRole.AlternateBase: c["View", "BackgroundAlternate"],
        QPalette.ColorRole.Text: c["View", "ForegroundNormal"],
        QPalette.ColorRole.PlaceholderText: c["View", "ForegroundInactive"],
        QPalette.ColorRole.Button: button,
        QPalette.ColorRole.ButtonText: c["Button", "ForegroundNormal"],
        QPalette.ColorRole.Highlight: c["Selection", "BackgroundNormal"],
        QPalette.ColorRole.HighlightedText: c["Selection", "ForegroundNormal"],
        QPalette.ColorRole.ToolTipBase: c["Tooltip", "BackgroundNormal"],
        QPalette.ColorRole.ToolTipText: c["Tooltip", "ForegroundNormal"],
        QPalette.ColorRole.Link: c["View", "ForegroundLink"],
        QPalette.ColorRole.LinkVisited: c["View", "ForegroundVisited"],
    }
    groups = (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive, QPalette.ColorGroup.Disabled)
    for group in groups:
        for role, colour in roles.items():
            pal.setColor(group, role, colour)
    # Greyed-out text: faded towards the background it sits on.
    R = QPalette.ColorRole
    for text_role, back_role in ((R.WindowText, R.Window), (R.Text, R.Base),
                                 (R.ButtonText, R.Button), (R.HighlightedText, R.Highlight)):
        pal.setColor(QPalette.ColorGroup.Disabled, text_role,
                     mix(roles[text_role], roles[back_role], 0.55))
    return pal


class MenuStyle(QProxyStyle):
    """Qt's standard style, with menu tick boxes that stay visible in dark themes
    (the standard ones draw their outline darker than a dark background)."""

    def drawPrimitive(self, element, option, painter, widget=None):
        if element != QStyle.PrimitiveElement.PE_IndicatorCheckBox or not isinstance(widget, QMenu):
            return super().drawPrimitive(element, option, painter, widget)
        pal = option.palette
        on = bool(option.state & QStyle.StateFlag.State_On)
        enabled = bool(option.state & QStyle.StateFlag.State_Enabled)
        side = min(option.rect.width(), option.rect.height(), 16) - 2
        box = QRect(0, 0, side, side)
        box.moveCenter(option.rect.center())
        accent = pal.color(QPalette.ColorRole.Highlight)
        text = pal.color(QPalette.ColorRole.WindowText)
        back = pal.color(QPalette.ColorRole.Window)
        painter.save()
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if not enabled:
            painter.setOpacity(0.45)
        frame = box.toRectF().adjusted(0.5, 0.5, -0.5, -0.5)
        painter.setPen(QPen(accent if on else mix(text, back, 0.45), 1))
        painter.setBrush(accent if on else pal.color(QPalette.ColorRole.Base))
        painter.drawRoundedRect(frame, 2.5, 2.5)
        if on:
            tick = QPainterPath()
            tick.moveTo(frame.left() + side * 0.24, frame.top() + side * 0.52)
            tick.lineTo(frame.left() + side * 0.43, frame.top() + side * 0.70)
            tick.lineTo(frame.left() + side * 0.77, frame.top() + side * 0.30)
            pen = QPen(pal.color(QPalette.ColorRole.HighlightedText), max(1.6, side / 8))
            pen.setCapStyle(Qt.PenCapStyle.RoundCap)
            pen.setJoinStyle(Qt.PenJoinStyle.RoundJoin)
            painter.setPen(pen)
            painter.setBrush(Qt.BrushStyle.NoBrush)
            painter.drawPath(tick)
        painter.restore()


class DesktopTheme(QObject):
    """Applies the desktop's colours and fonts, and again whenever they change."""

    WATCHED = ("org.kde.kdeglobals", "org.freedesktop.appearance", "org.gnome.desktop.interface")

    def __init__(self, app):
        super().__init__(app)
        self.app = app
        self.kde = on_kde()
        self.style = None
        self.active = False
        self.timer = QTimer(self)
        self.timer.setSingleShot(True)
        self.timer.setInterval(300)
        self.timer.timeout.connect(self.apply)
        self.apply()
        bus = QDBusConnection.sessionBus()
        if bus.isConnected():
            bus.connect(PORTAL[0], PORTAL[1], PORTAL[2], "SettingChanged", self.setting_changed)

    @pyqtSlot(QDBusMessage)
    def setting_changed(self, msg):
        args = msg.arguments()
        namespace = str(args[0]) if args else ""
        if namespace.startswith(self.WATCHED):
            self.timer.start()

    def use_style(self):
        if self.style is None:
            self.style = MenuStyle(QStyleFactory.create("Fusion"))
            self.app.setStyle(self.style)

    def apply(self):
        try:
            if self.kde:
                self.apply_kde()
            else:
                self.apply_portal()
        except Exception as err:          # never let theming stop the app starting
            print(f"{APP_NAME}: could not apply the desktop theme: {err}", file=sys.stderr)

    def apply_kde(self):
        data = read_kdeglobals_file()
        scheme = None
        if not any(group.startswith("Colors:") for group in data):
            portal_data, scheme = read_kdeglobals_portal()
            for group, values in portal_data.items():
                data.setdefault(group, {}).update(values)
        defaults = BREEZE_DARK if scheme == 1 else BREEZE_LIGHT
        colours = {}
        for group, keys in KDE_COLOUR_KEYS.items():
            for key in keys:
                raw = data.get(f"Colors:{group}", {}).get(key)
                colour = parse_kde_colour(raw) if raw is not None else None
                colours[group, key] = colour or parse_kde_colour(defaults[group][key])
        self.use_style()
        self.app.setPalette(kde_palette(colours))
        general = data.get("General", {})
        font = QFont()
        if general.get("font") and font.fromString(general["font"]):
            self.app.setFont(font)
        menu_font = QFont()
        if general.get("menuFont") and menu_font.fromString(general["menuFont"]):
            self.app.setFont(menu_font, "QMenu")
        self.active = True

    def apply_portal(self):
        reader = PortalReader()
        scheme = reader.read("org.freedesktop.appearance", "color-scheme", timeout=2000)
        if not isinstance(scheme, int) or isinstance(scheme, bool):
            return                        # no portal, or it does not say: keep Qt's look
        base = ADWAITA_DARK if scheme == 1 else ADWAITA_LIGHT
        colours = {(group, key): parse_kde_colour(value)
                   for group, keys in base.items() for key, value in keys.items()}
        accent = portal_accent(reader.read("org.freedesktop.appearance", "accent-color"))
        if accent is None:
            name = reader.read("org.gnome.desktop.interface", "accent-color")
            if isinstance(name, str) and name in GNOME_ACCENTS:
                accent = parse_kde_colour(GNOME_ACCENTS[name])
        if accent is not None:
            colours["Selection", "BackgroundNormal"] = accent
        self.use_style()
        self.app.setPalette(kde_palette(colours))
        font = pango_font(reader.read("org.gnome.desktop.interface", "font-name"))
        if font is not None:
            self.app.setFont(font)
            self.app.setFont(font, "QMenu")
        self.active = True


_paper = None


def paper_palette():
    """The light palette the notes have always used, whatever the desktop theme.
    Every colour is set explicitly, so nothing is taken over from the desktop."""
    global _paper
    if _paper is None:
        standard = QStyleFactory.create("Fusion").standardPalette()
        _paper = QPalette()
        for group in (QPalette.ColorGroup.Active, QPalette.ColorGroup.Inactive,
                      QPalette.ColorGroup.Disabled):
            for role in QPalette.ColorRole:
                if role not in (QPalette.ColorRole.NoRole, QPalette.ColorRole.NColorRoles):
                    _paper.setBrush(group, role, standard.brush(group, role))
    return _paper


class Popup(QWidget):
    """A small message window built like a note, so it opens just as quickly.
    (Qt's standard message boxes were slow to appear on KDE.)"""

    open_popups = []

    def __init__(self, text, title, buttons=None, default=0):
        """buttons: (label, action, warning) for each button; action runs after the
        window closes, None just closes it. Enter presses the default button."""
        super().__init__()
        self.setObjectName("popup")
        self.setPalette(paper_palette())
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.setWindowTitle(title)
        self.setWindowFlags(Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
                            | Qt.WindowType.WindowStaysOnTopHint)
        heading = QLabel(title, self)
        heading.setObjectName("heading")
        body = QLabel(text, self)
        body.setWordWrap(True)
        body.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        row = QHBoxLayout()
        row.addStretch(1)
        self.default_button = None
        for index, (label, action, warning) in enumerate(buttons or [("OK", None, False)]):
            button = QPushButton(label, self)
            if warning:
                button.setObjectName("warning")
            button.clicked.connect(lambda _=False, a=action: self.choose(a))
            if index == default:
                button.setDefault(True)
                self.default_button = button
            row.addWidget(button)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        layout.addWidget(heading)
        layout.addWidget(body)
        layout.addLayout(row)
        self.setStyleSheet("""
            #popup { background: #fffbe6; border: 1px solid #b9ad6a; }
            QLabel { color: #2b2b2b; font-size: 10pt; }
            #heading { font-weight: bold; font-size: 11pt; }
            QPushButton { color: #2b2b2b; background: #f0e6a8; border: 1px solid #b9ad6a;
                          border-radius: 3px; padding: 4px 18px; font-size: 10pt; }
            QPushButton:hover { background: #e8dc92; }
            QPushButton#warning { color: #a11212; font-weight: bold; }
        """)
        # Give the text a definite width, so the window is made tall enough for it
        # (Qt guesses too short a height for long word-wrapped text).
        self.ensurePolished()
        body.ensurePolished()
        doc = QTextDocument()
        doc.setHtml(text)
        metrics = QFontMetrics(body.font())
        longest = max((metrics.horizontalAdvance(line) for line in doc.toPlainText().splitlines()),
                      default=0)
        body.setFixedWidth(max(268, min(488, longest + 8)))
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.center() - QPoint(self.width() // 2, self.height() // 2))
        Popup.open_popups.append(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    def choose(self, action):
        self.close()
        if action is not None:
            action()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter) and self.default_button:
            self.default_button.click()
        elif event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
            self.close()
        else:
            super().keyPressEvent(event)

    def showEvent(self, event):
        super().showEvent(event)
        QTimer.singleShot(0, lambda: self.isVisible() and set_skip_taskbar(int(self.winId()), True))

    def closeEvent(self, event):
        if self in Popup.open_popups:
            Popup.open_popups.remove(self)
        event.accept()


def notify(text, title=APP_NAME, buttons=None, default=0):
    popup = Popup(text, title, buttons, default)
    popup.show()
    popup.raise_()
    popup.activateWindow()
    return popup


class Header(QWidget):
    """Top strip of a note: drag it to move the note."""

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton:
            handle = self.window().windowHandle()
            if handle:
                handle.startSystemMove()


class Note(QWidget):
    def __init__(self, manager, data):
        super().__init__()
        self.manager = manager
        self.note_id = data.get("id") or uuid.uuid4().hex
        self.colour = data.get("colour") if data.get("colour") in COLOURS else DEFAULT_COLOUR
        self.pinned = bool(data.get("pinned", False))

        self.setObjectName("note")
        self.setPalette(paper_palette())
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setWindowTitle(APP_NAME)
        self.setMinimumSize(160, 120)

        self.header = Header(self)
        self.header.setObjectName("header")
        self.header.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.header.setCursor(Qt.CursorShape.SizeAllCursor)

        new_btn = self._button("+", "New note (Ctrl+N)", lambda: manager.new_note(self))
        self.colour_btn = self._button("", "Note colour")
        menu = QMenu(self)
        for name, hex_colour in COLOURS.items():
            menu.addAction(swatch(hex_colour), name, lambda n=name: self.set_colour(n))
        self.colour_btn.setMenu(menu)
        self.colour_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        menu_btn = self._button("\u2630", "Menu")
        menu_btn.setMenu(manager.build_menu(self))
        menu_btn.setPopupMode(QToolButton.ToolButtonPopupMode.InstantPopup)
        self.header.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.header.customContextMenuRequested.connect(
            lambda pos: menu_btn.menu().exec(self.header.mapToGlobal(pos)))
        self.pin_btn = self._button("\U0001F4CC", "Keep on top")
        self.pin_btn.setCheckable(True)
        self.pin_btn.setChecked(self.pinned)
        self.pin_btn.toggled.connect(self.set_pinned)
        del_btn = self._button("✕", "Delete note", self.delete)

        bar = QHBoxLayout(self.header)
        bar.setContentsMargins(4, 2, 4, 2)
        bar.setSpacing(0)
        bar.addWidget(new_btn)
        bar.addWidget(self.colour_btn)
        bar.addWidget(menu_btn)
        bar.addStretch(1)
        bar.addWidget(self.pin_btn)
        bar.addWidget(del_btn)

        # Delete confirmation lives inside the note, so it appears instantly
        # instead of waiting for the desktop to open a separate dialog window.
        self.confirm_bar = QWidget(self)
        self.confirm_bar.setObjectName("confirm")
        self.confirm_bar.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        confirm_row = QHBoxLayout(self.confirm_bar)
        confirm_row.setContentsMargins(8, 3, 4, 3)
        confirm_row.setSpacing(2)
        confirm_row.addWidget(QLabel("Delete this note?", self.confirm_bar), 1)
        for text, slot in (("Delete", self._confirm_delete), ("Cancel", self._cancel_delete)):
            btn = QToolButton(self.confirm_bar)
            btn.setText(text)
            btn.setObjectName(text.lower())
            btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            btn.clicked.connect(slot)
            confirm_row.addWidget(btn)
        self.confirm_bar.hide()
        self.cancel_action = QAction(self)
        self.cancel_action.setShortcut("Esc")
        self.cancel_action.setEnabled(False)
        self.cancel_action.triggered.connect(self._cancel_delete)
        self.addAction(self.cancel_action)

        self.editor = QTextEdit(self)
        self.editor.setAcceptRichText(False)
        self.editor.setFrameStyle(0)
        self.editor.setPlaceholderText("Type a note…")
        self.editor.setPlainText(str(data.get("text", "")))
        self.editor.textChanged.connect(manager.schedule_save)

        grip_row = QHBoxLayout()
        grip_row.setContentsMargins(0, 0, 0, 0)
        grip_row.addStretch(1)
        self.grip = QSizeGrip(self)
        grip_row.addWidget(self.grip)

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)
        layout.addWidget(self.header)
        layout.addWidget(self.confirm_bar)
        layout.addWidget(self.editor, 1)
        layout.addLayout(grip_row)

        new_action = QAction(self)
        new_action.setShortcut("Ctrl+N")
        new_action.triggered.connect(lambda: manager.new_note(self))
        self.addAction(new_action)
        for keys, slot in (("Ctrl+H", manager.hide_all), ("Ctrl+Q", manager.quit)):
            action = QAction(self)
            action.setShortcut(keys)
            action.triggered.connect(slot)
            self.addAction(action)

        self._apply_flags()
        self._apply_style()
        self._restore_geometry(data)

    def _button(self, text, tip, slot=None):
        btn = QToolButton(self.header)
        btn.setText(text)
        btn.setToolTip(tip)
        btn.setCursor(Qt.CursorShape.ArrowCursor)
        btn.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        if slot:
            btn.clicked.connect(slot)
        return btn

    def _apply_flags(self):
        # A plain window, not Qt.Tool: tool windows are all tied to one group,
        # and the window manager then refuses to restack them on click.
        flags = Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint
        if self.pinned:
            flags |= Qt.WindowType.WindowStaysOnTopHint
        self.setWindowFlags(flags)

    def _apply_style(self):
        base = QColor(COLOURS[self.colour])
        self.colour_btn.setIcon(swatch(base.name(), 14))
        self.setStyleSheet(f"""
            #note {{ background: {base.name()}; border: 1px solid {base.darker(125).name()}; }}
            #header {{ background: {base.darker(110).name()}; }}
            #confirm {{ background: {base.darker(120).name()}; }}
            #confirm QLabel {{ color: #2b2b2b; font-size: 10pt; }}
            #confirm QToolButton {{ color: #2b2b2b; font-size: 10pt; padding: 2px 8px; }}
            #confirm QToolButton#delete {{ font-weight: bold; color: #a11212; }}
            QTextEdit {{ background: transparent; color: #2b2b2b; font-size: 11pt;
                         padding: 4px 6px; selection-background-color: {base.darker(140).name()};
                         selection-color: #000; }}
            QToolButton {{ border: none; color: #444; font-size: 13px; padding: 3px 6px;
                           border-radius: 3px; }}
            QToolButton:hover {{ background: rgba(0, 0, 0, 30); }}
            QToolButton:checked {{ background: rgba(0, 0, 0, 55); }}
            QToolButton::menu-indicator {{ image: none; }}
        """)
        # Style sheets stop the note's own palette reaching the scroll bars and the
        # resize grip, so set it on them directly: they keep the light look they
        # have always had, whatever the desktop theme.
        for part in (self.editor.verticalScrollBar(), self.editor.horizontalScrollBar(), self.grip):
            part.setPalette(paper_palette())

    def _restore_geometry(self, data):
        def num(key, default=None):
            value = data.get(key, default)
            return int(value) if isinstance(value, (int, float)) else default

        w, h = num("w", DEFAULT_SIZE[0]), num("h", DEFAULT_SIZE[1])
        self.resize(w, h)
        if num("x") is not None and num("y") is not None:
            rect = QRect(num("x"), num("y"), w, h)
            # Only reuse the saved spot if that monitor is still there.
            if QGuiApplication.screenAt(rect.center()) or QGuiApplication.screenAt(rect.topLeft()):
                self.move(rect.topLeft())
                return
        self.move(self.manager.next_position())

    def set_colour(self, name):
        self.colour = name
        self._apply_style()
        self.manager.schedule_save()

    def set_pinned(self, pinned):
        self.pinned = pinned
        pos = self.pos()
        self._apply_flags()   # changing flags hides the window
        self.move(pos)
        self.show()
        self.manager.schedule_save()

    def delete(self):
        if self.confirm_bar.isVisible():
            self._cancel_delete()   # clicking X again backs out
        elif self.editor.toPlainText().strip():
            self.confirm_bar.show()
            self.cancel_action.setEnabled(True)
        else:
            self.manager.remove_note(self)

    def _confirm_delete(self):
        self.manager.remove_note(self)

    def _cancel_delete(self):
        self.confirm_bar.hide()
        self.cancel_action.setEnabled(False)

    def to_dict(self):
        return {
            "id": self.note_id, "text": self.editor.toPlainText(),
            "colour": self.colour, "pinned": self.pinned,
            "x": self.x(), "y": self.y(), "w": self.width(), "h": self.height(),
        }

    def hide_note(self):
        self.saved_pos = self.pos()
        self.hide()
        # Drop the desktop window so showing again builds a fresh one, exactly
        # like at startup. Re-showing a withdrawn window can be slow under KWin.
        self.destroy()

    def show_note(self):
        if hasattr(self, "saved_pos"):
            self.move(self.saved_pos)
        self.show()

    def apply_taskbar(self):
        if self.isVisible():
            set_skip_taskbar(int(self.winId()), not self.manager.show_in_taskbar)

    def showEvent(self, event):
        super().showEvent(event)
        # The window manager only accepts this once the window is on screen.
        QTimer.singleShot(0, self.apply_taskbar)
        QTimer.singleShot(300, self.apply_taskbar)

    def event(self, event):
        # Clicking anywhere on a note brings it in front of the others.
        if event.type() == QEvent.Type.WindowActivate:
            self.raise_()
        return super().event(event)

    def moveEvent(self, event):
        self.manager.schedule_save()

    def resizeEvent(self, event):
        self.manager.schedule_save()

    def closeEvent(self, event):
        # Alt+F4 etc. should not lose the note; only the X button deletes.
        # When the app is quitting the close must be accepted, or Qt cancels the quit.
        if self.manager.quitting:
            event.accept()
        else:
            event.ignore()


class Manager:
    def __init__(self, app):
        self.app = app
        self.notes = []
        self.loading = True
        self.quitting = False
        self.hidden = False
        self.show_in_taskbar = False
        self.show_tray = True
        self.restore_actions = []
        saved_notes = self.load()
        self.taskbar_action = QAction("Display Application Icon")
        self.taskbar_action.setCheckable(True)
        self.taskbar_action.setChecked(self.show_in_taskbar)
        self.taskbar_action.toggled.connect(self.set_show_in_taskbar)
        self.tray_action = QAction("Display Tray Icon")
        self.tray_action.setCheckable(True)
        self.tray_action.setChecked(self.show_tray)
        self.tray_action.toggled.connect(self.set_show_tray)
        self.autostart_action = QAction("Run at Startup")
        self.autostart_action.setCheckable(True)
        self.autostart_action.setChecked(AUTOSTART_FILE.exists())
        self.autostart_action.toggled.connect(self.set_autostart)
        self.refresh_autostart()
        self.save_timer = QTimer()
        self.save_timer.setSingleShot(True)
        self.save_timer.setInterval(400)
        self.save_timer.timeout.connect(self.save)

        self.tray = None
        if QSystemTrayIcon.isSystemTrayAvailable():
            self.tray = QSystemTrayIcon(app.windowIcon())
            self.tray.setToolTip(APP_NAME)
            self.tray_menu = QMenu()
            self.tray_menu.addAction("New Note", lambda: self.new_note())
            self.toggle_action = self.tray_menu.addAction("Hide Notes", self.toggle_notes)
            self.tray_menu.addAction(self.taskbar_action)
            self.tray_menu.addAction(self.tray_action)
            self.tray_menu.addAction(self.autostart_action)
            self.tray_menu.addSeparator()
            self.tray_menu.addAction("Import Notes\u2026", self.import_notes)
            self.tray_menu.addAction("Export Notes\u2026", self.export_notes)
            self.tray_menu.addAction("Clear All Notes\u2026", self.clear_all)
            self.restore_actions.append(
                self.tray_menu.addAction("Restore Cleared Notes", self.restore_cleared))
            self.tray_menu.aboutToShow.connect(self.update_restore_actions)
            self.tray_menu.addSeparator()
            self.tray_menu.addAction("About", self.about)
            self.tray_menu.addAction("Quit Stickywell", self.quit)
            self.tray.setContextMenu(self.tray_menu)
            self.tray.activated.connect(self._tray_clicked)
            self.tray.setVisible(self.show_tray)
        else:
            self.tray_action.setEnabled(False)

        for data in saved_notes:
            self._add(data)
        self.loading = False
        if not self.notes:
            self.new_note()

    def _tray_clicked(self, reason):
        if reason == QSystemTrayIcon.ActivationReason.Trigger:
            self.toggle_notes()

    def build_menu(self, parent):
        menu = QMenu(parent)
        menu.addAction("New Note\tCtrl+N", lambda: self.new_note(parent))
        menu.addAction("Hide All Notes\tCtrl+H", self.hide_all)
        menu.addAction(self.taskbar_action)
        menu.addAction(self.tray_action)
        menu.addAction(self.autostart_action)
        menu.addSeparator()
        menu.addAction("Import Notes\u2026", self.import_notes)
        menu.addAction("Export Notes\u2026", self.export_notes)
        menu.addAction("Clear All Notes\u2026", self.clear_all)
        self.restore_actions.append(menu.addAction("Restore Cleared Notes", self.restore_cleared))
        menu.aboutToShow.connect(self.update_restore_actions)
        menu.addSeparator()
        menu.addAction("About", self.about)
        menu.addAction("Quit Stickywell\tCtrl+Q", self.quit)
        return menu

    def set_autostart(self, enabled):
        try:
            if enabled:
                AUTOSTART_FILE.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART_FILE.write_text(autostart_entry(), encoding="utf-8")
            else:
                AUTOSTART_FILE.unlink(missing_ok=True)
        except OSError as err:
            notify(f"Could not change the startup setting:\n{err}")
            self.autostart_action.blockSignals(True)
            self.autostart_action.setChecked(AUTOSTART_FILE.exists())
            self.autostart_action.blockSignals(False)

    def refresh_autostart(self):
        # If the app file was moved or replaced by a newer version, keep the
        # startup entry pointing at the copy that is running now.
        try:
            if AUTOSTART_FILE.exists() and AUTOSTART_FILE.read_text(encoding="utf-8") != autostart_entry():
                AUTOSTART_FILE.write_text(autostart_entry(), encoding="utf-8")
        except OSError:
            pass

    def tray_visible(self):
        return bool(self.tray) and self.show_tray

    def set_show_tray(self, show):
        self.show_tray = show
        if self.tray:
            self.tray.setVisible(show)
        self.save()

    def set_show_in_taskbar(self, show):
        self.show_in_taskbar = show
        for note in self.notes:
            note.apply_taskbar()
        self.save()

    def export_notes(self):
        self.save()
        start = str(USER_HOME / "stickywell-notes.json")
        path, _ = QFileDialog.getSaveFileName(
            None, "Export Notes", start,
            "Stickywell backup (*.json);;Plain text (*.txt)")
        if not path:
            return
        try:
            if path.lower().endswith(".txt"):
                texts = [n.editor.toPlainText().strip() for n in self.notes]
                body = "\n\n----------\n\n".join(t for t in texts if t) + "\n"
            else:
                if not path.lower().endswith(".json"):
                    path += ".json"
                payload = {"version": 1, "notes": [n.to_dict() for n in self.notes]}
                body = json.dumps(payload, indent=2, ensure_ascii=False)
            Path(path).write_text(body, encoding="utf-8")
        except OSError as err:
            notify(f"Could not export notes:\n{err}")
            return
        notify(f"Exported {len(self.notes)} note(s) to:\n{path}")

    BACKUP_NAME = "notes-before-clear.json"

    def backup_path(self):
        return DATA_DIR / self.BACKUP_NAME

    def backup_shown(self):
        """The backup's location as people would type it, with ~ for the home folder."""
        shown = str(self.backup_path())
        if shown.startswith(str(USER_HOME) + os.sep):
            shown = "~" + shown[len(str(USER_HOME)):]
        return html.escape(shown)

    def update_restore_actions(self):
        available = self.backup_path().is_file()
        for action in list(self.restore_actions):
            try:
                action.setEnabled(available)
            except RuntimeError:          # its note was deleted, and its menu with it
                self.restore_actions.remove(action)

    def clear_all(self):
        if not any(note.editor.toPlainText().strip() for note in self.notes):
            notify("There are no notes to clear.")
            return
        count = len(self.notes)
        what = "your note" if count == 1 else f"all {count} notes"
        notify(f"Delete {what}?<br><br>"
               "Before deleting, Stickywell saves a backup copy of your notes, so you can "
               "get them back: straight away with <i>Undo</i>, or later with "
               "<i>Restore Cleared Notes</i> in the \u2630 menu.<br><br>"
               f"The backup is saved in<br><i>{self.backup_shown()}</i><br>"
               "Only one backup is kept: clearing all notes again replaces it.",
               "Clear All Notes",
               buttons=[("Clear All", self._clear_all_now, True), ("Cancel", None, False)],
               default=1)

    def _clear_all_now(self):
        if self.hidden:
            self.show_all()
        backup = self.backup_path()
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            payload = {"version": 1, "notes": [n.to_dict() for n in self.notes]}
            tmp = backup.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, backup)
        except OSError as err:
            notify("Could not save the backup copy, so nothing was deleted:<br>"
                   f"{html.escape(str(err))}")
            return
        for note in list(self.notes):
            self.notes.remove(note)
            note.hide()
            note.deleteLater()
        self.new_note()               # one fresh, empty note to start again with
        self.save()
        notify("All notes deleted.<br><br>"
               "To get them back, click <i>Undo</i> now, or later choose "
               "<i>Restore Cleared Notes</i> in the \u2630 menu.<br><br>"
               f"The backup copy is in<br><i>{self.backup_shown()}</i><br>"
               "It stays there until the next time you clear all notes.",
               "Clear All Notes",
               buttons=[("Undo", self.restore_cleared, False), ("OK", None, False)], default=1)

    def restore_cleared(self):
        """Bring back the notes saved by the last Clear All (Undo, or the menu item later)."""
        try:
            data = json.loads(self.backup_path().read_text(encoding="utf-8"))
            saved = [n for n in data.get("notes", []) if isinstance(n, dict)]
        except FileNotFoundError:
            notify("There are no cleared notes to restore.")
            return
        except (OSError, ValueError, AttributeError) as err:
            notify(f"Could not restore the cleared notes:<br>{html.escape(str(err))}")
            return
        if self.hidden:
            self.show_all()
        existing = {(n.editor.toPlainText(), n.colour) for n in self.notes}
        missing = [n for n in saved
                   if (str(n.get("text", "")), n.get("colour", DEFAULT_COLOUR)) not in existing]
        if not missing:
            notify("The cleared notes are already back.")
            return
        # Remove the empty note that clearing left, unless something was typed into it.
        for note in list(self.notes):
            if not note.editor.toPlainText().strip():
                self.notes.remove(note)
                note.hide()
                note.deleteLater()
        for item in missing:
            self._add(item).raise_()
        self.save()

    def import_notes(self):
        path, _ = QFileDialog.getOpenFileName(
            None, "Import Notes", str(USER_HOME),
            "Notes (*.json *.txt);;All files (*)")
        if not path:
            return
        try:
            raw = Path(path).read_text(encoding="utf-8")
            if path.lower().endswith(".json"):
                data = json.loads(raw)
                items = data.get("notes", []) if isinstance(data, dict) else data
                items = [n for n in items if isinstance(n, dict) and isinstance(n.get("text", ""), str)]
            else:
                items = [{"text": raw.strip()}] if raw.strip() else []
        except (OSError, ValueError) as err:
            notify(f"Could not import that file:\n{err}")
            return
        if not items:
            notify("No notes were found in that file.")
            return
        if self.hidden:
            self.show_all()
        existing = {(n.editor.toPlainText(), n.colour) for n in self.notes}
        added = 0
        for item in items:
            if (item.get("text", ""), item.get("colour", DEFAULT_COLOUR)) in existing:
                continue   # already have this note
            item = dict(item)
            item.pop("id", None)
            self._add(item).raise_()
            added += 1
        self.save()
        skipped = len(items) - added
        extra = f" ({skipped} already here, skipped)" if skipped else ""
        notify(f"Imported {added} note(s){extra}.")

    def quit(self):
        self.save()
        self.quitting = True
        self.app.quit()

    def _set_hidden(self, hidden):
        self.hidden = hidden
        if self.tray:
            self.toggle_action.setText("Show Notes" if hidden else "Hide Notes")

    def hide_all(self):
        self.save()
        for note in self.notes:
            note.hide_note()
        self._set_hidden(True)
        if self.tray_visible():
            if not getattr(self, "hint_shown", False):
                self.hint_shown = True
                self.tray.showMessage(APP_NAME, "Notes hidden. Click this icon to show them again.",
                                      self.app.windowIcon(), 4000)
        elif not getattr(self, "relaunch_hint_shown", False):
            self.relaunch_hint_shown = True
            notify(
                "Notes are hidden and Stickywell is still running in the background.\n\n"
                "To bring them back, start Stickywell again from your application menu.")

    def toggle_notes(self):
        if self.hidden:
            self.show_all()
        else:
            self.hide_all()

    def load(self):
        try:
            data = json.loads(DATA_FILE.read_text(encoding="utf-8"))
            settings = data.get("settings", {})
            self.show_in_taskbar = bool(settings.get("show_in_taskbar", False))
            self.show_tray = bool(settings.get("show_tray", True))
            return [n for n in data.get("notes", []) if isinstance(n, dict)]
        except FileNotFoundError:
            return []
        except (OSError, ValueError, AttributeError):
            # Keep the unreadable file rather than overwrite it.
            try:
                DATA_FILE.rename(DATA_FILE.with_suffix(".json.broken"))
            except OSError:
                pass
            return []

    def save(self):
        if self.loading:
            return
        self.save_timer.stop()
        try:
            DATA_DIR.mkdir(parents=True, exist_ok=True)
            tmp = DATA_FILE.with_suffix(".json.tmp")
            payload = {"version": 1,
                       "settings": {"show_in_taskbar": self.show_in_taskbar,
                                    "show_tray": self.show_tray},
                       "notes": [n.to_dict() for n in self.notes]}
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
            os.replace(tmp, DATA_FILE)
        except OSError as err:
            print(f"{APP_NAME}: could not save notes: {err}", file=sys.stderr)

    def schedule_save(self):
        if not self.loading and not self.hidden and not self.quitting:
            self.save_timer.start()

    def next_position(self):
        screen = QGuiApplication.primaryScreen().availableGeometry()
        step = 28 * (len(self.notes) % 10)
        return QPoint(screen.x() + 80 + step, screen.y() + 80 + step)

    def _add(self, data):
        note = Note(self, data)
        self.notes.append(note)
        note.show()
        return note

    def position_near(self, source):
        """A spot just down and right of the note the new one was added from,
        kept on the same monitor."""
        w, h = DEFAULT_SIZE
        step = 30
        screen = QGuiApplication.screenAt(source.geometry().center()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        x, y = source.x() + step, source.y() + step
        if x + w > area.right():
            x = source.x() - step
        if y + h > area.bottom():
            y = source.y() - step
        x = max(area.left(), min(x, area.right() - w))
        y = max(area.top(), min(y, area.bottom() - h))
        return x, y

    def new_note(self, source=None):
        if self.hidden:
            self.show_all()
        if source in self.notes:
            x, y = self.position_near(source)
            data = {"colour": source.colour, "x": x, "y": y}
        else:
            data = {"colour": self.notes[-1].colour if self.notes else DEFAULT_COLOUR}
        note = self._add(data)
        note.raise_()
        note.activateWindow()
        note.editor.setFocus()
        self.save()

    def remove_note(self, note):
        self.notes.remove(note)
        note.hide()
        note.deleteLater()
        self.save()
        if not self.notes and not self.tray_visible():
            self.quit()   # nothing left on screen to bring the app back from

    def show_all(self):
        if self.hidden:
            # Show them all in one go so they appear together.
            for note in self.notes:
                note.show_note()
        else:
            for note in self.notes:
                note.show()
                note.raise_()
        self._set_hidden(False)
        if self.notes:
            self.notes[-1].activateWindow()
        else:
            self.new_note()

    def about(self):
        text = f"<b>{APP_NAME}</b> {VERSION}<br>Simple sticky notes for your desktop."
        if IN_SNAP:
            text += ("<br><br><b>Updating</b><br>"
                     "The Snap Store can't update Stickywell while it is open. Hiding "
                     "your notes doesn't close it: it stays open until you choose "
                     "<i>Quit Stickywell</i> from the \u2630 menu (or press Ctrl+Q).<br><br>"
                     "To update, quit Stickywell, let the update finish, then start it "
                     "again. Your notes are saved as you type, so nothing is lost.<br><br>"
                     "If Stickywell stays open, the update is installed anyway within two "
                     "weeks and takes effect the next time it starts.")
        notify(text, f"About {APP_NAME}")


def main():
    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setDesktopFileName(APP_ID)
    app.setWindowIcon(app_icon())
    app.setQuitOnLastWindowClosed(False)

    # Single instance: a second launch just brings the notes back into view.
    server_name = f"{APP_ID}-{os.getuid()}"
    probe = QLocalSocket()
    probe.connectToServer(server_name)
    if probe.waitForConnected(300):
        probe.write(b"show")
        probe.waitForBytesWritten(300)
        return 0
    QLocalServer.removeServer(server_name)
    server = QLocalServer()
    server.listen(server_name)

    theme = DesktopTheme(app)   # noqa: F841 (kept alive for theme changes)
    manager = Manager(app)

    def on_connection():
        conn = server.nextPendingConnection()
        conn.disconnected.connect(conn.deleteLater)
        manager.show_all()

    server.newConnection.connect(on_connection)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
