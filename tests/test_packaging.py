"""Packaging safety scenarios: local://inklow-packaging-test-design.json."""
import configparser
import json
from pathlib import Path
import runpy
import shlex
from types import SimpleNamespace

import pytest

from inklow import resources, system

BUILD = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools" / "build_app.py"))


def mark(bundle, platform="linux"):
    path = BUILD["marker_path"](bundle, platform)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"application": resources.APP_ID, "platform": platform}))



def test_asset_lookup_uses_relocated_package_and_refuses_escape(tmp_path, monkeypatch):
    # R1/S2: frozen layout must not depend on cwd or source checkout.
    package = tmp_path / "frozen" / "_internal" / "inklow"
    assets = package / "assets"
    assets.mkdir(parents=True)
    (assets / "app.png").write_bytes(resources.asset_path("app.png").read_bytes())
    outside = package / "private.txt"
    outside.write_text("not an asset")
    monkeypatch.setattr(resources, "__file__", str(package / "resources.py"))
    monkeypatch.chdir(tmp_path)
    assert resources.asset_path("app.png") == assets / "app.png"
    for name in ("../private.txt", str(outside), "missing.png"):
        with pytest.raises(FileNotFoundError):
            resources.asset_path(name)


@pytest.fixture
def macos_relocated_assets(tmp_path, monkeypatch):
    contents = tmp_path / "BroTypist.app" / "Contents"
    package = contents / "Frameworks" / "inklow"
    assets = contents / "Resources" / "inklow" / "assets"
    package.mkdir(parents=True)
    assets.mkdir(parents=True)
    (assets / "app.png").write_bytes(resources.asset_path("app.png").read_bytes())
    outside = assets.parent / "private.txt"
    outside.write_bytes(b"not an application asset")
    try:
        (package / "assets").symlink_to(assets, target_is_directory=True)
        (assets / "escape.txt").symlink_to(outside)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this runner")
    monkeypatch.setattr(resources, "__file__", str(package / "resources.py"))
    monkeypatch.chdir(tmp_path)
    return assets, outside


def test_asset_lookup_accepts_macos_symlinked_resource_root(macos_relocated_assets):
    assets, _ = macos_relocated_assets
    assert resources.asset_path("app.png") == assets / "app.png"


@pytest.mark.parametrize("escape", ["parent", "absolute", "symlink", "missing"])
def test_symlinked_asset_root_still_refuses_escape(macos_relocated_assets, escape):
    _, outside = macos_relocated_assets
    name = {"parent": "../private.txt", "absolute": str(outside),
            "symlink": "escape.txt", "missing": "missing.png"}[escape]
    with pytest.raises(FileNotFoundError):
        resources.asset_path(name)


@pytest.mark.parametrize("platform", ["linux", "win32", "darwin"])
def test_replacement_refuses_unmarked_application_without_mutating_it(tmp_path, platform):
    # R3/S3: --replace is not permission to delete an unrelated application.
    destination = tmp_path / "Other.app"
    destination.mkdir()
    sentinel = destination / "user-document.txt"
    sentinel.write_bytes(b"keep this exact content")
    with pytest.raises(ValueError):
        BUILD["ensure_destination"](destination, replace=True, platform=platform)
    assert sentinel.read_bytes() == b"keep this exact content"
    assert list(destination.iterdir()) == [sentinel]


def test_replacement_refuses_symlink_even_if_target_is_owned(tmp_path):
    # R3/S3: ownership markers must not turn a symlink into deletion authority.
    target = tmp_path / "owned"
    mark(target)
    (target / "data").write_bytes(b"preserve")
    link = tmp_path / "link"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("Creating symlinks is not permitted on this runner")
    with pytest.raises(ValueError):
        BUILD["ensure_destination"](link, replace=True, platform="linux")
    assert link.is_symlink()
    assert (target / "data").read_bytes() == b"preserve"


