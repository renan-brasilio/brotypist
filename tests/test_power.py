"""Constrain power parsing, shared sampling, and low-battery delivery policy.

OS reads use controlled subprocess, ctypes, or temporary sysfs boundaries;
no fixture changes the machine's actual power source or desktop preferences.
"""

from types import SimpleNamespace
import subprocess
import threading

import pytest

from inklow import system
from inklow.power import LowBatteryHelpPolicy, charging_help_text, power_presentation


def reading(*, available=True, has_battery=True, on_battery=True, charging=False, percent=19):
    return dict(available=available, has_battery=has_battery, on_battery=on_battery,
                charging=charging, percent=percent)


def pmset(source="Battery Power", percent="19", state="discharging"):
    """Build a textual macOS power report without invoking the native command."""
    return (f"Now drawing from '{source}'\n"
            f" -InternalBattery-0 (id=1234567)\t{percent}%; {state}; 2:15 remaining present: true\n")


@pytest.fixture
def native_sample(monkeypatch):
    """Expose a manual cache clock, mutable response slot, and subprocess call log."""
    clock = [0.0]
    responses = [pmset()]
    calls = []

    def run(command, **options):
        calls.append((command, options))
        value = responses[0]
        if isinstance(value, Exception):
            raise value
        return SimpleNamespace(stdout=value)

    monkeypatch.setattr(system.sys, "platform", "darwin")
    monkeypatch.setattr(system.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(system.subprocess, "run", run)
    monkeypatch.setattr(system, "_battery_cache", None)
    return clock, responses, calls


@pytest.mark.parametrize("output,expected", [
    (pmset(), reading()),
    (pmset(percent="0"), reading(percent=0)),
    (pmset(percent="100"), reading(percent=100)),
    (pmset("AC Power", "37", "charging"), reading(on_battery=False, charging=True, percent=37)),
    (pmset("AC Power", "99", "finishing charge"), reading(on_battery=False, charging=True, percent=99)),
    (pmset("AC Power", "100", "charged"), reading(on_battery=False, percent=100)),
    (pmset("AC Power", "79", "AC attached; not charging"), reading(on_battery=False, percent=79)),
    ("Now drawing from 'AC Power'\n -InternalBattery-0 (id=1234567)\t80%; AC attached; not charging present: true\n",
     reading(on_battery=False, percent=80)),
    ("Now drawing from 'AC Power'\n", reading(has_battery=False, on_battery=False, charging=None, percent=None)),
    ("Now drawing from 'AC Power'\nNo batteries.\n", reading(has_battery=False, on_battery=False, charging=None, percent=None)),
    ("Now drawing from 'AC Power'\nunsupported diagnostic\n",
     reading(available=False, has_battery=False, on_battery=False, charging=None, percent=None)),
    (pmset("AC Power").replace("present: true", "present: false"),
     reading(has_battery=False, on_battery=False, charging=None, percent=None)),
    (pmset(percent="101"), reading(percent=None)),
    (pmset(percent="-1"), reading(percent=None)),
    (pmset(percent="unknown"), reading(percent=None)),
    (pmset(state="unknown"), reading(charging=None)),
    (pmset("AC Power", "19", "discharging"), reading(on_battery=False, charging=False)),
    (pmset(state="charging"), reading(available=False, on_battery=True, charging=None)),
    ("Now drawing from 'Battery Power'\n", reading(available=False, charging=None, percent=None)),
    ("Now drawing from 'AC Power'\n -InternalBattery-0 unreadable\n",
     reading(available=False, on_battery=False, charging=None, percent=None)),
    ("unrecognized power report", reading(available=False, has_battery=False, on_battery=False, charging=None, percent=None)),
    ("", reading(available=False, has_battery=False, on_battery=False, charging=None, percent=None)),
])
def test_native_power_payload_distinguishes_actual_battery_states(native_sample, output, expected):
    _, responses, calls = native_sample
    responses[0] = output
    assert system.power_status() == expected
    assert len(calls) == 1


def test_power_status_and_legacy_reader_share_one_bounded_sample(native_sample):
    clock, responses, calls = native_sample
    assert system.on_battery() is True
    first = system.power_status()
    first["percent"] = 100
    responses[0] = pmset("AC Power", "40", "charging")
    clock[0] = 14.999
    assert system.power_status() == reading()
    assert system.on_battery() is True
    assert len(calls) == 1
    clock[0] = 15.0
    assert system.power_status() == reading(on_battery=False, charging=True, percent=40)
    assert system.on_battery() is False
    assert len(calls) == 2
    command, options = calls[0]
    assert command == ["/usr/bin/pmset", "-g", "batt"]
    assert options["timeout"] == 3
    assert options["check"] is True


@pytest.mark.parametrize("error", [OSError("unavailable"), subprocess.TimeoutExpired("pmset", 3),
                                 subprocess.CalledProcessError(1, "pmset")])
def test_failed_sampling_is_unknown_and_cached_without_stale_low_power(native_sample, error):
    clock, responses, calls = native_sample
    assert system.power_status() == reading()
    responses[0] = error
    clock[0] = 15.0
    expected = reading(available=False, has_battery=False, on_battery=False, charging=None, percent=None)
    assert system.power_status() == expected
    assert system.on_battery() is False
    clock[0] = 29.999
    assert system.power_status() == expected
    assert len(calls) == 2
    clock[0] = 30.0
    responses[0] = pmset("AC Power", "45", "charging")
    assert system.power_status()["charging"] is True
    assert len(calls) == 3


def test_multiple_internal_batteries_do_not_invent_aggregate_percentage(native_sample):
    _, responses, _ = native_sample
    responses[0] = pmset() + " -InternalBattery-1 (id=7654321)\t42%; discharging; present: true\n"
    status = system.power_status()
    assert status["has_battery"] is True
    assert status["on_battery"] is True
    assert status["percent"] is None
    assert not LowBatteryHelpPolicy().should_ask(status)


def test_concurrent_ui_and_runtime_readers_share_one_sample(native_sample, monkeypatch):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = []

    def run(*args, **kwargs):
        calls.append(args)
        entered.set()
        assert release.wait(5), "Test did not release the synthetic sample"
        return SimpleNamespace(stdout=pmset())

    monkeypatch.setattr(system.subprocess, "run", run)
    first = threading.Thread(target=lambda: results.append(system.power_status()))
    second = threading.Thread(target=lambda: results.append(system.on_battery()))
    first.start()
    try:
        assert entered.wait(5), "Synthetic sampler did not start"
        second.start()
    finally:
        release.set()
        first.join(5)
        if second.ident is not None:
            second.join(5)
    assert not first.is_alive() and not second.is_alive()
    assert len(calls) == 1
    assert reading() in results and True in results


@pytest.mark.parametrize("ac_line,flag,percent,expected", [
    (0, 1, 19, reading()),
    (1, 8, 38, reading(on_battery=False, charging=True, percent=38)),
    (1, 1, 100, reading(on_battery=False, percent=100)),
    (1, 128, 255, reading(has_battery=False, on_battery=False, charging=None, percent=None)),
    (255, 255, 255, reading(available=False, has_battery=False, on_battery=False, charging=None, percent=None)),
])
def test_windows_keeps_native_ac_line_boolean_without_inventing_percent(
        monkeypatch, ac_line, flag, percent, expected):
    import ctypes

    def sample(pointer):
        pointer._obj.ACLineStatus = ac_line
        pointer._obj.BatteryFlag = flag
        pointer._obj.BatteryLifePercent = percent
        return 1

    monkeypatch.setattr(system.sys, "platform", "win32")
    monkeypatch.setattr(system, "_battery_cache", None)
    monkeypatch.setattr(ctypes, "windll", SimpleNamespace(
        kernel32=SimpleNamespace(GetSystemPowerStatus=sample)), raising=False)
    assert system.power_status() == expected
    assert system.on_battery() is (ac_line == 0)


@pytest.mark.parametrize("state,expected", [
    ("Discharging", reading()), ("Charging", reading(on_battery=False, charging=True)),
    ("Full", reading(on_battery=False)), ("Unknown", reading(available=False, on_battery=False, charging=None, percent=None)),
])
def test_linux_keeps_sysfs_discharge_detection(monkeypatch, tmp_path, state, expected):
    battery = tmp_path / "BAT0"
    battery.mkdir()
    (battery / "type").write_text("Battery\n")
    (battery / "status").write_text(state + "\n")
    (battery / "capacity").write_text("19\n")
    monkeypatch.setattr(system.sys, "platform", "linux")
    monkeypatch.setattr(system, "_battery_cache", None)
    monkeypatch.setattr(system, "Path", lambda value: tmp_path)
    assert system.power_status() == expected
    assert system.on_battery() is (state == "Discharging")


@pytest.mark.parametrize("percent,expected", [(0, True), (19, True), (20, True), (21, False),
                                            (100, False), (-1, False), (101, False),
                                            (None, False), (True, False), (19.0, False), ("19", False)])
def test_help_requires_valid_known_low_discharge_percent(percent, expected):
    assert LowBatteryHelpPolicy().should_ask(reading(percent=percent)) is expected


@pytest.mark.parametrize("changes", [
    {"available": False}, {"has_battery": False}, {"on_battery": False},
    {"charging": True}, {"charging": None}, {"available": 1}, {"on_battery": 1},
])
def test_help_never_asks_for_unknown_ac_charging_or_missing_battery(changes):
    status = reading()
    status.update(changes)
    assert LowBatteryHelpPolicy().should_ask(status) is False


def test_help_deduplicates_whole_discharge_despite_threshold_or_unknown_flapping():
    policy = LowBatteryHelpPolicy()
    samples = [reading(percent=21), reading(percent=20), reading(percent=19), reading(percent=21),
               reading(available=False), reading(has_battery=False, on_battery=False, charging=None, percent=None),
               {}, reading(percent=None), reading(charging=None), reading(percent=19), reading(percent=0)]
    assert [policy.should_ask(sample) for sample in samples] == [False, True] + [False] * 9


@pytest.mark.parametrize("recharge", [reading(on_battery=False, charging=True, percent=19),
                                     reading(on_battery=False, charging=False, percent=100),
                                     reading(on_battery=False, charging=None, percent=None)])
def test_genuine_known_ac_resets_help_for_next_discharge(recharge):
    policy = LowBatteryHelpPolicy()
    assert policy.should_ask(reading()) is True
    assert policy.should_ask(recharge) is False
    assert policy.should_ask(reading()) is True
    assert policy.should_ask(reading()) is False


def test_unavailable_or_contradictory_charge_does_not_reset_help():
    policy = LowBatteryHelpPolicy()
    assert policy.should_ask(reading())
    assert not policy.should_ask(reading(available=False, on_battery=False, charging=True))
    assert not policy.should_ask(reading(on_battery=True, charging=True))
    assert not policy.should_ask(reading())


def test_consumer_transition_uses_real_sample_contract_without_ac_false_prompts(native_sample):
    clock, responses, _ = native_sample
    policy = LowBatteryHelpPolicy()
    outputs = [pmset(percent="21"), pmset(percent="20"), pmset(percent="19"),
               subprocess.TimeoutExpired("pmset", 3), pmset(percent="18"),
               pmset("AC Power", "18", "charging"), pmset(percent="19"),
               pmset("AC Power", "100", "charged"), pmset(percent="20")]
    asks = []
    for index, output in enumerate(outputs):
        clock[0] = index * 15.0
        responses[0] = output
        asks.append(policy.should_ask(system.power_status()))
    assert asks == [False, True, False, False, False, False, True, False, True]


@pytest.mark.parametrize("percent,state", [(19, "low"), (20, "low"), (21, "battery")])
def test_pure_presentation_includes_twenty_percent_without_toolkit(percent, state):
    view = power_presentation(reading(percent=percent))
    assert view.state == state
    assert f"{percent}%" in view.title and f"{percent}%" in view.compact


@pytest.mark.parametrize("sample,state", [
    ({}, "unknown"), (reading(available=False), "unknown"),
    (reading(has_battery=False), "absent"),
    (reading(on_battery=None), "unknown"), (reading(on_battery=1), "unknown"),
    (reading(on_battery=False, charging=True), "charging"),
    (reading(on_battery=False, percent=100), "full"),
    (reading(on_battery=False, percent=70), "external"),
    (reading(on_battery=False, charging=None), "external_unknown"),
])
def test_shared_presentation_truthfully_distinguishes_states(sample, state):
    assert power_presentation(sample).state == state


def test_shared_help_is_localized_and_absent_wording_is_device_neutral():
    assert charging_help_text("pt_BR")[0] == "Me coloca na tomada?"
    assert charging_help_text("en_US")[0] == "Could you plug me in?"
    assert "Mac" not in power_presentation(reading(has_battery=False)).detail


def test_pure_power_helpers_import_without_apple_apis():
    import os
    import sys
    script = """
import builtins
original = builtins.__import__
def guarded(name, *args, **kwargs):
    if name.split('.')[0] in {'AppKit', 'Quartz', 'objc', 'Foundation', 'PySide6'}:
        raise AssertionError('Pure helper imported toolkit: ' + name)
    return original(name, *args, **kwargs)
builtins.__import__ = guarded
from inklow.power import power_presentation, charging_help_text
assert power_presentation({}).state == 'unknown'
assert charging_help_text('pt_BR')[0] == 'Me coloca na tomada?'
"""
    result = subprocess.run([sys.executable, "-c", script], capture_output=True,
                            text=True, timeout=10,
                            env={**os.environ, "PYTHONPATH": os.pathsep.join(sys.path)})
    assert result.returncode == 0, result.stderr


def test_fallible_presenter_probes_without_consuming_then_commits_delivery():
    policy = LowBatteryHelpPolicy()
    for _ in range(3):
        assert policy.should_ask(reading(), delivered=False)
    assert policy.should_ask(reading())
    assert not policy.should_ask(reading(), delivered=False)


def linux_supply(root, name="BAT0", *, kind="Battery", state="Discharging",
                 percent="19", present=None, scope=None, online=None):
    """Write a temporary sysfs-style supply; None deliberately omits an attribute."""
    supply = root / name
    supply.mkdir()
    for key, value in {"type": kind, "status": state, "capacity": percent,
                       "present": present, "scope": scope, "online": online}.items():
        if value is not None:
            (supply / key).write_text(str(value) + "\n")
    return supply


@pytest.mark.parametrize("percent", ["", "unavailable", "-1", "101"])
def test_linux_missing_or_invalid_capacity_keeps_source_without_invented_percent(tmp_path, percent):
    linux_supply(tmp_path, percent=percent)
    status = system._linux_power_status(tmp_path)
    assert status == reading(percent=None)
    assert not LowBatteryHelpPolicy().should_ask(status)


def test_linux_absent_and_peripheral_batteries_are_not_system_batteries(tmp_path):
    linux_supply(tmp_path, present="0")
    linux_supply(tmp_path, "mouse", scope="Device")
    assert system._linux_power_status(tmp_path) == reading(
        has_battery=False, on_battery=False, charging=None, percent=None)


def test_linux_multi_pack_is_unknown_and_never_invents_aggregate_or_rearms(tmp_path):
    linux_supply(tmp_path)
    linux_supply(tmp_path, "BAT1", state="Charging", percent="90")
    status = system._linux_power_status(tmp_path)
    assert status["has_battery"]
    assert not status["available"]
    assert status["percent"] is None
    policy = LowBatteryHelpPolicy()
    assert policy.should_ask(reading())
    assert not policy.should_ask(status)
    assert not policy.should_ask(reading())


@pytest.mark.parametrize("state,online,available,on_battery,charging", [
    ("Discharging", "0", True, True, False),
    ("Discharging", "1", False, True, None),
    ("Charging", "0", False, False, None),
    ("Charging", "1", True, False, True),
    ("Full", "1", True, False, False),
    ("Not charging", None, False, False, None),
    ("Not charging", "1", True, False, False),
    ("Not charging", "0", True, True, False),
    ("Unknown", "1", False, False, None),
])
def test_linux_only_verified_source_states_authorize_ac_rearm(
        tmp_path, state, online, available, on_battery, charging):
    linux_supply(tmp_path, state=state)
    if online is not None:
        linux_supply(tmp_path, "AC", kind="Mains", online=online)
    status = system._linux_power_status(tmp_path)
    assert status["available"] is available
    assert status["on_battery"] is on_battery
    assert status["charging"] is charging


def test_linux_missing_status_or_unreadable_supply_is_unknown(tmp_path):
    supply = linux_supply(tmp_path)
    (supply / "status").unlink()
    status = system._linux_power_status(tmp_path)
    assert not status["available"] and status["has_battery"]
    assert status["percent"] is None


@pytest.mark.parametrize("fields,expected", [
    ("Active=yes\nLockedHint=no\nRemote=no\nType=wayland\nClass=user", True),
    ("Active=yes\nLockedHint=yes\nRemote=no\nType=wayland\nClass=user", False),
    ("Active=no\nLockedHint=no\nRemote=no\nType=x11\nClass=user", False),
    ("Active=yes\nLockedHint=no\nRemote=yes\nType=x11\nClass=user", False),
    ("Active=yes\nLockedHint=no\nRemote=no\nType=tty\nClass=user", False),
    ("Active=yes\nLockedHint=no\nRemote=no\nType=x11\nClass=greeter", False),
    ("Active=yes\nRemote=no\nType=wayland\nClass=user", None),
])
def test_linux_help_requires_explicit_local_active_unlocked_graphical_user(
        monkeypatch, fields, expected):
    monkeypatch.setattr(system.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_ID", "7")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "")
    monkeypatch.setattr(system.shutil, "which", lambda command: "/usr/bin/" + command)
    calls = []

    def run(command, **kwargs):
        calls.append((command, kwargs))
        return SimpleNamespace(stdout=fields)

    monkeypatch.setattr(system.subprocess, "run", run)
    facts = system.linux_session_display_preferences()
    assert facts["session_unlocked"] is expected
    assert facts["reduce_motion"] is None
    assert len(calls) == 1
    assert 0 < calls[0][1]["timeout"] <= .35
    assert calls[0][0][1] == "show-session"


@pytest.mark.parametrize("desktop,responses,reduced,contrast", [
    ("GNOME", ["false", "'HighContrast'"], True, True),
    ("GNOME", ["true", "'Adwaita'"], False, False),
    ("KDE", ["0"], True, None),
    ("KDE", ["0.5"], False, None),
    ("KDE", ["nan"], None, None),
    ("KDE", ["unknown"], None, None),
])
def test_linux_display_preferences_are_read_only_and_unknown_stays_static(
        monkeypatch, desktop, responses, reduced, contrast):
    monkeypatch.setattr(system.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_ID", "--invalid")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", desktop)
    monkeypatch.setattr(system.shutil, "which", lambda command: command)
    calls = []
    responses = iter(responses)

    def run(command, **kwargs):
        calls.append(command)
        return SimpleNamespace(stdout=next(responses))

    monkeypatch.setattr(system.subprocess, "run", run)
    facts = system.linux_session_display_preferences()
    assert facts["session_unlocked"] is None
    assert facts["reduce_motion"] is reduced
    assert facts["increase_contrast"] is contrast
    assert all("set" not in command and "write" not in command for command in calls)


def test_linux_session_timeout_and_cancellation_never_authorize_help(monkeypatch):
    monkeypatch.setattr(system.sys, "platform", "linux")
    monkeypatch.setenv("XDG_SESSION_ID", "7")
    monkeypatch.setenv("XDG_CURRENT_DESKTOP", "")
    monkeypatch.setattr(system.shutil, "which", lambda command: command)
    calls = []

    def run(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(system.subprocess, "run", run)
    assert system.linux_session_display_preferences()["session_unlocked"] is None
    cancel = threading.Event()
    cancel.set()
    assert system.linux_session_display_preferences(cancel=cancel)["session_unlocked"] is None
    assert len(calls) == 1
