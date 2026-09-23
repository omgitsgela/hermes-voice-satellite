#!/usr/bin/env python3
"""Controls the LEDs on the ReSpeaker 2mic HAT."""
import argparse
import asyncio
import contextlib
import logging
import time
from functools import partial
from math import ceil, cos, pi
from pathlib import Path
from typing import Optional, Tuple

import gpiozero
import spidev
from wyoming.asr import Transcript
from wyoming.event import Event
from wyoming.satellite import (
    RunSatellite,
    SatelliteConnected,
    SatelliteDisconnected,
    StreamingStarted,
    StreamingStopped,
)
from wyoming.server import AsyncEventHandler, AsyncServer
from wyoming.vad import VoiceStarted
from wyoming.wake import Detection

_LOGGER = logging.getLogger()

NUM_LEDS = 3
LEDS_GPIO = 12
RGB_MAP = {
    "rgb": [3, 2, 1],
    "rbg": [3, 1, 2],
    "grb": [2, 3, 1],
    "gbr": [2, 1, 3],
    "brg": [1, 3, 2],
    "bgr": [1, 2, 3],
}


async def main() -> None:
    """Main entry point."""
    parser = argparse.ArgumentParser()
    parser.add_argument("--uri", required=True, help="unix:// or tcp://")
    parser.add_argument("--debug", action="store_true", help="Log DEBUG messages")
    parser.add_argument(
        "--led-brightness",
        type=int,
        choices=range(1, 32),
        default=31,
        help="LED brightness (integer from 1 to 31)",
    )
    parser.add_argument(
        "--startup-pulse-seconds",
        type=float,
        default=180.0,
        help="Cap on the white startup pulse if the host never connects",
    )
    parser.add_argument(
        "--startup-pulse-window",
        type=float,
        default=180.0,
        help="Only show the startup pulse when system uptime is below this many seconds",
    )
    parser.add_argument(
        "--force-startup-pulse",
        action="store_true",
        help="Show the startup pulse even outside a real boot (for testing)",
    )
    parser.add_argument(
        "--busy-pulse-seconds",
        type=float,
        default=120.0,
        help="Cap on the working pulse if no reply ever arrives",
    )
    parser.add_argument("--pulse-low", type=int, default=6, help="Pulse dimmest level, percent")
    parser.add_argument("--pulse-high", type=int, default=45, help="Pulse brightest level, percent")
    parser.add_argument("--pulse-period", type=float, default=1.8, help="Pulse period, seconds")
    args = parser.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.debug else logging.INFO)
    _LOGGER.debug(args)

    _LOGGER.info("Ready")

    # Turn on power to LEDs
    led_power = gpiozero.LED(LEDS_GPIO, active_high=False)
    led_power.on()

    leds = APA102(num_led=NUM_LEDS, global_brightness=args.led_brightness)
    controller = LEDController(leds, NUM_LEDS)

    # A gentle white pulse while the device is still coming up, so a power cycle shows a
    # visible "starting" state instead of three dark LEDs. Gated to once per boot: this
    # service is also restarted by hand and by updates, and a crash loop must not become a
    # light show. The pulse ends when the host connects, because SatelliteConnected cancels it.
    if _boot_pulse_wanted(args):
        await controller.pulse(
            _WHITE,
            label="startup",
            low=args.pulse_low,
            high=args.pulse_high,
            period=args.pulse_period,
            max_seconds=args.startup_pulse_seconds,
        )

    # Start server
    server = AsyncServer.from_uri(args.uri)

    try:
        await server.run(partial(LEDsEventHandler, args, controller))
    except KeyboardInterrupt:
        pass
    finally:
        await controller.cancel_pulse()
        leds.cleanup()
        led_power.off()


# -----------------------------------------------------------------------------

