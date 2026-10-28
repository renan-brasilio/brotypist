#!/usr/bin/env python3
"""Exercise production Wayland inline text in an owned, disposable KWin.

Requires the built native library, application venv, cached GGUF, KWin and
D-Bus. KWin must advertise the actual ghost protocols (including wp_presentation);
version alone is not proof. Xvfb plus Qt's xcb plugin provide scene screenshots.
Each run copies Python into two private executable identities and declares only
their role-specific KDE interfaces under its isolated XDG_DATA_HOME. The venv
packages and original stdlib remain in use; no shared interpreter gets a grant.
No compilation/download/install, host socket, desktop selection or settings.
Exit 0: complete proof at both scales; 1: failure; 2: incomplete proof.
"""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import secrets
import shutil
import signal
import subprocess
import sys
import tempfile
import time


def main() -> int:
    """Run independent 100%/125% sessions and persist their aggregate evidence.

    Missing primitives remain incomplete, not passing. Every launched session
    gets an owned process group; cleanup also covers surviving descendants.
    """
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model-path", required=True, type=Path,
                        help="Existing cached GGUF; only a launch override")
    parser.add_argument("--output", required=True, type=Path,
                        help="New private evidence directory")
    parser.add_argument("--timeout", type=int, default=240,
                        help="Per-scale process-group deadline, 30..600 seconds")
    parser.add_argument("--backend", choices=("auto", "x11", "virtual"), default="auto",
                        help="auto prefers private Xvfb/X11; virtual cannot prove scene pixels")
    args = parser.parse_args()
    if sys.platform != "linux":
        parser.error("Run in the disposable Linux guest with its application venv")
    if not 30 <= args.timeout <= 600:
        parser.error("--timeout must be 30..600 seconds")
    model = args.model_path.expanduser().resolve(strict=True)
    if not model.is_file() or model.suffix.lower() != ".gguf":
        parser.error("--model-path must identify an existing GGUF")
    commands = {name: shutil.which(name) for name in ("dbus-run-session", "kwin_wayland", "Xvfb")}
    if not commands["dbus-run-session"] or not commands["kwin_wayland"]:
        parser.error("dbus-run-session and kwin_wayland must already be installed")
    repo = Path(__file__).resolve().parents[1]
    if not (repo / "src/inklow/native/libinklow_wayland.so").is_file():
        parser.error("Parent must compile src/inklow/native/libinklow_wayland.so first")
    output = args.output.expanduser().absolute()
    output.mkdir(parents=True, exist_ok=False, mode=0o700)
    output.chmod(0o700)
    report = {"schema": 2, "result": "failed", "runs": [], "limits": [],
              "required_scales": [100, 125],
              "isolation": {"owned_kwin": True, "private_bus": True,
                            "host_settings_changed": False, "host_display_inherited": False,
                            "desktop_grants": "private copied fixture executables only",
                            "permission_checks_bypassed": False},
              "image_semantics": {"scene-*.png": "owned X11-windowed KWin framebuffer via private Xvfb",
                                  "ghost-*.png": "production client ARGB buffers, NOT scene proof",
                                  "target-*.png": "Qt client widget captures, NOT scene proof"}}
    process = None
    started = time.monotonic()

    def interrupted(number, _frame):
        """Route termination through receipt writing and owned-group cleanup."""
        raise InterruptedError(f"Smoke interrupted by signal {number}")

    signals = (signal.SIGINT, signal.SIGTERM)
    handlers = {number: signal.signal(number, interrupted) for number in signals}
    try:
        for percent in (100, 125):
            run_output = output / str(percent)
            run_output.mkdir(mode=0o700)
            run = {"scale_percent": percent, "result": "failed", "checks": [], "limits": []}
            report["runs"].append(run)
            with tempfile.TemporaryDirectory(prefix="inklow-wayland-smoke-") as temporary:
                root = Path(temporary)
                root.chmod(0o700)
                # Allowlist: no inherited DISPLAY, bus, AT-SPI, WAYLAND_SOCKET,
                # Qt scale overrides, keyboard layout, credentials or proxies.
                env = {"PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                       "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                       "PYTHONPATH": str(repo / "src"), "PYTHONUNBUFFERED": "1",
                       "XDG_SESSION_TYPE": "wayland", "XDG_CURRENT_DESKTOP": "KDE",
                       "QT_QPA_PLATFORM": "wayland", "QT_IM_MODULE": "wayland",
                       "QT_WAYLAND_TEXT_INPUT_PROTOCOL": "zwp_text_input_v3",
                       "QT_QUICK_BACKEND": "software", "QT_OPENGL": "software",
                       "LIBGL_ALWAYS_SOFTWARE": "1", "GALLIUM_DRIVER": "llvmpipe",
                       "KWIN_COMPOSE": "O2", "KWIN_DRM_DEVICES": str(root / "no-hardware-gpu"),
                       "DBUS_SYSTEM_BUS_ADDRESS": "unix:path=" + str(root / "no-system-bus"),
                       "QT_LINUX_ACCESSIBILITY_ALWAYS_ON": "1",
                       "QT_ACCESSIBILITY": "1", "NO_AT_BRIDGE": "0",
                       "XDG_DATA_DIRS": "/usr/local/share:/usr/share",
                       "INKLOW_SMOKE_ROOT": str(root), "INKLOW_SMOKE_OUTPUT": str(run_output),
                       "INKLOW_SMOKE_TOKEN": secrets.token_hex(24),
                       "INKLOW_SMOKE_MODEL": str(model), "INKLOW_SMOKE_TIMEOUT": str(args.timeout),
                       "INKLOW_SMOKE_SCALE": str(percent), "INKLOW_SMOKE_BACKEND": args.backend,
                       "INKLOW_SMOKE_KWIN": commands["kwin_wayland"],
                       "INKLOW_SMOKE_XVFB": commands["Xvfb"] or ""}
                for key, name in (("HOME", "home"), ("XDG_RUNTIME_DIR", "runtime"),
                                  ("XDG_CONFIG_HOME", "config"), ("XDG_CONFIG_DIRS", "config-dirs"),
                                  ("XDG_DATA_HOME", "data"), ("XDG_CACHE_HOME", "cache"),
                                  ("INKLOW_HOME", "inklow")):
                    directory = root / name
                    directory.mkdir(mode=0o700)
                    env[key] = str(directory)
                (root / "owner.json").write_text(json.dumps({"token": env["INKLOW_SMOKE_TOKEN"],
                                                             "uid": os.getuid()}))
                # Explicit disposable keyboard only; never the user's layout.
                (root / "config/kxkbrc").write_text(
                    "[Layout]\nUse=true\nLayoutList=us\nVariantList=intl\n")
                settings = {"setup_completed": True, "acceleration": "cpu", "min_chars": 8,
                            "max_tokens": 48, "debounce_ms": 500, "idle_release_seconds": 0,
                            "punctuation_mode": "always", "trailing_space": True,
                            "clipboard_context": False, "screen_context": False,
                            "record_history": False, "statistics_enabled": False,
                            "battery_on_demand": False, "emoji_enabled": False,
                            "synonyms_enabled": False, "shortcuts": {"all": "Ctrl+F12"}}
                (root / "inklow/settings.json").write_text(json.dumps(settings))
                fixture = Path(__file__).with_name("wayland_smoke_fixture.py")
                command = [commands["dbus-run-session"], "--", sys.executable, str(fixture), "session"]
                with (run_output / "session.log").open("wb") as log:
                    process = subprocess.Popen(command, env=env, cwd=repo, stdout=log,
                                               stderr=subprocess.STDOUT, start_new_session=True)
                    try:
                        run["session_returncode"] = process.wait(timeout=args.timeout)
                    except subprocess.TimeoutExpired:
                        run["limits"].append("whole-owned-session-deadline")
                    finally:
                        # Also stop descendants when the bus/session leader has
                        # exited: all private services inherit this owned group.
                        terminate_group(process.pid)
                        process = None
                for name in ("target-report.json", "session-report.json"):
                    result_path = run_output / name
                    if result_path.is_file():
                        run.update(json.loads(result_path.read_text()))
                # Retain the observed filtered registries even if a client
                # cannot reach its full Qt/production entrypoint.
                run["registry_evidence"] = {}
                for role in ("im", "target"):
                    evidence = run_output / (role + "-registry.json")
                    if evidence.is_file():
                        run["registry_evidence"][role] = json.loads(evidence.read_text())
                identities = run_output / "identities.json"
                if identities.is_file():
                    run["fixture_identities"] = json.loads(identities.read_text())
                if run.get("session_returncode") not in (0, 2):
                    run["result"] = "failed"
                    run["limits"].append("Owned compositor/session failed or exceeded its deadline")
                elif run.get("session_returncode") == 2 and run["result"] == "passed":
                    run["result"] = "incomplete"
            if run["result"] == "failed":
                # Continue the independently owned scale so its exact primitives
                # are visible even if one scale fails.
                report["limits"].append(f"{percent}% run failed")
        results = [run["result"] for run in report["runs"]]
        report["result"] = "failed" if "failed" in results else (
            "passed" if results == ["passed", "passed"] else "incomplete")
    except (OSError, ValueError, RuntimeError) as error:
        report["limits"].append(f"{type(error).__name__}: {error}")
    finally:
        for number in signals:
            signal.signal(number, signal.SIG_IGN)
        if process is not None:
            terminate_group(process.pid)
        report["elapsed_seconds"] = round(time.monotonic() - started, 3)
        (output / "report.json").write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n")
        for number, handler in handlers.items():
            signal.signal(number, handler)
    print(json.dumps({"result": report["result"], "report": str(output / "report.json")}))
    return {"passed": 0, "incomplete": 2}.get(report["result"], 1)


def terminate_group(pid: int) -> None:
    """Terminate an owned process-group ID, escalating after two seconds.

    The caller must supply a group created with start_new_session=True, never an
    arbitrary application PID. A missing group is already cleaned up.
    """
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 2
    while time.monotonic() < deadline:
        try:
            os.killpg(pid, 0)
        except ProcessLookupError:
            return
        time.sleep(0.05)
    try:
        os.killpg(pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


if __name__ == "__main__":
    raise SystemExit(main())
