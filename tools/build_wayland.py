"""Build the KDE Wayland input method and inactive public-display renderer.

Usage: python tools/build_wayland.py
Requires a C compiler, pkg-config, wayland-scanner, wayland-protocols,
libwayland-client and libxkbcommon development files. Uses installed standard
protocol XML plus notice-preserving vendored KDE/wlr metadata; never downloads
or installs packages. Generated files live only in a temporary build directory.
"""
from __future__ import annotations

import argparse
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile

ROOT = Path(__file__).resolve().parents[1]
DEFAULT_OUTPUT = ROOT / "src" / "inklow" / "native" / "libinklow_wayland.so"
NATIVE = ROOT / "src" / "inklow" / "native"


def run(command: list[str]) -> str:
    """Return stripped command output or raise with captured failure diagnostics."""
    result = subprocess.run(command, check=False, capture_output=True, text=True)
    if result.returncode:
        detail = result.stderr.strip() or result.stdout.strip()
        raise RuntimeError(f"Command failed: {shlex.join(command)}\n{detail}")
    return result.stdout.strip()


def build(output: Path = DEFAULT_OUTPUT) -> Path:
    """Compile atomically; an unsuccessful build leaves the old library intact."""
    if not sys.platform.startswith("linux"):
        raise RuntimeError("The native Wayland input method must be built on Linux.")
    compiler = shlex.split(os.environ.get("CC", "cc"))
    if not compiler or not shutil.which(compiler[0]):
        raise RuntimeError("Missing C compiler; install a system C toolchain or set CC.")
    pkg_config = shutil.which("pkg-config")
    scanner = shutil.which("wayland-scanner")
    if not pkg_config or not scanner:
        raise RuntimeError("Missing pkg-config or wayland-scanner; install the system Wayland development tools.")
    packages = ["wayland-client", "xkbcommon", "wayland-protocols"]
    missing = [package for package in packages if subprocess.run(
        [pkg_config, "--exists", package], check=False, stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    ).returncode]
    if missing:
        raise RuntimeError("Missing pkg-config development metadata: " + ", ".join(missing)
                           + ". Install the corresponding system development packages.")
    protocol_root_text = run([pkg_config, "--variable=pkgdatadir", "wayland-protocols"])
    if not protocol_root_text:
        # Some baseline distro .pc files provide datadir rather than pkgdatadir.
        datadir = run([pkg_config, "--variable=datadir", "wayland-protocols"])
        if not datadir:
            raise RuntimeError("wayland-protocols pkg-config metadata has no data directory.")
        protocol_root_text = str(Path(datadir) / "wayland-protocols")
    protocol_root = Path(protocol_root_text)
    candidates = {
        "input-method": [protocol_root / "unstable/input-method/input-method-unstable-v1.xml"],
        "xdg-shell": [protocol_root / "stable/xdg-shell/xdg-shell.xml"],
        "xdg-output": [protocol_root / "unstable/xdg-output/xdg-output-unstable-v1.xml"],
        # Fractional scale moved from staging to stable after Ubuntu 24.04's
        # baseline. Do not assume the build host has the newer layout.
        "fractional-scale": [protocol_root / "stable/fractional-scale/fractional-scale-v1.xml",
                             protocol_root / "staging/fractional-scale/fractional-scale-v1.xml"],
        "viewporter": [protocol_root / "stable/viewporter/viewporter.xml"],
        "presentation-time": [protocol_root / "stable/presentation-time/presentation-time.xml"],
        "wlr-layer-shell": [NATIVE / "protocols/wlr-layer-shell-unstable-v1.xml"],
        "plasma-window-management": [NATIVE / "protocols/plasma-window-management.xml"],
    }
    protocols: dict[str, Path] = {}
    for name, paths in candidates.items():
        match = next((path for path in paths if path.is_file()), None)
        if match is None:
            raise RuntimeError(f"Missing {name} protocol XML; searched: "
                               + ", ".join(str(path) for path in paths))
        protocols[name] = match
    flags = shlex.split(run([pkg_config, "--cflags", "--libs", "wayland-client", "xkbcommon"]))
    output = Path(output).absolute()
    if output.is_symlink():
        raise RuntimeError(f"Refusing a symlink output library: {output}")
    output.parent.mkdir(parents=True, exist_ok=True)
    # Generate and link beside the destination so the final rename is atomic
    # and a compiler/scanner failure cannot truncate the previously built library.
    with tempfile.TemporaryDirectory(prefix=".inklow-wayland-", dir=output.parent) as temporary:
        directory = Path(temporary)
        sources: list[str] = []
        library = directory / output.name
        for name, protocol in protocols.items():
            header = directory / f"{name}-client-protocol.h"
            source = directory / f"{name}-protocol.c"
            run([scanner, "client-header", str(protocol), str(header)])
            run([scanner, "private-code", str(protocol), str(source)])
            sources.append(str(source))
        command = [
            *compiler, "-std=c11", "-O2", "-fPIC", "-fvisibility=hidden",
            "-Wall", "-Wextra", "-Werror", "-Wl,-z,relro,-z,now", "-Wl,--no-undefined",
            *shlex.split(os.environ.get("CPPFLAGS", "")),
            *shlex.split(os.environ.get("CFLAGS", "")),
            "-shared", "-I", str(directory),
            str(NATIVE / "wayland_client.c"), str(NATIVE / "wayland_ghost.c"), *sources,
            "-o", str(library), *flags,
            *shlex.split(os.environ.get("LDFLAGS", "")),
        ]
        print(shlex.join(command), flush=True)
        run(command)
        library.chmod(0o755)
        os.replace(library, output)
    return output


def main() -> int:
    """Build the requested library and return 1 for toolchain or filesystem errors."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT,
                        help="destination shared library (default: native/libinklow_wayland.so)")
    args = parser.parse_args()
    try:
        print(build(args.output))
    except (OSError, RuntimeError) as error:
        print(f"Wayland native build failed: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
