# Jetson Fan Control

Temperature-based on/off control for the 24V fan wired to the DSBOX-NX2's
`DIGITAL_OUT1` output (Jetson Xavier NX, L4T R35.6). Reads CPU/GPU/thermal
zone temperatures via [jetson-stats](https://github.com/rbonghi/jetson_stats)
(`jtop`) and switches the fan on above a threshold, off below a lower
threshold (hysteresis, to avoid rapid on/off chatter).

This is on/off switching of a digital output, not PWM speed control — see
[Hardware background](#hardware-background) below for why.

## Install

```bash
sudo ./install.sh
```

The script:

1. installs anything missing (`python3.8`, `python3.8-venv`, `python3-pip`
   via apt; jetson-stats via pip) and checks `jtop.service` is running,
   reading its version. Python 3.8 is the system Python on L4T R35 (Ubuntu
   20.04); on newer Ubuntu releases it is not in the standard repositories
   and the script stops with an error. pip needs network access;
2. builds the code and a fresh virtualenv with the matching jetson-stats
   version in `/opt/jetson-fan-control.new` (plain `venv` + `pip`, so the
   service does not depend on pyenv or Poetry), and checks it all imports;
3. identifies the fan driver for this board, and offers to pulse the fan
   (off for 4 seconds, then on) so you can confirm it is the right one — if
   you answer no, it tries the other driver. On an update, the driver
   confirmed last time is kept unless you test again, so a remote update
   does not need anyone watching the fan;
4. asks for the on/off temperatures;
5. moves the new build into `/opt/jetson-fan-control` and installs the
   `jetson-fan-stats` command (see [Temperature history](#temperature-history));
6. installs and starts `jetson-fan-control.service`, enabled on boot and
   restarted automatically if it ever exits, and checks it stays running.

### Updating

Pull the new code and run `sudo ./install.sh` again; it keeps the current
settings as the defaults for its prompts, so over SSH you can press Enter
at every prompt. Without a terminal it takes every default and skips the
fan test.

What happens to a running install:

- **The fan stays under control during the build.** The new version is
  built in `/opt/jetson-fan-control.new` while the old service keeps
  running. The service is only stopped for the fan test (if you choose it)
  and for the final swap into `/opt/jetson-fan-control`.
- **A failed build changes nothing.** If anything fails or you press Ctrl-C
  before the swap, the build is deleted and, if the script had stopped the
  service, the old one is started again.
- **A failed start is rolled back.** After the swap the script waits 10
  seconds and checks the service is still running and has not restarted
  itself. If not, it shows the log and moves the previous version back,
  starting it if it was running before. The new settings file, unit file
  and `jetson-fan-stats` command stay in place; older code works with all
  of them except `jetson-fan-stats`, which needs a version with history.
- **The fan driver is kept.** The driver saved by the previous install
  becomes the default and the fan test defaults to "no", so nobody needs to
  watch the fan. The I2C check still runs; if it disagrees with the saved
  driver the script says so (e.g. after a board swap) but keeps the saved
  one unless you test. A test tries the saved driver first.
- **The temperature history is kept.** It lives in
  `/var/lib/jetson-fan-control`, which the installer never touches (see
  [Temperature history](#temperature-history)).

### Managing the service

```bash
systemctl status jetson-fan-control.service     # is it running?
journalctl -u jetson-fan-control.service -f     # live log (every fan on/off)
sudo systemctl restart jetson-fan-control.service
sudo systemctl stop jetson-fan-control.service  # leaves the fan ON
```

The driver and temperatures chosen during the install are saved to
`/etc/default/jetson-fan-control`. To change them, edit that file (any
variable from [Configuration](#configuration) can go there) and restart the
service, or re-run the script.

How it behaves:

- It starts at boot together with `jtop.service`, which it needs for
  temperature readings. Stopping or restarting `jtop.service` stops or
  restarts the fan controller too.
- If it exits for any reason, systemd restarts it after 5 seconds.
- On a clean stop it forces the fan **on** (see
  `FAN_CONTROL_FAIL_SAFE_ON_EXIT`), so a stopped controller never leaves the
  box without cooling.

To remove it:

```bash
sudo systemctl disable --now jetson-fan-control.service
sudo rm /etc/systemd/system/jetson-fan-control.service /etc/default/jetson-fan-control
sudo rm /usr/local/bin/jetson-fan-stats
sudo rm -r /opt/jetson-fan-control /var/lib/jetson-fan-control  # the latter holds the history
sudo systemctl daemon-reload
```

## Temperature history

The service records the temperature it acts on (see
`FAN_CONTROL_TEMP_SENSOR`) to an SQLite database,
`/var/lib/jetson-fan-control/history.db`. Like
[vnstat](https://humdi.net/vnstat/), it stores summaries rather than every
reading: one row per minute (kept for 30 days by default) and one row per
hour (kept forever, under 9,000 rows a year). Each row holds the min, mean
and max temperature and how much of the period the fan was on. A row is
written as each minute ends, so the current minute does not show yet.

Recording is best effort: if the database cannot be opened or written, the
service logs a warning and carries on controlling the fan. Set
`FAN_CONTROL_HISTORY_DB` to an empty value to turn it off.

`jetson-fan-stats` presents it, in local time, without needing root (it
opens the database read-only, so it cannot disturb the service):

```bash
jetson-fan-stats              # last 30 days
jetson-fan-stats minute       # last 60 minutes
jetson-fan-stats hour -n 48   # last 48 hours
jetson-fan-stats month        # last 12 months
jetson-fan-stats day --csv    # CSV, e.g. for a spreadsheet
jetson-fan-stats --db FILE    # read another database, e.g. a set-aside one
```

It reads `/etc/default/jetson-fan-control` like the service does, so it
finds the database even if `FAN_CONTROL_HISTORY_DB` was changed there.

```text
day            min     avg     max  fan on
------------------------------------------
2026-10-31    33.0    35.5    38.0     58%
2026-11-01    32.4    35.1    39.2     55%
2026-11-02    31.9    34.7    38.6     49%
(temperatures in deg C)
```

### Times and time zones

Times are stored as Unix timestamps (UTC seconds) and only converted to
dates when displayed, using the Jetson's time zone. Other tools reading the
database can show them in any zone. Hourly rows start on the UTC hour, so
in a time zone with a half-hour offset (e.g. Adelaide, UTC+9:30) each local
day or hour would include half an hour of its neighbour. Whole-hour zones
such as Melbourne are exact.

### Updates and format changes

Updating the service with `install.sh` keeps the history: the database is
in `/var/lib/jetson-fan-control`, which the installer never touches.

The database carries a format version in SQLite's `user_version` header
(`SCHEMA_VERSION` in [history.py](jetson_fan_control/history.py)). If the
service finds a database with a different version, it renames it to
`history.db.v<N>` (adding a timestamp if that name is taken) and starts a
fresh one, so old data is never deleted; the matching version of
`jetson-fan-stats` can still read it with `--db`. `jetson-fan-stats` itself
refuses a database whose version it does not match, rather than showing
wrong numbers. **Whenever the tables change shape, bump `SCHEMA_VERSION`.**

### Reading the database directly

```bash
sudo apt install sqlite3   # not installed by default on L4T
sqlite3 -readonly /var/lib/jetson-fan-control/history.db
```

The `minute` and `hour` tables have one row per period: `start` (UTC Unix
time of the period's start), `samples`, `temp_sum` (divide by `samples` for
the mean), `temp_min`, `temp_max` and `fan_on_samples` (divide by `samples`
for the fraction of the period the fan was on). For example:

```sql
SELECT datetime(start, 'unixepoch', 'localtime'), temp_sum / samples
FROM hour ORDER BY start DESC LIMIT 24;
```

## Configuration

All settings have defaults in [jetson_fan_control/config.py](jetson_fan_control/config.py)
and can be overridden with environment variables. For the service, put
them in `/etc/default/jetson-fan-control`, one `NAME=value` per line;
`install.sh` writes the driver and the two temperatures there.

| Variable | Default | Meaning |
|---|---|---|
| `FAN_CONTROL_ON_TEMP` | `35.0` | °C at/above which the fan turns on |
| `FAN_CONTROL_OFF_TEMP` | `33.0` | °C at/below which the fan turns off |
| `FAN_CONTROL_TEMP_SENSOR` | `max` | jtop sensor to use (`max`, or e.g. `CPU`, `GPU`, `thermal`) |
| `FAN_CONTROL_POLL_INTERVAL_SEC` | `2.0` | seconds between jtop reads |
| `FAN_CONTROL_DRIVER` | `auto` | `expander`, `gpio`, or `auto` (expander if one answers on I2C, else GPIO) — see [Hardware background](#hardware-background) |
| `FAN_CONTROL_I2C_BUS` | `8` | expander driver: I2C bus the I/O expander is on |
| `FAN_CONTROL_I2C_ADDRESS` | `0x38` | expander driver: I2C address of the I/O expander |
| `FAN_CONTROL_EXPANDER_PIN` | `5` | expander driver: pin wired to DIGITAL_OUT1 |
| `FAN_CONTROL_GPIO_NUMBER` | `487` | GPIO driver: sysfs GPIO number for DIGITAL_OUT1 |
| `FAN_CONTROL_GPIO_LINE_NAME` | `PY.03` | GPIO driver: sysfs directory name fallback |
| `FAN_CONTROL_FAIL_SAFE_ON_EXIT` | `1` | force fan ON on clean shutdown (safer than leaving it off) |
| `FAN_CONTROL_HISTORY_DB` | `/var/lib/jetson-fan-control/history.db` | temperature history database; empty disables recording |
| `FAN_CONTROL_HISTORY_MINUTE_DAYS` | `30` | days of per-minute history to keep (hourly history is kept forever) |

## Development setup

Only needed to work on the code; the installed service does not use any of
this. You need:

- pyenv
- Poetry 2.x (older versions cannot read this project's `pyproject.toml`).
  Poetry 2.x itself needs Python 3.10 or newer to run, and its `virtualenv`
  must be 20.26.x: newer releases can no longer create the Python 3.8
  environment this project uses.
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

  (The installed service is not affected by this pin: `install.sh` always
  installs the version that matches the running `jtop.service`.)

```bash
pyenv install -s 3.8.20   # matches this project's .python-version
poetry env use $(pyenv which python)
poetry install
```

### Running by hand

Stop the service first so two controllers do not fight over the fan. The
I2C expander driver only needs access to `/dev/i2c-8` (root, or a user in
the `i2c` group); the sysfs GPIO driver needs root, so add `sudo` on boards
that use it:

```bash
sudo systemctl stop jetson-fan-control.service
$(poetry env info --path)/bin/jetson-fan-control
```

### Testing

```bash
poetry run pytest
```

Tests cover the hysteresis decision logic and temperature-sensor selection
in isolation, both fan drivers against a fake I2C bus and a fake sysfs
directory, and the temperature history and its reports against a temporary
database (no real hardware/jtop needed). The I2C, GPIO and jtop integration
itself can only be exercised on the actual device.

## Troubleshooting

- **The fan does not switch.** Re-run `sudo ./install.sh` and use its fan
  test: the wrong driver accepts writes without error but switches nothing.
  If neither driver moves the fan, check the wiring — the output is a
  low-side switch, so fan + goes to +24V, fan - to `DIGITAL_OUT1`, and the
  24V supply's ground to a `GND_ISO` pin on the I/O connector.
- **The service is not running.** `journalctl -u jetson-fan-control.service -b`
  shows why. If it has no entries at all after a reboot, look for an
  "ordering cycle" message in `journalctl -b`: the unit must stay
  `WantedBy=jtop.service` (see the comment in
  [the unit file](systemd/jetson-fan-control.service)).
- **jtop version errors in the log.** jetson-stats was upgraded after the
  install. Re-run `sudo ./install.sh` to rebuild the service's virtualenv
  with the matching version.
- **`jetson-fan-stats` shows nothing.** Rows appear once each minute ends.
  If there are still none after a few minutes, look for "Temperature
  history" warnings in `journalctl -u jetson-fan-control.service`.
- **`jetson-fan-stats` reports a different format version.** The service
  and the command come from different versions; re-run `sudo ./install.sh`
  so both are updated together.
- **The install was rolled back.** The log shown by the script says why the
  new version did not stay running. The previous version is in place again
  and nothing else needs undoing.

## Design decisions

The choices behind the current design, and why, so future changes can keep
or revisit them deliberately.

### Fan control

- **On/off, not PWM.** The fan draws about 50mA, well within the rating of
  the board's `DIGITAL_OUT1` low-side switch, so it is switched directly
  without a relay or speed control (see
  [Hardware background](#hardware-background)).
- **Two thresholds (hysteresis).** The fan turns on at `ON_TEMP` and off
  only at the lower `OFF_TEMP`, so a temperature hovering at one threshold
  cannot switch it rapidly on and off. The config refuses an `OFF_TEMP`
  that is not below `ON_TEMP`.
- **Hottest sensor by default.** `TEMP_SENSOR=max` acts on the hottest of
  the sensors jtop reports, so whichever part of the module is working
  hardest decides.
- **Fail safe means fan on.** On a clean stop the controller turns the fan
  on. For an unattended, solar-powered box, unnecessary cooling costs far
  less than silent overheating if the service does not come back.
- **Two drivers, chosen per board.** Which chip drives `DIGITAL_OUT1`
  depends on the board revision. `auto` probes for the I2C expander, but
  `install.sh` saves an explicit driver, confirmed by the fan test where
  possible, because the wrong driver fails silently (writes succeed but
  nothing switches).

### Service and installer

- **Started by `jtop.service`, not `multi-user.target`.** jtop.service is
  ordered after multi-user.target, so a unit wanted by multi-user.target
  and ordered after jtop.service forms an ordering cycle that systemd
  resolves by dropping it from the boot. The controller also retries its
  jtop connection for 60 seconds, because at boot it starts before
  jtop.service's socket is ready.
- **Each jtop connection attempt is time-limited.** jtop creates its socket
  a few seconds before it is ready to answer, and the jtop client has no
  timeout of its own, so connecting in that window could hang the
  controller indefinitely. An attempt that takes over 15 seconds is
  interrupted (with `SIGALRM`, since a signal can break a blocking socket
  read); the controller then forces the fan on and exits, and systemd
  restarts it with a fresh connection 5 seconds later.
- **Plain `venv` + `pip` in `/opt`, not Poetry.** The service must not
  depend on developer tooling (pyenv, Poetry). Each install builds a fresh
  virtualenv so packages from older versions do not linger.
- **jetson-stats matched to the running service.** jtop refuses to connect
  when the client library and service versions differ, so the installer
  installs whatever version `jtop --version` reports rather than the pin
  in `pyproject.toml`, and checks the result before going live.
- **Build alongside, then swap.** Building in `/opt/jetson-fan-control.new`
  keeps the old service controlling the fan during the slow part (pip), and
  means a failed build (e.g. no network) never breaks a working install. A
  moved virtualenv still works with `python -m`, which is how the unit
  runs it.
- **Health check counts restarts.** With `Restart=always`, a version that
  crashes on startup looks "active" between restarts. The installer waits
  10 seconds and also checks systemd's restart counter did not move.
- **Remote updates need no one at the fan.** A driver saved by an earlier
  install is trusted over detection and the fan test defaults to "no" on
  updates, so `sudo ./install.sh` over SSH with every default is safe.

### Temperature history

- **SQLite.** It is in Python's standard library (no new dependency),
  survives power loss cleanly, can be read safely while the service writes,
  and can be queried by other tools. The default rollback journal is used
  rather than WAL so that non-root users can open the database read-only.
- **Summaries, not raw readings.** At one reading every 2 seconds, raw
  storage would be about 43,000 rows a day. Like vnstat, the service keeps
  per-minute rows for 30 days and per-hour rows forever (under 9,000 a
  year); day and month figures are worked out from the hourly rows when
  displayed. It writes once a minute, which keeps eMMC wear negligible.
- **Sums, not averages.** Rows store sample count, sum, min, max and the
  number of samples with the fan on. Sums combine exactly, so minutes add
  into hours, hours into days, and a restart part way through a minute
  merges into the same row.
- **Unix timestamps, converted on display.** Storing UTC seconds avoids
  daylight-saving ambiguity and lets any reader choose its own time zone.
  The cost is the half-hour time zone limitation described above; finer
  stored periods (e.g. 30 minutes) would remove it if ever needed.
- **Recording never affects fan control.** Database errors are logged and
  skipped, and on shutdown the fan is forced on before the last partial
  minute is saved.
- **History lives outside the install directory.** `/var/lib`, created by
  the unit's `StateDirectory=`, is where systemd services keep state;
  keeping it apart from `/opt/jetson-fan-control` means updates and
  rollbacks cannot touch it.
- **Format changes set data aside, never delete it.** A version number in
  the database lets a new version recognise an old layout and rename the
  file instead of misreading or overwriting it. Migrating old data
  automatically was left out until there is a real format change to
  migrate.

## Hardware background

Forecr DSBOX-NX2 carrier board, Jetson Xavier NX, L4T R35.6. The fan is a
24V ebm-papst 4414FL (~50mA draw), switched directly by the board's
`DIGITAL_OUT1` low-side digital output (I/O connector pin 11) — no relay or
PWM speed control, since the draw is well within the output's rating. Power
comes from a solar/battery rail regulated to a clean 24V by a MEAN WELL
DDR-60G-24 DC-DC converter.

Which chip drives `DIGITAL_OUT1` depends on the DSBOARD-NX2 revision, so
there are two drivers, chosen with `FAN_CONTROL_DRIVER`. The default `auto`
uses the expander if one answers on the I2C bus and the GPIO otherwise.
`install.sh` makes the same check and then writes the result explicitly. To
check a board yourself, run `i2cdetect -y -r 8`: an entry at `38` means it
has the expander.

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