_BLACK = (0, 0, 0)
_WHITE = (255, 255, 255)
_RED = (255, 0, 0)
_YELLOW = (255, 255, 0)
_BLUE = (0, 0, 255)
_GREEN = (0, 255, 0)
# Steady dim teal for the follow-up window. Dim on purpose: at full brightness it competes with the
# working pulse for attention, and this state means "waiting for you", not "busy".
_FOLLOWUP = (0, 64, 64)
# The host is thinking. Violet is the one colour no other state uses, so it cannot be
# confused with white (boot), blue (wake), yellow (listening), green (connected) or red
# (host gone).
_WORKING = (150, 70, 255)

# Extra event types so a command hook on this Pi can change the LEDs without a second
# network listener. The satellite's --tts-start-command and --tts-played-command run
# examples/led_cmd.py, which sends these to this service over loopback.
LED_BUSY_EVENT = "led-busy"
LED_IDLE_EVENT = "led-idle"
LED_FOLLOWUP_EVENT = "led-followup"
# The bridge asking for the microphone to be opened so a follow-up window has audio to listen to. It
# arrives here because the satellite forwards unrecognised server events to this service, which is the
# only path the bridge has to anything on this Pi. Loopback only: nothing new on the LAN.
WAKE_NOW_EVENT = "wake-now"
SHIM_CONTROL_ADDRESS = ("127.0.0.1", 10402)
SHIM_CONTROL_TIMEOUT = 4.0

# One animation frame. 0.1s is smooth enough at a 1.8s period on a Pi Zero 2 W and keeps
# the SPI traffic tiny.
_PULSE_STEP_SECONDS = 0.1

_BOOT_PULSE_MARKER = Path("/run/wyoming-led-boot-pulse")


def _boot_pulse_wanted(args: argparse.Namespace) -> bool:
    """True only for the first LED-service start of this boot."""
    if args.force_startup_pulse:
        _LOGGER.warning("Forcing the startup pulse (test mode)")
        return True

    if _BOOT_PULSE_MARKER.exists():
        _LOGGER.info("Startup pulse already shown this boot")
        return False

    # Only a real boot qualifies. This file is also restarted by hand and by updates, and
    # those must not look like a power cycle.
    try:
        uptime = float(Path("/proc/uptime").read_text().split()[0])
    except (OSError, ValueError, IndexError):
        _LOGGER.warning("Could not read uptime; skipping the startup pulse")
        return False

    if uptime > args.startup_pulse_window:
        _LOGGER.info("Uptime %.0fs is past the window, skipping startup pulse", uptime)
        return False

    try:
        _BOOT_PULSE_MARKER.touch()
    except OSError:
        _LOGGER.warning("Could not create %s", _BOOT_PULSE_MARKER)

    return True


