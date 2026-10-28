"""Native Wayland consumer contracts with synthetic AT-SPI/compositor peers.

R1: pre-read privacy and stale-context rejection; R2: stable context identity;
R3: exact shortcut consumption; R5: last-frame delivery and menu closure.
Only OS/protocol peers are fake; capture, insertion, key routing and rendering
are production methods. C transport bounds/physical key forwarding need the
isolated native smoke, not a Python reimplementation of the C validation.
"""
import ctypes
from types import SimpleNamespace

import pytest

from inklow.config import Config
from inklow.native.base import BridgeBase
from inklow.native.wayland import WaylandBridge, _Event
from inklow.runtime import Controller


class Accessible:
    def __init__(self, *, children=(), flags=(), role="window", path="/field"):
        self.children = children
        self.parent = None
        for child in children:
            child.parent = self
        self.flags = set(flags)
        self.role = role
        self.path = path
        self.name = "writer"

    @property
    def childCount(self):
        return len(self.children)

    def getChildAtIndex(self, index):
        return self.children[index]

    def clearCache(self):
        pass

    def getState(self):
        return SimpleNamespace(contains=self.flags.__contains__)

    def getRole(self):
        return self.role

    def getAttributes(self):
        return self.attributes

    def get_process_id(self):
        return 2147483647


class NativePeer:
    """A synthetic external field and native transport, never a bridge double."""
    def __init__(self):
        self.text = "Dear team, the update"
        self.executable = "/usr/bin/writer"
        self.caret = len(self.text)
        self.selection = 0
        self.revision, self.serial, self.context = 7, 19, 101
        self.ready = True
        self.surrounding = self.text
        self.reads = self.interfaces = 0
        self.after_read = None
        self.commits = []
        self.accept_commits = True
        self.frames = []
        self.present_result = 1
        self.hidden = 0
        self.closes = 0
        self.ghost_scene = 11
        self.ghost_capable = False
        self.composition_inactive = True
        self.compositor_pid = 2024
        self.desktop_states = []
        self.ghost_scale = 150
        self.ghost_ready = 1
        self.ghost_frames = []
        self.ghost_prepares = []
        self.field = Accessible(role="entry", flags=("focused", "editable", "enabled", "sensitive", "visible", "showing"))
        self.field.attributes = []
        self.field.queryEditableText = lambda: SimpleNamespace(
            insertText=lambda *args: pytest.fail("Wayland must not edit through AT-SPI"))
        self.field.queryText = self.query_text
        window = Accessible(children=(self.field,), flags=("active",))
        self.desktop = Accessible(children=(Accessible(children=(window,)),))

    def query_text(self):
        self.interfaces += 1
        return self

    @property
    def characterCount(self):
        return len(self.text)

    @property
    def caretOffset(self):
        return self.caret

    def getNSelections(self):
        return self.selection

    def getText(self, start, end):
        self.reads += 1
        text = self.text
        if self.after_read:
            self.after_read()
        return text

    def inklow_wl_state(self, client, revision, serial):
        revision._obj.value = self.revision
        serial._obj.value = self.serial
        return int(self.ready)

    def inklow_wl_context(self, client):
        return self.context

    def inklow_wl_matches(self, client, revision, serial, text, length, cursor):
        encoded = self.surrounding.encode("utf-8")
        return int(text == encoded and length == len(encoded)
                   and cursor == len(self.text[:self.caret].encode("utf-8")))

    def inklow_wl_commit(self, client, revision, serial, text):
        # Deliberately permissive: production guards must prevent stale writes.
        value = text.decode("utf-8")
        if not self.accept_commits:
            self.commits.append(value)
            return 1  # The protocol accepted the request, but the app filtered it.
        self.commits.append(value)
        self.text = self.text[:self.caret] + value + self.text[self.caret:]
        self.caret += len(value)
        self.surrounding = self.text
        self.revision += 1
        self.serial += 1
        return 1

    def inklow_wl_hide(self, client):
        self.hidden += 1

    def inklow_wl_flush(self, client):
        return 1

    def inklow_wl_present(self, client, pixels, width, height, stride, scale):
        self.frames.append(ctypes.string_at(pixels, stride * height))
        return self.present_result

    def inklow_wl_composition_inactive(self, client):
        return int(self.composition_inactive)

    def inklow_wl_ghost_window(self, client, pid, output):
        if not self.ghost_capable:
            return 0
        window = output._obj
        window.scene, window.pid = self.ghost_scene, pid
        window.x, window.y, window.width, window.height = 40, 60, 640, 480
        return 1

    def inklow_wl_ghost_prepare(self, client, pid, scene, x, y, width, height):
        self.ghost_prepares.append((pid, scene, x, y, width, height))
        return self.ghost_ready

    def inklow_wl_ghost_scale(self, client):
        return self.ghost_scale

    def inklow_wl_ghost_present(self, client, scene, frame, pixels, width, height, stride):
        self.ghost_frames.append((scene, frame, width, height, ctypes.string_at(pixels, stride * height)))
        return self.present_result

    def inklow_wl_ghost_hide(self, client):
        pass

    def inklow_wl_ghost_flush(self, client):
        return 1

    def inklow_wl_compositor_pid(self, client):
        return self.compositor_pid

    def inklow_wl_ghost_desktop_state(self, client, state):
        self.desktop_states.append(state)

    def inklow_wl_close(self, client):
        self.closed = True
        self.closes += 1


