"""
Fan on/off control through the carrier board's PCF8574A I2C I/O expander.

On DSBOARD-NX2 revisions up to 1.22 the isolated digital outputs are not
Jetson GPIOs: they hang off a PCF8574A on the I2C bus. The expander has no
registers - a write sets all eight pin latches at once and a read returns
the eight pin levels - so every update is a read-modify-write of one byte.

Needs read/write access to /dev/i2c-<bus> (root, or membership of the
`i2c` group).
"""

from __future__ import annotations

import logging

from smbus2 import SMBus

logger = logging.getLogger(__name__)

# P0/P1 are DIGITAL_IN0/DIGITAL_IN1. PCF8574 pins are quasi-bidirectional:
# a pin only works as an input while its latch is 1, and it reads back 0
# whenever the input is low. Writing the read value straight back would
# therefore latch a low input low for good, so these bits are always
# written as 1.
_INPUT_PINS_MASK = 0b0000_0011


class ExpanderFanError(RuntimeError):
    pass


class ExpanderFan:
    """Controls one PCF8574A output pin wired as a fan on/off switch.

    The output is active-high: pin latch 1 closes the low-side switch
    (fan on), 0 opens it (fan off).
    """

    def __init__(self, i2c_bus: int, i2c_address: int, pin: int):
        if not 0 <= pin <= 7:
            raise ValueError(f"expander pin must be 0-7, got {pin}")
        self._i2c_bus = i2c_bus
        self._i2c_address = i2c_address
        self._pin_mask = 1 << pin
        self._bus: SMBus | None = None

    def setup(self) -> None:
        """Open the I2C bus and check the expander responds."""
        try:
            self._bus = SMBus(self._i2c_bus)
            self._bus.read_byte(self._i2c_address)
        except PermissionError as exc:
            raise ExpanderFanError(
                f"Permission denied opening /dev/i2c-{self._i2c_bus} - run "
                f"as root or add the user to the `i2c` group"
            ) from exc
        except OSError as exc:
            raise ExpanderFanError(
                f"No response from I/O expander at address "
                f"0x{self._i2c_address:02x} on I2C bus {self._i2c_bus}: {exc}. "
                f"Check `i2cdetect -y -r {self._i2c_bus}` and update "
                f"i2c_bus/i2c_address in config.py for this board."
            ) from exc
        logger.info(
            "Fan control ready on I2C bus %d, expander 0x%02x",
            self._i2c_bus, self._i2c_address,
        )

    def _require_bus(self) -> SMBus:
        if self._bus is None:
            raise ExpanderFanError("setup() must be called first")
        return self._bus

    def set(self, on: bool) -> None:
        bus = self._require_bus()
        state = bus.read_byte(self._i2c_address) | _INPUT_PINS_MASK
        if on:
            state |= self._pin_mask
        else:
            state &= ~self._pin_mask
        bus.write_byte(self._i2c_address, state & 0xFF)

    def get(self) -> bool:
        bus = self._require_bus()
        return bool(bus.read_byte(self._i2c_address) & self._pin_mask)
