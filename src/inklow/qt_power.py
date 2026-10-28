"""Native Qt power cards and nonactivating help; all OS sampling stays off-main."""
from __future__ import annotations

import threading

from PySide6.QtCore import (QEasingCurve, QObject, QPoint, QRectF, Qt, QLocale,
                            QPropertyAnimation, QTimer, Signal, Slot)
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPalette, QPen
from PySide6.QtWidgets import (QApplication, QFrame, QGraphicsOpacityEffect,
                               QHBoxLayout, QLabel, QPushButton, QStyle,
                               QVBoxLayout, QWidget)

from . import system
from .power import LowBatteryHelpPolicy, charging_help_text, power_presentation
from .runtime import ShutdownPending


class PowerSampler(QObject):
    """One owned, cancellable worker; cache-backed power and bounded session facts.

    Closing never waits on the GUI thread. The application must retain this owner
    and retry close after ShutdownPending until the actual worker has exited.
    """
    readingReady = Signal(dict)
    _sampleReady = Signal(dict)
    refresh_seconds = 3.0

    def __init__(self, parent=None):
        super().__init__(parent)
        self.reading = system._unknown_power_status()
        self.reading["session_unlocked"] = None
        self._cancel = threading.Event()
        self._worker = None
        self._closed = False
        self._sampleReady.connect(self._accept, Qt.ConnectionType.QueuedConnection)

    def start(self):
        if self._closed or self._worker is not None:
            return
        self._worker = threading.Thread(target=self._run, name="inklow-power", daemon=False)
        self._worker.start()

    def _run(self):
        while not self._cancel.is_set():
            try:
                reading = system.power_status()
            except (OSError, UnicodeError):
                reading = system._unknown_power_status()
            if self._cancel.is_set():
                break
            reading.update(system.linux_session_display_preferences(cancel=self._cancel))
            if self._cancel.is_set():
                break
            self._sampleReady.emit(reading)
            if self._cancel.wait(self.refresh_seconds):
                break

    @Slot(dict)
    def _accept(self, reading):
        if not self._closed:
            self.reading = dict(reading)
            self.readingReady.emit(dict(reading))

    def close(self):
        self._closed = True
        self._cancel.set()
        worker = self._worker
        if worker is not None:
            worker.join(timeout=0)
            if worker.is_alive():
                raise ShutdownPending("Finishing the bounded power/session sample.")
            self._worker = None


class _PowerGlyph(QWidget):
    """Palette-aware native painting; percentage comes from the actual reading."""
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setFixedSize(40, 32)
        self.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.presentation = power_presentation({})
        self.percent = None
        self.high_contrast = False

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        color = self.palette().color(QPalette.ColorRole.WindowText)
        state = self.presentation.state
        if not self.high_contrast:
            if state == "low":
                color = QColor("#b45f06") if self.palette().color(QPalette.ColorRole.Window).lightness() > 128 else QColor("#ffbc69")
            elif state == "charging":
                color = QColor("#207747") if self.palette().color(QPalette.ColorRole.Window).lightness() > 128 else QColor("#7ddca0")
        painter.setPen(QPen(color, 2))
        if state == "unknown":
            painter.drawEllipse(QRectF(8, 4, 24, 24))
            painter.drawText(self.rect(), Qt.AlignmentFlag.AlignCenter, "?")
            return
        if state in {"absent", "external_unknown"}:
            painter.drawLine(15, 4, 15, 12)
            painter.drawLine(25, 4, 25, 12)
            painter.drawRoundedRect(QRectF(12, 11, 16, 11), 3, 3)
            painter.drawLine(20, 22, 20, 29)
            return
        painter.drawRoundedRect(QRectF(3, 7, 31, 18), 3, 3)
        painter.drawLine(37, 12, 37, 20)
        if self.percent is not None:
            width = 25 * self.percent / 100
            painter.fillRect(QRectF(6, 10, width, 12), color)
        else:
            painter.drawText(QRectF(3, 7, 31, 18), Qt.AlignmentFlag.AlignCenter, "?")
        if state == "charging":
            path = QPainterPath()
            path.moveTo(22, 3)
            path.lineTo(14, 17)
            path.lineTo(21, 17)
            path.lineTo(17, 29)
            painter.setPen(QPen(self.palette().color(QPalette.ColorRole.Window), 5))
            painter.drawPath(path)
            painter.setPen(QPen(color, 2))
            painter.drawPath(path)