@pytest.fixture
def native(monkeypatch):
    peer = NativePeer()
    bridge = WaylandBridge.__new__(WaylandBridge)
    BridgeBase.__init__(bridge)
    bridge._available = True
    bridge._desktop = peer.desktop
    bridge.atspi = SimpleNamespace(**{
        name: value for name, value in (
            ("ROLE_ENTRY", "entry"), ("ROLE_TEXT", "text"), ("ROLE_PASSWORD_TEXT", "password"),
            *(("STATE_" + flag.upper(), flag) for flag in
              ("focused", "editable", "enabled", "sensitive", "active", "defunct", "multi_line", "visible", "showing")))})
    bridge._library, bridge._client = peer, object()
    bridge._read_notifier = bridge._write_notifier = None
    bridge._dispatching = bridge._close_pending = False
    bridge._ghost_read_notifier = bridge._ghost_write_notifier = None
    bridge._ghost_event = bridge._ghost_snapshot = None
    bridge._ghost_scene = bridge._ghost_frame = 0
    bridge._ghost_text = ""
    bridge._ghost_pending = bridge._ghost_presented = False
    bridge._native_depth = 0
    bridge._ui_callback = None
    bridge._needs_context_state = bridge._context_sync_pending = False
    bridge._context_synced = (0, 0)
    bridge._context_calls = set()
    bridge._desktop_watch = None
    bridge._panel_snapshot = None
    bridge._panel_paused = bridge._pending_render = False
    bridge._menu = None
    bridge._menu_page, bridge._menu_page_size, bridge._menu_selected = 0, 1, -1
    bridge._regions, bridge._pressed = [], None
    from inklow.native import linux
    monkeypatch.setattr(linux, "os", SimpleNamespace(**{**vars(linux.os),
                                                      "readlink": lambda path: peer.executable}))
    return bridge, peer


def event(bridge, **values):
    return bridge._event(None, ctypes.pointer(_Event(**values)))


def test_native_field_identity_survives_revisions_but_not_context_reactivation(native):
    # R2: revisions move the anchor, not the field lifetime.
    bridge, peer = native
    first = bridge.capture()
    assert first is not None
    assert first.text == "Dear team, the update"
    assert first.caret_rect is None and first.field_rect is None and first.has_anchor
    assert bridge.insert(first, " arrives ")
    second = bridge.capture()
    assert second.field_id == first.field_id
    assert second.native_anchor != first.native_anchor
    assert second.text == "Dear team, the update arrives "
    assert bridge.insert(second, "tomorrow")
    assert peer.text == "Dear team, the update arrives tomorrow"
    assert peer.commits == [" arrives ", "tomorrow"]
    peer.context += 1
    reactivated = bridge.capture()
    assert reactivated.field_id != second.field_id


@pytest.mark.parametrize("change", ["revision", "serial", "context", "surrounding", "selection", "purpose"])
def test_stale_or_unsafe_native_offer_never_reaches_commit(native, change):
    # R1: even identical visible text cannot authorize a stale protocol write.
    bridge, peer = native
    offered = bridge.capture()
    original = peer.text
    if change in ("revision", "serial", "context"):
        setattr(peer, change, getattr(peer, change) + 1)
    elif change == "surrounding":
        peer.surrounding = "Another native field"
    elif change == "selection":
        peer.selection = 1
    else:
        peer.ready = False
    assert not bridge.insert(offered, " must not appear")
    assert peer.text == original
    assert peer.commits == []


@pytest.mark.parametrize("guard", ["unknown-purpose", "password-role", "protected-attribute", "selection", "excluded-app", "unobservable-domain"])
def test_privacy_gates_precede_field_value_reads(native, guard):
    # R1: forbidden reads are errors, not merely discarded return values.
    bridge, peer = native
    if guard == "unknown-purpose":
        peer.ready = False
    elif guard == "password-role":
        peer.field.role = "password"
    elif guard == "protected-attribute":
        peer.field.attributes = ["protected:true"]
    elif guard == "selection":
        peer.selection = 1
    elif guard == "excluded-app":
        bridge.configure(Config(excluded_apps=("writer",)))
    else:
        peer.executable = "/usr/bin/firefox"
        bridge.configure(Config(app_rules={"domain:private.example": {"enabled": False}}))
    peer.getText = lambda *args: pytest.fail("Protected field value was read")
    assert bridge.capture() is None
    assert peer.reads == 0 and peer.commits == []
    if guard in ("unknown-purpose", "password-role", "protected-attribute", "excluded-app", "unobservable-domain"):
        assert peer.interfaces == 0


@pytest.mark.parametrize("race", ["revision", "focus", "protection"])
def test_capture_revalidates_native_and_accessible_state_after_value_read(native, race):
    bridge, peer = native
    def change():
        if race == "revision":
            peer.revision += 1
        elif race == "focus":
            peer.field.flags.remove("focused")
        else:
            peer.field.attributes = ["text-input-type:password"]
    peer.after_read = change
    assert bridge.capture() is None
    assert peer.reads == 1 and peer.commits == []


@pytest.mark.parametrize("name,key,modifiers,action,handled", [
    (b"Tab", 23, 0, "word", False),
    (b"Tab", 23, 0, "word", True),
    (b"ISO_Left_Tab", 23, 1, None, False),
    (b"Tab", 23, 8, None, False),
    (b"Tab", 23, 128, None, False),
    (b"1", 10, 5, "all", True),
])
def test_native_key_consumption_preserves_modifiers_and_false_callbacks(native, name, key, modifiers, action, handled):
    # R3: callback False preserves the original key; reserved/AltGr never dispatch.
    bridge, peer = native
    calls = []
    bridge.configure(Config(shortcuts={"word": "Tab", "all": "Ctrl+Shift+1"}))
    bridge.start_shortcuts(lambda value: calls.append(value) or handled)
    bridge.set_shortcuts_enabled(True)
    result = event(bridge, type=2, state=1, generation=peer.revision,
                   serial=peer.serial, name=name, key=key, modifiers=modifiers)
    assert result == int(handled)
    assert calls == ([action] if action else [])
    assert peer.text == "Dear team, the update" and peer.commits == []


def test_inactive_sensitive_and_old_generation_keys_never_call_python_consumer(native):
    bridge, peer = native
    calls = []
    bridge.configure(Config(shortcuts={"word": "Tab"}))
    bridge.start_shortcuts(lambda action: calls.append(action) or True)
    for enabled, ready, generation in ((False, True, peer.revision),
                                       (True, False, peer.revision),
                                       (True, True, peer.revision - 1)):
        bridge.set_shortcuts_enabled(enabled)
        peer.ready = ready
        assert event(bridge, type=2, state=1, generation=generation, name=b"Tab", key=23) == 0
    assert calls == [] and peer.commits == []