class LEDController:
    """Single owner of the strip: either one solid colour or one running pulse.

    Every write goes through here, so an incoming event always cancels an animation
    instead of two writers fighting over the same SPI bus.
    """

    def __init__(self, leds: "APA102", num_leds: int) -> None:
        self.leds = leds
        self.num_leds = num_leds
        self._pulse_task: Optional[asyncio.Task] = None
        self.pulse_label: Optional[str] = None

    def _write(self, rgb: Tuple[int, int, int], bright_percent: int = 100) -> None:
        for i in range(self.num_leds):
            self.leds.set_pixel(i, rgb[0], rgb[1], rgb[2], bright_percent)

        self.leds.show()

    async def set_color(self, rgb: Tuple[int, int, int]) -> None:
        """Show a solid colour, ending any running pulse."""
        await self.cancel_pulse()
        self._write(rgb)

    async def cancel_pulse(self) -> None:
        task = self._pulse_task
        label = self.pulse_label
        self._pulse_task = None
        self.pulse_label = None
        if task is not None and not task.done():
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            # Logged at INFO on purpose: the start/stop pair in the journal is the only
            # evidence that the end-of-turn hook on this Pi actually fired.
            if label:
                _LOGGER.info("LED pulse stop: %s", label)

    async def pulse(
        self,
        rgb: Tuple[int, int, int],
        *,
        label: str,
        low: int = 6,
        high: int = 45,
        period: float = 1.8,
        max_seconds: float = 120.0,
    ) -> None:
        """Start a breathing pulse, replacing any pulse already running."""
        await self.cancel_pulse()
        self.pulse_label = label
        self._pulse_task = asyncio.create_task(
            self._pulse(rgb, label, low, high, period, max_seconds)
        )

    async def _pulse(
        self,
        rgb: Tuple[int, int, int],
        label: str,
        low: int,
        high: int,
        period: float,
        max_seconds: float,
    ) -> None:
        started = time.monotonic()
        _LOGGER.info("LED pulse start: %s", label)
        try:
            while True:
                elapsed = time.monotonic() - started
                if elapsed >= max_seconds:
                    # Safety net. A turn that dies without ever replying, or a stream that
                    # stops with no answer, must not leave the lights breathing all night.
                    _LOGGER.warning("LED pulse %s hit its %.0fs cap", label, max_seconds)
                    self._write(_BLACK)
                    return

                level = low + ((high - low) * (1.0 - cos(2.0 * pi * elapsed / period)) / 2.0)
                self._write(rgb, max(0, min(100, int(round(level)))))
                await asyncio.sleep(_PULSE_STEP_SECONDS)
        except asyncio.CancelledError:
            _LOGGER.debug("LED pulse stop: %s", label)
            raise


class LEDsEventHandler(AsyncEventHandler):
    """Event handler for clients."""

    def __init__(
        self,
        cli_args: argparse.Namespace,
        controller: "LEDController",
        *args,
        **kwargs,
    ) -> None:
        super().__init__(*args, **kwargs)

        self.cli_args = cli_args
        self.client_id = str(time.monotonic_ns())
        self.controller = controller

        _LOGGER.debug("Client connected: %s", self.client_id)

    async def handle_event(self, event: Event) -> bool:
        _LOGGER.debug(event)

        if StreamingStarted.is_type(event.type):
            # Logged because this is the moment the microphone actually opens. Without it, a
            # follow-up window that is silent by construction looks the same as one that is broken
            # (learned the hard way on 2026-09-22).
            _LOGGER.info("streaming started: microphone open, host is listening")
            await self.controller.set_color(_YELLOW)
        elif Detection.is_type(event.type):
            _LOGGER.info("wake detection: opening the microphone")
            await self.controller.set_color(_BLUE)
            await asyncio.sleep(1.0)  # show for 1 sec
        elif VoiceStarted.is_type(event.type):
            await self.controller.set_color(_YELLOW)
        elif Transcript.is_type(event.type):
            await self.controller.set_color(_GREEN)
            await asyncio.sleep(1.0)  # show for 1 sec
        elif StreamingStopped.is_type(event.type):
            # Dark, and NOT the start of the working pulse. The bridge owns that state and
            # publishes it explicitly, because this event also fires for a rejected blip or an
            # empty transcript, where no work follows. Starting the pulse here on 2026-09-22
            # left the lights breathing after a blip and was reverted the same day.
            await self.controller.set_color(_BLACK)
        elif RunSatellite.is_type(event.type):
            await self.controller.set_color(_BLACK)
        elif SatelliteConnected.is_type(event.type):
            # Flash. This also ends the startup pulse, which is exactly when the device is
            # genuinely ready.
            for _ in range(3):
                await self.controller.set_color(_GREEN)
                await asyncio.sleep(0.3)
                await self.controller.set_color(_BLACK)
                await asyncio.sleep(0.3)
        elif SatelliteDisconnected.is_type(event.type):
            await self.controller.set_color(_RED)
        elif event.type == WAKE_NOW_EVENT:
            # Open the microphone by asking the wake shim to inject a wake detection. Best effort:
            # a follow-up window with nothing to hear is a missed convenience, never a broken turn.
            _LOGGER.info("wake-now: asking the wake shim to open the microphone")
            try:
                reader, writer = await asyncio.wait_for(
                    asyncio.open_connection(*SHIM_CONTROL_ADDRESS), timeout=SHIM_CONTROL_TIMEOUT)
            except (OSError, asyncio.TimeoutError) as exc:
                _LOGGER.warning("wake shim not reachable: %s", exc)
            else:
                try:
                    writer.write(b"wake\n")
                    await writer.drain()
                    reply = await asyncio.wait_for(reader.readline(),
                                                   timeout=SHIM_CONTROL_TIMEOUT)
                    if reply.strip() != b"ok":
                        _LOGGER.warning("wake shim refused the request: %r", reply)
                except (OSError, asyncio.TimeoutError) as exc:
                    _LOGGER.warning("wake shim did not answer: %s", exc)
                finally:
                    writer.close()
        elif event.type in (LED_BUSY_EVENT, LED_IDLE_EVENT, LED_FOLLOWUP_EVENT):
            # Sent by this Pi's own command hooks through examples/led_cmd.py. Log them,
            # because this line is how we know the end-of-turn hook fired at all.
            _LOGGER.info("LED control event: %s", event.type)
            if event.type == LED_BUSY_EVENT:
                await self._working_pulse()
            elif event.type == LED_FOLLOWUP_EVENT:
                # Steady dim teal: the host is listening for a follow-up with no wake word. Steady
                # rather than pulsing, so it cannot be mistaken for the working pulse, and dim so it
                # reads as "waiting" rather than as an alarm.
                await self.controller.set_color(_FOLLOWUP)
            else:
                await self.controller.set_color(_BLACK)

        return True

    async def _working_pulse(self) -> None:
        """Show that the host is busy with a command."""
        await self.controller.pulse(
            _WORKING,
            label="working",
            low=self.cli_args.pulse_low,
            high=self.cli_args.pulse_high,
            period=self.cli_args.pulse_period,
            max_seconds=self.cli_args.busy_pulse_seconds,
        )


