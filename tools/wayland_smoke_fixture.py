#!/usr/bin/env python3
"""Private helpers for smoke_wayland.py, not a shipped application test hook.

Protocol references (requests are sent only to the owned virtual socket):
https://wayland.freedesktop.org/docs/html/ch04.html
https://github.com/KDE/plasma-wayland-protocols/blob/master/src/protocols/fake-input.xml
https://github.com/KDE/kwin/blob/master/src/backends/fakeinput/fakeinputbackend.cpp
https://github.com/KDE/kwin/blob/master/src/scripting/scripting.cpp

KWin may deny fake input. Authentication uses this fixture's own name, never
Orca or a privileged desktop identity. A denied/missing protocol is incomplete
proof, not a successful keyboard test or an excuse to use QTest instead.
"""
from __future__ import annotations

import ctypes
import ctypes.util
from dataclasses import asdict
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import secrets
import shlex
import socket
import stat
import struct
import subprocess
import sys
import time
import traceback

INTERFACE = "org.inklow.WaylandSmoke"
IM_SERVICE = INTERFACE + ".IM"
TARGET_SERVICE = INTERFACE + ".Target"
SOCKET = "inklow-smoke"
REFUSED = " FORBIDDEN-NATIVE-COMMIT"
PREFIX = "The three steps for planting a tree are to"


class ProofUnavailable(RuntimeError):
    pass


def isolation(*, display: bool = False) -> tuple[Path, Path]:
    root = Path(os.environ["INKLOW_SMOKE_ROOT"]).resolve(strict=True)
    marker = json.loads((root / "owner.json").read_text())
    if marker != {"token": os.environ.get("INKLOW_SMOKE_TOKEN"), "uid": os.getuid()}:
        raise RuntimeError("Not an owned smoke environment")
    if root.stat().st_uid != os.getuid() or stat.S_IMODE(root.stat().st_mode) != 0o700:
        raise RuntimeError("Smoke root ownership/mode is not private")
    for key in ("HOME", "XDG_RUNTIME_DIR", "XDG_CONFIG_HOME", "XDG_CONFIG_DIRS",
                "XDG_DATA_HOME", "XDG_CACHE_HOME", "INKLOW_HOME"):
        Path(os.environ[key]).resolve(strict=True).relative_to(root)
    if not os.environ.get("DBUS_SESSION_BUS_ADDRESS"):
        raise RuntimeError("Missing private session bus")
    if os.environ.get("DISPLAY"):
        owned_x11(root)
    if display:
        name = os.environ.get("WAYLAND_DISPLAY")
        if name != SOCKET or not stat.S_ISSOCK((root / "runtime" / name).stat().st_mode):
            raise RuntimeError("Only the owned virtual KWin socket is allowed")
    return root, Path(os.environ["INKLOW_SMOKE_OUTPUT"])


def owned_x11(root):
    """Validate the private X server capability; never accept a caller display."""
    marker = json.loads((root / "x11.json").read_text())
    auth = Path(os.environ["XAUTHORITY"]).resolve(strict=True)
    auth.relative_to(root)
    if (marker.get("token") != os.environ["INKLOW_SMOKE_TOKEN"]
            or marker.get("display") != os.environ.get("DISPLAY")
            or marker.get("auth") != str(auth)
            or marker.get("uid") != os.getuid()
            or stat.S_IMODE(auth.stat().st_mode) != 0o600
            or os.getpgid(marker["pid"]) != os.getpgrp()):
        raise RuntimeError("X11 is not our private authenticated Xvfb")
    return marker


def start_xvfb(root, output, env):
    executable = env.get("INKLOW_SMOKE_XVFB")
    if not executable:
        raise ProofUnavailable("scene-readback: Xvfb is not installed in the guest")
    number = None
    for _ in range(128):
        candidate = 100 + secrets.randbelow(59000)
        if not Path(f"/tmp/.X11-unix/X{candidate}").exists() and not Path(f"/tmp/.X{candidate}-lock").exists():
            number = candidate
            break
    if number is None:
        raise ProofUnavailable("scene-readback: no unused private X display number")
    display = ":" + str(number)
    auth = root / "Xauthority"
    # Xauthority familyLocal, exact hostname/display, random MIT cookie.
    parts = (socket.gethostname().encode(), str(number).encode(),
             b"MIT-MAGIC-COOKIE-1", secrets.token_bytes(16))
    with auth.open("xb") as stream:
        auth.chmod(0o600)
        stream.write(struct.pack("!H", 256))
        for part in parts:
            stream.write(struct.pack("!H", len(part)) + part)
    log = (output / "xvfb.log").open("wb")
    try:
        process = subprocess.Popen([executable, display, "-screen", "0", "1800x1200x24",
                                    "-nolisten", "tcp", "-noreset", "-auth", str(auth)],
                                   env=env, stdout=log, stderr=subprocess.STDOUT)
    finally:
        log.close()
    try:
        deadline = time.monotonic() + 8
        while not Path(f"/tmp/.X11-unix/X{number}").exists():
            if process.poll() is not None or time.monotonic() >= deadline:
                raise ProofUnavailable("scene-readback: private Xvfb did not become ready; see xvfb.log")
            time.sleep(0.05)
        marker = {"token": env["INKLOW_SMOKE_TOKEN"], "uid": os.getuid(),
                  "display": display, "pid": process.pid, "auth": str(auth)}
        (root / "x11.json").write_text(json.dumps(marker))
        env.update(DISPLAY=display, XAUTHORITY=str(auth), INKLOW_SMOKE_SCENE="x11-windowed")
        return process
    except BaseException:
        process.terminate()
        process.wait(timeout=3)
        raise


def prepare_identities(root, output, fixture, env):
    """Grant only our copied role executables access to their required globals."""
    source = Path(sys.executable).resolve(strict=True)
    applications = Path(env["XDG_DATA_HOME"]) / "applications"
    applications.mkdir(mode=0o700)
    identities = root / "executables"
    identities.mkdir(mode=0o700)
    # A relocated Python must still load the real interpreter's stdlib and the
    # invoking venv's packages. Never authorize that shared interpreter itself.
    env["PYTHONHOME"] = sys.base_prefix
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(
        str(Path(path).resolve()) for path in sys.path if path))
    records = {}
    for role, interface in (("im", "org_kde_plasma_window_management"),
                            ("target", "org_kde_kwin_fake_input")):
        executable = identities / ("inklow-smoke-" + role)
        shutil.copyfile(source, executable, follow_symlinks=True)
        executable.chmod(0o700)
        executable = executable.resolve(strict=True)
        if executable.samefile(source) or executable.is_symlink():
            raise RuntimeError("Fixture identity must be a distinct copied executable")
        # Exec identifies /proc/PID/exe, not a shell wrapper or Python script.
        quoted = '"' + str(executable).replace("\\", "\\\\").replace(
            '"', '\\"').replace("`", "\\`").replace("$", "\\$") + '"'
        desktop = applications / ("org.inklow.WaylandSmoke." + role + ".desktop")
        desktop.write_text("[Desktop Entry]\nType=Application\n"
                           "Name=Inklow isolated Wayland smoke " + role + "\n"
                           "NoDisplay=true\nExec=" + quoted + "\n"
                           "X-KDE-Wayland-Interfaces=" + interface + "\n")
        desktop.chmod(0o600)
        launcher = root / (role + ".sh")
        launcher.write_text("#!/bin/sh\nexec " + shlex.join(
            [str(executable), str(fixture), role]) + "\n")
        launcher.chmod(0o700)
        records[role] = {"executable": str(executable), "desktop": str(desktop),
                         "requested_interfaces": [interface],
                         "desktop_contents": desktop.read_text()}
    records["python"] = {"source_executable": str(source), "home": env["PYTHONHOME"],
                         "import_paths": env["PYTHONPATH"].split(os.pathsep)}
    (root / "identities.json").write_text(json.dumps(records))
    (output / "identities.json").write_text(json.dumps(records, indent=2) + "\n")
    # Minimal guests have no desktop menu. KService derives its application
    # registry from one; without it even correctly declared identities vanish.
    # Keep both menu discovery and cache construction inside this private home.
    from xml.sax.saxutils import escape
    menus = Path(env["XDG_CONFIG_HOME"]) / "menus"
    menus.mkdir(mode=0o700)
    env["XDG_MENU_PREFIX"] = "inklow-smoke-"
    (menus / "inklow-smoke-applications.menu").write_text(
        "<Menu><Name>Applications</Name><AppDir>" + escape(str(applications)) +
        "</AppDir><Include><All/></Include></Menu>\n")
    cache_builder = shutil.which("kbuildsycoca6") or shutil.which("kbuildsycoca5")
    if cache_builder:
        with (output / "desktop-cache.log").open("wb") as log:
            result = subprocess.run([cache_builder, "--noincremental"], env=env,
                                    stdout=log, stderr=subprocess.STDOUT, timeout=20)
        if result.returncode:
            raise ProofUnavailable("private KService identity cache failed; see desktop-cache.log")