def test_unsupported_inline_never_submits_or_intercepts_acceptance(native, tmp_path, monkeypatch):
    # Real Controller gating, including a stale offer left over from a capable field.
    bridge, peer = native
    monkeypatch.setenv("INKLOW_HOME", str(tmp_path))
    controller = Controller(Config(record_history=False, shortcuts={"word": "Tab", "all": "AboveTab"}), bridge)
    controller.start()
    try:
        snapshot = bridge.capture()
        assert snapshot.has_anchor and not snapshot.native_inline and not snapshot.inline_safe
        assert not controller._submit(snapshot)
        assert controller._suggestion.offer(controller._suggestion.generation, snapshot, " arrives tomorrow")
        for name, key in ((b"Tab", 23), (b"grave", 49)):
            assert event(bridge, type=2, state=1, generation=peer.revision,
                         serial=peer.serial, name=name, key=key) == 0
        assert not controller.invoke("force")
        assert peer.text == "Dear team, the update"
        assert peer.commits == [] and peer.frames == []
        assert controller.stats()["totals"] == {"completions": 0, "words": 0, "characters": 0}
    finally:
        controller.stop()


def test_filtered_numbered_choice_leaves_digit_unconsumed_and_statistics_empty(native, tmp_path, monkeypatch):
    # R1/R3: successful wire submission is not application-level acceptance.
    bridge, peer = native
    monkeypatch.setenv("INKLOW_HOME", str(tmp_path))
    peer.accept_commits = False
    controller = Controller(Config(record_history=False, shortcuts={"word": "Tab"}), bridge)
    controller.start()
    try:
        snapshot = bridge.capture()
        controller._choices = [" arrives tomorrow"]
        controller._choice_snapshot = snapshot
        controller._replacement = (snapshot.caret, 0)
        controller._mode = "alternatives"
        controller.presentation_result(snapshot.key, " arrives tomorrow", "alternatives")
        assert event(bridge, type=2, state=1, generation=peer.revision,
                     serial=peer.serial, name=b"1", key=10) == 0
        assert peer.text == "Dear team, the update"
        assert peer.commits == [" arrives tomorrow"]
        assert controller._choices == []
        assert controller.stats()["totals"] == {"completions": 0, "words": 0, "characters": 0}
        assert event(bridge, type=2, state=1, generation=peer.revision,
                     serial=peer.serial, name=b"1", key=10) == 0
        assert peer.commits == [" arrives tomorrow"]
    finally:
        controller.stop()


@pytest.fixture
def qt_app(monkeypatch):
    monkeypatch.setenv("QT_QPA_PLATFORM", "offscreen")
    widgets = pytest.importorskip("PySide6.QtWidgets")
    app = widgets.QApplication.instance() or widgets.QApplication([])
    from PySide6.QtCore import QRect
    screen = SimpleNamespace(availableGeometry=lambda: QRect(0, 0, 640, 240),
                             devicePixelRatio=lambda: 1)
    monkeypatch.setattr(widgets.QApplication, "primaryScreen", lambda: screen)
    yield app


def click_region(bridge, peer, action):
    region = next(rect for rect, name, enabled in bridge._regions if name == action and enabled)
    x, y, _, _ = region
    for pressed in (1, 0):
        event(bridge, type=3, generation=peer.revision, serial=peer.serial,
              key=272, state=pressed, x=x + 1, y=y + 1)


def test_native_busy_buffers_deliver_latest_frame_without_stale_hit_actions(native, qt_app):
    # R5: pending frames disable previous hit regions until compositor release.
    bridge, peer = native
    calls = []
    bridge.configure(Config(shortcuts={"word": "Tab", "all": "AboveTab"}))
    bridge.start_shortcuts(lambda action: calls.append(action) or True)
    bridge.bind_ui(calls.append)
    bridge.set_shortcuts_enabled(True)
    anchor = bridge.capture().native_anchor
    assert bridge.present_native(anchor, "")
    assert bridge.show_native_menu([("first", "First manual action", True)])
    stale = next(rect for rect, name, active in bridge._regions if name == "menu")
    first_frame = peer.frames[-1]
    peer.present_result = 2
    assert bridge.show_native_menu([("intermediate", "Intermediate manual action", True)])
    assert bridge.show_native_menu([("manual", "Final manual action", True)])
    assert bridge._regions == []
    x, y, _, _ = stale
    for pressed in (1, 0):
        event(bridge, type=3, generation=peer.revision, serial=peer.serial,
              key=272, state=pressed, x=x + 1, y=y + 1)
    assert calls == []
    final_frame = peer.frames[-1]
    assert first_frame != final_frame
    assert peer.commits == []
    peer.present_result = 1
    event(bridge, type=5)
    assert peer.frames[-1] == final_frame
    assert not bridge._pending_render
    click_region(bridge, peer, "manual")
    assert calls == ["manual"]


def test_native_menu_pages_skip_disabled_items_and_restore_shortcuts_on_close(native, qt_app):
    # R5/R3: off-page enabled actions remain reachable; Escape is not acceptance.
    bridge, peer = native
    calls = []
    bridge.configure(Config(shortcuts={"word": "Tab"}))
    bridge.bind_ui(calls.append)
    bridge.start_shortcuts(lambda action: calls.append(action) or True)
    bridge.set_shortcuts_enabled(True)
    assert bridge.present_native(bridge.capture().native_anchor, "")
    actions = [(f"item:{index}", f"Synthetic action {index}", index % 2 == 0) for index in range(12)]
    assert bridge.show_native_menu(actions)
    assert event(bridge, type=2, state=1, generation=peer.revision, name=b"End") == 1
    assert bridge._menu_page > 0
    assert event(bridge, type=2, state=1, generation=peer.revision, name=b"Return") == 1
    assert calls == ["item:10"]
    assert bridge._menu is None and bridge._panel_snapshot is None
    assert bridge.present_native(bridge.capture().native_anchor, "")
    assert bridge.show_native_menu(actions)
    assert event(bridge, type=2, state=1, generation=peer.revision, name=b"Escape") == 1
    assert calls == ["item:10", "menu_closed"]
    assert event(bridge, type=2, state=1, generation=peer.revision, name=b"Tab", key=23) == 1
    assert calls == ["item:10", "menu_closed", "word"]
    assert peer.commits == []