class _PowerCard(QFrame):
    def __init__(self, owner, name, compact):
        super().__init__()
        self.owner = owner
        self.name = name
        self.setObjectName(f"inklow.power.{name}")
        row = QHBoxLayout(self)
        row.setContentsMargins(12, 10, 12, 10)
        self.glyph = _PowerGlyph(self)
        row.addWidget(self.glyph)
        labels = QVBoxLayout()
        self.title = QLabel()
        font = self.title.font()
        font.setBold(True)
        self.title.setFont(font)
        self.title.setWordWrap(True)
        self.detail = QLabel()
        self.detail.setWordWrap(True)
        self.detail.setVisible(not compact)
        labels.addWidget(self.title)
        labels.addWidget(self.detail)
        self.dismiss = QPushButton("Dismiss charging help")
        self.dismiss.setAccessibleName("Dismiss charging help")
        self.dismiss.setToolTip("Dismiss the low-battery request")
        self.dismiss.clicked.connect(owner.dismiss_help)
        self.dismiss.hide()
        labels.addWidget(self.dismiss)
        row.addLayout(labels, 1)
        self.opacity = QGraphicsOpacityEffect(self.glyph)
        self.glyph.setGraphicsEffect(self.opacity)

    def hideEvent(self, event):
        self.owner._stop_card_motion(self.name)
        super().hideEvent(event)


class _HelpPanel(QFrame):
    hidden = Signal()

    def hideEvent(self, event):
        super().hideEvent(event)
        self.hidden.emit()