def session() -> int:
    root, output = isolation()
    fixture = Path(__file__).resolve()
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)  # KWin also chooses its own QPA for --help.
    # Installed flags, not assumed distro/version flags. This is executed only
    # by the parent-authorized disposable smoke, never during implementation.
    help_result = subprocess.run([env["INKLOW_SMOKE_KWIN"], "--help"], env=env,
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=10)
    help_text = help_result.stdout.decode(errors="replace")
    (output / "kwin-help.txt").write_text(help_text)
    process = xvfb = None
    try:
        required = ("--socket", "--inputmethod", "--exit-with-session", "--width", "--height", "--scale")
        missing = [flag for flag in required if flag not in help_text]
        if missing:
            raise ProofUnavailable("owned-KWin-flags: missing " + ", ".join(missing))
        prepare_identities(root, output, fixture, env)
        backend = env["INKLOW_SMOKE_BACKEND"]
        if backend != "virtual" and "--x11-display" in help_text and env.get("INKLOW_SMOKE_XVFB"):
            try:
                xvfb = start_xvfb(root, output, env)
                flags = ["--x11-display", env["DISPLAY"]]
            except ProofUnavailable:
                if backend != "auto" or "--virtual" not in help_text:
                    raise
                flags = ["--virtual"]
                env["INKLOW_SMOKE_SCENE"] = "unavailable-Xvfb-see-xvfb.log"
        elif backend == "x11":
            raise ProofUnavailable("scene-readback: Xvfb or installed KWin --x11-display unavailable")
        elif "--virtual" in help_text:
            flags = ["--virtual"]
            env["INKLOW_SMOKE_SCENE"] = "unavailable-virtual-backend"
        else:
            raise ProofUnavailable("owned-KWin-backend: no isolated X11 or virtual backend")
        command = [env["INKLOW_SMOKE_KWIN"], *flags, "--width", "1280", "--height", "800",
                   "--scale", str(int(env["INKLOW_SMOKE_SCALE"]) / 100),
                   "--socket", SOCKET]
        command.extend(flag for flag in ("--no-lockscreen", "--no-global-shortcuts", "--no-kactivities")
                       if flag in help_text)
        command.extend(["--inputmethod", str(root / "im.sh"),
                        "--exit-with-session", str(root / "target.sh")])
        (output / "kwin-command.json").write_text(json.dumps(command, indent=2) + "\n")
        # Clients retain Wayland QPA in their own entrypoints. KWin chooses its
        # windowed backend explicitly; no host Wayland/X11 socket is inherited.
        with (output / "kwin.log").open("wb") as log:
            process = subprocess.Popen(command, env=env, stdout=log, stderr=subprocess.STDOUT)
            return process.wait(timeout=int(env["INKLOW_SMOKE_TIMEOUT"]) - 5)
    except ProofUnavailable as error:
        (output / "session-report.json").write_text(json.dumps(
            {"result": "incomplete", "limits": [str(error)],
             "unresolved_primitives": [str(error)]}, indent=2) + "\n")
        return 2
    finally:
        for child in (process, xvfb):
            if child is not None and child.poll() is None:
                child.terminate()
                try:
                    child.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    child.kill()
                    child.wait(timeout=2)