def test_owned_build_promotion_leaves_neighbors_untouched(tmp_path):
    # R3/S4: publication swaps only the marked destination.
    destination = tmp_path / "Inklow"
    mark(destination)
    (destination / "old").write_bytes(b"previous")
    neighbor = tmp_path / "Other.app"
    neighbor.mkdir()
    (neighbor / "document").write_bytes(b"private")
    staging = tmp_path / "staging"
    bundle = staging / "dist" / "Inklow"
    mark(bundle)
    (bundle / "new").write_bytes(b"new application")
    BUILD["publish"](bundle, destination, replace=True, platform="linux")
    assert (destination / "new").read_bytes() == b"new application"
    assert not (destination / "old").exists()
    assert not bundle.exists()
    assert (neighbor / "document").read_bytes() == b"private"


def test_failed_promotion_restores_previous_build(tmp_path, monkeypatch):
    # R3/S4: an OS rename failure must leave the prior app fully recoverable.
    destination = tmp_path / "Inklow"
    mark(destination)
    (destination / "document").write_bytes(b"previous content")
    bundle = tmp_path / "staging" / "dist" / "Inklow"
    mark(bundle)
    (bundle / "replacement").write_bytes(b"new content")
    rename = Path.rename

    def fail_new_promotion(path, target):
        if path == bundle:
            raise OSError("simulated destination rename failure")
        return rename(path, target)

    monkeypatch.setattr(Path, "rename", fail_new_promotion)
    with pytest.raises(OSError):
        BUILD["publish"](bundle, destination, replace=True, platform="linux")
    assert (destination / "document").read_bytes() == b"previous content"
    assert (bundle / "replacement").read_bytes() == b"new content"
    assert BUILD["marker_path"](destination, "linux").is_file()
    assert not (bundle.parent / "previous-build").exists()


@pytest.mark.parametrize("private_name", ["private.gguf", "weights.safetensors", "settings.json", ".env",
                                          "history.sqlite3", "statistics.db", "history.enc"])
def test_private_payload_is_rejected_but_native_libraries_are_allowed(tmp_path, private_name):
    # Native code may ship, but weights, settings and personal stores may not.
    library = tmp_path / "llama_cpp" / "lib" / "libggml-metal.dylib"
    library.parent.mkdir(parents=True)
    library.write_bytes(b"native library payload")
    BUILD["reject_private_payload"](tmp_path)
    private = tmp_path / "unexpected" / private_name
    private.parent.mkdir()
    private.write_bytes(b"must not ship")
    with pytest.raises(RuntimeError):
        BUILD["reject_private_payload"](tmp_path)
    assert library.read_bytes() == b"native library payload"
    assert private.read_bytes() == b"must not ship"


def desktop_entry(path):
    parser = configparser.ConfigParser(interpolation=None)
    parser.read(path, encoding="utf-8")
    return parser["Desktop Entry"]


@pytest.fixture
def frozen_wayland_application(tmp_path, monkeypatch):
    bundle = tmp_path / "Portable app"
    executable = bundle / "BroTypist"
    package = bundle / "_internal" / "inklow"
    native = package / "native" / "libinklow_wayland.so"
    native.parent.mkdir(parents=True)
    native.write_bytes(b"native payload")
    executable.write_bytes(b"owned executable")
    data = tmp_path / "user-data"
    monkeypatch.delenv("INKLOW_HOME", raising=False)
    monkeypatch.setenv("XDG_DATA_HOME", str(data))
    monkeypatch.setattr(system, "__file__", str(package / "system.py"))
    monkeypatch.setattr(system, "sys", SimpleNamespace(
        platform="linux", frozen=True, executable=str(executable)))
    return bundle, executable, data


def test_wayland_registration_tracks_relocated_frozen_identity(frozen_wayland_application, monkeypatch):
    bundle, executable, data = frozen_wayland_application
    entry = system.install_wayland_input_method()
    assert entry == data / "applications" / "com.inklow.Inklow.InputMethod.desktop"
    metadata = desktop_entry(entry)
    assert shlex.split(metadata["Exec"]) == [
        str(executable.resolve()), "--wayland-input-method", "--background"]
    assert metadata["X-KDE-Wayland-Interfaces"] == "org_kde_plasma_window_management"
    assert metadata["X-KDE-Wayland-VirtualKeyboard"] == "true"
    moved = bundle.with_name("Relocated portable app")
    bundle.rename(moved)
    alias = moved.parent / "brotypist-link"
    alias.symlink_to(moved / "BroTypist")
    monkeypatch.setattr(system.sys, "executable", str(alias))
    monkeypatch.setattr(system, "__file__", str(moved / "_internal/inklow/system.py"))
    assert system.install_wayland_input_method() == entry
    metadata = desktop_entry(entry)
    assert shlex.split(metadata["Exec"])[0] == str((moved / "BroTypist").resolve())
    assert metadata["Icon"] == str(moved / "_internal/inklow/assets/icons/app-256.png")
    assert sorted(path.relative_to(data).as_posix() for path in data.rglob("*") if path.is_file()) == [
        "applications/com.inklow.Inklow.InputMethod.desktop"]