@pytest.mark.parametrize("phase", [1, 2])
@pytest.mark.parametrize("transition", ["ready", "inactive", "unsupported", "focus-changed", "sensitive", "stopped"])
def test_initial_context_resync_preserves_capture_isolation(native, qt_app, monkeypatch, transition, phase):
    QtDBus = pytest.importorskip("PySide6.QtDBus")
    bridge, peer = native
    peer.ready = False
    bridge._needs_context_state = phase
    assert bridge.capture() is None and peer.reads == 0
    pending = []

    class Signal:
        def connect(self, callback):
            self.callback = callback

    class Watcher:
        def __init__(self, message, parent):
            self.message = message
            self.finished = Signal()
            pending.append(self)

        def reply(self):
            return SimpleNamespace(type=lambda: QtDBus.QDBusMessage.MessageType.ReplyMessage,
                                   arguments=lambda: self.arguments)

        def deleteLater(self):
            pass

    bus = SimpleNamespace(isConnected=lambda: True, asyncCall=lambda message, timeout: message)
    monkeypatch.setattr(QtDBus.QDBusConnection, "sessionBus", lambda: bus)
    monkeypatch.setattr(QtDBus, "QDBusPendingCallWatcher", Watcher)
    bridge._sync_context_state()
    replies = 0
    while pending:
        watcher = pending.pop(0)
        if replies == 0:
            if transition == "focus-changed":
                peer.context += 1
            elif transition == "sensitive":
                bridge._needs_context_state = False
            elif transition == "stopped":
                bridge.stop()
        arguments = watcher.message.arguments()
        if watcher.message.member() == "Set":
            # The external peer is permissive: production must prevent activation
            # after focus loss, teardown, or an ineligible compositor response.
            watcher.arguments = []
            if bridge._needs_context_state == 1:
                # Purpose arrives before safe surrounding text can be retained.
                event(bridge, type=1, state=2)
            else:
                peer.ready = True
                event(bridge, type=1, state=0)
        else:
            key = arguments[1]
            value = not ((key == "active" and transition == "inactive")
                         or (key == "activeClientSupportsTextInput" and transition == "unsupported"))
            watcher.arguments = [QtDBus.QDBusVariant(value)]
        replies += 1
        watcher.finished.callback(watcher)
        qt_app.processEvents()

    captured = bridge.capture()
    if transition == "ready":
        assert captured is not None and captured.text == peer.text and captured.native_anchor
    else:
        assert not peer.ready and captured is None and peer.reads == 0


class DesktopBus:
    """Controllable D-Bus boundary; production code owns identity and ordering."""

    def __init__(self, qt_dbus):
        self.qt_dbus = qt_dbus
        self.pending = []
        self.subscriptions = []
        self.connected = True
        self.refused_signal = None

    def isConnected(self):
        return self.connected

    def connect(self, service, path, interface, name, receiver, slot):
        if name == self.refused_signal:
            return False
        self.subscriptions.append((service, path, interface, name, receiver, slot))
        return True

    def disconnect(self, *subscription):
        self.subscriptions.remove(subscription)
        return True

    def asyncCall(self, message, timeout):
        assert timeout == 200
        assert message.member() in {"GetNameOwner", "GetConnectionUnixProcessID", "Get"}
        return message

    def watcher(self, message, parent):
        class Signal:
            def connect(self, callback):
                self.callback = callback

        class Watcher:
            def __init__(self):
                self.message = message
                self.finished = Signal()
                self.deleted = False

            def isFinished(self):
                return False

            def reply(self):
                return self.response

            def deleteLater(self):
                self.deleted = True

        watcher = Watcher()
        self.pending.append(watcher)
        return watcher

    def reply(self, arguments, *, error=False):
        watcher = self.pending.pop(0)
        message_type = self.qt_dbus.QDBusMessage.MessageType
        watcher.response = SimpleNamespace(
            type=lambda: message_type.ErrorMessage if error else message_type.ReplyMessage,
            arguments=lambda: arguments)
        # Deliberately deliver even deleted watchers to expose lifetime mistakes.
        watcher.finished.callback(watcher)
        return watcher

    def emit(self, service, name, arguments):
        for sender, _path, _interface, signal, receiver, slot in tuple(self.subscriptions):
            if signal == name and sender in ("", service):
                message = SimpleNamespace(service=lambda: service, arguments=lambda: arguments)
                getattr(receiver, slot.lstrip("12").split("(")[0])(message)


@pytest.fixture
def desktop_bus(native, qt_app, monkeypatch):
    qt_dbus = pytest.importorskip("PySide6.QtDBus")
    from inklow.native import _kwin_desktop
    bus = DesktopBus(qt_dbus)
    monkeypatch.setattr(_kwin_desktop.QDBusConnection, "sessionBus", lambda: bus)
    monkeypatch.setattr(_kwin_desktop, "QDBusPendingCallWatcher", bus.watcher)
    yield bus
    native[0].stop()


def identify_desktop(bridge, peer, bus):
    bridge._start_desktop_watch()
    assert peer.desktop_states == [-1]
    assert bus.pending[0].message.member() == "GetNameOwner"
    bus.reply([":1.42"])
    assert bus.pending[0].message.arguments() == [":1.42"]
    bus.reply([peer.compositor_pid])
    message = bus.pending[0].message
    assert message.service() == ":1.42"
    assert message.path() == "/KWin"
    assert message.arguments() == ["org.kde.KWin", "showingDesktop"]


def test_desktop_read_only_bootstrap_and_live_visibility(native, desktop_bus):
    bridge, peer = native
    identify_desktop(bridge, peer, desktop_bus)
    assert peer.desktop_states == [-1]  # Identity alone never grants visibility.
    desktop_bus.reply([desktop_bus.qt_dbus.QDBusVariant(False)])
    assert peer.desktop_states == [-1, 0]
    desktop_bus.emit(":1.99", "showingDesktopChanged", [True])
    assert peer.desktop_states[-1] == 0  # A different compositor cannot control it.
    desktop_bus.emit(":1.42", "showingDesktopChanged", [True])
    assert peer.desktop_states[-1] == 1
    desktop_bus.emit(":1.42", "showingDesktopChanged", [False])
    assert peer.desktop_states[-1] == 0
    assert not desktop_bus.pending


