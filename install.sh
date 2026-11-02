#!/usr/bin/env bash
#
# Installs the fan controller as a systemd service that starts on boot.
#
#   sudo ./install.sh
#
# Also updates an existing install: re-run it after pulling new code. The new
# version is built next to the installed one while that keeps running, and
# only swapped in at the end. If the new version does not stay running, the
# previous one is put back. Existing settings become the defaults for the
# prompts. Without a terminal (e.g. piped from a provisioning script) it takes
# every default and skips the fan test.

set -euo pipefail

SERVICE=jetson-fan-control.service
INSTALL_DIR=/opt/jetson-fan-control
NEW_DIR=$INSTALL_DIR.new   # the version being built
OLD_DIR=$INSTALL_DIR.old   # the previous version, until the new one is up
ENV_FILE=/etc/default/jetson-fan-control
UNIT_FILE=/etc/systemd/system/$SERVICE
STATS_CMD=/usr/local/bin/jetson-fan-stats
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# Paths once installed (what the unit file and the stats command use) ...
PYTHON="$INSTALL_DIR/.venv/bin/python"
# ... and while building.
NEW_PYTHON="$NEW_DIR/.venv/bin/python"

step() { printf '\n==> %s\n' "$*"; }
die() { printf 'ERROR: %s\n' "$*" >&2; exit 1; }

INTERACTIVE=0
[[ -t 0 ]] && INTERACTIVE=1

# ask <prompt> <default> -> echoes the answer (the default if non-interactive)
ask() {
    local answer=""
    if (( INTERACTIVE )); then
        read -r -p "$1 [$2]: " answer
    fi
    echo "${answer:-$2}"
}

# confirm <prompt> <default y|n> -> exit status 0 for yes
confirm() {
    local answer
    answer="$(ask "$1 (y/n)" "$2")"
    [[ "$answer" =~ ^[Yy] ]]
}

# set_env <KEY> <value>: add or replace one line in $ENV_FILE
set_env() {
    touch "$ENV_FILE"
    sed -i "/^$1=/d" "$ENV_FILE"
    echo "$1=$2" >> "$ENV_FILE"
}

# run_python: run a Python script from stdin inside the version being built
run_python() { (cd "$NEW_DIR" && "$NEW_PYTHON" -); }


[[ $EUID -eq 0 ]] || die "run this as root: sudo $0"
command -v systemctl >/dev/null || die "systemd is required"

# If this script stops the running service and then fails or is interrupted
# before the new version is in place, start the old one again rather than
# leave the box without fan control.
WAS_ACTIVE=0
systemctl is-active --quiet "$SERVICE" && WAS_ACTIVE=1
STOPPED=0
SWAPPED=0
on_exit() {
    local status=$?
    rm -rf "$NEW_DIR"
    if (( status != 0 && STOPPED && !SWAPPED )); then
        echo "Restarting the previously installed $SERVICE"
        systemctl start "$SERVICE" || true
    fi
}
trap on_exit EXIT

stop_service() {
    if (( !STOPPED )) && systemctl is-active --quiet "$SERVICE"; then
        echo "Stopping the running $SERVICE"
        systemctl stop "$SERVICE"
        STOPPED=1
    fi
}

# ---------------------------------------------------------------------------
step "Checking requirements"
# apt_install <package>...: install packages, updating the index once first
APT_UPDATED=0
apt_install() {
    echo "Installing $*"
    (( APT_UPDATED )) || { apt-get update; APT_UPDATED=1; }
    DEBIAN_FRONTEND=noninteractive apt-get install -y "$@"
}

# The project is pinned to Python 3.8 (the system Python on L4T R35 /
# Ubuntu 20.04). On a newer Ubuntu it is not in the standard repositories.
command -v python3.8 >/dev/null || apt_install python3.8 \
    || die "python3.8 is not available on this system (this project needs Python 3.8)"
# python3.8-venv provides ensurepip, which `python3.8 -m venv` needs.
python3.8 -c "import ensurepip" 2>/dev/null || apt_install python3.8-venv
# sqlite3 is in libpython3.8-stdlib, which python3.8 depends on; just check.
python3.8 -c "import sqlite3" 2>/dev/null \
    || die "python3.8 has no sqlite3 module (needed for the temperature history)"
command -v pip3 >/dev/null || apt_install python3-pip
echo "Python $(python3.8 -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])') with venv, pip and sqlite3"

