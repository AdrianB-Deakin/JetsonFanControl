"""
Configuration for the fan controller.

Every value can be overridden with an environment variable of the same name
(see the FAN_CONTROL_* prefix below) so the systemd unit can tune behaviour
without editing code, e.g.:

    Environment=FAN_CONTROL_ON_TEMP=60.0
    Environment=FAN_CONTROL_OFF_TEMP=50.0

Hardware background (Forecr DSBOX-NX2 / Xavier NX, L4T R35.6):
The 24V fan is switched by DIGITAL_OUT1 (I/O connector pin 11), a low-side
digital output. Which chip drives that output depends on the carrier board
revision, so there are two drivers (see FAN_CONTROL_DRIVER below):

- DSBOARD-NX2 rev 1.22 or older: pin P5 of a PCF8574A I/O expander at
  address 0x38 on I2C bus 8 (expander_fan.py).
- DSBOARD-NX2 rev 1.23 or newer: Jetson GPIO PY.03, global sysfs gpio number
  487 on this L4T release (gpio_fan.py). Sysfs GPIO numbering shifts between
  L4T/JetPack releases, so both the number and the line name are kept.
"""

import os
from dataclasses import dataclass


def _env_float(name: str, default: float) -> float:
    return float(os.environ.get(f"FAN_CONTROL_{name}", default))


def _env_str(name: str, default: str) -> str:
    return os.environ.get(f"FAN_CONTROL_{name}", default)


@dataclass(frozen=True)
class Config:
    # --- Fan driver (DIGITAL_OUT1 / I/O connector pin 11) ---
    # "expander", "gpio", or "auto" to use the expander if one answers on
    # the I2C bus and fall back to the sysfs GPIO otherwise.
    fan_driver: str = _env_str("DRIVER", "auto")

    # --- I/O expander driver ---
    # int(..., 0) so the address can be given as hex (0x38) or decimal.
    i2c_bus: int = int(_env_str("I2C_BUS", "8"))
    i2c_address: int = int(_env_str("I2C_ADDRESS", "0x38"), 0)
    expander_pin: int = int(_env_str("EXPANDER_PIN", "5"))

    # --- Sysfs GPIO driver ---
    gpio_number: int = int(_env_str("GPIO_NUMBER", "487"))
    gpio_line_name: str = _env_str("GPIO_LINE_NAME", "PY.03")

    # --- Temperature source ---
    # Which jtop.temperature entry to act on, or "max" to use the hottest
    # of all sensors jtop reports as online.
    temp_sensor: str = _env_str("TEMP_SENSOR", "max")

    # --- Thresholds (deg C) with hysteresis to avoid relay/output chatter ---
    on_temp: float = _env_float("ON_TEMP", 35)
    off_temp: float = _env_float("OFF_TEMP", 33.0)

    # --- Polling ---
    poll_interval_sec: float = _env_float("POLL_INTERVAL_SEC", 2.0)

    # If the controller exits cleanly (SIGTERM/SIGINT), leave the fan ON
    # rather than OFF: for an unattended, solar-powered industrial box the
    # cost of unnecessary cooling is far lower than the cost of silent
    # overheating if the service doesn't restart promptly.
    fail_safe_on_exit: bool = _env_str("FAIL_SAFE_ON_EXIT", "1") not in ("0", "false", "False")

    def __post_init__(self) -> None:
        if self.fan_driver not in ("auto", "expander", "gpio"):
            raise ValueError(
                f"fan_driver must be 'auto', 'expander' or 'gpio', "
                f"got '{self.fan_driver}'"
            )
        if self.off_temp >= self.on_temp:
            raise ValueError(
                f"off_temp ({self.off_temp}) must be lower than on_temp "
                f"({self.on_temp}) to provide hysteresis"
            )
