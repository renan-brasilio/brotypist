"""KWin private input method with a separately owned, verified public ghost.

Native connections and presentation live on the Qt application thread. AT-SPI
text is usable only when it matches the private compositor context; optional
public-display evidence gates inline rendering without authorizing insertion.
"""
from __future__ import annotations

import atexit
import ctypes
from dataclasses import replace
import os
from pathlib import Path
import stat
import threading
import time
from typing import Callable

from ..core import Snapshot
from .base import BridgeBase, Shortcut
from .linux import LinuxBridge

_prepared_fd: int | None = None
_preparation_lock = threading.Lock()


def close_prepared_input_method() -> None:
    """Release an unused private socket, including an early duplicate launch."""
    global _prepared_fd
    with _preparation_lock:
        fd, _prepared_fd = _prepared_fd, None
    if fd is not None:
        os.close(fd)


atexit.register(close_prepared_input_method)


def prepare_input_method(fd: int) -> None:
    """Take ownership of KWin's private WAYLAND_SOCKET before Qt starts.

    Remove its environment advertisement so Qt opens its own ordinary display,
    not the privileged input-method connection. No desktop configuration changes.
    The caller must not close or reuse the transferred descriptor.
    """
    global _prepared_fd
    if not isinstance(fd, int) or isinstance(fd, bool) or fd < 0:
        raise ValueError("A private input-method socket descriptor is required")
    with _preparation_lock:
        if _prepared_fd is not None:
            raise RuntimeError("An input-method descriptor is already prepared")
        try:
            advertised = os.environ.get("WAYLAND_SOCKET")
            if advertised is not None and int(advertised) != fd:
                raise ValueError("WAYLAND_SOCKET does not identify this descriptor")
            if not stat.S_ISSOCK(os.fstat(fd).st_mode):
                raise ValueError("The input-method descriptor is not a socket")
            os.set_inheritable(fd, False)
        except Exception:
            os.close(fd)
            raise
        os.environ.pop("WAYLAND_SOCKET", None)
        _prepared_fd = fd


def available_input_method() -> bool:
    """Whether a private descriptor is waiting for a single bridge constructor."""
    with _preparation_lock:
        return _prepared_fd is not None


class _Event(ctypes.Structure):
    _fields_ = [("type", ctypes.c_int),
                *((name, ctypes.c_uint32) for name in
                  ("generation", "serial", "time", "key", "state", "modifiers")),
                ("x", ctypes.c_int32), ("y", ctypes.c_int32), ("name", ctypes.c_char_p),
                ("scene", ctypes.c_uint64), ("frame", ctypes.c_uint64)]


_Callback = ctypes.CFUNCTYPE(ctypes.c_int, ctypes.c_void_p, ctypes.POINTER(_Event))


class _GhostWindow(ctypes.Structure):
    _fields_ = [("scene", ctypes.c_uint64), ("pid", ctypes.c_uint32),
                *((name, ctypes.c_int32) for name in ("x", "y", "width", "height"))]


def _load_library():
    """Load the adjacent native library and bind this tree's exact ctypes ABI."""
    library = ctypes.CDLL(str(Path(__file__).with_name("libinklow_wayland.so")))
    pointer, u32 = ctypes.c_void_p, ctypes.c_uint32
    signatures = {
        "connect": ([ctypes.c_int, _Callback, pointer], pointer),
        "fd": ([pointer], ctypes.c_int),
        "compositor_pid": ([pointer], u32),
        "dispatch": ([pointer], ctypes.c_int),
        "flush": ([pointer], ctypes.c_int),
        "close": ([pointer], None),
        "state": ([pointer, ctypes.POINTER(u32), ctypes.POINTER(u32)], ctypes.c_int),
        "context": ([pointer], ctypes.c_uint64),
        "matches": ([pointer, u32, u32, ctypes.c_char_p, ctypes.c_size_t, ctypes.c_size_t], ctypes.c_int),
        "commit": ([pointer, u32, u32, ctypes.c_char_p], ctypes.c_int),
        "present": ([pointer, pointer, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_int),
        "hide": ([pointer], None),
        "ghost_fd": ([pointer], ctypes.c_int),
        "ghost_dispatch": ([pointer], ctypes.c_int),
        "ghost_flush": ([pointer], ctypes.c_int),
        "ghost_window": ([pointer, u32, ctypes.POINTER(_GhostWindow)], ctypes.c_int),
        "ghost_prepare": ([pointer, u32, ctypes.c_uint64, *([ctypes.c_int] * 4)], ctypes.c_int),
        "ghost_scale": ([pointer], ctypes.c_int),
        "ghost_present": ([pointer, ctypes.c_uint64, ctypes.c_uint64, pointer,
                           ctypes.c_int, ctypes.c_int, ctypes.c_int], ctypes.c_int),
        "ghost_hide": ([pointer], None),
        "ghost_desktop_state": ([pointer, ctypes.c_int], None),
        "composition_inactive": ([pointer], ctypes.c_int),
    }
    for name, (arguments, result) in signatures.items():
        function = getattr(library, f"inklow_wl_{name}")
        function.argtypes, function.restype = arguments, result
    return library


class WaylandBridge(LinuxBridge):
    """Bind AT-SPI snapshots to input context and separately prove ghost visibility.

    Call native operations on QApplication's thread. The inherited lock
    serializes work but does not make Wayland connections cross-thread safe.
    """

    supports_native_overlay = True
    strict_visible_acceptance = True

    def __init__(self) -> None:
        """Consume the prepared socket and attach owned connections to Qt notifiers."""
        global _prepared_fd
        super().__init__(accessibility_only=True)
        self._client = None
        self._library = None
        self._read_notifier = None
        self._write_notifier = None
        self._ghost_read_notifier = None
        self._ghost_write_notifier = None
        self._ghost_event = None
        self._ghost_snapshot = None
        self._ghost_scene = self._ghost_frame = 0
        self._ghost_text = ""
        self._ghost_pending = self._ghost_presented = False
        self._dispatching = False
        self._native_depth = 0
        self._close_pending = False
        self._ui_callback: Callable[..., None] | None = None
        self._needs_context_state = 0
        self._context_sync_pending = False
        self._context_synced = (0, 0)
        self._context_calls: set = set()
        self._desktop_watch = None
        self._panel_snapshot: Snapshot | None = None
        self._panel_paused = False
        self._menu: list[tuple[str, str, bool]] | None = None
        self._menu_page = 0
        self._menu_page_size = 1
        self._menu_selected = -1
        self._pending_render = False
        self._regions: list[tuple[tuple[int, int, int, int], str, bool]] = []
        self._pressed: tuple[str, str] | None = None
        self._native_callback = _Callback(self._event)
        with _preparation_lock:
            fd, _prepared_fd = _prepared_fd, None
        if fd is None:
            self._available = False
            self._set_status("Wayland requires an explicit KWin input-method launch with its private socket.")
            return
        if not self._available:
            os.close(fd)
            return
        try:
            from PySide6.QtCore import QSocketNotifier, QThread
            from PySide6.QtWidgets import QApplication
            app = QApplication.instance()
            if app is None or QThread.currentThread() != app.thread():
                raise RuntimeError("The Wayland bridge requires QApplication's thread")
            self._library = _load_library()
            owned_fd, fd = fd, None
            self._dispatching = True
            try:
                self._client = self._library.inklow_wl_connect(owned_fd, self._native_callback, None)
            finally:
                self._dispatching = False
            if not self._client or self._close_pending:
                raise RuntimeError("KWin input-method connection unavailable")
            display_fd = self._library.inklow_wl_fd(self._client)
            if display_fd < 0:
                raise RuntimeError("Input-method descriptor unavailable")
            self._read_notifier = QSocketNotifier(display_fd, QSocketNotifier.Type.Read)
            self._write_notifier = QSocketNotifier(display_fd, QSocketNotifier.Type.Write)
            self._write_notifier.setEnabled(False)
            self._read_notifier.activated.connect(self._read_ready)
            self._write_notifier.activated.connect(self._write_ready)
            public_fd = self._native_call("ghost_fd", self._client)
            if public_fd >= 0:
                self._ghost_read_notifier = QSocketNotifier(public_fd, QSocketNotifier.Type.Read)
                self._ghost_write_notifier = QSocketNotifier(public_fd, QSocketNotifier.Type.Write)
                self._ghost_write_notifier.setEnabled(False)
                self._ghost_read_notifier.activated.connect(self._ghost_read_ready)
                self._ghost_write_notifier.activated.connect(self._ghost_write_ready)
            self._flush()
            if self._client:
                self._set_status(
                    "KWin input method ready. Inline completion requires verified compositor "
                    "capabilities, an undecorated/CSD window with matching client geometry and "
                    "observable host-font metrics. Unsupported fields remain manual-only; no "
                    "preedit or suggestion popup is sent. Passwords and selections are excluded."
                )
                self._start_desktop_watch()
                self._queue_context_sync()
        except Exception as exc:
            if fd is not None:
                os.close(fd)
            self._release_native()
            self._available = False
            self._set_status(
                f"KWin input method unavailable ({type(exc).__name__}: {exc}). "
                "Start the installed BroTypist input-method entry through KDE; the native library, Qt and desktop AT-SPI must be available."
            )

    def _native_call(self, name: str, *arguments):
        """Defer destruction until synchronous native callbacks have returned."""
        self._native_depth += 1
        try:
            return getattr(self._library, f"inklow_wl_{name}")(*arguments)
        finally:
            self._native_depth -= 1
            if self._close_pending and not self._native_depth and not self._dispatching:
                self._release_native()

    def _state(self) -> tuple[int, int] | None:
        """Return the safe native generation/serial pair, or None when unavailable."""
        if not self._available or not self._client or self._stopped.is_set():
            return None
        generation, serial = ctypes.c_uint32(), ctypes.c_uint32()
        if self._native_call("state", self._client, ctypes.byref(generation), ctypes.byref(serial)) != 1:
            return None
        return generation.value, serial.value

    @staticmethod
    def _anchor(state: tuple[int, int]) -> str:
        return f"{state[0]}:{state[1]}"

    def _capture_native(self) -> Snapshot | None:
        """Match AT-SPI text/caret to native UTF-8 state before deriving geometry."""
        before = self._state()
        if before is None:
            return None  # Do not obtain an AT-SPI Text interface while native purpose is unknown.
        result = super()._capture(geometry=False, reject_selection=True)
        if result is None:
            return None
        snapshot, _, text_iface = result
        if snapshot.protected or snapshot.selection_length or "\x00" in snapshot.text:
            return None
        # Revalidate focus and field identity after the synchronous provider read.
        focused = self._focused()
        if focused is None:
            return None
        element, _, (pid, app_id) = focused
        object_path = getattr(element, "path", None)
        field_id = f"{pid}:{object_path if object_path else hash(element)}"
        state = element.getState()
        a = self.atspi
        if (app_id != snapshot.app_id or field_id != snapshot.field_id
                or element.getRole() not in (a.ROLE_ENTRY, a.ROLE_TEXT)
                or not all(state.contains(flag) for flag in
                           (a.STATE_FOCUSED, a.STATE_EDITABLE, a.STATE_ENABLED, a.STATE_SENSITIVE))
                or state.contains(a.STATE_DEFUNCT)
                or int(text_iface.caretOffset) != snapshot.caret
                or int(text_iface.characterCount) != len(snapshot.text)
                or int(text_iface.getNSelections()) != 0):
            return None
        for attribute in element.getAttributes():
            if ":" not in attribute:
                continue
            key, value = (part.casefold() for part in attribute.split(":", 1))
            if ((("password" in key or "protected" in key) and value not in ("false", "0", "no"))
                    or value == "password"
                    or (key in ("preedit", "composing", "composition-active")
                        and value not in ("false", "0", "no", ""))):
                return None
        encoded = snapshot.text.encode("utf-8", errors="strict")
        cursor = len(snapshot.prefix.encode("utf-8", errors="strict"))
        if self._state() != before or self._native_call(
                "matches", self._client, *before, encoded, len(encoded), cursor) != 1:
            return None
        context_id = self._native_call("context", self._client)
        if not context_id:
            return None
        current = replace(snapshot, field_id=f"{snapshot.field_id}:wl:{context_id}",
                          caret_rect=None, field_rect=None, native_anchor=self._anchor(before),
                          native_inline=False)
        rendered = self._ghost_geometry(current, element, text_iface, pid)
        message = ("Current field supports verified compositor inline text."
                   if rendered is not None else
                   f"{self._geometry_reason}; manual controls only.")
        if getattr(self, "_inline_status", None) != message:
            self._inline_status = message
            self._set_status(message)
        return rendered or current

    def _ghost_window(self, pid: int):
        """Return unique active-window evidence for this PID, never guessed bounds."""
        if getattr(self, "_ghost_disabled", False):
            return None
        window = _GhostWindow()
        if (self._native_call("ghost_window", self._client, pid, ctypes.byref(window)) != 1
                or not window.scene or window.pid != pid or window.width <= 0 or window.height <= 0):
            return None
        return window

    def _ghost_geometry(self, snapshot, element, interface, pid):
        """Translate WINDOW coordinates only with an independently proven origin."""
        self._geometry_reason = "Current field lacks verified client/font metrics"
        try:
            window = self._ghost_window(pid)
            if window is None:
                self._geometry_reason = "Compositor inline capability or unique active-window metadata unavailable"
                return None
            if self._native_call("composition_inactive", self._client) != 1:
                self._geometry_reason = "Current field has no positive inactive-composition proof"
                return None
            ancestor = self._window_ancestor(element)
            if ancestor is None:
                return None
            a = self.atspi
            state = ancestor.getState()
            if not all(state.contains(flag) for flag in (a.STATE_ACTIVE, a.STATE_VISIBLE, a.STATE_SHOWING)):
                return None
            bounds = ancestor.queryComponent().getExtents(a.WINDOW_COORDS)
            # No inferred titlebar, border, DPI or symmetric decoration offsets.
            # Only exact client/frame agreement establishes a global client origin.
            if (bounds.x, bounds.y, bounds.width, bounds.height) != (0, 0, window.width, window.height):
                return None
            x, y, width, height = interface.getCharacterExtents(snapshot.caret, a.WINDOW_COORDS)
            if width < 0 or height <= 0:
                return None
            rect = (float(window.x + x), float(window.y + y), float(max(1, width)), float(height))
            attrs = dict(item.split(":", 1) for item in element.getAttributes() if ":" in item)
            render = self._inline_metrics(
                element, interface, snapshot.text, snapshot.caret, rect, attrs,
                coordinates=a.WINDOW_COORDS, origin=(window.x, window.y), composition_inactive=True)
            after = self._ghost_window(pid)
            state = self._state()
            if (render is None or after is None or bytes(after) != bytes(window)
                    or state is None or self._anchor(state) != snapshot.native_anchor
                    or self._native_call("composition_inactive", self._client) != 1):
                return None
            render = replace(render, source=f"wayland-atspi-font-run:{window.scene}")
            return replace(snapshot, caret_rect=rect, inline_render=render, window_pid=pid,
                           window_bounds=(window.x, window.y, window.width, window.height),
                           window_revision=str(window.scene))
        except Exception:
            return None

    def context_matches(self, snapshot):
        """Decline background context verification across the Qt-thread boundary."""
        # The private input-method connection belongs exclusively to the Qt
        # thread. A background context worker cannot safely verify its serial.
        return False

    def capture(self) -> Snapshot | None:
        """Return a guarded snapshot on the Qt thread, or None on provider errors."""
        with self._lock:
            try:
                return self._capture_native()
            except Exception:
                return None

    def insert_inline(self, snapshot: Snapshot, text: str) -> bool:
        """Ordinary acceptance always needs the still-presented exact frame."""
        with self._lock:
            if (not isinstance(text, str) or not text or self._ghost_event is None
                    or not self._ghost_presented or not self._ghost_text.startswith(text)
                    or snapshot.key != self._ghost_snapshot.key or not self._ghost_current()):
                self._hide_ghost()
                return False
            return self.insert(snapshot, text)

    def insert(self, snapshot: Snapshot, text: str) -> bool:
        """Commit to the unchanged context and confirm the resulting AT-SPI text.

        False includes unconfirmed delivery; it is not proof that no text was
        inserted and must not be used as an automatic retry signal.
        """
        if not isinstance(text, str) or not text or "\x00" in text or snapshot.protected or snapshot.selection_length:
            return False
        with self._lock:
            try:
                self._hide_ghost()
                current = self._capture_native()
                if (current is None or current.key != snapshot.key
                        or current.native_anchor != snapshot.native_anchor):
                    return False
                state = self._state()
                if state is None or self._anchor(state) != snapshot.native_anchor:
                    return False
                context_id = self._native_call("context", self._client)
                base_field = current.field_id.removesuffix(f":wl:{context_id}")
                expected = replace(current, field_id=base_field, native_anchor="",
                                   text=current.prefix + text + current.suffix, caret=current.caret + len(text))
                committed = self._native_call(
                    "commit", self._client, *state, text.encode("utf-8", errors="strict")) == 1
                self._flush()
                if committed:
                    committed = self._confirm_insertion(expected, context_id)
                if committed:
                    self.hide_native()
                return committed
            except Exception:
                return False

    def _confirm_insertion(self, expected: Snapshot, context_id: int) -> bool:
        """Poll the same field with a 250 ms deadline, aborting on identity changes."""
        # commit_string has no success reply. Confirm the same target actually
        # applied the text; do not count a validator-rejected send as acceptance.
        deadline = time.monotonic() + 0.25
        while time.monotonic() < deadline:
            if self._native_call("context", self._client) != context_id:
                return False
            focused = self._focused()
            if focused is None:
                return False
            element, _, (pid, app_id) = focused
            path = getattr(element, "path", None)
            field_id = f"{pid}:{path if path else hash(element)}"
            if app_id != expected.app_id or field_id != expected.field_id:
                return False
            observed = LinuxBridge._capture(self, geometry=False, reject_selection=True)
            if observed is not None and observed[0].key == expected.key:
                return True
            time.sleep(0.003)
        return False

    def replace_range(self, snapshot: Snapshot, start: int, length: int, text: str) -> bool:
        """Support only zero-length insertion at the existing unselected caret."""
        if (self._valid_replacement(snapshot, start, length, text) and length == 0
                and start == snapshot.caret and not snapshot.selection_length):
            return self.insert(snapshot, text)
        return False  # Never delete selected text or an emoji suffix before committing.

    def configure(self, config) -> None:
        """Publish policy and invalidate visible controls tied to the old policy."""
        BridgeBase.configure(self, config)
        self.hide_native()

    def start_shortcuts(self, callback: Shortcut) -> None:
        """Register dispatch without starting the inherited X11 hook thread."""
        BridgeBase.start_shortcuts(self, callback)

    def set_shortcuts_enabled(self, enabled: bool) -> None:
        """Change dispatch permission and cancel an in-progress pointer gesture."""
        BridgeBase.set_shortcuts_enabled(self, enabled)
        self._pressed = None

    @staticmethod
    def _key(event: _Event) -> tuple[str, tuple[str, ...]]:
        """Map native key names while retaining unsupported modifiers as rejection."""
        mask = int(event.modifiers)
        # Unsupported modifiers are preserved as a rejection, never dropped.
        if mask & ~(1 | 4 | 8 | 64):
            return "", ("AltGr",)
        modifiers = tuple(name for bit, name in ((4, "Ctrl"), (8, "Alt"), (1, "Shift"), (64, "Super"))
                          if mask & bit)
        name = (event.name or b"").decode("ascii", errors="strict")
        key = {"ISO_Left_Tab": "Tab", "space": "Space", "KP_Enter": "Return"}.get(name, name)
        if int(event.key) == 49:
            key = "AboveTab"
        elif len(key) == 1 and key.isascii() and key.isalnum():
            key = key.upper()
        elif key not in {"Tab", "Escape", "Space", "Return", "Up", "Down", "Home", "End", *(f"F{i}" for i in range(1, 13))}:
            key = ""
        return key, modifiers

    def _menu_key(self, key: str, modifiers: tuple[str, ...]) -> bool:
        """Consume unmodified menu navigation, skipping disabled action rows."""
        if self._menu is None or modifiers:
            return False
        if key == "Escape":
            self._menu = None
            self._render()
            if self._ui_callback:
                self._ui_callback("menu_closed")
            return True
        enabled = [index for index, (_, _, active) in enumerate(self._menu) if active]
        if key in {"Up", "Down", "Home", "End"}:
            if enabled:
                selected = enabled.index(self._menu_selected) if self._menu_selected in enabled else 0
                if key == "Home":
                    selected = 0
                elif key == "End":
                    selected = len(enabled) - 1
                else:
                    selected = (selected + (1 if key == "Down" else -1)) % len(enabled)
                self._menu_selected = enabled[selected]
                self._menu_page = self._menu_selected // self._menu_page_size
                self._render()
            return True
        if key == "Return":
            if self._menu_selected in enabled:
                action = self._menu[self._menu_selected][0]
                self._menu = None
                self.hide_native()
                if self._ui_callback:
                    self._ui_callback(action)
            return True
        return False

    def _event(self, userdata, pointer) -> int:
        """Handle native callbacks; return 1 only for a consumed keyboard action."""
        try:
            event = pointer.contents
            if event.type == 1:
                self._note_input()
                self._needs_context_state = event.state
                if self._needs_context_state:
                    self._queue_context_sync()
                state = self._state()
                if self._panel_snapshot and (state is None or self._anchor(state) != self._panel_snapshot.native_anchor):
                    self.hide_native()
                if self._ghost_snapshot and not self._ghost_current():
                    self._hide_ghost()
                if state is not None and self._ui_callback:
                    self._ui_callback("context_changed")
            elif event.type == 2 and event.state == 1:
                self._note_input()
                state = self._state()
                if state is None or state[0] != event.generation:
                    return 0
                if self._ghost_snapshot and not self._ghost_current():
                    self._hide_ghost()
                key, modifiers = self._key(event)
                if self._menu is not None:
                    return int(self._menu_key(key, modifiers))
                if not self._enabled:
                    return 0
                action = self._key_action(key, modifiers, platform="linux") if key else None
                return int(bool(action and self._dispatch(action)))
            elif event.type == 3:
                self._pointer(event)
            elif event.type == 4:
                self._set_status("The KWin input-method connection closed; native typing is unavailable.")
                self.stop()
            elif event.type == 5 and self._pending_render:
                if not self._render():
                    self.hide_native()
            elif event.type == 6:
                if event.state == 1:
                    self._hide_ghost(deferred=True)
                    if self._ui_callback:
                        self._ui_callback("context_changed")
                elif event.state == 2 and self._ghost_pending and event.scene == self._ghost_scene:
                    self._render_ghost()
            elif event.type == 7:
                if (self._ghost_event is not None and event.scene == self._ghost_scene
                        and event.frame == self._ghost_frame):
                    if (event.state == 1 and self._state() == (event.generation, event.serial)
                            and self._ghost_current()):
                        self._ghost_presented = True
                        self._presentation(self._ghost_event, self._ghost_text)
                    else:
                        self._hide_ghost(deferred=True)
        except Exception:
            # A ctypes callback must never leak an exception or suppress a normal key.
            self._set_status("Native input-method callback failed; interception disabled.")
            self.set_shortcuts_enabled(False)
            self.hide_native()
        return 0

    def _start_desktop_watch(self) -> None:
        """Bootstrap public visibility without blocking or changing private input."""
        if self._desktop_watch is not None or not self._client or self._stopped.is_set():
            return
        try:
            from ._kwin_desktop import DesktopWatch
            pid = self._native_call("compositor_pid", self._client)
            self._desktop_watch = DesktopWatch(pid, self._desktop_watch_state)
            self._desktop_watch.start()
        except Exception:
            self._stop_desktop_watch()
            self._desktop_watch_state(-1)

    def _desktop_watch_state(self, state: int) -> None:
        """Forward desktop evidence and hide inline text on unknown/desktop states."""
        if not self._client:
            return
        self._native_call("ghost_desktop_state", self._client, state)
        if state != 0:
            self._hide_ghost()
        self._ghost_flush()

    def _stop_desktop_watch(self) -> None:
        watch, self._desktop_watch = self._desktop_watch, None
        if watch is not None:
            watch.stop()


    def _queue_context_sync(self) -> None:
        """Coalesce bootstrap work onto the next Qt event-loop turn."""
        if (not self._needs_context_state or self._context_sync_pending
                or not self._client or self._stopped.is_set()):
            return
        from PySide6.QtCore import QTimer
        self._context_sync_pending = True
        QTimer.singleShot(0, self._sync_context_state)

    def _sync_context_state(self) -> None:
        """Bootstrap purpose, then safe surrounding state; never retry a phase."""
        self._context_sync_pending = False
        if not self._client or not self._needs_context_state or self._stopped.is_set():
            return
        context = self._native_call("context", self._client)
        phase = self._needs_context_state
        if not context or (context, phase) == self._context_synced:
            return
        self._context_synced = (context, phase)
        from PySide6.QtCore import QCoreApplication
        from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusVariant
        bus = QDBusConnection.sessionBus()
        if not bus.isConnected():
            return

        def current() -> bool:
            return bool(self._client and not self._stopped.is_set() and self._needs_context_state == phase
                        and self._native_call("context", self._client) == context)

        def request(method, arguments, completed):
            if not current():
                return
            message = QDBusMessage.createMethodCall(
                "org.kde.KWin", "/VirtualKeyboard", "org.freedesktop.DBus.Properties", method)
            message.setArguments(["org.kde.kwin.VirtualKeyboard", *arguments])
            watcher = QDBusPendingCallWatcher(bus.asyncCall(message, 200), QCoreApplication.instance())
            self._context_calls.add(watcher)

            def finished(call):
                self._context_calls.discard(call)
                reply = call.reply()
                call.deleteLater()
                if current() and reply.type() != QDBusMessage.MessageType.ErrorMessage:
                    completed(reply.arguments())

            watcher.finished.connect(finished)

        def is_true(arguments) -> bool:
            if len(arguments) != 1:
                return False
            value = arguments[0]
            return (value.variant() if isinstance(value, QDBusVariant) else value) is True

        def supported(arguments):
            if is_true(arguments):
                # Set the already-true property to request adoptInputMethodContext;
                # never change selection, visibility mode, or purpose defaults.
                request("Set", ["active", QDBusVariant(True)], lambda _arguments: None)

        def active(arguments):
            if is_true(arguments):
                request("Get", ["activeClientSupportsTextInput"], supported)

        request("Get", ["active"], active)

    def _read_ready(self, *unused) -> None:
        """Dispatch private events without reentry, then honor deferred destruction."""
        if not self._client or self._dispatching:
            return
        self._read_notifier.setEnabled(False)
        self._write_notifier.setEnabled(False)
        self._dispatching = True
        try:
            result = self._native_call("dispatch", self._client)
        finally:
            self._dispatching = False
        if self._close_pending:
            self._release_native()
        elif result < 0:
            self._set_status("The KWin input-method connection closed; native typing is unavailable.")
            self.stop()
        elif self._client:
            self._read_notifier.setEnabled(True)
            self._write_notifier.setEnabled(result == 0)
            self._ghost_flush()

    def _write_ready(self, *unused) -> None:
        if self._write_notifier:
            self._write_notifier.setEnabled(False)
        self._flush()

    def _flush(self) -> None:
        """Flush private output and arm write readiness only while backpressured."""
        if not self._client or self._stopped.is_set():
            return
        result = self._native_call("flush", self._client)
        if result < 0:
            self._set_status("The KWin input-method connection closed; native typing is unavailable.")
            self.stop()
        elif self._write_notifier and not self._dispatching:
            self._write_notifier.setEnabled(result == 0)
        self._ghost_flush()

    def _ghost_read_ready(self, *unused) -> None:
        """Dispatch public-display evidence independently of private input events."""
        if not self._client or self._dispatching or self._ghost_read_notifier is None:
            return
        self._ghost_read_notifier.setEnabled(False)
        if self._ghost_write_notifier:
            self._ghost_write_notifier.setEnabled(False)
        self._dispatching = True
        try:
            result = self._native_call("ghost_dispatch", self._client)
        finally:
            self._dispatching = False
        if self._close_pending:
            self._release_native()
        elif result < 0:
            self._disable_ghost_display()
        elif self._client and self._ghost_read_notifier:
            self._ghost_read_notifier.setEnabled(True)
            if self._ghost_write_notifier:
                self._ghost_write_notifier.setEnabled(result == 0)
            self._flush()

    def _ghost_write_ready(self, *unused) -> None:
        if self._ghost_write_notifier:
            self._ghost_write_notifier.setEnabled(False)
        self._ghost_flush()

    def _ghost_flush(self) -> None:
        """Flush ghost output; display failure disables inline rendering only."""
        if not self._client or self._stopped.is_set() or getattr(self, "_ghost_disabled", False):
            return
        result = self._native_call("ghost_flush", self._client)
        if result < 0:
            self._disable_ghost_display()
        elif self._ghost_write_notifier and not self._dispatching:
            self._ghost_write_notifier.setEnabled(result == 0)

    def _disable_ghost_display(self) -> None:
        """Retire public-display observers without shutting down manual input."""
        self._ghost_disabled = True
        self._stop_desktop_watch()
        for name in ("_ghost_read_notifier", "_ghost_write_notifier"):
            notifier = getattr(self, name, None)
            if notifier is not None:
                notifier.setEnabled(False)
                notifier.deleteLater()
                setattr(self, name, None)
        self._hide_ghost()
        self._set_status("Compositor inline rendering unavailable; native manual controls remain available.")

    def _presentation(self, event, text: str) -> None:
        if self._ui_callback:
            self._ui_callback("presentation", {"key": event.get("key"), "text": text, "mode": "completion"})

    def _hide_ghost(self, *, deferred: bool = False) -> None:
        """Revoke acceptance immediately and optionally defer native frame removal."""
        previous, self._ghost_event = self._ghost_event, None
        self._ghost_snapshot = None
        self._ghost_text = ""
        self._ghost_pending = self._ghost_presented = False
        if previous is not None:
            self._presentation(previous, "")
        if deferred:
            from PySide6.QtCore import QTimer
            token = (self._ghost_scene, self._ghost_frame)
            # A queued retirement must not hide a newer scene/frame created
            # before this event-loop turn runs.
            def retire():
                if (self._ghost_event is None and token == (self._ghost_scene, self._ghost_frame)
                        and self._client and not self._stopped.is_set()):
                    self._native_call("ghost_hide", self._client)
                    self._ghost_flush()
            QTimer.singleShot(0, retire)
        elif self._client and not self._stopped.is_set():
            self._native_call("ghost_hide", self._client)

    def _ghost_current(self) -> bool:
        """Recapture text, anchor and geometry to reject stale visible-frame state."""
        expected = self._ghost_snapshot
        current = self.capture() if expected is not None else None
        return bool(current is not None and current.key == expected.key
                    and current.native_anchor == expected.native_anchor
                    and current.caret_rect == expected.caret_rect
                    and current.inline_render == expected.inline_render
                    and current.window_revision == str(self._ghost_scene))

    def present_inline_native(self, event: dict) -> bool:
        """Queue a transparent native frame; only presentation feedback arms it."""
        with self._lock:
            if self._menu is not None:
                self._presentation(event, "")
                return False
            self.hide_native()
            self._presentation(event, "")
            current = self.capture()
            if (self._menu is not None or current is None or not current.inline_safe
                    or current.key != event.get("key") or current.native_anchor != event.get("native_anchor")
                    or not event.get("inline") or current.inline_render is None):
                return False
            from dataclasses import asdict
            if (asdict(current.inline_render) != event.get("render")
                    or list(current.caret_rect) != list(event.get("rect") or ())):
                return False
            self._ghost_event = dict(event)
            self._ghost_snapshot = current
            self._ghost_scene = int(current.window_revision)
            self._ghost_pending = True
            return self._render_ghost()

    def _render_ghost(self) -> bool:
        try:
            return self._paint_ghost()
        except Exception:
            self._hide_ghost()
            return False

    def _paint_ghost(self) -> bool:
        """Paint only fitting grapheme prefixes at the compositor's current scale."""
        import math
        from PySide6.QtCore import QPointF, Qt, QTextBoundaryFinder
        from PySide6.QtGui import QFont, QFontInfo, QFontMetricsF, QImage, QPainter, QColor

        if not self._ghost_current():
            self._hide_ghost()
            return False
        snapshot, event = self._ghost_snapshot, self._ghost_event
        render = snapshot.inline_render
        x = math.floor(snapshot.caret_rect[0])
        y = math.floor(render.baseline - render.ascent)
        right = math.floor(render.clip_rect[0] + render.clip_rect[2])
        height = math.ceil(render.baseline + render.descent) - y
        if (x < render.clip_rect[0] or y < render.clip_rect[1] or right <= x or height <= 0
                or y + height > render.clip_rect[1] + render.clip_rect[3]):
            self._hide_ghost()
            return False
        # Prepare the full available row before font measurement: configure and
        # preferred fractional scale are asynchronous compositor facts.
        ready = self._native_call("ghost_prepare", self._client, snapshot.window_pid,
                                  self._ghost_scene, x, y, right - x, height)
        scale120 = self._native_call("ghost_scale", self._client)
        if ready == 2 or (ready == 1 and scale120 == 0):
            self._ghost_pending = True
            self._ghost_flush()
            return True
        if ready != 1 or scale120 <= 0:
            self._hide_ghost()
            return False
        ratio = scale120 / 120.0
        image = QImage(math.ceil((right - x) * ratio), math.ceil(height * ratio),
                       QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(ratio)
        image.fill(Qt.GlobalColor.transparent)
        font = QFont(render.font_name)
        font.setPointSizeF(render.font_size)
        font.setWeight(QFont.Weight(render.font_weight))
        font.setItalic(render.italic)
        info, metrics = QFontInfo(font), QFontMetricsF(font, image)
        if (info.family().casefold() != render.font_name.casefold()
                or abs(metrics.ascent() - render.ascent) * ratio > 1.5
                or abs(metrics.descent() - render.descent) * ratio > 1.5):
            self._hide_ghost()
            return False
        text = str(event.get("text") or "").split("\n", 1)[0].split("\r", 1)[0]
        finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, text)
        encoded, visible = text.encode("utf-16-le"), ""
        dx, baseline = snapshot.caret_rect[0] - x, render.baseline - y
        while (end := finder.toNextBoundary()) >= 0:
            prefix = encoded[:end * 2].decode("utf-16-le")
            ink = metrics.tightBoundingRect(prefix)
            if (dx + min(0, ink.left()) < 0
                    or dx + max(metrics.horizontalAdvance(prefix), ink.right()) > right - x
                    or baseline + ink.top() < 0 or baseline + ink.bottom() > height):
                break
            visible = prefix
        if not visible:
            self._hide_ghost()
            return False
        painter = QPainter(image)
        painter.setFont(font)
        painter.setPen(QColor(128, 128, 128))
        painter.drawText(QPointF(dx, baseline), visible)
        painter.end()
        if not self._ghost_current():
            self._hide_ghost()
            return False
        self._ghost_frame += 1
        # Submission is not visibility: acceptance stays disarmed until the
        # matching scene/frame receives presentation feedback in _event.
        self._ghost_text = visible
        self._ghost_presented = False
        pixels = (ctypes.c_ubyte * image.sizeInBytes()).from_buffer(image.bits())
        result = self._native_call("ghost_present", self._client, self._ghost_scene, self._ghost_frame,
                                   ctypes.addressof(pixels), image.width(), image.height(), image.bytesPerLine())
        self._ghost_pending = result == 2
        self._ghost_flush()
        if result not in (1, 2):
            self._hide_ghost()
            return False
        return True

    def bind_ui(self, callback: Callable[..., None]) -> None:
        """Install the Qt-thread UI callback for actions and presentation receipts."""
        self._ui_callback = callback

    def present_native(self, native_anchor: str, text: str, paused: bool = False) -> bool:
        """Present only an explicit manual control, never ordinary preedit."""
        with self._lock:
            if self._menu is not None:
                return False
            if self._ghost_event is not None:
                self._hide_ghost()
            current = self.capture()
            if (current is None or not native_anchor or current.native_anchor != native_anchor
                    or text != ""):
                self.hide_native()
                return False
            self._panel_snapshot, self._panel_paused = current, bool(paused)
            if not self._render():
                self.hide_native()
                return False
            return True

    def hide_native(self) -> None:
        """Clear acceptance, panel hit regions and menus before hiding native surfaces."""
        self._hide_ghost()
        had_menu = self._menu is not None
        self._panel_snapshot = None
        self._menu = None
        self._menu_page = 0
        self._menu_selected = -1
        self._pending_render = False
        self._regions = []
        self._pressed = None
        if self._client and not self._stopped.is_set():
            self._native_call("hide", self._client)
            self._flush()
        if had_menu and self._ui_callback:
            self._ui_callback("menu_closed")

    def show_native_menu(self, actions: list[tuple[str, str, bool]]) -> bool:
        """Copy action/label/enabled rows only while the manual panel anchor matches."""
        with self._lock:
            if not self._panel_snapshot:
                return False
            current = self.capture()
            if (current is None or current.key != self._panel_snapshot.key
                    or current.native_anchor != self._panel_snapshot.native_anchor):
                self.hide_native()
                return False
            self._menu = list(actions)
            self._menu_page = 0
            self._menu_selected = next((index for index, (_, _, active) in enumerate(self._menu) if active), -1)
            self._pressed = None
            if not self._render():
                self.hide_native()
                return False
            return True

    def _render(self) -> bool:
        """Rasterize manual controls; expose hit regions only after native acceptance."""
        from PySide6.QtCore import QRect, QRectF, Qt
        from PySide6.QtGui import QFont, QFontMetrics, QImage, QPainter, QPalette
        from PySide6.QtWidgets import QApplication
        from ..resources import app_icon

        state = self._state()
        if not self._panel_snapshot or state is None or self._anchor(state) != self._panel_snapshot.native_anchor:
            return False
        font = QFont(QApplication.font())
        metrics = QFontMetrics(font)
        row = max(36, metrics.height() + 14)
        screen = QApplication.primaryScreen()
        bounds = screen.availableGeometry() if screen else QRect(0, 0, 1024, 768)
        page = []
        paged = False
        if self._menu is not None:
            self._menu_page_size = max(1, (bounds.height() - 24) // row - 2)
            pages = max(1, (len(self._menu) + self._menu_page_size - 1) // self._menu_page_size)
            self._menu_page = min(self._menu_page, pages - 1)
            offset = self._menu_page * self._menu_page_size
            page = self._menu[offset:offset + self._menu_page_size]
            paged = pages > 1
            labels = [label for _, label, _ in self._menu]
            width = min(480, max(210, max((metrics.horizontalAdvance(label) for label in labels), default=140) + 32))
            height = row * (len(page) + 1 + int(paged)) + 8
        else:
            # Only the brand/manual-menu control is available on this path.
            width = row + 8
            height = row + 8
        width = min(width, max(44, bounds.width() - 16))
        scale = max(1, min(4, int(round(screen.devicePixelRatio())) if screen else 1))
        image = QImage(width * scale, height * scale, QImage.Format.Format_ARGB32_Premultiplied)
        image.setDevicePixelRatio(scale)
        image.fill(Qt.GlobalColor.transparent)
        painter = QPainter(image)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        painter.setFont(font)
        palette = QApplication.palette()
        text_color = palette.color(QPalette.ColorRole.WindowText)
        disabled_color = palette.color(QPalette.ColorGroup.Disabled, QPalette.ColorRole.WindowText)
        if self._menu is not None:
            painter.setPen(palette.color(QPalette.ColorRole.Mid))
            painter.setBrush(palette.color(QPalette.ColorRole.Window))
            painter.drawRoundedRect(QRectF(0.5, 0.5, width - 1, height - 1), 9, 9)
        painter.setOpacity(0.45 if self._panel_paused else 1.0)
        painter.drawPixmap(QRect(10, (row + 8 - 24) // 2, 24, 24), app_icon(compact=True).pixmap(24 * scale, 24 * scale))
        painter.setOpacity(1.0)
        painter.setPen(text_color)
        regions = [((4, 4, 38, row), "menu", True)]
        align = Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft
        if self._menu is not None:
            painter.drawText(QRect(44, 4, width - 52, row), align, "BroTypist")
            for index, (action, label, enabled) in enumerate(page):
                y = (index + 1) * row + 4
                selected = offset + index == self._menu_selected
                if selected:
                    painter.fillRect(QRect(4, y, width - 8, row), palette.brush(QPalette.ColorRole.Highlight))
                painter.setPen(palette.color(QPalette.ColorRole.HighlightedText) if selected else text_color if enabled else disabled_color)
                painter.drawText(QRect(14, y, width - 28, row), align,
                                 metrics.elidedText(label, Qt.TextElideMode.ElideRight, width - 28))
                regions.append(((4, y, width - 8, row), action, bool(enabled)))
            if paged:
                y = (len(page) + 1) * row + 4
                for left, action, title, active in (
                        (4, "page:previous", "Previous", self._menu_page > 0),
                        (width // 2, "page:next", "Next", self._menu_page + 1 < pages)):
                    painter.setPen(text_color if active else disabled_color)
                    painter.drawText(QRect(left + 10, y, width // 2 - 18, row), align, title)
                    regions.append(((left, y, width // 2 - 4, row), action, active))
        painter.end()
        pixels = (ctypes.c_ubyte * image.sizeInBytes()).from_buffer(image.bits())
        result = self._native_call(
            "present", self._client, ctypes.addressof(pixels), image.width(), image.height(), image.bytesPerLine(), scale)
        self._pending_render = result == 2
        self._regions = regions if result == 1 else []
        self._flush()
        return result in (1, 2) and bool(self._client)

    def _hit(self, x: int, y: int) -> str | None:
        for (left, top, width, height), action, enabled in self._regions:
            if enabled and left <= x < left + width and top <= y < top + height:
                return action
        return None

    def _pointer(self, event: _Event) -> None:
        """Dispatch clicks only when press/release share an action and native anchor."""
        if event.key != 272 or not self._panel_snapshot:
            return
        state = self._state()
        anchor = self._panel_snapshot.native_anchor
        if state is None or self._anchor(state) != anchor or state != (event.generation, event.serial):
            self.hide_native()
            return
        action = self._hit(event.x, event.y)
        if event.state == 1:
            self._pressed = (action, anchor) if action else None
            return
        pressed, self._pressed = self._pressed, None
        if not action or pressed != (action, anchor):
            return
        if self._menu is not None:
            if action in {"page:previous", "page:next"}:
                self._menu_page += -1 if action == "page:previous" else 1
                self._menu_selected = -1
                self._render()
                return
            if action == "menu":
                self._menu = None
                self._render()
                if self._ui_callback:
                    self._ui_callback("menu_closed")
            else:
                self._menu = None
                self.hide_native()
                if self._ui_callback:
                    self._ui_callback(action)
        elif action == "menu":
            if self._ui_callback:
                self._ui_callback("menu")
        elif self._enabled:
            self._dispatch(action)

    def _release_native(self) -> None:
        """Retire Qt observers before closing the native owner outside its callbacks."""
        self._stop_desktop_watch()
        for call in self._context_calls:
            call.deleteLater()
        self._context_calls.clear()
        for name in ("_read_notifier", "_write_notifier", "_ghost_read_notifier", "_ghost_write_notifier"):
            notifier = getattr(self, name, None)
            if notifier is not None:
                notifier.setEnabled(False)
                notifier.deleteLater()
                setattr(self, name, None)
        if self._client:
            client, self._client = self._client, None
            self._library.inklow_wl_close(client)
        self._close_pending = False

    def stop(self) -> None:
        """Disable callbacks now and defer destruction until native stack frames exit."""
        BridgeBase.stop(self)
        self._ui_callback = None
        self._stop_desktop_watch()
        self.hide_native()
        self._available = False
        self._desktop = None
        if self._dispatching or self._native_depth:
            self._close_pending = True
        else:
            self._release_native()