def capture_scene() -> int:
    """Read the owned KWin X11 output window, never a Qt target/widget image."""
    root, output = isolation()
    owned_x11(root)
    if os.environ.get("QT_QPA_PLATFORM") != "xcb":
        raise RuntimeError("Scene readback requires our private X11 QPA")
    label = os.environ["INKLOW_SMOKE_CAPTURE"]
    if not re.fullmatch(r"[a-z0-9-]+", label):
        raise RuntimeError("Invalid scene artifact label")
    from PySide6.QtGui import QGuiApplication
    app = QGuiApplication(["inklow-owned-scene-readback"])
    library = ctypes.util.find_library("X11")
    if not library:
        raise ProofUnavailable("scene-readback: libX11 unavailable")
    x11 = ctypes.CDLL(library)
    pointer = ctypes.c_void_p
    xid = ctypes.c_ulong
    x11.XOpenDisplay.argtypes = [ctypes.c_char_p]
    x11.XOpenDisplay.restype = pointer
    x11.XDefaultRootWindow.argtypes = [pointer]
    x11.XDefaultRootWindow.restype = xid
    x11.XQueryTree.argtypes = [pointer, xid, ctypes.POINTER(xid), ctypes.POINTER(xid),
                              ctypes.POINTER(ctypes.POINTER(xid)), ctypes.POINTER(ctypes.c_uint)]
    x11.XGetGeometry.argtypes = [pointer, xid, ctypes.POINTER(xid), ctypes.POINTER(ctypes.c_int),
                                ctypes.POINTER(ctypes.c_int), ctypes.POINTER(ctypes.c_uint),
                                ctypes.POINTER(ctypes.c_uint), ctypes.POINTER(ctypes.c_uint),
                                ctypes.POINTER(ctypes.c_uint)]
    x11.XFree.argtypes = [pointer]
    x11.XCloseDisplay.argtypes = [pointer]
    connection = x11.XOpenDisplay(os.environ["DISPLAY"].encode())
    if not connection:
        raise ProofUnavailable("scene-readback: cannot authenticate to owned Xvfb")
    try:
        root_id = x11.XDefaultRootWindow(connection)
        returned_root, parent = xid(), xid()
        children = ctypes.POINTER(xid)()
        count = ctypes.c_uint()
        if not x11.XQueryTree(connection, root_id, ctypes.byref(returned_root),
                              ctypes.byref(parent), ctypes.byref(children), ctypes.byref(count)):
            raise ProofUnavailable("scene-readback: private root tree unavailable")
        candidates = []
        try:
            for index in range(count.value):
                child = children[index]
                returned = xid()
                x, y = ctypes.c_int(), ctypes.c_int()
                width, height, border, depth = (ctypes.c_uint() for _ in range(4))
                if x11.XGetGeometry(connection, child, ctypes.byref(returned), ctypes.byref(x),
                                    ctypes.byref(y), ctypes.byref(width), ctypes.byref(height),
                                    ctypes.byref(border), ctypes.byref(depth)):
                    if width.value >= 800 and height.value >= 500:
                        candidates.append({"xid": int(child), "x": x.value, "y": y.value,
                                           "width": width.value, "height": height.value})
        finally:
            if children:
                x11.XFree(children)
        if len(candidates) != 1:
            raise ProofUnavailable("scene-readback: KWin output X11 window is not uniquely identifiable: "
                                   + json.dumps(candidates))
        screen = app.primaryScreen()
        if screen is None:
            raise ProofUnavailable("scene-readback: private Xvfb QScreen unavailable")
        image = screen.grabWindow(candidates[0]["xid"]).toImage()
        if image.isNull():
            raise ProofUnavailable("scene-readback: X11 QScreen.grabWindow returned no framebuffer")
        path = output / ("scene-" + label + ".png")
        if not image.save(str(path)):
            raise RuntimeError("Could not save owned scene framebuffer")
        receipt = {"kind": "actual-compositor-scene", "source": "QScreen.grabWindow(KWin-X11-output-XID)",
                   "backend": "owned-Xvfb-X11-windowed-KWin", "window": candidates[0],
                   "width": image.width(), "height": image.height(), "file": path.name,
                   "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        (output / ("scene-" + label + ".json")).write_text(json.dumps(receipt, indent=2) + "\n")
        return 0
    finally:
        x11.XCloseDisplay(connection)


class WaylandRegistry:
    """Real public-socket registry observation, without binding privileged globals."""

    def __init__(self, role):
        root, output = isolation(display=True)
        identity = json.loads((root / "identities.json").read_text())[role]
        actual = str(Path("/proc/self/exe").resolve(strict=True))
        if actual != identity["executable"]:
            raise RuntimeError("Fixture process does not match its own desktop executable")
        self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.socket.settimeout(2)
        self.socket.connect(str(root / "runtime" / SOCKET))
        self.next_id = 3
        self.globals = {}
        try:
            self.send(1, 1, struct.pack("=I", 2))  # wl_display.get_registry
            self.sync()
            self.evidence = {"role": role, "pid": os.getpid(), "executable": actual,
                             "socket": SOCKET, "connection": "owned-public-socket",
                             "requested_interfaces": identity["requested_interfaces"],
                             "globals": {name: {"name": value[0], "version": value[1]}
                                         for name, value in sorted(self.globals.items())}}
            (output / (role + "-registry.json")).write_text(
                json.dumps(self.evidence, indent=2) + "\n")
        except BaseException:
            self.close()
            raise

    def allocate(self):
        identifier = self.next_id
        self.next_id += 1
        return identifier

    @staticmethod
    def string(text):
        data = text.encode() + b"\0"
        return struct.pack("=I", len(data)) + data + b"\0" * (-len(data) % 4)

    def send(self, identifier, opcode, payload=b""):
        self.socket.sendall(struct.pack("=II", identifier, ((8 + len(payload)) << 16) | opcode) + payload)

    def receive(self, count):
        result = bytearray()
        while len(result) < count:
            data = self.socket.recv(count - len(result))
            if not data:
                raise ProofUnavailable("Owned Wayland fixture connection closed")
            result.extend(data)
        return bytes(result)

    def sync(self):
        callback = self.allocate()
        self.send(1, 0, struct.pack("=I", callback))
        deadline = time.monotonic() + 2
        while time.monotonic() < deadline:
            identifier, word = struct.unpack("=II", self.receive(8))
            size, opcode = word >> 16, word & 0xffff
            if size < 8 or size > 65535 or size % 4:
                raise RuntimeError("Invalid Wayland event size")
            payload = self.receive(size - 8)
            if identifier == 1 and opcode == 0:
                raise ProofUnavailable("Owned compositor returned a Wayland protocol error")
            if identifier == 2 and opcode == 0:
                name, length = struct.unpack_from("=II", payload)
                interface = payload[8:8 + length - 1].decode()
                version = struct.unpack_from("=I", payload, 8 + ((length + 3) & ~3))[0]
                self.globals[interface] = name, version
            if identifier == callback and opcode == 0:
                return
        raise ProofUnavailable("Owned Wayland sync deadline expired")

    def close(self):
        self.socket.close()


class FakeInput(WaylandRegistry):
    """Send real key requests; actual target delivery remains a separate gate."""

    def __init__(self):
        super().__init__("target")
        try:
            advertised = self.globals.get("org_kde_kwin_fake_input")
            if advertised is None or advertised[1] < 4:
                raise ProofUnavailable("Owned KWin did not advertise fake-input keyboard v4; see target-registry.json")
            self.proxy = self.allocate()
            self.send(2, 0, struct.pack("=I", advertised[0]) + self.string("org_kde_kwin_fake_input")
                      + struct.pack("=II", 4, self.proxy))
            self.send(self.proxy, 0, self.string("Inklow isolated Wayland smoke")
                      + self.string("Synthetic input into our owned virtual compositor only"))
            self.sync()
        except BaseException:
            self.close()
            raise

    def key(self, code, pressed):
        self.send(self.proxy, 10, struct.pack("=II", code, int(pressed)))

    def chord(self, code, modifiers=()):
        for modifier in modifiers:
            self.key(modifier, True)
        self.key(code, True)
        self.key(code, False)
        for modifier in reversed(modifiers):
            self.key(modifier, False)
        self.sync()



def im() -> int:
    _, output = isolation(display=True)
    registry = WaylandRegistry("im")
    registry.close()
    os.environ["QT_QPA_PLATFORM"] = "wayland"
    # Importing Qt classes is fine; constructing QApplication before production
    # prepare_input_method is not. Record this ordering inside the observer.
    from PySide6.QtCore import ClassInfo, QObject, QTimer, Slot
    from PySide6.QtDBus import QDBusConnection
    from PySide6.QtGui import QImage
    from PySide6.QtWidgets import QApplication
    from inklow import qt_ui
    from inklow.__main__ import main as production_main
    from inklow.runtime import ShutdownPending

    inherited_fd = os.environ.get("WAYLAND_SOCKET")
    if inherited_fd is None or QApplication.instance() is not None:
        raise RuntimeError("KWin did not supply a private fd before QApplication")
    fd_number = int(inherited_fd)
    fd_socket = stat.S_ISSOCK(os.fstat(fd_number).st_mode)
    original_init, original_start = qt_ui.MenuAgent.__init__, qt_ui.MenuAgent.start

    @ClassInfo(**{"D-Bus Interface": INTERFACE})
    class Observer(QObject):
        def __init__(self, agent):
            super().__init__(agent.app)
            self.agent = agent
            self.saved = {}
            self.calls = []
            self.frames = []
            self.decisions = []
            self.presentations = []
            self.ghost_rectangle = None
            self.ghost_scale = 0
            self.worker = None
            self.entry = {"private_fd_received": fd_socket,
                          "private_fd_environment_removed": "WAYLAND_SOCKET" not in os.environ,
                          "qt_exists_after_preparation": QApplication.instance() is not None,
                          "bridge": type(agent.controller.bridge).__name__}
            bridge = agent.controller.bridge
            original_call = bridge._native_call

            def observe_call(name, *arguments):
                # Copy the actual production frame before it goes out of scope;
                # never generate an imitation prediction or alter the result.
                image = None
                frame_scene = frame_id = None
                if name == "present":
                    _, address, width, height, stride, scale = arguments
                elif name == "ghost_present":
                    _, frame_scene, frame_id, address, width, height, stride = arguments
                    scale = self.ghost_scale / 120
                if name in ("present", "ghost_present"):
                    data = ctypes.string_at(address, stride * height)
                    image = QImage(data, width, height, stride,
                                   QImage.Format.Format_ARGB32_Premultiplied).copy()
                    if scale > 0:
                        image.setDevicePixelRatio(scale)
                result = original_call(name, *arguments)
                if name == "ghost_scale" and int(result) > 0:
                    self.ghost_scale = int(result)
                if name == "ghost_prepare":
                    _, pid, scene_id, x, y, width, height = arguments
                    self.ghost_rectangle = {"pid": int(pid), "scene": int(scene_id),
                                            "rect": [x, y, width, height], "result": int(result)}
                if image is not None:
                    filename = "ghost-latest.png" if name == "ghost_present" else "panel-latest.png"
                    image.save(str(output / filename))
                    # Inspect original premultiplied ARGB pixels; translucent
                    # antialiasing is real renderer output, not synthetic paint.
                    opaque = grey = 0
                    if name == "ghost_present":
                        for y in range(image.height()):
                            for x in range(image.width()):
                                color = image.pixelColor(x, y)
                                if color.alpha() > 24:
                                    opaque += 1
                                    if max(color.red(), color.green(), color.blue()) - min(
                                            color.red(), color.green(), color.blue()) <= 8:
                                        grey += 1
                    self.frames.append({"result": int(result), "width": image.width(),
                                        "height": image.height(), "request": name, "file": filename,
                                        "scene": frame_scene, "frame": frame_id,
                                        "scale_120": self.ghost_scale, "nontransparent_pixels": opaque,
                                        "neutral_grey_pixels": grey, "rectangle": self.ghost_rectangle,
                                        "kind": "production-client-pixels-not-screen-capture"})
                    self.frames[:] = self.frames[-64:]
                if name == "commit":
                    self.calls.append({"anchor": f"{arguments[1]}:{arguments[2]}",
                                       "text": arguments[3].decode("utf-8"), "result": int(result)})
                return result

            bridge._native_call = observe_call
            original_ui = bridge._ui_callback
            self.presentation_count = 0

            def observe_ui(action, *arguments):
                if action == "presentation":
                    payload = arguments[0]
                    self.presentation_count += 1
                    self.presentations.append({"key": str(payload.get("key")), "sequence": self.presentation_count,
                                               "text": payload.get("text", ""),
                                               "mode": payload.get("mode"),
                                               "scene": bridge._ghost_scene,
                                               "frame": bridge._ghost_frame})
                    self.presentations[:] = self.presentations[-128:]
                if original_ui is not None:
                    return original_ui(action, *arguments)

            bridge._ui_callback = observe_ui
            original_invoke = agent.controller.invoke

            def observe_invoke(action):
                result = original_invoke(action)
                self.decisions.append({"action": action, "accepted": bool(result)})
                self.decisions[:] = self.decisions[-128:]
                return result

            agent.controller.invoke = observe_invoke

        def state(self):
            controller = self.agent.controller
            bridge = controller.bridge
            snapshot = bridge.capture()
            status = controller.status()
            process = controller._client._process or controller._client._terminating
            if process is not None:
                self.worker = process
            return {"entry": self.entry, "status": status,
                    "native_state": bridge._state(),
                    "composition_inactive": bool(bridge._native_call("composition_inactive", bridge._client)),
                    "snapshot": asdict(snapshot) if snapshot else None,
                    "suggestion": controller._suggestion.text,
                    "suggestion_snapshot": asdict(controller._suggestion.snapshot) if controller._suggestion.snapshot else None,
                    "panel": {"pending": bridge._pending_render,
                              "anchor": bridge._panel_snapshot.native_anchor if bridge._panel_snapshot else "",
                              "menu": bridge._menu is not None, "rows": bridge._menu,
                              "selected": bridge._menu_selected, "page": bridge._menu_page},
                    "ghost": {"text": bridge._ghost_text, "scene": bridge._ghost_scene,
                              "frame": bridge._ghost_frame, "pending": bridge._ghost_pending,
                              "presented": bridge._ghost_presented, "event": bridge._ghost_event,
                              "snapshot": asdict(bridge._ghost_snapshot) if bridge._ghost_snapshot else None,
                              "rectangle": self.ghost_rectangle, "scale_120": self.ghost_scale},
                    "presentations": self.presentations,
                    "presentation_count": self.presentation_count,
                    "frames": self.frames, "commits": self.calls, "decisions": self.decisions,
                    "pid": os.getpid()}

        @Slot(str, result=str)
        def request(self, payload):
            try:
                data = json.loads(payload)
                if data.pop("token", None) != os.environ["INKLOW_SMOKE_TOKEN"]:
                    raise RuntimeError("Invalid private fixture token")
                action = data["op"]
                result = None
                if action == "state":
                    result = self.state()
                elif action == "invoke":
                    result = self.agent.controller.invoke(data["action"])
                elif action == "save":
                    bridge = self.agent.controller.bridge
                    snapshot = bridge.capture()
                    if snapshot is None:
                        raise RuntimeError("No eligible real native snapshot to save")
                    manual = bool(data.get("manual", False))
                    text = REFUSED if manual else bridge._ghost_text
                    if not manual and (not text or not bridge._ghost_presented
                                       or bridge._ghost_snapshot is None
                                       or snapshot.key != bridge._ghost_snapshot.key):
                        raise RuntimeError("No exact presented inline frame to save")
                    self.saved[data["name"]] = (snapshot, text, manual)
                    result = asdict(snapshot)
                elif action == "insert_saved":
                    bridge = self.agent.controller.bridge
                    snapshot, text, manual = self.saved[data["name"]]
                    insert = bridge.insert if manual else bridge.insert_inline
                    result = insert(snapshot, text)
                elif action == "menu":
                    self.agent._native_action("menu")
                    result = self.agent._native_menu_active
                elif action == "menu_close":
                    self.agent._native_action("menu_closed")
                    result = not self.agent._native_menu_active
                elif action == "stop":
                    try:
                        self.agent.stop()
                    except ShutdownPending as error:
                        return json.dumps({"ok": True, "value": {
                            "pending": True, "reason": str(error)}})
                    process = self.worker
                    result = {"pending": False, "status": self.agent.controller.status(),
                              "worker_pid": process.pid if process else None,
                              "worker_returncode": process.poll() if process else None,
                              "native_client_closed": self.agent.controller.bridge._client is None,
                              "read_notifier_removed": self.agent.controller.bridge._read_notifier is None,
                              "notifiers_removed": all(value is None for name, value in
                                  vars(self.agent.controller.bridge).items()
                                  if name.endswith("_notifier"))}
                    (output / "im-stop.json").write_text(json.dumps(result, indent=2) + "\n")
                    QTimer.singleShot(150, self.agent.app.quit)
                else:
                    raise ValueError("Unknown fixture operation")
                return json.dumps({"ok": True, "value": result})
            except Exception as error:
                return json.dumps({"ok": False, "error": f"{type(error).__name__}: {error}"})

    def observed_init(agent, *args, **kwargs):
        original_init(agent, *args, **kwargs)
        observer = Observer(agent)
        bus = QDBusConnection.sessionBus()
        if not bus.registerService(IM_SERVICE) or not bus.registerObject(
                "/Smoke", observer, QDBusConnection.RegisterOption.ExportAllSlots):
            raise RuntimeError("Cannot register observer on private bus")
        agent._smoke_observer = observer

    def observed_start(agent, *args, **kwargs):
        original_start(agent, *args, **kwargs)
        if agent.settings is not None:
            agent.settings.hide()  # Only our isolated app's settings window.

    qt_ui.MenuAgent.__init__ = observed_init
    qt_ui.MenuAgent.start = observed_start
    return production_main(["--wayland-input-method", "--model-path", os.environ["INKLOW_SMOKE_MODEL"],
                            "--acceleration", "cpu", "--background", "--no-tray"])


def target() -> int:
    root, output = isolation(display=True)
    registry = WaylandRegistry("target")
    registry.close()
    os.environ["QT_QPA_PLATFORM"] = "wayland"
    from PySide6.QtCore import ClassInfo, QEvent, QEventLoop, QObject, Qt, QTimer, Slot
    from PySide6.QtDBus import QDBusConnection, QDBusMessage, QDBusPendingCallWatcher, QDBusVariant
    from PySide6.QtGui import QFont, QFontDatabase, QFontInfo, QImage
    from PySide6.QtWidgets import QApplication, QLabel, QLineEdit, QVBoxLayout, QWidget

    app = QApplication(["inklow-wayland-smoke-target"])
    app.setApplicationName("Inklow synthetic Wayland target")
    app.setDesktopFileName("org.inklow.WaylandSmoke.Target")
    app.setQuitOnLastWindowClosed(False)
    app.setCursorFlashTime(0)
    family = "DejaVu Sans Mono"
    if family not in QFontDatabase.families():
        raise ProofUnavailable("field-font: install fonts-dejavu-core in the disposable guest")
    host_font = QFont(family, 16)
    host_font.setStyleStrategy(QFont.StyleStrategy.NoFontMerging)
    app.setFont(host_font)
    report = {"result": "failed", "checks": [], "limits": [], "scenes": [],
              "unresolved_primitives": [], "scene_captures": [],
              "injection": "not-yet-proven", "target_pid": os.getpid(),
              "scale_percent": int(os.environ["INKLOW_SMOKE_SCALE"]),
              "fixture_keyboard": "private-kxkbrc: us(intl); all shortcut Ctrl+F12",
              "host_font": {"requested": family, "resolved": QFontInfo(host_font).family(),
                            "point_size": host_font.pointSizeF()},
              "image_semantics": "scene-*.png is private KWin output readback only; target/panel/ghost PNGs are client pixels"}
    bus = QDBusConnection.sessionBus()
    events = []
    commits = []
    compositions = []

    class Field(QLineEdit):
        def event(self, event):
            if event.type() in (QEvent.Type.KeyPress, QEvent.Type.KeyRelease):
                events.append({"field": self.objectName(), "type": "press" if event.type() == QEvent.Type.KeyPress else "release",
                               "key": int(event.key()), "modifiers": int(event.modifiers().value),
                               "text": event.text(), "scan": event.nativeScanCode(),
                               "autorepeat": event.isAutoRepeat()})
                # Keep test focus fixed while recording genuine compositor Tab
                # delivery. No QTest/sendEvent/direct shortcut calls are used.
                if event.key() in (Qt.Key.Key_Tab, Qt.Key.Key_Backtab):
                    return True
            return super().event(event)

        def inputMethodEvent(self, event):
            compositions.append({"field": self.objectName(), "preedit": event.preeditString(),
                                 "commit": event.commitString()})
            if event.commitString():
                commits.append({"field": self.objectName(), "text": event.commitString(),
                                "replace_start": event.replacementStart(),
                                "replace_length": event.replacementLength()})
            super().inputMethodEvent(event)

    window = QWidget(None, Qt.WindowType.Window | Qt.WindowType.FramelessWindowHint)
    window.setWindowTitle("Inklow synthetic Wayland target")
    window.resize(900, 300)
    layout = QVBoxLayout(window)
    layout.addWidget(QLabel("Disposable synthetic fields only — no personal applications"))
    fields = []
    for name in ("writing", "other"):
        field = Field()
        field.setObjectName(name)
        field.setAccessibleName("Synthetic " + name)
        layout.addWidget(field)
        fields.append(field)
    writing, other = fields
    window.show()
    window.activateWindow()
    writing.setFocus(Qt.FocusReason.OtherFocusReason)

    @ClassInfo(**{"D-Bus Interface": INTERFACE})
    class SceneSink(QObject):
        @Slot(str, result=str)
        def Scene(self, payload):
            value = json.loads(payload)
            report["scenes"].append(value)
            return "recorded"

    sink = SceneSink(app)
    if not bus.registerService(TARGET_SERVICE) or not bus.registerObject(
            "/Smoke", sink, QDBusConnection.RegisterOption.ExportAllSlots):
        raise RuntimeError("Cannot register target scene sink on private bus")

    def bus_call(service, path, interface, method, *arguments):
        message = QDBusMessage.createMethodCall(service, path, interface, method)
        message.setArguments(list(arguments))
        # The observer reads our AT-SPI endpoint while replying. BlockWithGui
        # can exclude those deliveries; explicitly dispatch all target events.
        watcher = QDBusPendingCallWatcher(bus.asyncCall(message, 1500), app)
        if not watcher.isFinished():
            loop = QEventLoop()
            watcher.finished.connect(loop.quit)
            loop.exec(QEventLoop.ProcessEventsFlag.AllEvents)
        reply = watcher.reply()
        watcher.deleteLater()
        if reply.type() == QDBusMessage.MessageType.ErrorMessage:
            raise RuntimeError(f"{service}.{method}: {reply.errorName()}: {reply.errorMessage()}")
        return reply.arguments()

    ipc = {"state": None, "polling": False, "command": False, "closing": False}
    watchers = set()

    def send_rpc(op, arguments, completed):
        payload = {"token": os.environ["INKLOW_SMOKE_TOKEN"], "op": op, **arguments}
        message = QDBusMessage.createMethodCall(IM_SERVICE, "/Smoke", INTERFACE, "request")
        message.setArguments([json.dumps(payload)])
        watcher = QDBusPendingCallWatcher(bus.asyncCall(message, 1500), app)
        watchers.add(watcher)

        def replied(call):
            watchers.discard(call)
            reply = call.reply()
            call.deleteLater()
            try:
                if reply.type() == QDBusMessage.MessageType.ErrorMessage:
                    raise RuntimeError(reply.errorName() + ": " + reply.errorMessage())
                data = json.loads(reply.arguments()[0])
                if not data["ok"]:
                    raise RuntimeError(data["error"])
            except Exception as error:
                completed(None, str(error))
            else:
                completed(data["value"], None)

        watcher.finished.connect(replied)

    def poll_state():
        if ipc["polling"] or ipc["command"] or ipc["closing"]:
            return
        ipc["polling"] = True

        def received(value, error):
            ipc["polling"] = False
            if error is None:
                ipc["state"] = value

        send_rpc("state", {}, received)

    poll_timer = QTimer(app)
    poll_timer.timeout.connect(poll_state)
    poll_timer.start(100)

    def command(op, arguments):
        ipc["command"] = True
        result = {}
        try:
            yield from wait_for(lambda: not ipc["polling"], "previous asynchronous state reply", 3)
            def received(value, error):
                result.update(done=True, value=value, error=error)
            send_rpc(op, arguments, received)
            yield from wait_for(lambda: result.get("done"), "asynchronous " + op + " reply", 3)
            if result["error"] is not None:
                raise RuntimeError(result["error"])
            return result["value"]
        finally:
            ipc["command"] = False

    def rpc(op, **arguments):
        # State predicates use actual asynchronous observations; commands yield
        # the target GUI thread so the agent can read its AT-SPI endpoint.
        return ipc["state"] if op == "state" else command(op, arguments)

    def state_or_none():
        return ipc["state"]

    def check(name, condition, **evidence):
        report["checks"].append({"name": name, "passed": bool(condition), **evidence})
        if not condition:
            raise AssertionError(name)

    def wait_for(predicate, label, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            value = predicate()
            if value:
                return value
            yield 0.1
        raise AssertionError("Deadline: " + label)

    def eligible(expected, field=writing):
        state = state_or_none()
        if (state and state["snapshot"] and state["snapshot"]["text"] == expected
                and state["snapshot"]["caret"] == len(expected) and field.hasFocus()):
            return state
        return None

    def prepare(text=PREFIX, field=writing):
        writing.setEchoMode(QLineEdit.EchoMode.Normal)
        writing.setInputMethodHints(Qt.InputMethodHint.ImhNone)
        field.setText(text[:-1])
        field.setCursorPosition(len(text) - 1)
        window.activateWindow()
        field.setFocus(Qt.FocusReason.OtherFocusReason)
        app.inputMethod().show()
        # Initial surrounding precedes content_type in KWin. Wait for the
        # safe type, then make a genuine widget edit; never weaken the guard.
        yield 0.4
        field.insert(text[-1:])
        app.inputMethod().update(Qt.InputMethodQuery.ImSurroundingText | Qt.InputMethodQuery.ImCursorPosition | Qt.InputMethodQuery.ImAnchorPosition)
        return (yield from wait_for(lambda: eligible(text, field), "matching safe native + AT-SPI context"))

    def offer(*, force=True):
        def inline_context():
            state = state_or_none()
            snapshot = state.get("snapshot") if state else None
            if (snapshot and snapshot.get("inline_render")
                    and snapshot.get("window_pid", 0) > 0
                    and snapshot.get("window_bounds") and snapshot.get("window_revision")
                    and state["composition_inactive"]):
                return state
            return None

        try:
            yield from wait_for(inline_context, "genuine inline-capable native/provider snapshot")
        except AssertionError as error:
            state = state_or_none()
            raise ProofUnavailable(
                "ordinary-inline: registry exists but verified active-window/font/geometry "
                "metadata did not become available; " + json.dumps({
                    "status": state.get("status") if state else None,
                    "snapshot": state.get("snapshot") if state else None,
                    "registry_evidence": "im-registry.json"})) from error

        if force:
            check("production force command accepts safe context", (yield from rpc("invoke", action="force")))

        def ready():
            state = rpc("state")
            if state["status"]["state"] == "error":
                raise AssertionError(state["status"]["message"])
            ghost = state["ghost"]
            if (state["suggestion"] and state["status"]["state"] == "ready"
                    and state["status"]["model_loaded"] and state["status"]["worker_pid"]
                    and ghost["text"] and state["suggestion"].startswith(ghost["text"])
                    and not ghost["pending"] and ghost["presented"]
                    and state["frames"] and any(frame["request"] == "ghost_present"
                        and frame["scene"] == ghost["scene"] and frame["frame"] == ghost["frame"]
                        and frame["result"] == 1 for frame in state["frames"])
                    and any(ack["text"] == ghost["text"] and ack["scene"] == ghost["scene"]
                            and ack["frame"] == ghost["frame"] for ack in state["presentations"])):
                return state
            return None
        try:
            return (yield from wait_for(ready, "real generation and wp_presentation-acknowledged inline frame", 100))
        except AssertionError as error:
            state = rpc("state")
            raise ProofUnavailable("ordinary-inline: no matching generated/painted/presented prefix; "
                                   + json.dumps({"status": state["status"], "ghost": state["ghost"],
                                                 "frames": state["frames"][-2:]})) from error

    def scene(label, im_pid):
        before = len(report["scenes"])
        script = root / ("scene-" + label + ".js")
        # The private socket may attribute an input-panel client to KWin's PID.
        # This entire compositor is owned; include its native input-method role.
        script.write_text("""var rows = [];
var owned = %s;
var windows = workspace.stackingOrder;
for (var i = 0; i < windows.length; ++i) {
    var w = windows[i];
    if (owned.indexOf(w.pid) < 0 && !w.inputMethod) continue;
    var g = w.frameGeometry;
    rows.push({pid:w.pid, inputMethod:w.inputMethod, hidden:w.hidden,
               opacity:w.opacity, minimized:w.minimized, active:w.active,
               geometry:[g.x,g.y,g.width,g.height]});
}
var outputs = [];
var screens = workspace.screens;
if (screens) for (var j = 0; j < screens.length; ++j) {
    var o = screens[j], r = o.geometry;
    outputs.push({name:o.name, scale:o.scale, geometry:[r.x,r.y,r.width,r.height]});
}
callDBus(%s, '/Smoke', %s, 'Scene', JSON.stringify({label:%s, rows:rows, outputs:outputs}));
""" % (json.dumps([os.getpid(), im_pid]), json.dumps(TARGET_SERVICE),
       json.dumps(INTERFACE), json.dumps(label)))
        loaded = bus_call("org.kde.KWin", "/Scripting", "org.kde.kwin.Scripting", "loadScript",
                          str(script), "inklow-smoke-" + label)
        identifier = int(loaded[0])
        if identifier < 0:
            raise ProofUnavailable("Owned KWin scripting API rejected scene observer")
        path = "/Scripting/Script" + str(identifier)
        try:
            bus_call("org.kde.KWin", path, "org.kde.kwin.Script", "run")
            value = yield from wait_for(lambda: report["scenes"][-1] if len(report["scenes"]) > before else None,
                                        "owned compositor scene callback", 5)
            if not value.get("outputs"):
                # KWin 5 lacks workspace.screens. Its own supportInformation
                # exposes actual output geometry/scale, not Qt DPR/env.
                support = str(bus_call("org.kde.KWin", "/KWin", "org.kde.KWin", "supportInformation")[0])
                matches = re.finditer(
                    r"(?m)^Name: ([^\n]+)\nEnabled: 1\nGeometry: (-?\d+),(-?\d+),(\d+)x(\d+)\nScale: ([\d.]+)",
                    support)
                value["outputs"] = [{"name": match[1],
                    "geometry": [int(match[index]) for index in (2, 3, 4, 5)],
                    "scale": float(match[6]), "source": "KWin.supportInformation"}
                    for match in matches]
            return value
        finally:
            bus_call("org.kde.KWin", path, "org.kde.kwin.Script", "stop")

    def capture(label):
        if os.environ.get("INKLOW_SMOKE_SCENE") != "x11-windowed":
            gap = "scene-readback: owned X11-windowed KWin inside Xvfb unavailable"
            if gap not in report["limits"]:
                report["limits"].append(gap)
                report["unresolved_primitives"].append(gap)
            return None
        env = dict(os.environ)
        env.update(QT_QPA_PLATFORM="xcb", QT_IM_MODULE="", INKLOW_SMOKE_CAPTURE=label)
        env.pop("WAYLAND_DISPLAY", None)
        env.pop("WAYLAND_SOCKET", None)
        try:
            process = subprocess.run([sys.executable, str(Path(__file__).resolve()), "capture"],
                                     env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=12)
        except subprocess.TimeoutExpired:
            gap = "scene-readback: private Qt/xcb capture exceeded its finite 12s deadline"
            report["limits"].append(gap)
            report["unresolved_primitives"].append(gap)
            return None
        (output / ("scene-" + label + ".log")).write_bytes(process.stdout)
        receipt_path = output / ("scene-" + label + ".json")
        if process.returncode != 0 or not receipt_path.is_file():
            gap = "scene-readback: private capture failed; see scene-" + label + ".log"
            report["limits"].append(gap)
            report["unresolved_primitives"].append(gap)
            return None
        receipt = json.loads(receipt_path.read_text())
        report["scene_captures"].append(receipt)
        return receipt

    def literal_tab(label):
        if injector is None:
            return
        start = len(events)
        before = writing.text(), writing.selectionStart(), writing.selectedText(), writing.cursorPosition()
        injector.chord(15)
        yield 0.3
        delivered = [event for event in events[start:] if event["key"] in
                     (int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab))]
        check(label, [event["type"] for event in delivered] == ["press", "release"]
              and before == (writing.text(), writing.selectionStart(), writing.selectedText(), writing.cursorPosition()),
              events=delivered, host_before=before, host_after=writing.text())

    def output_transition():
        executable = shutil.which("kscreen-doctor")
        if executable is None:
            gap = "stale-output: guest kscreen-doctor unavailable for live compositor scale change"
            report["limits"].append(gap)
            report["unresolved_primitives"].append(gap)
            return
        listing = subprocess.run([executable, "-j"], stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                 timeout=8)
        (output / "kscreen-before.json").write_bytes(listing.stdout)
        try:
            outputs = [value for value in json.loads(listing.stdout)["outputs"] if value.get("enabled")]
            if listing.returncode != 0 or len(outputs) != 1:
                raise ValueError("Expected exactly one enabled owned output")
            identifier = int(outputs[0]["id"])
            original_scale = float(outputs[0]["scale"])
        except (ValueError, KeyError, TypeError) as error:
            gap = "stale-output: no unique output/scale from private kscreen-doctor -j: " + str(error)
            report["limits"].append(gap)
            report["unresolved_primitives"].append(gap)
            return
        check("KScreen reports the independently configured initial scale",
              abs(original_scale - report["scale_percent"] / 100) < 0.001,
              output=outputs[0])
        old = yield from offer()
        old_scene, ack_start = old["ghost"]["scene"], old["presentation_count"]
        different = 1.25 if original_scale == 1 else 1
        changed = subprocess.run([executable, f"output.{identifier}.scale.{different:g}"],
                                 stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
        (output / "kscreen-change.log").write_bytes(changed.stdout)
        if changed.returncode != 0:
            gap = "stale-output: owned compositor refused live scale change; see kscreen-change.log"
            report["limits"].append(gap)
            report["unresolved_primitives"].append(gap)
            return
        try:
            yield from wait_for(lambda: rpc("state")["ghost"]["scene"] != old_scene,
                                "compositor output change invalidates old ghost scene")
            refreshed = yield from offer()
            check("output reconfiguration revokes stale frame before fresh scaled presentation",
                  refreshed["ghost"]["scene"] != old_scene
                  and refreshed["ghost"]["scale_120"] == round(different * 120)
                  and any(not ack["text"] for ack in refreshed["presentations"] if ack["sequence"] > ack_start),
                  previous=old["ghost"], current=refreshed["ghost"],
                  acknowledgements=[ack for ack in refreshed["presentations"] if ack["sequence"] > ack_start])
            metadata = yield from scene("output-changed", refreshed["pid"])
            check("changed compositor output independently confirms live scale",
                  len(metadata["outputs"]) == 1
                  and abs(metadata["outputs"][0]["scale"] - different) < 0.001,
                  compositor=metadata)
            capture("output-changed")
        finally:
            restored = subprocess.run([executable, f"output.{identifier}.scale.{original_scale:g}"],
                                      stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=8)
            (output / "kscreen-restore.log").write_bytes(restored.stdout)
            check("owned compositor output scale restores", restored.returncode == 0)
        restored_offer = yield from offer()
        check("restored output requires original actual preferred fractional scale",
              restored_offer["ghost"]["scale_120"] == round(original_scale * 120),
              ghost=restored_offer["ghost"])

    def desktop_transition():
        yield from prepare(PREFIX)
        before = yield from offer()
        yield from rpc("save", name="desktop")
        start = len(commits)

        def showing():
            values = bus_call("org.kde.KWin", "/KWin", "org.freedesktop.DBus.Properties",
                              "Get", "org.kde.KWin", "showingDesktop")
            if len(values) != 1:
                raise AssertionError("Owned KWin returned invalid desktop state")
            value = values[0].variant() if isinstance(values[0], QDBusVariant) else values[0]
            if type(value) is not bool:
                raise AssertionError("Owned KWin desktop state is not a boolean")
            return value

        check("owned compositor starts with desktop hidden", showing() is False)
        # Only this disposable KWin's private bus is reachable in the fixture.
        bus_call("org.kde.KWin", "/KWin", "org.kde.KWin", "showDesktop", True)
        try:
            yield from wait_for(showing, "owned compositor enters show-desktop")
            yield from wait_for(lambda: not rpc("state")["ghost"]["presented"],
                                "show-desktop revokes the presented native frame")
            refused = yield from rpc("insert_saved", name="desktop")
            accepted = yield from rpc("invoke", action="all")
            check("show-desktop refuses saved and shortcut acceptance without host edits",
                  refused is False and accepted is False and not commits[start:]
                  and writing.text() == PREFIX,
                  before=before["ghost"], after=rpc("state")["ghost"])
        finally:
            bus_call("org.kde.KWin", "/KWin", "org.kde.KWin", "showDesktop", False)
            yield from wait_for(lambda: showing() is False, "owned compositor exits show-desktop")
        yield from prepare(PREFIX)
        restored = yield from offer()
        check("desktop restore requires a new acknowledged scene",
              restored["ghost"]["scene"] != before["ghost"]["scene"]
              and writing.text() == PREFIX,
              before=before["ghost"], after=restored["ghost"])

    def stop_agent():
        deadline = time.monotonic() + 8
        attempts = []
        while True:
            stopped = yield from rpc("stop")
            attempts.append(stopped)
            if not stopped.get("pending"):
                report["stop_attempts"] = attempts
                return stopped
            if time.monotonic() >= deadline:
                report["stop_attempts"] = attempts
                raise AssertionError("Owned agent shutdown remained pending after 8 seconds")
            yield 0.1

    injector = None

    def scenarios():
        nonlocal injector
        # Observe each role's actual filtered registry, not the compositor's
        # version string or a generic interpreter's advertised privileges.
        im_registry = output / "im-registry.json"
        try:
            yield from wait_for(im_registry.is_file, "input-method registry evidence", 25)
        except AssertionError as error:
            raise ProofUnavailable("No IM registry observation; inspect kwin.log and identities.json") from error
        registries = {role: json.loads((output / (role + "-registry.json")).read_text())
                      for role in ("im", "target")}
        report["registries"] = registries
        required = {"wl_compositor": 3, "wl_shm": 1, "wl_output": 2,
                    "zwlr_layer_shell_v1": 3, "org_kde_plasma_window_management": 13,
                    "zxdg_output_manager_v1": 1, "wp_viewporter": 1,
                    "wp_fractional_scale_manager_v1": 1, "wp_presentation": 1}
        missing = [name + ">=" + str(version) for name, version in required.items()
                   if registries["im"]["globals"].get(name, {}).get("version", 0) < version]
        if missing:
            raise ProofUnavailable("ordinary-inline registry primitives missing: " + ", ".join(missing)
                                   + "; see im-registry.json")
        initial = yield from wait_for(state_or_none, "KWin-launched production MenuAgent observer", 25)
        check("R6 real entrypoint prepares private fd before Qt", all(initial["entry"].get(key) for key in
              ("private_fd_received", "private_fd_environment_removed", "qt_exists_after_preparation"))
              and initial["entry"]["bridge"] == "WaylandBridge", entry=initial["entry"])
        report["initial_status"] = initial["status"]
        support = str(bus_call("org.kde.KWin", "/KWin", "org.kde.KWin", "supportInformation")[0])
        renderer_lines = [line for line in support.splitlines() if re.search(
            r"renderer|compositing type|kwin version|qt version", line, re.IGNORECASE)]
        check("R6 owned compositor uses software rendering",
              bool(re.search(r"llvmpipe|softpipe|software rasterizer|QPainter", support, re.IGNORECASE)),
              renderer=renderer_lines)
        keyboard_limit = None
        try:
            injector = FakeInput()
            writing.setText("")
            writing.setFocus()
            yield 0.3
            start = len(events)
            injector.chord(30)  # Linux KEY_A, not an XKB/Qt synthetic invocation.
            try:
                yield from wait_for(lambda: writing.text() == "a", "fixture key reaches native Qt target", 3)
            except AssertionError as error:
                raise ProofUnavailable("fake-input advertised but native KEY_A delivery was not established") from error
            delivered = events[start:]
            check("R3 compositor injection calibration", len([e for e in delivered if e["type"] == "press" and e["text"] == "a"]) == 1,
                  text=writing.text(), events=delivered)
            report["injection"] = "real-org_kde_kwin_fake_input-to-owned-KWin-to-native-Qt"
        except (ProofUnavailable, OSError) as error:
            keyboard_limit = str(error)
            report["limits"].append(keyboard_limit)
            report["unresolved_primitives"].append("physical-shortcut/undo/dead-compose: " + keyboard_limit)
            report["injection"] = "unproven-no-QTest-fallback"
            if injector is not None:
                injector.close()
            injector = None

        before = yield from prepare()
        check("bridge captures exact real Qt text without selection", before["snapshot"]["text"] == PREFIX
              and writing.selectionStart() == -1, snapshot=before["snapshot"])
        yield from rpc("invoke", action="dismiss")
        yield 0.2
        baseline = capture("baseline")
        host_before = (writing.text(), writing.cursorPosition(), writing.selectionStart(), writing.selectedText())
        host_undo_before = writing.isUndoAvailable()
        host_commit_before = len(commits)
        if injector is not None:
            yield 3.2  # Let the default field-scoped Escape pause expire.
            # Actual physical edit returns to the identical baseline string.
            # Generation must be automatic from normal context/debounce, not a
            # direct controller force call disguised as ordinary prediction.
            injector.chord(14)  # KEY_BACKSPACE removes final 'o'.
            yield from wait_for(lambda: eligible(PREFIX[:-1]), "ordinary physical backspace native context")
            yield 0.7  # Let normal debounce observe the intermediate context.
            injector.chord(24)  # KEY_O restores exact original baseline.
            yield from wait_for(lambda: eligible(PREFIX), "ordinary physical edit context")
            offered = yield from offer(force=False)
            report["generation_trigger"] = "physical-backspace-and-o/normal-debounce"
        else:
            offered = yield from offer()
            report["generation_trigger"] = "explicit-production-force-not-physical-edit-proof"
        report["provider_offer"] = offered
        ghost = offered["ghost"]
        frame = next(frame for frame in reversed(offered["frames"]) if frame["request"] == "ghost_present"
                     and frame["scene"] == ghost["scene"] and frame["frame"] == ghost["frame"])
        check("inactive ghost leaves host text/caret/selection/undo untouched",
              host_before == (writing.text(), writing.cursorPosition(), writing.selectionStart(), writing.selectedText())
              and len(commits) == host_commit_before and writing.isUndoAvailable() == host_undo_before,
              host_before=host_before, ghost=ghost)
        check("real renderer paints neutral grey transparent glyphs",
              frame["nontransparent_pixels"] > 0
              and frame["neutral_grey_pixels"] >= frame["nontransparent_pixels"] * 0.95
              and frame["nontransparent_pixels"] < frame["width"] * frame["height"] * 0.8,
              frame=frame)
        actual_scale = ghost["scale_120"]
        expected_scale = report["scale_percent"] * 120 // 100
        check("native compositor preferred fractional scale matches requested output",
              actual_scale == expected_scale, actual_120=actual_scale, expected_120=expected_scale)
        rectangle = ghost["rectangle"]["rect"]
        check("fractional SHM buffer has exact ceil logical size",
              frame["width"] == (rectangle[2] * actual_scale + 119) // 120
              and frame["height"] == (rectangle[3] * actual_scale + 119) // 120,
              rectangle=rectangle, frame=frame)
        render = offered["snapshot"]["inline_render"]
        caret = offered["snapshot"]["caret_rect"]
        check("ordinary ghost uses actual provider font and same caret line",
              render is not None and render["font_name"] and render["source"]
              and render["ascent"] > 0 and render["descent"] >= 0
              and rectangle[1] <= caret[1] + caret[3] and rectangle[1] + rectangle[3] >= caret[1],
              provider_render=render, provider_caret=caret, geometry=rectangle)
        (output / "ghost-offer.png").write_bytes((output / "ghost-latest.png").read_bytes())
        window.grab().save(str(output / "target-before.png"))
        mapped = None
        try:
            mapped = yield from scene("offer", offered["pid"])
            outputs = mapped.get("outputs", [])
            if len(outputs) != 1 or outputs[0].get("scale") is None:
                raise ProofUnavailable("output-scale: KWin scripting did not expose one output scale")
            check("KWin independently reports actual output fractional scale",
                  abs(outputs[0]["scale"] - report["scale_percent"] / 100) < 0.001,
                  compositor_output=outputs[0])
            targets = [row for row in mapped["rows"] if row["pid"] == os.getpid() and row.get("active")]
            check("ghost does not steal native target focus", writing.hasFocus() and bool(targets), scene=mapped)
        except (RuntimeError, ProofUnavailable) as error:
            gap = "compositor-output-metadata: " + str(error)
            report["limits"].append(gap)
            report["unresolved_primitives"].append(gap)
        visible_scene = capture("offer")
        if baseline and visible_scene and mapped and len(mapped.get("outputs", [])) == 1:
            a = QImage(str(output / baseline["file"]))
            b = QImage(str(output / visible_scene["file"]))
            output_geometry = mapped["outputs"][0]["geometry"]
            ratio_x = b.width() / output_geometry[2]
            ratio_y = b.height() / output_geometry[3]
            x0 = max(0, int((rectangle[0] - output_geometry[0]) * ratio_x))
            y0 = max(0, int((rectangle[1] - output_geometry[1]) * ratio_y))
            x1 = min(b.width(), int((rectangle[0] + rectangle[2] - output_geometry[0]) * ratio_x + 1))
            y1 = min(b.height(), int((rectangle[1] + rectangle[3] - output_geometry[1]) * ratio_y + 1))
            check("scene readbacks share output dimensions", a.size() == b.size())
            changed = grey = 0
            for y in range(y0, y1):
                for x in range(x0, x1):
                    if a.pixel(x, y) != b.pixel(x, y):
                        changed += 1
                        color = b.pixelColor(x, y)
                        if (max(color.red(), color.green(), color.blue()) -
                                min(color.red(), color.green(), color.blue()) <= 12
                                and 12 < color.lightness() < 245):
                            grey += 1
            check("actual compositor scene contains grey changed pixels on the caret line",
                  changed > 0 and grey > 0, changed_pixels=changed, grey_pixels=grey,
                  physical_region=[x0, y0, x1, y1], baseline=baseline, offered=visible_scene)
        panel = output / "panel-latest.png"
        # R5: actual compositor keyboard-grab navigation, not _menu_key calls.
        check("R5 production native menu opens", (yield from rpc("menu")))
        menu_state = yield from wait_for(lambda: rpc("state") if rpc("state")["panel"]["menu"]
                                         and not rpc("state")["panel"]["pending"] else None,
                                         "attached real native menu")
        if panel.exists():
            (output / "panel-menu.png").write_bytes(panel.read_bytes())
        if injector is not None:
            enabled = [index for index, row in enumerate(menu_state["panel"]["rows"]) if row[2]]
            check("R5 native menu exposes actionable entries", len(enabled) > 1)
            for code, expected in ((107, enabled[-1]), (102, enabled[0]), (108, enabled[1]), (103, enabled[0])):
                injector.chord(code)  # Linux End, Home, Down, Up.
                yield from wait_for(lambda expected=expected: rpc("state")["panel"]["selected"] == expected,
                                    "native menu selection follows compositor key")
            injector.chord(1)  # KEY_ESC closes the menu, not the input context.
        else:
            check("R5 direct menu close (not keyboard proof)", (yield from rpc("menu_close")))
        yield from wait_for(lambda: not rpc("state")["panel"]["menu"], "native menu dismissed")
        check("manual dismissal does not edit host text", writing.text() == PREFIX and writing.selectionStart() == -1)

        # A fresh acknowledged frame after manual UI closes is the sole source
        # of accepted text, not the longer unpainted model suggestion.
        offered = yield from offer()
        displayed = offered["ghost"]["text"]
        snapshot = offered["snapshot"]
        im_start, call_start = len(commits), len(offered["commits"])
        if injector is not None:
            injector.chord(88, (29,))  # Physical Ctrl+F12, private all shortcut.
            yield from wait_for(lambda: writing.text() == PREFIX + displayed,
                                "physical all accepts exactly the acknowledged painted prefix")
            yield 0.25
            check("native commit inserts exact displayed prefix once without replacement",
                  commits[im_start:] == [{"field": "writing", "text": displayed,
                                         "replace_start": 0, "replace_length": 0}],
                  displayed=displayed, actual=writing.text(), input_method_events=commits[im_start:])
            advanced = yield from wait_for(lambda: eligible(PREFIX + displayed), "fresh native insertion readback")
            check("native readback correlates same field and a new revision",
                  advanced["snapshot"]["field_id"] == snapshot["field_id"]
                  and advanced["snapshot"]["native_anchor"] != snapshot["native_anchor"]
                  and len(advanced["commits"]) == call_start + 1,
                  before=snapshot, after=advanced["snapshot"])
            yield from rpc("invoke", action="dismiss")
            injector.chord(44, (29,))  # Ctrl+Z, actual host undo.
            yield from wait_for(lambda: writing.text() == PREFIX, "native host undo restores original prefix")
            check("physical undo restores original text/caret without selected replacement",
                  writing.text() == PREFIX and writing.selectionStart() == -1, actual=writing.text())
        else:
            report["unresolved_primitives"].append("exact-visible-prefix-physical-acceptance-and-host-undo")
        window.grab().save(str(output / "target-after.png"))

        # R1/R4: each saved snapshot comes from a real compositor revision.
        yield from prepare("Synthetic stale revision boundary")
        yield from offer()
        saved = yield from rpc("save", name="stale")
        writing.insert(" changed")
        yield from wait_for(lambda: eligible("Synthetic stale revision boundary changed"), "edited revision")
        start = len(commits)
        refused = yield from rpc("insert_saved", name="stale")
        yield 0.2
        check("R1 stale revision refuses before actual commit", refused is False and not commits[start:]
              and writing.text() == "Synthetic stale revision boundary changed", saved=saved,
              actual=writing.text())
        yield from rpc("invoke", action="dismiss")
        yield from literal_tab("stale revision Tab passes through without insertion")

        yield from prepare(PREFIX)
        # Keep a real active offer while changing the native selection.
        yield from offer()
        yield from rpc("save", name="selection")
        writing.setSelection(0, 9)
        app.inputMethod().update(Qt.InputMethodQuery.ImSurroundingText | Qt.InputMethodQuery.ImCursorPosition | Qt.InputMethodQuery.ImAnchorPosition | Qt.InputMethodQuery.ImCurrentSelection)
        yield from wait_for(lambda: rpc("state")["snapshot"] is None, "native selected-text refusal")
        start = len(commits)
        refused = yield from rpc("insert_saved", name="selection")
        accepted = yield from rpc("invoke", action="all")
        yield 0.2
        check("R1 selection refuses capture and acceptance", refused is False and accepted is False
              and not commits[start:] and writing.text() == PREFIX
              and writing.selectedText() == PREFIX[:9], actual=writing.text(), selected=writing.selectedText())
        check("selection immediately revokes acknowledged ghost", not rpc("state")["ghost"]["presented"])
        yield from literal_tab("selected text survives literal stale Tab")

        yield from prepare(PREFIX)
        yield from offer()
        yield from rpc("save", name="protected")
        writing.setEchoMode(QLineEdit.EchoMode.Password)
        writing.setInputMethodHints(Qt.InputMethodHint.ImhHiddenText | Qt.InputMethodHint.ImhSensitiveData
                                    | Qt.InputMethodHint.ImhNoPredictiveText)
        app.inputMethod().update(Qt.InputMethodQuery.ImEnabled | Qt.InputMethodQuery.ImHints)
        # Qt can stop reporting IM updates for password controls. AT-SPI must
        # independently exclude them even while the last native revision exists.
        yield from wait_for(lambda: rpc("state")["snapshot"] is None,
                            "real password control refuses capture")
        start = len(commits)
        refused = yield from rpc("insert_saved", name="protected")
        accepted = yield from rpc("invoke", action="all")
        forced = yield from rpc("invoke", action="force")
        yield 0.2
        protected = rpc("state")
        check("R1 real password refuses capture, commit and generation", refused is False and accepted is False
              and forced is False and not commits[start:] and writing.text() == PREFIX
              and not protected["panel"]["anchor"] and not protected["suggestion"], state=protected)

        # Exercise native sensitive-data rejection on re-entering an ordinary
        # editable control; password exclusion above is independently AT-SPI-gated.
        yield from prepare(PREFIX)
        yield from offer()
        yield from rpc("save", name="sensitive")
        writing.setInputMethodHints(Qt.InputMethodHint.ImhSensitiveData | Qt.InputMethodHint.ImhNoPredictiveText)
        app.inputMethod().update(Qt.InputMethodQuery.ImHints)
        report["sensitive_target"] = {"hints": int(writing.inputMethodHints().value),
                                     "query_hints": int(writing.inputMethodQuery(Qt.InputMethodQuery.ImHints))}
        other.setFocus(Qt.FocusReason.OtherFocusReason)
        yield 0.3
        writing.setFocus(Qt.FocusReason.OtherFocusReason)
        app.inputMethod().show()
        yield from wait_for(lambda: rpc("state")["native_state"] is None and rpc("state")["snapshot"] is None,
                            "native sensitive-data hint clears readiness")
        start = len(commits)
        refused = yield from rpc("insert_saved", name="sensitive")
        accepted = yield from rpc("invoke", action="all")
        forced = yield from rpc("invoke", action="force")
        yield 0.2
        sensitive = rpc("state")
        check("R1 native sensitive hint refuses capture, commit and generation",
              refused is False and accepted is False and forced is False
              and not commits[start:] and writing.text() == PREFIX
              and not sensitive["panel"]["anchor"] and not sensitive["suggestion"], state=sensitive)
        if injector is not None:
            start = len(events)
            injector.chord(15)
            yield 0.3
            delivered = [e for e in events[start:] if e["key"] in (int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab))]
            check("R3 sensitive transition forwards original Tab exactly once", [e["type"] for e in delivered] == ["press", "release"],
                  events=delivered)

        yield from prepare("Synthetic original forwarding boundary")
        yield from rpc("invoke", action="dismiss")
        if injector is not None:
            for label, modifiers, expected_modifier in (("Tab", (), 0), ("Shift+Tab", (42,), int(Qt.KeyboardModifier.ShiftModifier.value))):
                start = len(events)
                injector.chord(15, modifiers)
                yield 0.3
                delivered = [e for e in events[start:] if e["key"] in (int(Qt.Key.Key_Tab), int(Qt.Key.Key_Backtab))]
                check("R3 inactive " + label + " forwards once with modifiers", [e["type"] for e in delivered] == ["press", "release"]
                      and all(e["modifiers"] == expected_modifier for e in delivered), events=delivered)

        yield from prepare(PREFIX)
        focused_offer = yield from offer()
        saved = yield from rpc("save", name="focus")
        focus_ack_start = focused_offer["presentation_count"]
        other_before = yield from prepare("Synthetic other focused native field", other)
        start = len(commits)
        refused = yield from rpc("insert_saved", name="focus")
        yield 0.2
        check("R1 old native field context cannot commit to new focus", refused is False and not commits[start:]
              and other.text() == "Synthetic other focused native field"
              and writing.text() == PREFIX
              and other_before["snapshot"]["field_id"] != saved["field_id"],
              before=saved, after=other_before["snapshot"])
        focus_state = rpc("state")
        check("focus transition revokes the old displayed frame",
              any(not ack["text"] for ack in focus_state["presentations"] if ack["sequence"] > focus_ack_start)
              and (focus_state["ghost"]["snapshot"] is None
                   or focus_state["ghost"]["snapshot"]["field_id"] != saved["field_id"]),
              acknowledgements=[ack for ack in focus_state["presentations"] if ack["sequence"] > focus_ack_start])
        yield from rpc("invoke", action="dismiss")
        yield from literal_tab("focus-transition stale Tab is forwarded without insertion")

        yield from prepare(PREFIX)
        clip_offer = yield from offer()
        writing.setFixedWidth(180)  # Same text/caret; Qt now scrolls the line.
        yield from wait_for(lambda: not rpc("state")["ghost"]["presented"],
                            "real provider scroll/clip invalidates prior visible line")
        rejected = yield from rpc("invoke", action="all")
        check("horizontal scroll cannot accept an old painted frame", rejected is False
              and writing.text() == PREFIX and writing.cursorPosition() == len(PREFIX),
              previous_ghost=clip_offer["ghost"], current=rpc("state")["ghost"])
        yield from literal_tab("horizontally clipped stale Tab passes through")
        writing.setMinimumWidth(0)
        writing.setMaximumWidth(16777215)

        # Real caret-driven horizontal scrolling, not fabricated layout attrs.
        long_text = "Synthetic scrolling context " * 18
        yield from prepare(long_text)
        scroll_saved = yield from rpc("save", name="scroll", manual=True)
        yield from rpc("invoke", action="dismiss")
        writing.setCursorPosition(0)
        app.inputMethod().update(Qt.InputMethodQuery.ImCursorPosition | Qt.InputMethodQuery.ImAnchorPosition)
        yield from wait_for(lambda: rpc("state")["native_state"] !=
                            [int(part) for part in scroll_saved["native_anchor"].split(":")],
                            "horizontal scroll/caret publishes a fresh native revision")
        refused = yield from rpc("insert_saved", name="scroll")
        check("scrolled stale geometry cannot insert old completion", refused is False
              and writing.text() == long_text and writing.cursorPosition() == 0
              and not rpc("state")["ghost"]["presented"], saved=scroll_saved)
        yield from literal_tab("scrolled stale Tab remains literal")

        yield from prepare(PREFIX)
        geometry_offer = yield from offer()
        previous_scene = geometry_offer["ghost"]["scene"]
        window.resize(940, 320)
        yield from wait_for(lambda: rpc("state")["ghost"]["scene"] != previous_scene,
                            "compositor frame resize invalidates old scene")
        resized = yield from offer()
        check("geometry transition only presents in a new scene",
              resized["ghost"]["scene"] != previous_scene and writing.text() == PREFIX
              and writing.selectionStart() == -1, before=geometry_offer["ghost"], after=resized["ghost"])
        yield from output_transition()
        yield from desktop_transition()

        if injector is not None:
            yield from prepare(PREFIX)
            yield from offer()
            yield from rpc("save", name="compose")
            host_before_compose = (writing.text(), writing.cursorPosition(), writing.selectionStart())
            start = len(commits)
            injector.chord(40)  # Real dead_acute from this compositor's us(intl).
            yield from wait_for(lambda: not rpc("state")["composition_inactive"],
                                "actual XKB dead key marks composition pending", 3)
            # A repeated surrounding update with unchanged host text must not
            # turn native composition proof back into an unconditional true.
            app.inputMethod().update(Qt.InputMethodQuery.ImSurroundingText | Qt.InputMethodQuery.ImCursorPosition)
            yield 0.3
            composing = rpc("state")
            forced = yield from rpc("invoke", action="force")
            refused = yield from rpc("insert_saved", name="compose")
            accepted = yield from rpc("invoke", action="all")
            check("dead composition refuses ghost/acceptance/generation despite unchanged surrounding",
                  not composing["composition_inactive"] and not composing["ghost"]["presented"]
                  and composing["snapshot"] is None and forced is False
                  and refused is False and accepted is False
                  and host_before_compose == (writing.text(), writing.cursorPosition(), writing.selectionStart())
                  and not commits[start:], state=composing)
            injector.chord(18)  # KEY_E completes actual dead_acute + e -> é.
            try:
                yield from wait_for(lambda: writing.text() == PREFIX + "é",
                                    "real Qt compose commits exact expected UTF-8 splice", 3)
            except AssertionError as error:
                raise ProofUnavailable("dead-compose-result: owned us(intl) did not produce exact é; "
                                       "see recorded key/preedit/commit events") from error
            rearmed = yield from wait_for(lambda: eligible(PREFIX + "é")
                                          if rpc("state")["composition_inactive"] else None,
                                          "composition rearms only after exact committed surrounding splice")
            check("composition native readback matches actual composed host string",
                  rearmed["snapshot"]["text"] == PREFIX + "é"
                  and rearmed["snapshot"]["caret"] == len(PREFIX) + 1,
                  snapshot=rearmed["snapshot"], composition_events=compositions[-8:])
            yield from offer()
            capture("compose-rearmed")
        report["final_native_state"] = rpc("state")
        stopped = yield from stop_agent()
        check("R4 owned worker and native client stop", stopped["native_client_closed"]
              and stopped["read_notifier_removed"] and stopped["notifiers_removed"]
              and stopped["status"]["worker_pid"] is None
              and stopped["worker_pid"] is not None and stopped["worker_returncode"] is not None,
              stop=stopped)
        report["result"] = "incomplete" if report["limits"] else "passed"

    generator = scenarios()
    finished = False

    def finish(error=None):
        nonlocal finished
        if finished:
            return
        finished = True
        ipc["closing"] = True
        poll_timer.stop()
        if error is not None:
            report["result"] = "incomplete" if isinstance(error, ProofUnavailable) else "failed"
            report["limits"].append(f"{type(error).__name__}: {error}")
            if isinstance(error, ProofUnavailable):
                report["unresolved_primitives"].append(str(error))
            report["traceback"] = traceback.format_exc()
            report["failure_native_state"] = ipc["state"]
        if injector is not None:
            injector.close()

        def persist(value=None, stop_error=None):
            if value is not None:
                report["failure_stop"] = value
            if stop_error is not None:
                report["limits"].append("Observer shutdown: " + stop_error)
            report["target_key_events"] = events
            report["target_input_method_events"] = commits
            report["target_composition_events"] = compositions
            (output / "target-report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
            window.close()
            app.exit({"passed": 0, "incomplete": 2}.get(report["result"], 1))

        if error is not None:
            deadline = time.monotonic() + 8

            def stopped(value=None, stop_error=None):
                if stop_error is None and value is not None and value.get("pending"):
                    report.setdefault("failure_stop_attempts", []).append(value)
                    if time.monotonic() < deadline:
                        QTimer.singleShot(100, lambda: send_rpc("stop", {}, stopped))
                        return
                    stop_error = "Owned agent shutdown remained pending after 8 seconds"
                persist(value, stop_error)

            send_rpc("stop", {}, stopped)
        else:
            persist()

    def step():
        try:
            delay = next(generator)
            QTimer.singleShot(max(1, round(delay * 1000)), step)
        except StopIteration:
            finish()
        except Exception as error:
            finish(error)

    QTimer.singleShot(0, step)
    QTimer.singleShot((int(os.environ["INKLOW_SMOKE_TIMEOUT"]) - 10) * 1000,
                      lambda: finish(TimeoutError("Target's finite whole-run deadline expired")))
    return app.exec()


if __name__ == "__main__":
    roles = {"session": session, "im": im, "target": target, "capture": capture_scene}
    if len(sys.argv) != 2 or sys.argv[1] not in roles:
        raise SystemExit("Private fixture; use smoke_wayland.py instead")
    try:
        raise SystemExit(roles[sys.argv[1]]())
    except ProofUnavailable as error:
        _, evidence = isolation()
        if sys.argv[1] != "capture":
            (evidence / "target-report.json").write_text(json.dumps(
                {"result": "incomplete", "checks": [], "limits": [str(error)],
                 "unresolved_primitives": [str(error)]}, indent=2) + "\n")
        print(str(error), file=sys.stderr)
        raise SystemExit(2)