# ---------------------------------------------------------------------------
step "Checking jetson-stats (jtop)"
if ! command -v jtop >/dev/null; then
    echo "jtop not found - installing jetson-stats system-wide"
    pip3 install -U jetson-stats
    # jetson-stats registers jtop.service when it is installed.
    systemctl daemon-reload
fi
systemctl is-active --quiet jtop.service || systemctl restart jtop.service
systemctl is-active --quiet jtop.service \
    || die "jtop.service is not running (a reboot can be needed right after installing jetson-stats)"
# The client library must be the same version as the running jtop service.
JTOP_VERSION="$(jtop --version | awk '{print $NF}')"
[[ "$JTOP_VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || die "could not read the jtop version"
echo "jtop.service is running, version $JTOP_VERSION"

# ---------------------------------------------------------------------------
step "Building the new version in $NEW_DIR"
rm -rf "$NEW_DIR"
mkdir -p "$NEW_DIR"
cp -r "$SRC_DIR/jetson_fan_control" "$NEW_DIR/"
find "$NEW_DIR/jetson_fan_control" -name __pycache__ -prune -exec rm -rf {} +

# A fresh virtualenv each time, so packages from older versions do not linger.
python3.8 -m venv "$NEW_DIR/.venv"
"$NEW_PYTHON" -m pip install --quiet --upgrade pip
"$NEW_PYTHON" -m pip install --quiet "jetson-stats==$JTOP_VERSION" "smbus2>=0.6,<0.7"

# Check every module imports and the jtop client matches the service, before
# going anywhere near the running install.
run_python <<EOF || die "the new version failed its import check - nothing was changed"
import sqlite3, smbus2, jtop
import jetson_fan_control.controller, jetson_fan_control.report
assert jtop.__version__ == "$JTOP_VERSION", "jtop client " + jtop.__version__
EOF
echo "Built with jetson-stats $JTOP_VERSION"

# Existing settings (from a previous install) become the defaults below, and
# carry any I2C/GPIO overrides into the driver detection.
if [[ -f "$ENV_FILE" ]]; then
    set -a
    # shellcheck disable=SC1090
    . "$ENV_FILE"
    set +a
fi

# ---------------------------------------------------------------------------
step "Identifying the fan driver"
# Older DSBOARD-NX2 revisions switch the fan through an I2C I/O expander,
# newer ones through a Jetson GPIO. If the expander answers, it is the one.
# (Only a read, so it is safe while the installed service is running.)
if run_python >/dev/null 2>&1 <<'EOF'
from jetson_fan_control.config import Config
from jetson_fan_control.expander_fan import ExpanderFan

config = Config()
ExpanderFan(config.i2c_bus, config.i2c_address, config.expander_pin).setup()
EOF
then
    DETECTED=expander
    echo "Found the I2C I/O expander -> 'expander' driver"
else
    DETECTED=gpio
    echo "No I2C I/O expander found -> 'gpio' driver"
fi

# A driver confirmed by an earlier install (saved in $ENV_FILE) is trusted
# over the detection, so an update can be done without watching the fan.
SAVED="${FAN_CONTROL_DRIVER:-}"
case "$SAVED" in
    expander|gpio)
        DEFAULT="$SAVED"
        TEST_DEFAULT=n
        if [[ "$SAVED" == "$DETECTED" ]]; then
            echo "Keeping the '$SAVED' driver from $ENV_FILE"
        else
            echo "Keeping the '$SAVED' driver from $ENV_FILE (the detection disagrees;" \
                 "test the fan if the board was changed)"
        fi
        ;;
    *)
        DEFAULT="$DETECTED"
        TEST_DEFAULT=y
        ;;
esac
[[ "$DEFAULT" == expander ]] && OTHER=gpio || OTHER=expander

# pulse_fan <driver>: fan off for 4 seconds, then back on
pulse_fan() {
    FAN_CONTROL_DRIVER="$1" run_python <<'EOF'
import time

from jetson_fan_control.config import Config
from jetson_fan_control.controller import setup_fan

fan = setup_fan(Config())
fan.set(False)
time.sleep(4)
fan.set(True)
EOF
}