@pytest.mark.parametrize("late_error", [False, True])
def test_desktop_initial_reply_cannot_override_newer_signal(native, desktop_bus, late_error):
    bridge, peer = native
    identify_desktop(bridge, peer, desktop_bus)
    desktop_bus.emit(":1.42", "showingDesktopChanged", [True])
    desktop_bus.reply([False], error=late_error)
    assert peer.desktop_states == [-1, 1]
    desktop_bus.emit(":1.42", "showingDesktopChanged", [False])
    assert peer.desktop_states[-1] == 0


@pytest.mark.parametrize("pid", [0, -1, True, "2024", 2025])
def test_desktop_owner_pid_must_match_private_compositor(native, desktop_bus, pid):
    bridge, peer = native
    bridge._start_desktop_watch()
    desktop_bus.reply([":1.42"])
    desktop_bus.reply([pid])
    assert 0 not in peer.desktop_states
    assert not desktop_bus.pending and not desktop_bus.subscriptions
    assert bridge.capture() is not None  # Private input is independent.


@pytest.mark.parametrize("phase", ["owner", "pid", "property", "watching"])
@pytest.mark.parametrize("failure", ["owner-loss", "timeout", "stop", "disconnect"])
def test_desktop_lifetime_invalidates_pending_and_live_state(native, desktop_bus, phase, failure):
    bridge, peer = native
    bridge._start_desktop_watch()
    if phase != "owner":
        desktop_bus.reply([":1.42"])
    if phase in {"property", "watching"}:
        desktop_bus.reply([peer.compositor_pid])
    if phase == "watching":
        desktop_bus.reply([False])
    watch = bridge._desktop_watch
    if failure == "owner-loss":
        desktop_bus.emit("org.freedesktop.DBus", "NameOwnerChanged",
                         ["org.kde.KWin", ":1.42", ":1.43"])
    elif failure == "timeout":
        if phase == "watching":
            assert not watch._deadline.isActive()
            return  # The deadline applies to bootstrap, not an idle valid watch.
        watch._deadline.timeout.emit()
    elif failure == "disconnect":
        desktop_bus.emit("", "Disconnected", [])
    else:
        bridge.stop()
    assert peer.desktop_states[-1] == -1
    states = list(peer.desktop_states)
    while desktop_bus.pending:
        desktop_bus.reply([False])
    desktop_bus.emit(":1.42", "showingDesktopChanged", [False])
    assert peer.desktop_states == states
    assert not desktop_bus.subscriptions
    if failure == "stop":
        bridge.stop()
        assert peer.closes == 1
    else:
        assert bridge.capture() is not None


@pytest.mark.parametrize("bad", [None, 0, 1, "false", [], {}, False])
@pytest.mark.parametrize("source", ["property", "signal"])
def test_desktop_bad_values_and_reply_shape_fail_closed(native, desktop_bus, bad, source):
    bridge, peer = native
    identify_desktop(bridge, peer, desktop_bus)
    arguments = [] if bad is False else [bad]
    if source == "property":
        desktop_bus.reply(arguments)
    else:
        desktop_bus.reply([False])
        desktop_bus.emit(":1.42", "showingDesktopChanged", arguments)
    assert peer.desktop_states[-1] == -1
    assert not desktop_bus.subscriptions


@pytest.mark.parametrize("phase", ["owner", "pid", "property"])
def test_desktop_dbus_errors_never_grant_or_retry(native, desktop_bus, phase):
    bridge, peer = native
    bridge._start_desktop_watch()
    if phase != "owner":
        desktop_bus.reply([":1.42"])
    if phase == "property":
        desktop_bus.reply([peer.compositor_pid])
    desktop_bus.reply([], error=True)
    assert 0 not in peer.desktop_states
    assert not desktop_bus.pending and not desktop_bus.subscriptions


@pytest.mark.parametrize("signal", ["NameOwnerChanged", "showingDesktopChanged", "Disconnected"])
def test_desktop_requires_all_lifetime_subscriptions(native, desktop_bus, signal):
    bridge, peer = native
    desktop_bus.refused_signal = signal
    bridge._start_desktop_watch()
    if signal == "showingDesktopChanged":
        desktop_bus.reply([":1.42"])
        desktop_bus.reply([peer.compositor_pid])
    assert 0 not in peer.desktop_states
    assert not desktop_bus.pending and not desktop_bus.subscriptions



@pytest.mark.parametrize("text", [" tomorrow", " tomorrow\nnext line", ""])
def test_unsupported_inline_only_allows_empty_manual_control(native, qt_app, monkeypatch, text):
    bridge, peer = native
    calls = []
    native_call = bridge._native_call
    def observe(name, *arguments):
        calls.append(name)
        return native_call(name, *arguments)
    monkeypatch.setattr(bridge, "_native_call", observe)
    snapshot = bridge.capture()
    before = peer.text
    assert not snapshot.native_inline and not snapshot.inline_safe
    assert bridge.present_native(snapshot.native_anchor, text) is (text == "")
    assert bool(peer.frames) is (text == "")
    assert peer.text == before and peer.commits == []
    assert "preedit" not in calls


def test_stale_native_control_anchor_never_displays_or_commits(native, monkeypatch):
    bridge, peer = native
    monkeypatch.setattr(bridge, "_render", lambda: pytest.fail("Stale anchor rendered a panel"))
    snapshot = bridge.capture()
    peer.serial += 1
    peer.revision += 1
    assert not bridge.present_native(snapshot.native_anchor, "")
    assert peer.commits == []


def test_selection_while_manual_control_is_shown_refuses_commit_without_reading_value(native, monkeypatch):
    bridge, peer = native
    monkeypatch.setattr(bridge, "_render", lambda: True)
    snapshot = bridge.capture()
    assert bridge.present_native(snapshot.native_anchor, "")
    peer.selection = 1
    peer.getText = lambda *args: pytest.fail("Selected value was read")
    assert bridge.capture() is None
    assert not bridge.insert(snapshot, " tomorrow")
    assert peer.commits == []


def test_wayland_context_worker_never_touches_private_connection(native, monkeypatch):
    bridge, peer = native
    snapshot = bridge.capture()
    monkeypatch.setattr(bridge, "_native_call", lambda *args: pytest.fail("Worker touched compositor connection"))
    assert not bridge.context_matches(snapshot)
    assert bridge.focused_context(snapshot, authorized=lambda: True)["status"] == "blocked"