class QtPowerUI(QObject):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.shell = None  # MenuAgent is not QObject; assigned by its integration owner.
        self.reading = system._unknown_power_status()
        self.presentation = power_presentation(self.reading)
        self.reduce_motion = True  # Unknown desktop preference means static feedback.
        self.increase_contrast = False
        self.reduce_transparency = True  # All surfaces use opaque system materials.
        self.help_showing = False
        self.policy = LowBatteryHelpPolicy()
        self.cards = {}
        self._animations = {}
        self._closed = False
        self._menu_visible = False
        self._pane_visible = False
        self._requested_motion = None
        self._requested_contrast = None
        self._help_panel = None
        self._help_timer = QTimer(self)
        self._help_timer.setSingleShot(True)
        self._help_timer.setInterval(12000)
        self._help_timer.timeout.connect(self.dismiss_help)
        app = QApplication.instance()
        if app is not None:
            hints = app.styleHints()
            hints.colorSchemeChanged.connect(self._native_preferences_changed)
            # QAccessibilityHints was introduced in Qt 6.10, not our 6.8 minimum.
            accessibility = getattr(hints, "accessibility", None)
            if callable(accessibility):
                contrast = accessibility()
                changed = getattr(contrast, "contrastPreferenceChanged", None)
                if changed is not None:
                    changed.connect(self._native_preferences_changed)
        self.apply_display_preferences()

    def _native_preferences_changed(self, *args):
        if not self._closed:
            self.apply_display_preferences(reduce_motion=self._requested_motion,
                                           increase_contrast=self._requested_contrast,
                                           reduce_transparency=self.reduce_transparency)

    def apply_display_preferences(self, *, reduce_motion=None, increase_contrast=None,
                                  reduce_transparency=None):
        self._requested_motion = reduce_motion
        self._requested_contrast = increase_contrast
        app = QApplication.instance()
        duration = app.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration) if app else 0
        self.reduce_motion = reduce_motion is not False or duration <= 0
        contrast = bool(increase_contrast)
        if app is not None:
            accessibility = getattr(app.styleHints(), "accessibility", None)
            if callable(accessibility):
                preference = accessibility().contrastPreference()
                contrast = contrast or getattr(preference, "name", "") == "HighContrast"
        self.increase_contrast = contrast
        self.reduce_transparency = reduce_transparency is not False
        self.stop_motion()
        for card in self.cards.values():
            self._render_card(card)
        if self._help_panel is not None:
            self._style_box(self._help_panel)

    def _style_box(self, box):
        border = "2px solid palette(window-text)" if self.increase_contrast else "1px solid palette(mid)"
        box.setStyleSheet(f"QFrame#{box.objectName().replace('.', '_')} {{ background: palette(window); border: {border}; border-radius: 8px; }}")
        # CSS identifiers cannot contain dots; retain the public accessible name.
        box.setObjectName(box.objectName().replace(".", "_"))
        box.setAutoFillBackground(True)

    def make_card(self, name="pane", compact=False):
        self.release_card(name)
        card = _PowerCard(self, name, compact)
        self.cards[name] = card
        self._render_card(card)
        return card

    def release_card(self, name):
        self._stop_card_motion(name)
        card = self.cards.pop(name, None)
        if card is not None:
            card.hide()
            card.deleteLater()

    def _render_card(self, card):
        p = self.presentation
        self._style_box(card)
        card.title.setText(p.title)
        card.detail.setText(p.detail)
        card.setAccessibleName(p.title)
        card.setAccessibleDescription(p.detail)
        card.setToolTip(p.title + ". " + p.detail)
        card.dismiss.setVisible(self.help_showing)
        card.glyph.presentation = p
        percent = self.reading.get("percent")
        card.glyph.percent = percent if type(percent) is int and 0 <= percent <= 100 else None
        card.glyph.high_contrast = self.increase_contrast
        card.glyph.setAccessibleName(p.title)
        card.glyph.update()

    def _stop_card_motion(self, name):
        animation = self._animations.pop(name, None)
        if animation is not None:
            animation.stop()
            animation.deleteLater()
        card = self.cards.get(name)
        if card is not None:
            card.opacity.setOpacity(1.0)

    def _charge_motion(self, name):
        card = self.cards[name]
        if (self.reduce_motion or self.increase_contrast or self._closed
                or self.reading.get("session_unlocked") is not True or not card.isVisible()):
            return
        self._stop_card_motion(name)
        animation = QPropertyAnimation(card.opacity, b"opacity", self)
        animation.setDuration(1200)
        animation.setEasingCurve(QEasingCurve.Type.InOutSine)
        animation.setStartValue(1.0)
        animation.setKeyValueAt(.5, .45)
        animation.setEndValue(1.0)
        animation.setLoopCount(3)  # 3.6 seconds total, never a perpetual frame timer.
        self._animations[name] = animation
        animation.start()

    def set_visibility(self, *, menu=None, pane=None):
        if menu is not None:
            self._menu_visible = bool(menu)
        if pane is not None:
            self._pane_visible = bool(pane)
        for name in self.cards:
            visible = self._menu_visible if name == "menu" else self._pane_visible
            if not visible:
                self._stop_card_motion(name)
            elif self.presentation.state == "charging":
                self._charge_motion(name)

    def present(self, reading, *, allow_help=False):
        if self._closed:
            return self.presentation
        previous = self.presentation
        self.reading = dict(reading)
        self.presentation = power_presentation(self.reading)
        if any(key in reading for key in ("reduce_motion", "increase_contrast", "reduce_transparency")):
            self.apply_display_preferences(reduce_motion=reading.get("reduce_motion"),
                                           increase_contrast=reading.get("increase_contrast"),
                                           reduce_transparency=reading.get("reduce_transparency"))
        if previous != self.presentation:
            self.stop_motion()
        for card in self.cards.values():
            self._render_card(card)
        unlocked = reading.get("session_unlocked") is True
        if not unlocked or self.presentation.state not in {"low", "battery"}:
            self.dismiss_help()
        if not unlocked:
            self.stop_motion()
        elif previous.state != "charging" and self.presentation.state == "charging":
            for name in self.cards:
                if (self._menu_visible if name == "menu" else self._pane_visible):
                    self._charge_motion(name)
        # Preview rendering never consumes a delivery OR rearms a real discharge.
        if allow_help and self.policy.should_ask(reading, delivered=False):
            if unlocked and self._show_help():
                self.policy.should_ask(reading)
        return self.presentation

    def _help_anchor(self):
        if self.reading.get("session_unlocked") is not True:
            return None
        tray = getattr(self.shell, "tray", None)
        if tray is not None and tray.isVisible():
            rect = tray.geometry()
            if rect.isValid() and not rect.isEmpty():
                screen = QApplication.screenAt(rect.center())
                if screen is not None:
                    return rect, screen
        for card in self.cards.values():
            if card.isVisible() and not card.window().isMinimized():
                rect = card.rect()
                rect.moveTopLeft(card.mapToGlobal(QPoint(0, 0)))
                screen = QApplication.screenAt(rect.center())
                if screen is not None:
                    return rect, screen
        return None

    def _show_help(self):
        target = self._help_anchor()
        if target is None:
            return False
        rect, screen = target
        if self._help_panel is None:
            panel = _HelpPanel(None, Qt.WindowType.ToolTip | Qt.WindowType.WindowDoesNotAcceptFocus)
            panel.hidden.connect(self.dismiss_help)
            panel.setObjectName("inklow_power_help")
            panel.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
            panel.setAttribute(Qt.WidgetAttribute.WA_QuitOnClose, False)
            panel.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            title, detail = charging_help_text(QLocale.system().name())
            panel.setAccessibleName(title)
            panel.setAccessibleDescription(detail)
            layout = QVBoxLayout(panel)
            layout.setContentsMargins(16, 12, 16, 12)
            heading = QLabel(title)
            font = heading.font()
            font.setBold(True)
            heading.setFont(font)
            body = QLabel(detail)
            body.setWordWrap(True)
            body.setMaximumWidth(290)
            dismiss = QPushButton("Dismiss")
            dismiss.setAccessibleName("Dismiss charging help")
            dismiss.setToolTip("Dismiss the low-battery request")
            dismiss.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            dismiss.clicked.connect(self.dismiss_help)
            layout.addWidget(heading)
            layout.addWidget(body)
            layout.addWidget(dismiss)
            self._help_panel = panel
        panel = self._help_panel
        self._style_box(panel)
        panel.adjustSize()
        area = screen.availableGeometry()
        x = max(area.left(), min(rect.right() - panel.width(), area.right() - panel.width() + 1))
        y = rect.bottom() + 8
        if y + panel.height() > area.bottom():
            y = rect.top() - panel.height() - 8
        y = max(area.top(), min(y, area.bottom() - panel.height() + 1))
        panel.move(x, y)  # Only our nonactivating surface; never the user's window.
        panel.show()
        self.help_showing = panel.isVisible()
        if self.help_showing:
            self._help_timer.start()
            for card in self.cards.values():
                card.dismiss.show()
        return self.help_showing

    def dismiss_help(self):
        self._help_timer.stop()
        self.help_showing = False
        if self._help_panel is not None:
            self._help_panel.hide()
        for card in self.cards.values():
            card.dismiss.hide()

    def stop_motion(self):
        for name in list(self._animations):
            self._stop_card_motion(name)

    def close(self):
        if self._closed:
            return
        self._closed = True
        self.dismiss_help()
        self.stop_motion()
        for name in list(self.cards):
            self.release_card(name)
        if self._help_panel is not None:
            self._help_panel.close()
            self._help_panel.deleteLater()
            self._help_panel = None
