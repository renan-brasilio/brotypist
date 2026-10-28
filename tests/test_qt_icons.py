"""Small and large logical icon requests must retain different visible artwork."""
import pytest

pytest.importorskip("PySide6")
from PySide6.QtCore import QSize
from PySide6.QtWidgets import QApplication, QWidget

from inklow.resources import configure_identity


@pytest.mark.parametrize("logical_size,dpr,pills", [
    (16, 1.0, 1), (16, 2.0, 1),
    (24, 1.0, 1), (24, 2.0, 1),
    (32, 1.0, 1), (32, 2.0, 1),
    (33, 1.0, 2), (33, 2.0, 2),
    (64, 1.0, 2), (64, 2.0, 2),
])
def test_window_icon_uses_logical_size_for_visible_ghost_pills(logical_size, dpr, pills):
    app = QApplication.instance() or QApplication([])
    configure_identity(app)
    window = QWidget()
    pixmap = window.windowIcon().pixmap(QSize(logical_size, logical_size), dpr)
    assert pixmap.devicePixelRatio() == dpr
    assert pixmap.width() == round(logical_size * dpr)
    image = pixmap.toImage()
    assert not image.isNull()
    # Count separated mint strokes across the center row, not hashes or a copy
    # of the source asset. At equal physical size, 32pt@2x needs one pill while
    # 64pt@1x needs two; routing by raster dimensions cannot satisfy both.
    runs, in_pill = 0, False
    for x in range(image.width()):
        color = image.pixelColor(x, image.height() // 2)
        mint = (color.alpha() > 128 and color.green() - color.red() > 20
                and color.green() - color.blue() > 5)
        if mint and not in_pill:
            runs += 1
        in_pill = mint
    assert runs == pills
    window.close()


def test_ghost_caret_artwork_tracks_native_palette_without_changing_silhouette():
    from PySide6.QtGui import QColor, QPalette
    from inklow.qt_ui import GhostMark
    app = QApplication.instance() or QApplication([])
    mark = GhostMark()
    masks = []
    try:
        for color in (QColor("#101010"), QColor("#eeeeee")):
            palette = mark.palette()
            palette.setColor(QPalette.ColorRole.WindowText, color)
            mark.setPalette(palette)
            app.processEvents()
            image = mark.pixmap().toImage()
            visible = [image.pixelColor(x, y) for y in range(image.height()) for x in range(image.width())
                       if image.pixelColor(x, y).alpha() > 128]
            assert visible
            assert all(abs(pixel.red() - color.red()) <= 2 and abs(pixel.green() - color.green()) <= 2
                       and abs(pixel.blue() - color.blue()) <= 2 for pixel in visible)
            masks.append([image.pixelColor(x, y).alpha() for y in range(image.height()) for x in range(image.width())])
        assert masks[0] == masks[1]
        assert mark.accessibleName()
    finally:
        mark.deleteLater()
        app.processEvents()


def test_icon_actions_have_accessible_names_keyboard_path_and_native_focus():
    from PySide6.QtCore import Qt
    from PySide6.QtWidgets import QStyle
    from inklow.qt_ui import _icon_action
    app = QApplication.instance() or QApplication([])
    parent = QWidget()
    button = _icon_action(parent, "Close window", QStyle.StandardPixmap.SP_DialogCloseButton,
                          lambda: None, "Ctrl+W")
    assert not button.icon().isNull()
    assert button.accessibleName() == "Close window"
    assert "Close window" in button.toolTip()
    assert button.focusPolicy() == Qt.FocusPolicy.StrongFocus
    assert not button.shortcut().isEmpty()
    parent.deleteLater()
    app.processEvents()