DRIVER="$DEFAULT"
if (( INTERACTIVE )) && confirm "Test the fan now? It will stop for 4 seconds, then restart" "$TEST_DEFAULT"; then
    # The running service would switch the fan back mid-test.
    stop_service
    DRIVER=""
    for candidate in "$DEFAULT" "$OTHER"; do
        echo "Testing the '$candidate' driver - watch the fan..."
        if ! pulse_fan "$candidate"; then
            echo "The '$candidate' driver failed to run on this board."
            continue
        fi
        if confirm "Did the fan stop and then start again?" y; then
            DRIVER="$candidate"
            break
        fi
    done
    [[ -n "$DRIVER" ]] || die "neither driver switched the fan. Check the wiring: fan + to +24V, fan - to DIGITAL_OUT1, supply ground to GND_ISO. Nothing was changed."
fi
echo "Using the '$DRIVER' driver"

# ---------------------------------------------------------------------------
step "Temperature settings"
read -r DEFAULT_ON DEFAULT_OFF < <(run_python <<'EOF'
from jetson_fan_control.config import Config

config = Config()
print(config.on_temp, config.off_temp)
EOF
) || die "could not read the default temperatures from config.py"

is_number() { [[ "$1" =~ ^[0-9]+(\.[0-9]+)?$ ]]; }

while true; do
    ON_TEMP="$(ask "Turn the fan ON at or above (deg C)" "$DEFAULT_ON")"
    OFF_TEMP="$(ask "Turn the fan OFF at or below (deg C)" "$DEFAULT_OFF")"
    if ! is_number "$ON_TEMP" || ! is_number "$OFF_TEMP"; then
        echo "Enter plain numbers, e.g. 55 or 48.5."
    elif ! awk -v on="$ON_TEMP" -v off="$OFF_TEMP" 'BEGIN { exit !(off < on) }'; then
        echo "The OFF temperature must be lower than the ON temperature."
    else
        break
    fi
    (( INTERACTIVE )) || die "invalid temperature settings: on=$ON_TEMP off=$OFF_TEMP"
done

# ---------------------------------------------------------------------------
step "Installing to $INSTALL_DIR"
stop_service
rm -rf "$OLD_DIR"
[[ -d "$INSTALL_DIR" ]] && mv "$INSTALL_DIR" "$OLD_DIR"
mv "$NEW_DIR" "$INSTALL_DIR"
SWAPPED=1

set_env FAN_CONTROL_DRIVER "$DRIVER"
set_env FAN_CONTROL_ON_TEMP "$ON_TEMP"
set_env FAN_CONTROL_OFF_TEMP "$OFF_TEMP"
echo "Settings saved to $ENV_FILE"

# Reads the same settings file as the service, so it finds the history
# database even if FAN_CONTROL_HISTORY_DB was changed there.
cat > "$STATS_CMD" <<EOF
#!/bin/sh
set -a
[ -r $ENV_FILE ] && . $ENV_FILE
set +a
PYTHONPATH=$INSTALL_DIR exec $PYTHON -m jetson_fan_control.report "\$@"
EOF
chmod 755 "$STATS_CMD"
echo "Installed $STATS_CMD"

install -m 644 "$SRC_DIR/systemd/$SERVICE" "$UNIT_FILE"
systemctl daemon-reload
# reenable, not enable: it also removes links left by an older unit file
# whose [Install] section differed.
systemctl reenable "$SERVICE"

# ---------------------------------------------------------------------------
step "Starting $SERVICE"
systemctl restart "$SERVICE"
# Restart=always hides a crash on startup as a quick restart, so wait a
# little and check it neither stopped nor restarted in the meantime.
RESTARTS="$(systemctl show -p NRestarts --value "$SERVICE")"
sleep 10
if ! systemctl is-active --quiet "$SERVICE" \
        || [[ "$(systemctl show -p NRestarts --value "$SERVICE")" != "$RESTARTS" ]]; then
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    if [[ -d "$OLD_DIR" ]]; then
        systemctl stop "$SERVICE" || true
        rm -rf "$INSTALL_DIR"
        mv "$OLD_DIR" "$INSTALL_DIR"
        if (( WAS_ACTIVE )); then
            systemctl start "$SERVICE" || true
        fi
        die "the new version did not stay running (log above). The previous version was put back."
    fi
    die "$SERVICE did not stay running - see the log above"
fi
rm -rf "$OLD_DIR"

cat <<EOF

The fan controller is running and will start on every boot.
  Fan on at ${ON_TEMP}C, off at ${OFF_TEMP}C, driver: $DRIVER

  Status:           systemctl status $SERVICE
  Live log:         journalctl -u $SERVICE -f
  Temperatures:     $(basename "$STATS_CMD") [minute|hour|day|month]
  Change settings:  edit $ENV_FILE, then: sudo systemctl restart $SERVICE
                    (or re-run this script)
EOF
