"""Actual Qt controls with controlled OS boundaries, never fake hardware events."""
import threading
import time
from types import SimpleNamespace

import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import Qt, QRect
from PySide6.QtWidgets import QApplication, QPushButton, QStyle

from inklow import system
from inklow.qt_power import PowerSampler, QtPowerUI
from inklow.runtime import ShutdownPending


def reading(**changes):
    """Build an explicitly unlocked low-battery sample, overriding only named fields."""
    sample = dict(available=True, has_battery=True, on_battery=True,
                  charging=False, percent=19, session_unlocked=True)
    sample.update(changes)
    return sample


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


@pytest.fixture
def ui(app):
    """Own the power UI and drain deferred widget disposal after closing it."""
    owner = QtPowerUI(app)
    yield owner
    owner.close()
    app.processEvents()


@pytest.mark.parametrize("sample,state,level", [
    (reading(percent=19), "low", "19%"),
    (reading(percent=20), "low", "20%"),
    (reading(percent=21), "battery", "21%"),
    (reading(percent=0), "low", "0%"),
    (reading(percent=54, on_battery=False, charging=True), "charging", "54%"),
    (reading(percent=100, on_battery=False), "full", "100%"),
    (reading(percent=84, on_battery=False), "external", "84%"),
    (reading(percent=84, on_battery=False, charging=None), "external_unknown", "84%"),
    (reading(has_battery=False, percent=None), "absent", None),
    (reading(available=False, percent=19), "unknown", None),
    (reading(percent=None), "battery", None),
    (reading(percent=True), "battery", None),
    (reading(percent=101), "battery", None),
    (reading(on_battery=None), "unknown", None),
])
def test_native_card_truthfully_renders_all_power_states(ui, sample, state, level):
    card = ui.make_card()
    result = ui.present(sample)
    assert result.state == state
    assert card.title.text() == result.title
    assert card.detail.text() == result.detail
    assert card.accessibleName() == result.title
    assert card.accessibleDescription() == result.detail
    assert card.glyph.accessibleName() == result.title
    assert not card.glyph.grab().isNull()
    if level:
        assert level in card.title.text() and level in result.compact
    else:
        assert "%" not in card.title.text() and "%" not in result.compact
    assert not ui.help_showing  # Rendering previews cannot notify.


@pytest.mark.parametrize("percent,asks", [(19, 1), (20, 1), (21, 0)])
def test_help_delivery_boundaries(ui, monkeypatch, percent, asks):
    delivered = []
    monkeypatch.setattr(ui, "_show_help", lambda: delivered.append(percent) or True)
    ui.present(reading(percent=percent), allow_help=True)
    ui.present(reading(percent=percent), allow_help=True)
    assert len(delivered) == asks


def test_refused_unknown_locked_and_preview_delivery_does_not_consume(ui, monkeypatch):
    delivered = []
    monkeypatch.setattr(ui, "_show_help", lambda: False)
    ui.present(reading(), allow_help=True)
    monkeypatch.setattr(ui, "_show_help", lambda: delivered.append(True) or True)
    ui.present(reading(session_unlocked=None), allow_help=True)
    ui.present(reading(session_unlocked=False), allow_help=True)
    ui.present(reading())
    assert not delivered
    ui.present(reading(), allow_help=True)
    assert delivered == [True]


def test_only_genuine_ac_not_previews_unknown_or_threshold_flaps_rearms(ui, monkeypatch):
    delivered = []
    monkeypatch.setattr(ui, "_show_help", lambda: delivered.append(True) or True)
    ui.present(reading(), allow_help=True)
    ui.dismiss_help()
    ui.present(reading(on_battery=False, charging=True))  # Synthetic AC preview.
    for sample in (reading(available=False, on_battery=False, charging=True),
                   reading(percent=21), reading(charging=True), reading()):
        ui.present(sample, allow_help=True)
    assert delivered == [True]
    ui.present(reading(on_battery=False, charging=None, percent=None), allow_help=True)
    ui.present(reading(), allow_help=True)
    assert delivered == [True, True]


def test_real_presenter_without_anchor_creates_nothing(ui):
    ui.present(reading(), allow_help=True)
    assert ui._help_panel is None
    assert not ui.help_showing
    assert ui.policy.should_ask(reading(), delivered=False)


def test_actual_nonactivating_help_has_native_mouse_and_keyboard_dismiss_paths(ui, app, monkeypatch):
    screen = app.primaryScreen()
    if screen is None:
        pytest.skip("A real Qt screen is required")
    # Only the OS-boundary eligibility and anchor are injected. Surface/rendering,
    # nonactivation attributes and native clicked signals remain real.
    area = screen.availableGeometry()
    monkeypatch.setattr(ui, "_help_anchor", lambda: (QRect(area.left() + 20, area.top() + 20, 30, 20), screen))
    card = ui.make_card()
    card.show()
    app.processEvents()
    active = app.activeWindow()
    focus = app.focusWidget()
    ui.present(reading(), allow_help=True)
    panel = ui._help_panel
    assert panel is not None and panel.isVisible() and ui.help_showing
    assert panel.windowFlags() & Qt.WindowType.WindowDoesNotAcceptFocus
    assert panel.testAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating)
    assert panel.focusPolicy() == Qt.FocusPolicy.NoFocus
    assert app.activeWindow() is active
    assert app.focusWidget() is focus
    assert ui._help_timer.isActive()
    assert ui._help_timer.interval() == 12000
    assert card.dismiss.isVisible()
    assert card.dismiss.focusPolicy() != Qt.FocusPolicy.NoFocus
    card.dismiss.click()
    assert not panel.isVisible() and not ui.help_showing
    assert not ui._help_timer.isActive()
    ui.present(reading(on_battery=False), allow_help=True)
    ui.present(reading(), allow_help=True)
    panel.findChild(QPushButton).click()
    assert not ui.help_showing


