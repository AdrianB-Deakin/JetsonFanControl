# Jetson Fan Control

Temperature-based on/off control for the 24V fan wired to the DSBOX-NX2's
`DIGITAL_OUT1` output (Jetson Xavier NX, L4T R35.6.3). Reads CPU/GPU/thermal
zone temperatures via [jetson-stats](https://github.com/rbonghi/jetson_stats)
(`jtop`) and switches the fan on above a threshold, off below a lower
threshold (hysteresis, to avoid rapid on/off chatter).

This is on/off switching of a digital output, not PWM speed control — see
[Hardware background](#hardware-background) below for why.

## Requirements

- pyenv
- Poetry
- jetson-stats installed **system-wide** (not just as a poetry dependency) —
  it ships a systemd service (`jtop.service`) that must already be running
  before this project's poetry-managed client library can talk to it:

  ```bash
  sudo -H pip3 install -U jetson-stats
  sudo systemctl restart jtop.service
  jtop --version
  ```

  The `jetson-stats` version pinned in [pyproject.toml](pyproject.toml) must
  match the version reported by `jtop --version` above — jtop refuses to
  connect if the client library and the running service versions differ.
  Update the pin if they don't match:

  ```bash
  poetry add jetson-stats==<version from jtop --version>
  ```

## Setup

```bash
pyenv install -s 3.8.20   # matches this project's .python-version
poetry env use $(pyenv which python)
poetry install
```

## Running

Manual run. The I2C expander driver only needs access to `/dev/i2c-8` (root,
or a user in the `i2c` group); the sysfs GPIO driver needs root, so add
`sudo` on boards that use it:

```bash
$(poetry env info --path)/bin/jetson-fan-control
```

### As a systemd service (recommended for deployment)

```bash
sudo mkdir -p /opt/jetson-fan-control
sudo cp -r . /opt/jetson-fan-control
cd /opt/jetson-fan-control
sudo poetry env use python3.8
sudo poetry install --only main
sudo cp systemd/jetson-fan-control.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl enable --now jetson-fan-control.service
systemctl status jetson-fan-control.service
journalctl -u jetson-fan-control.service -f
```

## Configuration

All settings have defaults in [jetson_fan_control/config.py](jetson_fan_control/config.py)
and can be overridden with environment variables (e.g. in the systemd unit):

| Variable | Default | Meaning |
|---|---|---|
| `FAN_CONTROL_ON_TEMP` | `55.0` | °C at/above which the fan turns on |
| `FAN_CONTROL_OFF_TEMP` | `48.0` | °C at/below which the fan turns off |
| `FAN_CONTROL_TEMP_SENSOR` | `max` | jtop sensor to use (`max`, or e.g. `CPU`, `GPU`, `thermal`) |
| `FAN_CONTROL_POLL_INTERVAL_SEC` | `2.0` | seconds between jtop reads |
| `FAN_CONTROL_DRIVER` | `auto` | `expander`, `gpio`, or `auto` (expander if one answers on I2C, else GPIO) — see [Hardware background](#hardware-background) |
| `FAN_CONTROL_I2C_BUS` | `8` | expander driver: I2C bus the I/O expander is on |
| `FAN_CONTROL_I2C_ADDRESS` | `0x38` | expander driver: I2C address of the I/O expander |
| `FAN_CONTROL_EXPANDER_PIN` | `5` | expander driver: pin wired to DIGITAL_OUT1 |
| `FAN_CONTROL_GPIO_NUMBER` | `487` | GPIO driver: sysfs GPIO number for DIGITAL_OUT1 |
| `FAN_CONTROL_GPIO_LINE_NAME` | `PY.03` | GPIO driver: sysfs directory name fallback |
| `FAN_CONTROL_FAIL_SAFE_ON_EXIT` | `1` | force fan ON on clean shutdown (safer than leaving it off) |

## Testing

```bash
poetry run pytest
```

Tests cover the hysteresis decision logic and temperature-sensor selection
in isolation, plus both fan drivers against a fake I2C bus and a fake sysfs
directory (no real hardware/jtop needed). The I2C, GPIO and jtop integration
itself can only be exercised on the actual device.

## Hardware background

Forecr DSBOX-NX2 carrier board, Jetson Xavier NX, L4T R35.6.3. The fan is a
24V ebm-papst 4414FL (~50mA draw), switched directly by the board's
`DIGITAL_OUT1` low-side digital output (I/O connector pin 11) — no relay or
PWM speed control, since the draw is well within the output's rating. Power
comes from a solar/battery rail regulated to a clean 24V by a MEAN WELL
DDR-60G-24 DC-DC converter.

Which chip drives `DIGITAL_OUT1` depends on the DSBOARD-NX2 revision, so
there are two drivers, chosen with `FAN_CONTROL_DRIVER`. The default `auto`
uses the expander if one answers on the I2C bus and the GPIO otherwise.

### Rev 1.22 or older: I2C I/O expander

`DIGITAL_OUT1` is pin P5 of a PCF8574A I/O expander at address `0x38` on I2C
bus 8. [`expander_fan.py`](jetson_fan_control/expander_fan.py) drives it
directly over I2C with `smbus2`, so no kernel driver or device-tree change
is needed. The output is active-high (pin high = fan on), and the expander
powers up with all pins high, so the fan runs from power-on until the
controller takes over.

The expander's other pins are P0/P1 = `DIGITAL_IN0`/`DIGITAL_IN1`, P4/P6 =
`DIGITAL_OUT0`/`DIGITAL_OUT2` and P2/P3/P7 = the power LED. The controller
only changes the fan's bit and leaves the rest as it finds them. To toggle
the fan by hand:

```bash
i2cset -y 8 0x38 0xdf   # fan off
i2cset -y 8 0x38 0xff   # fan on
```

### Rev 1.23 or newer: Jetson GPIO

`DIGITAL_OUT1` is Jetson GPIO `PY.03`, global sysfs GPIO **487** on L4T R35.6
(`tegra194-gpio` chip, base offset 335 + device-tree line offset 152). After
exporting, this kernel creates the sysfs directory under the device-tree
line name (`/sys/class/gpio/PY.03/`) rather than `/sys/class/gpio/gpio487/`.
[`gpio_fan.py`](jetson_fan_control/gpio_fan.py) checks both locations at
startup so it keeps working if a future kernel/L4T revision changes this
naming behavior. Sysfs GPIO numbering itself is **not stable across
JetPack/L4T versions** — if you re-flash or move to a different L4T release,
re-derive the number from `sudo cat /sys/kernel/debug/gpio` (look for the
`tegra194-gpio` chip's base offset) cross-checked against Forecr's
[jetson-gpio](https://github.com/forecr/jetson-gpio) pin tables, and update
`FAN_CONTROL_GPIO_NUMBER` / `FAN_CONTROL_GPIO_LINE_NAME` accordingly.
