#!/usr/bin/env bash
#
# Installs the fan controller as a systemd service that starts on boot.
#
#   sudo ./install.sh
#
# Safe to re-run: it updates the installed code and keeps existing settings
# as the defaults for the prompts. Without a terminal (e.g. piped from a
# provisioning script) it takes every default and skips the fan test.

set -euo pipefail

SERVICE=jetson-fan-control.service
INSTALL_DIR=/opt/jetson-fan-control
ENV_FILE=/etc/default/jetson-fan-control
UNIT_FILE=/etc/systemd/system/$SERVICE
SRC_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="$INSTALL_DIR/.venv"
PYTHON="$VENV/bin/python"

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

# run_python: run a Python script from stdin inside the installed project
run_python() { (cd "$INSTALL_DIR" && "$PYTHON" -); }


[[ $EUID -eq 0 ]] || die "run this as root: sudo $0"
command -v systemctl >/dev/null || die "systemd is required"

# The service must not fight the installer for the fan during the test.
if systemctl is-active --quiet "$SERVICE"; then
    step "Stopping the running $SERVICE"
    systemctl stop "$SERVICE"
fi

# ---------------------------------------------------------------------------
step "Checking jetson-stats (jtop)"
if ! command -v jtop >/dev/null; then
    echo "jtop not found - installing jetson-stats system-wide"
    command -v pip3 >/dev/null || { apt-get update && apt-get install -y python3-pip; }
    pip3 install -U jetson-stats
fi
systemctl is-active --quiet jtop.service || systemctl restart jtop.service
systemctl is-active --quiet jtop.service \
    || die "jtop.service is not running (a reboot can be needed right after installing jetson-stats)"
# The client library must be the same version as the running jtop service.
JTOP_VERSION="$(jtop --version | awk '{print $NF}')"
[[ "$JTOP_VERSION" =~ ^[0-9]+(\.[0-9]+)+$ ]] || die "could not read the jtop version"
echo "jtop.service is running, version $JTOP_VERSION"

# ---------------------------------------------------------------------------
step "Installing the project to $INSTALL_DIR"
command -v python3.8 >/dev/null || die "python3.8 not found (this project needs Python 3.8)"
python3.8 -c "import ensurepip" 2>/dev/null \
    || { apt-get update && apt-get install -y python3.8-venv; }

mkdir -p "$INSTALL_DIR"
rm -rf "$INSTALL_DIR/jetson_fan_control"
cp -r "$SRC_DIR/jetson_fan_control" "$INSTALL_DIR/"
find "$INSTALL_DIR/jetson_fan_control" -name __pycache__ -prune -exec rm -rf {} +

python3.8 -m venv "$VENV"
"$PYTHON" -m pip install --quiet --upgrade pip
"$PYTHON" -m pip install --quiet "jetson-stats==$JTOP_VERSION" "smbus2>=0.6,<0.7"
echo "Installed with jetson-stats $JTOP_VERSION"

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
if run_python >/dev/null 2>&1 <<'EOF'
from jetson_fan_control.config import Config
from jetson_fan_control.expander_fan import ExpanderFan

config = Config()
ExpanderFan(config.i2c_bus, config.i2c_address, config.expander_pin).setup()
EOF
then
    DETECTED=expander
    OTHER=gpio
    echo "Found the I2C I/O expander -> 'expander' driver"
else
    DETECTED=gpio
    OTHER=expander
    echo "No I2C I/O expander found -> 'gpio' driver"
fi

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

DRIVER="$DETECTED"
if (( INTERACTIVE )) && confirm "Test the fan now? It will stop for 4 seconds, then restart" y; then
    DRIVER=""
    for candidate in "$DETECTED" "$OTHER"; do
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
    [[ -n "$DRIVER" ]] || die "neither driver switched the fan. Check the wiring: fan + to +24V, fan - to DIGITAL_OUT1, supply ground to GND_ISO. Nothing was installed as a service."
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

set_env FAN_CONTROL_DRIVER "$DRIVER"
set_env FAN_CONTROL_ON_TEMP "$ON_TEMP"
set_env FAN_CONTROL_OFF_TEMP "$OFF_TEMP"
echo "Settings saved to $ENV_FILE"

# ---------------------------------------------------------------------------
step "Installing and starting $SERVICE"
install -m 644 "$SRC_DIR/systemd/$SERVICE" "$UNIT_FILE"
systemctl daemon-reload
# reenable, not enable: it also removes links left by an older unit file
# whose [Install] section differed.
systemctl reenable "$SERVICE"
systemctl restart "$SERVICE"

sleep 3
if ! systemctl is-active --quiet "$SERVICE"; then
    journalctl -u "$SERVICE" -n 20 --no-pager || true
    die "$SERVICE did not stay running - see the log above"
fi

cat <<EOF

The fan controller is running and will start on every boot.
  Fan on at ${ON_TEMP}C, off at ${OFF_TEMP}C, driver: $DRIVER

  Status:           systemctl status $SERVICE
  Live log:         journalctl -u $SERVICE -f
  Change settings:  edit $ENV_FILE, then: sudo systemctl restart $SERVICE
                    (or re-run this script)
EOF