# -----------------------------------------------------------------------------


class APA102:
    """
    Driver for APA102 LEDS (aka "DotStar").
    (c) Martin Erzberger 2016-2017
    """

    # Constants
    MAX_BRIGHTNESS = 0b11111  # Safeguard: Set to a value appropriate for your setup
    LED_START = 0b11100000  # Three "1" bits, followed by 5 brightness bits

    def __init__(
        self,
        num_led,
        global_brightness,
        order="rgb",
        bus=0,
        device=1,
        max_speed_hz=8000000,
    ):
        self.num_led = num_led  # The number of LEDs in the Strip
        order = order.lower()
        self.rgb = RGB_MAP.get(order, RGB_MAP["rgb"])
        # Limit the brightness to the maximum if it's set higher
        if global_brightness > self.MAX_BRIGHTNESS:
            self.global_brightness = self.MAX_BRIGHTNESS
        else:
            self.global_brightness = global_brightness
        _LOGGER.debug("LED brightness: %d", self.global_brightness)

        self.leds = [self.LED_START, 0, 0, 0] * self.num_led  # Pixel buffer
        self.spi = spidev.SpiDev()  # Init the SPI device
        self.spi.open(bus, device)  # Open SPI port 0, slave device (CS) 1
        # Up the speed a bit, so that the LEDs are painted faster
        if max_speed_hz:
            self.spi.max_speed_hz = max_speed_hz

    def clock_start_frame(self):
        """Sends a start frame to the LED strip.

        This method clocks out a start frame, telling the receiving LED
        that it must update its own color now.
        """
        self.spi.xfer2([0] * 4)  # Start frame, 32 zero bits

    def clock_end_frame(self):
        """Sends an end frame to the LED strip.

        As explained above, dummy data must be sent after the last real colour
        information so that all of the data can reach its destination down the line.
        The delay is not as bad as with the human example above.
        It is only 1/2 bit per LED. This is because the SPI clock line
        needs to be inverted.

        Say a bit is ready on the SPI data line. The sender communicates
        this by toggling the clock line. The bit is read by the LED
        and immediately forwarded to the output data line. When the clock goes
        down again on the input side, the LED will toggle the clock up
        on the output to tell the next LED that the bit is ready.

        After one LED the clock is inverted, and after two LEDs it is in sync
        again, but one cycle behind. Therefore, for every two LEDs, one bit
        of delay gets accumulated. For 300 LEDs, 150 additional bits must be fed to
        the input of LED one so that the data can reach the last LED.

        Ultimately, we need to send additional numLEDs/2 arbitrary data bits,
        in order to trigger numLEDs/2 additional clock changes. This driver
        sends zeroes, which has the benefit of getting LED one partially or
        fully ready for the next update to the strip. An optimized version
        of the driver could omit the "clockStartFrame" method if enough zeroes have
        been sent as part of "clockEndFrame".
        """

        self.spi.xfer2([0xFF] * 4)

        # Round up num_led/2 bits (or num_led/16 bytes)
        # for _ in range((self.num_led + 15) // 16):
        #    self.spi.xfer2([0x00])

    def set_pixel(self, led_num, red, green, blue, bright_percent=100):
        """Sets the color of one pixel in the LED stripe.

        The changed pixel is not shown yet on the Stripe, it is only
        written to the pixel buffer. Colors are passed individually.
        If brightness is not set the global brightness setting is used.
        """
        if led_num < 0:
            return  # Pixel is invisible, so ignore
        if led_num >= self.num_led:
            return  # again, invisible

        # Calculate pixel brightness as a percentage of the
        # defined global_brightness. Round up to nearest integer
        # as we expect some brightness unless set to 0
        brightness = int(ceil(bright_percent * self.global_brightness / 100.0))

        # LED startframe is three "1" bits, followed by 5 brightness bits
        ledstart = (brightness & 0b00011111) | self.LED_START

        start_index = 4 * led_num
        self.leds[start_index] = ledstart
        self.leds[start_index + self.rgb[0]] = red
        self.leds[start_index + self.rgb[1]] = green
        self.leds[start_index + self.rgb[2]] = blue

    def set_pixel_rgb(self, led_num, rgb_color, bright_percent=100):
        """Sets the color of one pixel in the LED stripe.

        The changed pixel is not shown yet on the Stripe, it is only
        written to the pixel buffer.
        Colors are passed combined (3 bytes concatenated)
        If brightness is not set the global brightness setting is used.
        """
        self.set_pixel(
            led_num,
            (rgb_color & 0xFF0000) >> 16,
            (rgb_color & 0x00FF00) >> 8,
            rgb_color & 0x0000FF,
            bright_percent,
        )

    def rotate(self, positions=1):
        """Rotate the LEDs by the specified number of positions.

        Treating the internal LED array as a circular buffer, rotate it by
        the specified number of positions. The number could be negative,
        which means rotating in the opposite direction.
        """
        cutoff = 4 * (positions % self.num_led)
        self.leds = self.leds[cutoff:] + self.leds[:cutoff]

    def show(self):
        """Sends the content of the pixel buffer to the strip.

        Todo: More than 1024 LEDs requires more than one xfer operation.
        """
        self.clock_start_frame()
        # xfer2 kills the list, unfortunately. So it must be copied first
        # SPI takes up to 4096 Integers. So we are fine for up to 1024 LEDs.
        data = list(self.leds)
        while data:
            self.spi.xfer2(data[:32])
            data = data[32:]
        self.clock_end_frame()

    def cleanup(self):
        """Release the SPI device; Call this method at the end"""

        self.spi.close()  # Close SPI port


# -----------------------------------------------------------------------------

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass
