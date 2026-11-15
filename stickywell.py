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
import json
import os
import sys
import uuid
from pathlib import Path

# Wayland does not let apps place their own windows, so notes could not
# reopen where you left them. Prefer X11 (XWayland), fall back to Wayland.
os.environ.setdefault("QT_QPA_PLATFORM", "xcb;wayland")

from PyQt6.QtCore import QEvent, QPoint, QRect, Qt, QTimer
from PyQt6.QtGui import QAction, QColor, QCursor, QGuiApplication, QIcon, QPainter, QPixmap
from PyQt6.QtNetwork import QLocalServer, QLocalSocket
from PyQt6.QtWidgets import (
    QApplication, QFileDialog, QHBoxLayout, QLabel, QMenu, QPushButton, QSizeGrip,
    QSystemTrayIcon, QTextEdit, QToolButton, QVBoxLayout, QWidget,
)

APP_NAME = "Stickywell"
APP_ID = "stickywell"
VERSION = "1.0"

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

DATA_DIR = Path(os.environ.get("XDG_DATA_HOME") or Path.home() / ".local/share") / APP_ID
DATA_FILE = DATA_DIR / "notes.json"

# Carry over notes from before the app was renamed.
_OLD_DIR = DATA_DIR.parent / "stickwell"
if _OLD_DIR.is_dir() and not DATA_DIR.exists():
    try:
        _OLD_DIR.rename(DATA_DIR)
    except OSError:
        pass


IN_SNAP = bool(os.environ.get("SNAP") and os.environ.get("SNAP_USER_DATA"))
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


class Popup(QWidget):
    """A small message window built like a note, so it opens just as quickly.
    (Qt's standard message boxes were slow to appear on KDE.)"""

    open_popups = []

    def __init__(self, text, title):
        super().__init__()
        self.setObjectName("popup")
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
        ok = QPushButton("OK", self)
        ok.setDefault(True)
        ok.clicked.connect(self.close)
        row = QHBoxLayout()
        row.addStretch(1)
        row.addWidget(ok)
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
        """)
        self.setMinimumWidth(300)
        self.setMaximumWidth(520)
        self.adjustSize()
        screen = QGuiApplication.screenAt(QCursor.pos()) or QGuiApplication.primaryScreen()
        area = screen.availableGeometry()
        self.move(area.center() - QPoint(self.width() // 2, self.height() // 2))
        Popup.open_popups.append(self)

    def mousePressEvent(self, event):
        if event.button() == Qt.MouseButton.LeftButton and self.windowHandle():
            self.windowHandle().startSystemMove()

    def keyPressEvent(self, event):
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter, Qt.Key.Key_Escape):
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


def notify(text, title=APP_NAME):
    popup = Popup(text, title)
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
        grip_row.addWidget(QSizeGrip(self))

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
        notify(f"<b>{APP_NAME}</b> {VERSION}<br>Simple sticky notes for your desktop.",
               f"About {APP_NAME}")


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

    manager = Manager(app)

    def on_connection():
        conn = server.nextPendingConnection()
        conn.disconnected.connect(conn.deleteLater)
        manager.show_all()

    server.newConnection.connect(on_connection)
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())