@pytest.mark.parametrize("missing", [True, False])
def test_linux_context_revalidates_window_before_tree_reads(native, monkeypatch, missing):
    from inklow.native.linux import LinuxBridge
    bridge, peer = native
    snapshot = LinuxBridge._capture(bridge, geometry=False)[0]
    focused = bridge._focused()
    calls = 0
    def changed_focus():
        nonlocal calls
        calls += 1
        if calls == 1:
            return focused
        if missing:
            return None
        return focused[0], focused[1], (123, "/usr/bin/another-writer")
    monkeypatch.setattr(bridge, "_focused", changed_focus)
    monkeypatch.setattr(bridge, "context_matches", lambda value: LinuxBridge.context_matches(bridge, value))
    peer.getText = lambda *args: pytest.fail("Changed focused window's context was read")
    assert LinuxBridge.focused_context(bridge, snapshot, authorized=lambda: True)["status"] == "blocked"


def test_prepared_socket_is_private_and_unused_launch_releases_only_owned_fd(monkeypatch):
    import os
    import socket
    from inklow.native import wayland
    ours, external = socket.socketpair()
    fd = ours.detach()
    monkeypatch.setenv("WAYLAND_SOCKET", str(fd))
    try:
        wayland.prepare_input_method(fd)
        assert "WAYLAND_SOCKET" not in os.environ
        assert not os.get_inheritable(fd)
        assert wayland.available_input_method()
        wayland.close_prepared_input_method()
        wayland.close_prepared_input_method()
        assert not wayland.available_input_method()
        with pytest.raises(OSError):
            os.fstat(fd)
        assert external.recv(1) == b""
    finally:
        wayland.close_prepared_input_method()
        external.close()


def test_duplicate_native_launch_exits_normally_and_closes_unclaimed_private_socket(monkeypatch, tmp_path):
    import os
    import socket
    from inklow import __main__ as cli
    from inklow.instance import instance_lock
    from inklow.native import wayland
    monkeypatch.setenv("INKLOW_HOME", str(tmp_path))
    monkeypatch.setattr(cli.sys, "platform", "linux")
    ours, external = socket.socketpair()
    fd = ours.detach()
    monkeypatch.setenv("WAYLAND_SOCKET", str(fd))
    try:
        with instance_lock() as owned:
            assert owned
            assert cli.main(["--background", "--wayland-input-method"]) == 0
        assert not wayland.available_input_method()
        with pytest.raises(OSError):
            os.fstat(fd)
        assert external.recv(1) == b""
    finally:
        wayland.close_prepared_input_method()
        external.close()


def test_native_stop_releases_connection_once_and_never_dispatches_again(native):
    bridge, peer = native
    bridge.start_shortcuts(lambda _action: pytest.fail("Stopped bridge dispatched a shortcut"))
    bridge.set_shortcuts_enabled(True)
    bridge._panel_snapshot = bridge.capture()
    bridge._menu = [("settings", "Settings", True)]
    bridge._ui_callback = lambda _action: pytest.fail("Shutdown reentered the UI")
    bridge.stop()
    bridge.stop()
    assert peer.closes == 1
    assert bridge._client is None and bridge._ui_callback is None
    assert bridge._panel_snapshot is None and bridge._menu is None
    assert bridge.capture() is None
    assert event(bridge, type=2, state=1, generation=peer.revision, name=b"Tab", key=23) == 0
    assert peer.commits == []


@pytest.mark.parametrize("public", [False, True])
def test_connection_loss_defers_destruction_until_native_callback_returns(native, qt_app, public):
    import socket
    from PySide6.QtCore import QSocketNotifier
    bridge, peer = native
    read, write = socket.socketpair()
    prefix = "_ghost_" if public else "_"
    read_notifier = QSocketNotifier(read.fileno(), QSocketNotifier.Type.Read)
    write_notifier = QSocketNotifier(read.fileno(), QSocketNotifier.Type.Write)
    setattr(bridge, prefix + "read_notifier", read_notifier)
    setattr(bridge, prefix + "write_notifier", write_notifier)
    def dispatch(_client):
        assert event(bridge, type=4) == 0
        assert peer.closes == 0
        assert bridge._close_pending
        return -1
    setattr(peer, "inklow_wl_ghost_dispatch" if public else "inklow_wl_dispatch", dispatch)
    try:
        (bridge._ghost_read_ready if public else bridge._read_ready)()
        assert peer.closes == 1
        assert bridge._client is None
        assert bridge._read_notifier is bridge._write_notifier is None
        assert bridge._ghost_read_notifier is bridge._ghost_write_notifier is None
        assert not read_notifier.isEnabled() and not write_notifier.isEnabled()
        assert not bridge._close_pending
    finally:
        bridge.stop()
        read.close()
        write.close()


def test_x11_stop_waits_for_hook_owned_socket_and_grab_release():
    import socket
    import threading
    from inklow.native.linux import LinuxBridge
    bridge = LinuxBridge.__new__(LinuxBridge)
    BridgeBase.__init__(bridge)
    bridge._available, bridge._thread = True, None
    bridge._wake_read, bridge._wake_write = socket.socketpair()
    bridge._wake_read.setblocking(False)
    bridge._wake_write.setblocking(False)
    read, write = socket.socketpair()
    entered, released = threading.Event(), threading.Event()
    class Connection:
        def screen(self):
            return SimpleNamespace(root=object())
        def keysym_to_keycode(self, _value):
            return 0
        def get_modifier_mapping(self):
            return []
        def pending_events(self):
            entered.set()
            return 0
        def fileno(self):
            return read.fileno()
        def ungrab_keyboard(self, _time):
            released.set()
        def close(self):
            read.close()
    bridge.x = SimpleNamespace(ControlMask=4, Mod1Mask=8, ShiftMask=1, Mod4Mask=64,
                               LockMask=2, CurrentTime=0)
    bridge.xk = SimpleNamespace(string_to_keysym=lambda value: value)
    bridge._display_type = Connection
    try:
        bridge.start_shortcuts(lambda _action: False)
        thread = bridge._thread
        assert entered.wait(2)
        bridge.stop()
        assert not thread.is_alive()
        assert released.is_set()
        assert read.fileno() == -1
        assert bridge._wake_read.fileno() == bridge._wake_write.fileno() == -1
        bridge.stop()
    finally:
        bridge.stop()
        read.close()
        write.close()


