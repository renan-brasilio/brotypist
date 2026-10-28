"""Read-only, identity-bound KWin desktop state with an async bootstrap deadline."""
from __future__ import annotations

from PySide6.QtCore import QObject, QTimer, SLOT, Slot
from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusVariant


class DesktopWatch(QObject):
    """Track one compositor lifetime on the owning Qt event-loop thread.

    ``changed`` receives -1 for unknown/lost evidence, 0 for normal windows,
    and 1 for showing the desktop. Loss is terminal; create a new watch rather
    than retrying against a potentially different compositor identity.
    """

    def __init__(self, compositor_pid: int, changed):
        super().__init__()
        self._pid = compositor_pid
        self._changed = changed
        self._bus = None
        self._owner = ""
        self._revision = 0
        self._stopped = False
        self._calls = set()
        self._subscriptions = []
        self._deadline = QTimer(self)
        self._deadline.setSingleShot(True)
        self._deadline.timeout.connect(self.stop)

    def start(self) -> None:
        """Subscribe before resolving KWin, with a 200 ms bootstrap deadline."""
        self._changed(-1)
        if self._stopped:
            return
        try:
            self._bus = QDBusConnection.sessionBus()
            if type(self._pid) is not int or self._pid <= 0 or not self._bus.isConnected():
                self.stop()
                return
            # Watch before resolving the name so owner changes during bootstrap
            # invalidate even replies already queued by the bus daemon.
            if not self._subscribe("org.freedesktop.DBus", "/org/freedesktop/DBus",
                                   "org.freedesktop.DBus", "NameOwnerChanged", "ownerChanged"):
                return
            if not self._subscribe("", "/org/freedesktop/DBus/Local",
                                   "org.freedesktop.DBus.Local", "Disconnected", "disconnected"):
                return
            self._deadline.start(200)
            self._request("org.freedesktop.DBus", "/org/freedesktop/DBus",
                          "org.freedesktop.DBus", "GetNameOwner", ["org.kde.KWin"], self._identified)
        except Exception:
            self.stop()

    def _subscribe(self, service, path, interface, signal, slot) -> bool:
        subscription = (service, path, interface, signal, self, SLOT(f"{slot}(QDBusMessage)"))
        if not self._bus.connect(*subscription):
            self.stop()
            return False
        self._subscriptions.append(subscription)
        return True

    def _request(self, service, path, interface, method, arguments, completed) -> None:
        """Own an async reply watcher and discard replies superseded by signals."""
        if self._stopped:
            return
        revision = self._revision
        message = QDBusMessage.createMethodCall(service, path, interface, method)
        message.setArguments(arguments)
        call = QDBusPendingCallWatcher(self._bus.asyncCall(message, 200), self)
        self._calls.add(call)

        def finished(watcher):
            if watcher not in self._calls:
                return
            self._calls.remove(watcher)
            try:
                # Signals are newer evidence than the initial property request,
                # including when that request subsequently fails or times out.
                if self._stopped or revision != self._revision:
                    return
                reply = watcher.reply()
                if reply.type() != QDBusMessage.MessageType.ReplyMessage:
                    self.stop()
                    return
                completed(reply.arguments())
            except Exception:
                self.stop()
            finally:
                watcher.deleteLater()

        call.finished.connect(finished)
        if call.isFinished():
            QTimer.singleShot(0, lambda: finished(call))

    def _identified(self, arguments) -> None:
        if (len(arguments) != 1 or type(arguments[0]) is not str
                or not arguments[0].startswith(":") or len(arguments[0]) < 2):
            self.stop()
            return
        self._owner = arguments[0]
        self._request("org.freedesktop.DBus", "/org/freedesktop/DBus",
                      "org.freedesktop.DBus", "GetConnectionUnixProcessID", [self._owner], self._authenticated)

    def _authenticated(self, arguments) -> None:
        """Bind property reads and signals to the compositor's verified bus owner."""
        if len(arguments) != 1 or type(arguments[0]) is not int or arguments[0] != self._pid:
            self.stop()
            return
        if not self._subscribe(self._owner, "/KWin", "org.kde.KWin",
                               "showingDesktopChanged", "desktopChanged"):
            return
        self._request(self._owner, "/KWin", "org.freedesktop.DBus.Properties", "Get",
                      ["org.kde.KWin", "showingDesktop"], self._property)

    def _property(self, arguments) -> None:
        if len(arguments) != 1:
            self.stop()
            return
        value = arguments[0]
        if isinstance(value, QDBusVariant):
            value = value.variant()
        self._apply(value)

    def _apply(self, value) -> None:
        if type(value) is not bool:
            self.stop()
            return
        self._deadline.stop()
        self._changed(int(value))

    @Slot(QDBusMessage)
    def desktopChanged(self, message) -> None:
        """Apply a signal only from the authenticated unique bus owner."""
        if self._stopped or message.service() != self._owner:
            return
        self._revision += 1
        arguments = message.arguments()
        if len(arguments) != 1:
            self.stop()
            return
        self._apply(arguments[0])

    @Slot(QDBusMessage)
    def ownerChanged(self, message) -> None:
        if self._stopped:
            return
        arguments = message.arguments()
        if arguments and arguments[0] == "org.kde.KWin":
            self.stop()

    @Slot(QDBusMessage)
    def disconnected(self, message) -> None:
        self.stop()

    def stop(self) -> None:
        """Publish unknown once, disconnect subscriptions, and defer Qt deletion."""
        if self._stopped:
            return
        self._stopped = True
        self._revision += 1
        self._deadline.stop()
        changed, self._changed = self._changed, None
        changed(-1)
        for call in self._calls:
            call.deleteLater()
        self._calls.clear()
        for subscription in self._subscriptions:
            self._bus.disconnect(*subscription)
        self._subscriptions.clear()
        self.deleteLater()
