#!/usr/bin/env python3
"""Offline tests for the 2mic LED controller and boot-pulse gate.

Runs on autumn against the Pi's LED service source with gpiozero and spidev stubbed out,
so the animation and the gating can be checked without touching the device.

    ~/hermes-voice-bridge/.venv/bin/python test_led_controller.py
"""
import asyncio
import importlib.util
import sys
import tempfile
import types
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent


def load_module():
    """Import 2mic_service.py with the Pi-only hardware modules stubbed."""
    for name in ("gpiozero", "spidev"):
        module = types.ModuleType(name)
        if name == "gpiozero":
            module.LED = lambda *a, **k: types.SimpleNamespace(on=lambda: None, off=lambda: None)
        else:
            module.SpiDev = lambda: types.SimpleNamespace(
                open=lambda *a: None, close=lambda: None, xfer2=lambda d: d
            )
        sys.modules.setdefault(name, module)

    spec = importlib.util.spec_from_file_location("led_service", HERE / "2mic_service.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeStrip:
    """Records every pixel write so the animation can be inspected."""

    def __init__(self):
        self.frames = []

    def set_pixel(self, index, red, green, blue, bright_percent=100):
        self.frames.append((index, red, green, blue, bright_percent))

    def show(self):
        pass

    def frames_for(self, index=0):
        return [f for f in self.frames if f[0] == index]


async def test_pulse_breathes_and_ends_on_cap(led):
    strip = FakeStrip()
    controller = led.LEDController(strip, 3)
    await controller.pulse(led._WHITE, label="startup", low=0, high=100, period=1.0,
                           max_seconds=0.5)
    await asyncio.sleep(0.8)

    lit = [f[4] for f in strip.frames_for() if f[1:4] != (0, 0, 0)]
    # It must have ramped up and back down, not sat at one level.
    assert max(lit) > 60, f"pulse never brightened: {lit}"
    assert min(lit) < 40, f"pulse never dimmed: {lit}"
    # The cap must end it with the strip dark, or a dead turn glows all night.
    assert strip.frames_for()[-1][1:4] == (0, 0, 0), "pulse did not finish black"
    print(f"  pulse: {len(lit)} lit frames, levels {min(lit)}..{max(lit)}, ends black")


async def test_pulse_is_gated_by_brightness_bounds(led):
    strip = FakeStrip()
    controller = led.LEDController(strip, 3)
    await controller.pulse(led._WHITE, label="startup", low=10, high=30, period=1.0,
                           max_seconds=0.4)
    await asyncio.sleep(0.6)

    # Only lit frames matter: the closing frame is RGB black, so its brightness is irrelevant.
    levels = [f[4] for f in strip.frames_for() if f[1:4] != (0, 0, 0)]
    assert levels, "no lit frames at all"
    assert min(levels) >= 10 and max(levels) <= 30, f"levels outside the requested range: {levels}"
    print(f"  bounded pulse: lit levels {min(levels)}..{max(levels)}, inside the requested 10-30")


async def test_set_color_cancels_the_pulse(led):
    strip = FakeStrip()
    controller = led.LEDController(strip, 3)
    await controller.pulse(led._WORKING, label="working", max_seconds=30)
    await asyncio.sleep(0.25)
    await controller.set_color(led._RED)
    frames_after_colour = len(strip.frames)
    await asyncio.sleep(0.4)

    assert strip.frames[-1][1:5] == (255, 0, 0, 100), f"last write was not solid red: {strip.frames[-1]}"
    assert len(strip.frames) == frames_after_colour, "pulse kept writing after a solid colour"
    print("  cancel: solid colour wins, no further animation frames")


async def test_second_pulse_replaces_the_first(led):
    strip = FakeStrip()
    controller = led.LEDController(strip, 3)
    await controller.pulse(led._WHITE, label="startup", max_seconds=30)
    await asyncio.sleep(0.2)
    await controller.pulse(led._WORKING, label="working", max_seconds=30)
    await asyncio.sleep(0.2)

    recent = strip.frames[-3:]
    assert all(f[1:4] == led._WORKING for f in recent), f"second pulse did not take over: {recent}"
    print("  replace: the working pulse took over from the startup pulse")


async def test_event_mapping(led):
    """Pin the mapping that broke: the filler must not end the pulse, the bridge must."""
    strip = FakeStrip()
    controller = led.LEDController(strip, 3)
    args = types.SimpleNamespace(pulse_low=6, pulse_high=45, pulse_period=1.8,
                                 busy_pulse_seconds=30)
    handler = object.__new__(led.LEDsEventHandler)   # bypass the transport-level base init
    handler.controller = controller
    handler.cli_args = args
    handler._red_task = None

    from wyoming.event import Event
    from wyoming.satellite import SatelliteConnected, SatelliteDisconnected, StreamingStopped

    # A stopped stream is not work: it also happens for a blip with nothing to answer.
    await handler.handle_event(StreamingStopped().event())
    assert controller.pulse_label is None, "stream stop must not start the working pulse"
    assert strip.frames[-1][1:4] == (0, 0, 0), "stream stop should be dark"

    # The bridge says it is working.
    await handler.handle_event(Event(type=led.LED_BUSY_EVENT))
    assert controller.pulse_label == "working", "led-busy did not start the working pulse"
    await asyncio.sleep(0.25)

    # The bridge says the answer is starting.
    await handler.handle_event(Event(type=led.LED_IDLE_EVENT))
    assert controller.pulse_label is None, "led-idle did not stop the pulse"
    frames = len(strip.frames)
    await asyncio.sleep(0.3)
    assert len(strip.frames) == frames, "pulse kept animating after led-idle"

    # The follow-up window: steady dim teal, deliberately not a pulse, so a person can tell
    # "waiting for you" from "working" from "off".
    await handler.handle_event(Event(type=led.LED_FOLLOWUP_EVENT))
    assert controller.pulse_label is None, "the follow-up state must not pulse"
    assert strip.frames[-1][1:4] == led._FOLLOWUP, "led-followup did not show the follow-up colour"
    followup_frames = len(strip.frames)
    await asyncio.sleep(0.3)
    assert len(strip.frames) == followup_frames, "the follow-up state kept animating"
    print("  follow-up: steady dim teal, no animation")

    # A disconnect schedules red but does not display it immediately. A reconnect cancels it.
    with patch.object(led, "DISCONNECT_RED_GRACE_SECONDS", 0.05), \
            patch.object(led, "_first_connect_this_boot", return_value=False):
        await handler.handle_event(SatelliteDisconnected().event())
        assert handler._red_task is not None, "disconnect did not schedule delayed red"
        assert strip.frames[-1][1:4] != led._RED, "disconnect showed red immediately"

        await handler.handle_event(SatelliteConnected().event())
        await asyncio.sleep(0.08)
        assert handler._red_task is None, "reconnect did not clear the delayed-red task"
        assert strip.frames[-1][1:4] != led._RED, "cancelled delayed red still appeared"

        # An outage that survives the grace period becomes visibly red.
        await handler.handle_event(SatelliteDisconnected().event())
        await asyncio.sleep(0.08)
        assert strip.frames[-1][1:4] == led._RED, "sustained outage did not show red"

    print("  mapping: stream stop dark, busy pulses, idle stops, follow-up teal, delayed host-gone red")


def test_boot_gate(led, tmp_path):
    marker = tmp_path / "pulse-marker"
    args = types.SimpleNamespace(force_startup_pulse=False, startup_pulse_window=180.0)

    with patch.object(led, "_BOOT_PULSE_MARKER", marker):
        # Uptime on a dev box is far past the window, so an ordinary restart must not pulse.
        assert led._boot_pulse_wanted(args) is False, "pulsed outside a boot"
        assert not marker.exists(), "marker written when no pulse was shown"
        print("  gate: a mid-session restart does not pulse")

        # A tiny window simulates a real boot: it pulses once and marks the boot.
        boot_args = types.SimpleNamespace(force_startup_pulse=False, startup_pulse_window=10_000_000.0)
        assert led._boot_pulse_wanted(boot_args) is True, "no pulse at boot"
        assert marker.exists(), "marker not written after pulsing"
        assert led._boot_pulse_wanted(boot_args) is False, "pulsed twice in one boot"
        print("  gate: a boot pulses once, then stays dark on a second start")

        forced = types.SimpleNamespace(force_startup_pulse=True, startup_pulse_window=180.0)
        assert led._boot_pulse_wanted(forced) is True, "force flag ignored"
        print("  gate: --force-startup-pulse overrides both guards (test mode)")


async def main():
    led = load_module()
    print("LED controller and boot gate:")
    await test_pulse_breathes_and_ends_on_cap(led)
    await test_pulse_is_gated_by_brightness_bounds(led)
    await test_set_color_cancels_the_pulse(led)
    await test_second_pulse_replaces_the_first(led)
    await test_event_mapping(led)
    with tempfile.TemporaryDirectory() as tmp:
        test_boot_gate(led, Path(tmp))
    print("all LED checks passed")


if __name__ == "__main__":
    asyncio.run(main())
