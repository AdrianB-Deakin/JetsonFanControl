"""
Sysfs GPIO control for the fan's digital output.

Used on DSBOARD-NX2 rev 1.23 or newer, where the output is a Jetson GPIO;
older revisions use the I2C I/O expander in expander_fan.py instead.

Requires root (the export/direction/value files under /sys/class/gpio are
only writable by root on stock L4T). Run the controller as root, e.g. via
the provided systemd unit.
"""

from __future__ import annotations

import logging
import time
from pathlib import Path

logger = logging.getLogger(__name__)

_GPIO_ROOT = Path("/sys/class/gpio")
_EXPORT_WAIT_ATTEMPTS = 10
_EXPORT_WAIT_DELAY_SEC = 0.1


class GpioFanError(RuntimeError):
    pass


class GpioFan:
    """Controls a single sysfs GPIO line wired as a fan on/off switch.

    Handles the sysfs naming quirk seen on this board/kernel, where an
    exported line with a device-tree name shows up as
    /sys/class/gpio/<name>/ instead of /sys/class/gpio/gpio<N>/.
    """

    def __init__(self, gpio_number: int, gpio_line_name: str):
        self._gpio_number = gpio_number
        self._gpio_line_name = gpio_line_name
        self._pin_dir: Path | None = None

    def _candidate_dirs(self):
        yield _GPIO_ROOT / f"gpio{self._gpio_number}"
        yield _GPIO_ROOT / self._gpio_line_name

    def _find_pin_dir(self) -> Path | None:
        for candidate in self._candidate_dirs():
            if candidate.is_dir():
                return candidate
        return None

    def setup(self) -> None:
        """Export the GPIO line (if needed) and set it to output mode."""
        pin_dir = self._find_pin_dir()

        if pin_dir is None:
            try:
                (_GPIO_ROOT / "export").write_text(str(self._gpio_number))
            except PermissionError as exc:
                raise GpioFanError(
                    "Permission denied writing to /sys/class/gpio/export "
                    "- the fan controller must run as root"
                ) from exc
            except OSError as exc:
                raise GpioFanError(
                    f"Failed to export gpio{self._gpio_number}: {exc}"
                ) from exc

            for _ in range(_EXPORT_WAIT_ATTEMPTS):
                pin_dir = self._find_pin_dir()
                if pin_dir is not None:
                    break
                time.sleep(_EXPORT_WAIT_DELAY_SEC)

        if pin_dir is None:
            raise GpioFanError(
                f"gpio{self._gpio_number} was exported but neither "
                f"{_GPIO_ROOT / f'gpio{self._gpio_number}'} nor "
                f"{_GPIO_ROOT / self._gpio_line_name} appeared. Check "
                f"`ls {_GPIO_ROOT}` and update gpio_number/gpio_line_name "
                f"in config.py for this kernel build."
            )

        self._pin_dir = pin_dir
        (pin_dir / "direction").write_text("out")
        logger.info("GPIO fan control ready at %s", pin_dir)

    def set(self, on: bool) -> None:
        if self._pin_dir is None:
            raise GpioFanError("setup() must be called before set()")
        (self._pin_dir / "value").write_text("1" if on else "0")

    def get(self) -> bool:
        if self._pin_dir is None:
            raise GpioFanError("setup() must be called before get()")
        return (self._pin_dir / "value").read_text().strip() == "1"
