"""
Fan on/off decision logic and the live control loop.

FanDecider is pure logic (no jtop/I2C calls) so it can be unit tested in
isolation; run() wires it up to jetson-stats and the configured fan driver.
"""

from __future__ import annotations

import logging
import signal
from typing import Optional

from jtop import jtop

from .config import Config
from .expander_fan import ExpanderFan, ExpanderFanError
from .gpio_fan import GpioFan

logger = logging.getLogger(__name__)


class FanDecider:
    """Two-threshold hysteresis: fan latches ON at on_temp, OFF at off_temp."""

    def __init__(self, on_temp: float, off_temp: float):
        self.on_temp = on_temp
        self.off_temp = off_temp
        self._fan_on: Optional[bool] = None

    @property
    def fan_on(self) -> Optional[bool]:
        return self._fan_on

    def decide(self, temp_c: float) -> bool:
        """Feed a new temperature reading, return whether the fan should be on."""
        if self._fan_on is None:
            self._fan_on = temp_c >= self.on_temp
        elif temp_c >= self.on_temp:
            self._fan_on = True
        elif temp_c <= self.off_temp:
            self._fan_on = False
        return self._fan_on


def pick_temperature(jtop_temperature: dict, sensor: str) -> float:
    """Extract a single control temperature from jtop's temperature dict."""
    online = {
        name: reading["temp"]
        for name, reading in jtop_temperature.items()
        if reading.get("online")
    }
    if not online:
        raise RuntimeError("No online temperature sensors reported by jtop")

    if sensor == "max":
        return max(online.values())

    if sensor not in online:
        raise RuntimeError(
            f"Configured TEMP_SENSOR '{sensor}' is not an online jtop sensor "
            f"(available: {sorted(online)})"
        )
    return online[sensor]


def setup_fan(config: Config):
    """Create and set up the fan driver selected by config.fan_driver."""
    expander = ExpanderFan(config.i2c_bus, config.i2c_address, config.expander_pin)
    gpio = GpioFan(config.gpio_number, config.gpio_line_name)

    if config.fan_driver == "expander":
        fan = expander
    elif config.fan_driver == "gpio":
        fan = gpio
    else:
        try:
            expander.setup()
            return expander
        except ExpanderFanError as exc:
            logger.info("No I/O expander (%s); using sysfs GPIO driver", exc)
            fan = gpio

    fan.setup()
    return fan


class _StopRequested(Exception):
    pass


def run(config: Config) -> None:
    logging.basicConfig(
        level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s"
    )

    fan = setup_fan(config)

    decider = FanDecider(on_temp=config.on_temp, off_temp=config.off_temp)

    def _handle_stop(signum, _frame):
        raise _StopRequested(signal.Signals(signum).name)

    signal.signal(signal.SIGTERM, _handle_stop)
    signal.signal(signal.SIGINT, _handle_stop)

    logger.info(
        "Starting fan control: on_temp=%.1fC off_temp=%.1fC sensor=%s interval=%.1fs",
        config.on_temp, config.off_temp, config.temp_sensor, config.poll_interval_sec,
    )

    try:
        with jtop(interval=config.poll_interval_sec) as jetson:
            while jetson.ok():
                temp_c = pick_temperature(jetson.temperature, config.temp_sensor)
                want_on = decider.decide(temp_c)

                if fan.get() != want_on:
                    fan.set(want_on)
                    logger.info(
                        "temp=%.1fC -> fan %s", temp_c, "ON" if want_on else "OFF"
                    )
                else:
                    logger.debug("temp=%.1fC -> fan unchanged (%s)", temp_c, want_on)
    except _StopRequested as exc:
        logger.info("Received %s, shutting down", exc)
    finally:
        if config.fail_safe_on_exit:
            logger.info("Fail-safe on exit: forcing fan ON")
            fan.set(True)