@pytest.fixture
def ghost_native(native, qt_app, monkeypatch):
    """Synthetic OS metric boundary; production geometry, paint and ACK paths."""
    from PySide6.QtGui import QFontInfo, QFontMetricsF, QImage
    from PySide6.QtWidgets import QApplication
    from inklow.core import InlineRender
    from dataclasses import asdict
    bridge, peer = native
    font = QApplication.font()
    device = QImage(800, 600, QImage.Format.Format_ARGB32_Premultiplied)
    device.setDevicePixelRatio(1.25)
    metrics = QFontMetricsF(font, device)
    family = QFontInfo(font).family()
    peer.ghost_capable = True
    bridge.atspi.WINDOW_COORDS = 1
    bridge.atspi.DESKTOP_COORDS = 0
    window = peer.field.parent
    window.flags.update(("visible", "showing"))
    peer.window_extents = SimpleNamespace(x=0, y=0, width=640, height=480)
    window.queryComponent = lambda: SimpleNamespace(getExtents=lambda coordinates: peer.window_extents)
    peer.getCharacterExtents = lambda offset, coordinates: (100, 40, 1, metrics.height())
    clip = (40, 60, 190, 400)
    render = InlineRender(family, font.pointSizeF(), 100 + metrics.ascent(), clip,
                          metrics.ascent(), metrics.descent(), source="native-peer-metric-boundary")
    monkeypatch.setattr(bridge, "_inline_metrics", lambda *args, **kwargs: render)
    def payload(text=" café e\u0301 tomorrow"):
        snapshot = bridge.capture()
        return snapshot, {"key": snapshot.key, "text": text, "mode": "completion",
                          "native_anchor": snapshot.native_anchor, "inline": True,
                          "render": asdict(snapshot.inline_render), "rect": list(snapshot.caret_rect)}
    return bridge, peer, payload


def test_ghost_requires_exact_window_client_geometry_and_positive_composition(ghost_native):
    bridge, peer, payload = ghost_native
    snapshot, _ = payload()
    assert snapshot.inline_safe and snapshot.window_revision == str(peer.ghost_scene)
    peer.window_extents.y = 28  # No invented titlebar correction is allowed.
    assert not bridge.capture().inline_safe
    peer.window_extents.y = 0
    peer.window_extents.width = 639
    assert not bridge.capture().inline_safe
    peer.window_extents.width = 640
    peer.composition_inactive = False
    assert not bridge.capture().inline_safe
    peer.composition_inactive = True
    peer.field.attributes = ["composition-active:true"]
    assert bridge.capture() is None


def test_ghost_only_matching_presentation_arms_exact_grapheme_prefix(ghost_native):
    import math
    from PySide6.QtCore import QTextBoundaryFinder
    bridge, peer, payload = ghost_native
    snapshot, offered = payload()
    acknowledgements = []
    bridge.bind_ui(lambda action, data=None: acknowledgements.append((action, data)))
    assert bridge.present_inline_native(offered)
    assert not bridge._ghost_presented and not peer.frames and peer.commits == []
    assert not any(data and data.get("text") for action, data in acknowledgements if action == "presentation")
    scene, frame, width, height, pixels = peer.ghost_frames[-1]
    _, _, _, _, logical_width, logical_height = peer.ghost_prepares[-1]
    assert width == math.ceil(logical_width * 1.25)
    assert height == math.ceil(logical_height * 1.25)
    assert any(pixels)  # A transparent text-only buffer, not an empty ACK.
    event(bridge, type=7, state=1, scene=scene, frame=frame + 1,
          generation=peer.revision, serial=peer.serial)
    assert not bridge._ghost_presented
    event(bridge, type=7, state=1, scene=scene, frame=frame,
          generation=peer.revision, serial=peer.serial)
    assert bridge._ghost_presented
    shown = acknowledgements[-1][1]["text"]
    assert shown and offered["text"].startswith(shown) and shown == bridge._ghost_text
    finder = QTextBoundaryFinder(QTextBoundaryFinder.BoundaryType.Grapheme, offered["text"])
    finder.setPosition(len(shown.encode("utf-16-le")) // 2)
    assert finder.isAtBoundary()
    assert not bridge.insert_inline(snapshot, offered["text"] + " invisible")
    assert peer.commits == []


@pytest.mark.parametrize("change", ["scene", "focus", "selection", "geometry", "serial"])
def test_stale_ghost_presentation_never_rearms(ghost_native, change):
    bridge, peer, payload = ghost_native
    _, offered = payload()
    acknowledgements = []
    bridge.bind_ui(lambda action, data=None: acknowledgements.append((action, data)))
    assert bridge.present_inline_native(offered)
    scene, frame = bridge._ghost_scene, bridge._ghost_frame
    if change == "scene":
        peer.ghost_scene += 1
    elif change == "focus":
        peer.field.flags.remove("focused")
    elif change == "selection":
        peer.selection = 1
    elif change == "geometry":
        peer.window_extents.height -= 1
    else:
        peer.serial += 1
    event(bridge, type=7, state=1, scene=scene, frame=frame,
          generation=peer.revision, serial=peer.serial)
    assert bridge._ghost_event is None and not bridge._ghost_presented
    assert not any(data and data.get("text") for action, data in acknowledgements if action == "presentation")


def test_pending_ghost_retries_without_rekey_and_revocation_disarms(ghost_native):
    bridge, peer, payload = ghost_native
    _, offered = payload()
    peer.ghost_ready = 2
    assert bridge.present_inline_native(offered)
    assert bridge._ghost_pending and not peer.ghost_frames
    scene = bridge._ghost_scene
    peer.ghost_ready = 1
    event(bridge, type=6, state=2, scene=scene)
    assert not bridge._ghost_pending and peer.ghost_frames
    assert bridge.capture().key == offered["key"] and bridge._ghost_scene == scene
    frame = bridge._ghost_frame
    event(bridge, type=7, state=1, scene=scene, frame=frame,
          generation=peer.revision, serial=peer.serial)
    assert bridge._ghost_presented
    event(bridge, type=7, state=0, scene=scene, frame=frame,
          generation=peer.revision, serial=peer.serial)
    assert not bridge._ghost_presented and bridge._ghost_event is None


def test_presented_ghost_prefix_commits_once_with_native_readback(ghost_native):
    bridge, peer, payload = ghost_native
    snapshot, offered = payload()
    before = peer.text
    assert bridge.present_inline_native(offered)
    event(bridge, type=7, state=1, scene=bridge._ghost_scene, frame=bridge._ghost_frame,
          generation=peer.revision, serial=peer.serial)
    visible = bridge._ghost_text
    assert visible and bridge.insert_inline(snapshot, visible)
    assert peer.text == before + visible and peer.commits == [visible]
    assert not bridge.insert_inline(snapshot, visible)
    assert peer.commits == [visible]


def test_public_display_failure_preserves_private_native_insertion(native, qt_app):
    import socket
    from PySide6.QtCore import QSocketNotifier
    bridge, peer = native
    read, write = socket.socketpair()
    bridge._ghost_read_notifier = QSocketNotifier(read.fileno(), QSocketNotifier.Type.Read)
    bridge._ghost_write_notifier = QSocketNotifier(read.fileno(), QSocketNotifier.Type.Write)
    peer.inklow_wl_ghost_dispatch = lambda client: -1
    try:
        bridge._ghost_read_ready()
        assert bridge._client is not None and peer.closes == 0
        assert bridge._ghost_read_notifier is bridge._ghost_write_notifier is None
        bridge._ghost_read_ready()
        bridge._ghost_write_ready()
        snapshot = bridge.capture()
        assert snapshot is not None and bridge.insert(snapshot, " tomorrow")
    finally:
        bridge.stop()
        read.close()
        write.close()


def test_revoked_ghost_rejects_already_claimed_acceptance_even_with_fresh_text(ghost_native, monkeypatch):
    bridge, peer, payload = ghost_native
    _, offered = payload()
    assert bridge.present_inline_native(offered)
    event(bridge, type=7, state=1, scene=bridge._ghost_scene, frame=bridge._ghost_frame,
          generation=peer.revision, serial=peer.serial)
    visible = bridge._ghost_text
    event(bridge, type=6, state=1, scene=bridge._ghost_scene + 1)
    current = bridge.capture()
    assert current is not None and not bridge.insert_inline(current, visible)
    monkeypatch.setattr(bridge, "_render", lambda: True)
    assert bridge.present_native(current.native_anchor, "")
    assert not bridge.insert_inline(current, visible)  # Manual remap cannot resurrect an old frame.
    assert peer.commits == []


def test_manual_alternative_replaces_a_live_ghost_without_becoming_inline_acceptance(ghost_native):
    bridge, peer, payload = ghost_native
    snapshot, offered = payload()
    before = peer.text
    assert bridge.present_inline_native(offered)
    event(bridge, type=7, state=1, scene=bridge._ghost_scene, frame=bridge._ghost_frame,
          generation=peer.revision, serial=peer.serial)

    assert bridge.insert(snapshot, " by a different route")

    assert peer.text == before + " by a different route"
    assert peer.commits == [" by a different route"]


def test_manual_alternative_after_frame_revocation_keeps_exact_native_target(ghost_native):
    bridge, peer, payload = ghost_native
    _, offered = payload()
    before = peer.text
    assert bridge.present_inline_native(offered)
    event(bridge, type=7, state=0, scene=bridge._ghost_scene, frame=bridge._ghost_frame,
          generation=peer.revision, serial=peer.serial)
    current = bridge.capture()

    assert current is not None and bridge.insert(current, " with a manual alternative")

    assert peer.text == before + " with a manual alternative"
    assert peer.commits == [" with a manual alternative"]


@pytest.mark.parametrize("owner", [None, False, 12, "", "org.kde.KWin", ":"])
def test_desktop_rejects_missing_or_nonunique_owner(native, desktop_bus, owner):
    bridge, peer = native
    bridge._start_desktop_watch()
    desktop_bus.reply([owner])
    assert peer.desktop_states[-1] == -1
    assert not desktop_bus.pending and not desktop_bus.subscriptions
    assert bridge.capture() is not None


@pytest.mark.parametrize("unavailable", ["peer", "bus"])
def test_desktop_unavailable_evidence_preserves_private_input(native, desktop_bus, unavailable):
    bridge, peer = native
    if unavailable == "peer":
        peer.compositor_pid = 0
    else:
        desktop_bus.connected = False
    bridge._start_desktop_watch()
    assert peer.desktop_states[-1] == -1
    assert not desktop_bus.pending and not desktop_bus.subscriptions
    snapshot = bridge.capture()
    assert snapshot is not None
    assert bridge.insert(snapshot, " tomorrow")
    assert peer.commits == [" tomorrow"]


@pytest.mark.parametrize("state", [True, None])
def test_desktop_revocation_retires_visible_acceptance(native, desktop_bus, state):
    bridge, peer = native
    identify_desktop(bridge, peer, desktop_bus)
    desktop_bus.reply([False])
    presentations = []
    bridge.bind_ui(lambda *arguments: presentations.append(arguments))
    bridge._ghost_event = {"key": "current-offer"}
    bridge._ghost_snapshot = bridge.capture()
    bridge._ghost_text = " tomorrow"
    bridge._ghost_pending = bridge._ghost_presented = True
    desktop_bus.emit(":1.42", "showingDesktopChanged", [state])
    assert peer.desktop_states[-1] == (1 if state is True else -1)
    assert bridge._ghost_event is None and bridge._ghost_snapshot is None
    assert not bridge._ghost_presented and not bridge._ghost_pending
    assert presentations == [("presentation", {"key": "current-offer", "text": "", "mode": "completion"})]


def test_public_display_loss_cancels_visibility_without_closing_private_input(native, desktop_bus):
    bridge, peer = native
    identify_desktop(bridge, peer, desktop_bus)
    bridge._disable_ghost_display()
    assert peer.desktop_states[-1] == -1
    states = list(peer.desktop_states)
    desktop_bus.reply([False])
    assert peer.desktop_states == states
    assert not desktop_bus.subscriptions
    assert bridge.capture() is not None and peer.closes == 0