def test_source_registration_never_authorizes_shared_python(frozen_wayland_application, monkeypatch):
    _, _, data = frozen_wayland_application
    monkeypatch.setattr(system.sys, "frozen", False)
    monkeypatch.setattr(system.sys, "executable", "/usr/bin/python3")
    with pytest.raises(RuntimeError, match="shares an interpreter identity"):
        system.install_wayland_input_method()
    assert not data.exists()


def test_isolated_profile_never_registers_desktop_identity(frozen_wayland_application, monkeypatch):
    _, _, data = frozen_wayland_application
    monkeypatch.setenv("INKLOW_HOME", str(data / "isolated"))
    with pytest.raises(RuntimeError, match="isolated profile"):
        system.install_wayland_input_method()
    assert not data.exists()


@pytest.mark.parametrize("kind", ["foreign", "symlink", "dangling"])
def test_wayland_registration_preserves_foreign_entries(frozen_wayland_application, kind):
    _, _, data = frozen_wayland_application
    entry = data / "applications/com.inklow.Inklow.InputMethod.desktop"
    entry.parent.mkdir(parents=True)
    target = data / "other.desktop"
    if kind == "foreign":
        entry.write_text("foreign entry")
    else:
        if kind == "symlink":
            target.write_text("X-Inklow-Owned=true\n")
        entry.symlink_to(target)
    with pytest.raises(RuntimeError, match="not overwritten"):
        system.install_wayland_input_method()
    if kind == "foreign":
        assert entry.read_text() == "foreign entry"
    else:
        assert entry.is_symlink()
        if kind == "symlink":
            assert target.read_text() == "X-Inklow-Owned=true\n"
        else:
            assert not target.exists()


@pytest.mark.parametrize("character", ["\\", '"', "`", "$", "%", "\t", "\n", "\r"])
def test_wayland_registration_refuses_ambiguous_kwin_paths(
        frozen_wayland_application, monkeypatch, character):
    bundle, executable, data = frozen_wayland_application
    unusual = bundle / f"Bro{character}Typist"
    executable.rename(unusual)
    monkeypatch.setattr(system.sys, "executable", str(unusual))
    with pytest.raises(RuntimeError, match="cannot safely match"):
        system.install_wayland_input_method()
    assert not data.exists()


def test_linux_payload_desktop_identity_is_executable_and_export_is_relocatable(tmp_path):
    bundle = tmp_path / "Built app"
    bundle.mkdir()
    (bundle / "BroTypist").write_bytes(b"application payload")
    BUILD["prepare_linux"](bundle, bundle)
    entries = [bundle / "BroTypist.desktop",
               bundle / "share/applications/com.inklow.Inklow.desktop",
               bundle / "share/applications/com.inklow.Inklow.InputMethod.desktop"]
    for entry in entries:
        metadata = desktop_entry(entry)
        assert shlex.split(metadata["Exec"])[0] == str(bundle / "BroTypist")
        assert metadata["X-KDE-Wayland-Interfaces"] == "org_kde_plasma_window_management"
    package = runpy.run_path(str(Path(__file__).resolve().parents[1] / "tools/package_linux.py"))
    exported = tmp_path / "Portable export"
    package["copy_application"](bundle, exported)
    assert (exported / "BroTypist").read_bytes() == b"application payload"
    assert (exported / "launch-brotypist").stat().st_mode & 0o111
    for entry in entries:
        assert not (exported / entry.relative_to(bundle)).exists()
        assert entry.is_file()
