"""Toolkit-independent, truthful power presentation and low-battery delivery policy."""
from __future__ import annotations

from dataclasses import dataclass


class LowBatteryHelpPolicy:
    """Unknown samples never ask or rearm an already delivered request."""

    def __init__(self) -> None:
        self._asked = False

    def should_ask(self, reading: dict, *, delivered: bool = True) -> bool:
        """Observe AC rearming; consume a low request only after actual delivery.

        Existing immediate presenters use the default. Fallible presenters first
        probe with ``delivered=False`` and commit only after showing their surface.
        """
        if reading.get("available") is not True or reading.get("has_battery") is not True:
            return False
        on_battery = reading.get("on_battery")
        charging = reading.get("charging")
        if on_battery is False:
            # A known external-power reading is genuine even when charge state
            # or percentage is temporarily unavailable (for example on attach).
            self._asked = False
            return False
        if on_battery is not True or charging is not False:
            return False
        percent = reading.get("percent")
        if type(percent) is not int or not 0 <= percent <= 20 or self._asked:
            return False
        if delivered:
            self._asked = True
        return True


@dataclass(frozen=True)
class PowerPresentation:
    state: str
    title: str
    detail: str
    compact: str
    symbol: str


def power_presentation(reading: dict) -> PowerPresentation:
    """Keep unknown, absent, discharging, charging and externally powered distinct."""
    percent = reading.get("percent")
    percent = percent if type(percent) is int and 0 <= percent <= 100 else None
    level = f"{percent}%" if percent is not None else "Battery level unavailable"
    if reading.get("available") is not True:
        return PowerPresentation("unknown", "Power status unavailable", "Waiting for a verified power reading.", "?", "questionmark.circle")
    if reading.get("has_battery") is False:
        return PowerPresentation("absent", "No battery", "This device reports no battery. Battery-only options are inactive.", "AC", "powerplug")
    if (reading.get("has_battery") is not True
            or reading.get("on_battery") is not True and reading.get("on_battery") is not False):
        return PowerPresentation("unknown", "Power status unavailable", "Waiting for a verified power reading.", "?", "questionmark.circle")
    if reading.get("on_battery") is True:
        detail = "Running on battery. Your battery preferences apply now."
        state = "low" if percent is not None and percent <= 20 else "battery"
        symbol = "battery.25percent" if state == "low" else "battery.75percent"
        return PowerPresentation(state, level + " · On battery", detail, f"{percent}% ↓" if percent is not None else "? ↓", symbol)
    if reading.get("charging") is True:
        return PowerPresentation("charging", level + " · Charging", "Plugged in and charging. A little energy for your next idea.", f"{percent}% ⚡" if percent is not None else "⚡", "bolt.batteryblock")
    if reading.get("charging") is False:
        full = percent == 100
        return PowerPresentation("full" if full else "external", level + (" · Fully charged" if full else " · Plugged in"), "Connected to power; not charging. Battery-only options are inactive.", "100%" if full else (f"{percent}% · AC" if percent is not None else "AC"), "battery.100percent" if full else "powerplug")
    return PowerPresentation("external_unknown", level + " · External power", "Connected to power; charging status is unavailable.", f"{percent}% · AC" if percent is not None else "AC", "powerplug")


def charging_help_text(locale: str) -> tuple[str, str]:
    if str(locale).replace("_", "-").lower().startswith("pt-br"):
        return "Me coloca na tomada?", "A bateria está baixinha. Uma carga ajuda a gente a continuar escrevendo."
    return "Could you plug me in?", "The battery is getting low. A little charge will keep us writing."