def test_lock_or_unknown_session_hides_existing_surface(ui, app, monkeypatch):
    screen = app.primaryScreen()
    if screen is None:
        pytest.skip("A real Qt screen is required")
    monkeypatch.setattr(ui, "_help_anchor", lambda: (screen.availableGeometry(), screen))
    ui.present(reading(), allow_help=True)
    assert ui.help_showing
    ui.present(reading(session_unlocked=False), allow_help=True)
    assert not ui.help_showing and not ui._help_panel.isVisible()
    ui.present(reading(on_battery=False), allow_help=True)
    ui.present(reading(), allow_help=True)
    assert ui.help_showing
    ui.present(reading(session_unlocked=None), allow_help=True)
    assert not ui.help_showing


def test_charging_motion_is_bounded_interruptible_and_respects_preferences(ui, app):
    card = ui.make_card()
    card.show()
    app.processEvents()
    ui.apply_display_preferences(reduce_motion=False, increase_contrast=False,
                                 reduce_transparency=True)
    ui.set_visibility(pane=True)
    ui.present(reading(on_battery=False, charging=True, percent=45))
    if app.style().styleHint(QStyle.StyleHint.SH_Widget_Animation_Duration) > 0:
        animation = ui._animations["pane"]
        assert animation.duration() * animation.loopCount() <= 5000
        assert animation.propertyName() == b"opacity"
        ui.set_visibility(pane=False)
        assert not ui._animations
        assert card.opacity.opacity() == 1
        ui.set_visibility(pane=True)
        assert ui._animations
    ui.apply_display_preferences(reduce_motion=True, increase_contrast=True,
                                 reduce_transparency=True)
    assert not ui._animations
    assert card.glyph.high_contrast
    assert "2px solid" in card.styleSheet()
    assert card.opacity.opacity() == 1
    assert card.autoFillBackground()


def test_unknown_motion_is_static_and_close_owns_all_surfaces(ui):
    card = ui.make_card()
    ui.apply_display_preferences()
    ui.present(reading(on_battery=False, charging=True))
    assert ui.reduce_motion
    assert not ui._animations
    ui.release_card("pane")
    assert not ui.cards
    ui.make_card()
    ui.close()
    ui.close()
    assert not ui.cards and not ui._animations and not ui.help_showing
    before = ui.presentation
    ui.present(reading())
    assert ui.presentation == before


def test_sampler_is_off_main_shared_cache_backed_and_emits_on_gui(app, monkeypatch):
    calls = []
    main = threading.get_ident()
    monkeypatch.setattr(system, "power_status", lambda: calls.append(threading.get_ident()) or reading())
    monkeypatch.setattr(system, "linux_session_display_preferences", lambda **kw: {"session_unlocked": True})
    sampler = PowerSampler(app)
    assert sampler.reading["available"] is False
    emitted = []
    sampler.readingReady.connect(lambda value: emitted.append((value, threading.get_ident())))
    sampler.start()
    deadline = time.monotonic() + 2
    try:
        while not emitted and time.monotonic() < deadline:
            app.processEvents()
            time.sleep(.005)
        assert emitted and calls[0] != main
        assert emitted[0][1] == main
        assert sampler.reading == emitted[0][0]
    finally:
        sampler._cancel.set()
        sampler._worker.join(2)
        sampler.close()


def test_sampler_close_is_nonblocking_and_keeps_shutdown_pending_until_real_exit(app, monkeypatch):
    entered, release = threading.Event(), threading.Event()
    emitted = []

    def blocked_sample():
        entered.set()
        assert release.wait(3)
        return reading()

    monkeypatch.setattr(system, "power_status", blocked_sample)
    sampler = PowerSampler(app)
    sampler.readingReady.connect(emitted.append)
    sampler.start()
    try:
        assert entered.wait(2)
        # The worker is inside the blocked OS boundary: close must request
        # cancellation without reporting ownership released before its real exit.
        before = time.monotonic()
        with pytest.raises(ShutdownPending):
            sampler.close()
        assert time.monotonic() - before < .2
        assert sampler._cancel.is_set() and sampler._worker.is_alive()
    finally:
        release.set()
        sampler._worker.join(2)
        sampler.close()
        sampler.close()
    app.processEvents()
    assert not emitted
    assert sampler._worker is None


def test_sampler_close_before_start_never_launches_work(app, monkeypatch):
    calls = []
    monkeypatch.setattr(system, "power_status", lambda: calls.append(True))
    sampler = PowerSampler(app)
    sampler.close()
    sampler.start()
    assert sampler._worker is None and not calls
