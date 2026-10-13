#!/usr/bin/env bash
#
# Installs the fan controller as a systemd service that starts on boot,
# then sets up the development tools (pyenv, Poetry and the project's
# Poetry environment) for the user who ran it.
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

# Development tools. Poetry 2.x needs Python >= 3.10 to run, so it gets its
# own pyenv-built interpreter. Its bundled virtualenv is pinned because newer
# releases can no longer create the Python 3.8 environment this project needs.
PROJECT_PYTHON_VERSION="$(cat "$SRC_DIR/.python-version")"
POETRY_VERSION=2.3.4
POETRY_PYTHON_VERSION=3.12.7
POETRY_VIRTUALENV_VERSION=20.26.6
DEV_USER="${SUDO_USER:-root}"
DEV_HOME="$(getent passwd "$DEV_USER" | cut -d: -f6)"
PYENV_ROOT="$DEV_HOME/.pyenv"
POETRY_HOME="$DEV_HOME/.local/share/pypoetry"
POETRY="$DEV_HOME/.local/bin/poetry"

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

# as_dev_user <command...>: run as the user who invoked sudo, with pyenv and
# Poetry on PATH
as_dev_user() {
    sudo -u "$DEV_USER" -H env \
        PYENV_ROOT="$PYENV_ROOT" \
        PATH="$PYENV_ROOT/bin:$DEV_HOME/.local/bin:/usr/local/bin:/usr/bin:/bin" \
        "$@"
}

# pyenv_install <version>: build a Python with pyenv unless already present
pyenv_install() {
    if [[ ! -x "$PYENV_ROOT/versions/$1/bin/python" ]]; then
        echo "Building Python $1 from source - this can take 10-20 minutes"
        as_dev_user pyenv install -s "$1"
    fi
    echo "Python $1 is available in pyenv"
}

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

The rest of this script only sets up development tools; the service above
is unaffected if it is interrupted or fails.
EOF

# ---------------------------------------------------------------------------
step "Installing pyenv for $DEV_USER"
apt-get update
apt-get install -y build-essential curl git libssl-dev zlib1g-dev libbz2-dev \
    libreadline-dev libsqlite3-dev libffi-dev liblzma-dev
if [[ ! -x "$PYENV_ROOT/bin/pyenv" ]]; then
    as_dev_user bash -c 'curl -fsSL https://pyenv.run | bash'
fi
if ! grep -qs 'pyenv init' "$DEV_HOME/.bashrc"; then
    as_dev_user tee -a "$DEV_HOME/.bashrc" >/dev/null <<'EOF'

# pyenv and Poetry (added by JetsonFanControl install.sh)
export PYENV_ROOT="$HOME/.pyenv"
export PATH="$PYENV_ROOT/bin:$HOME/.local/bin:$PATH"
eval "$(pyenv init - bash)"
EOF
fi
pyenv_install "$PROJECT_PYTHON_VERSION"

# ---------------------------------------------------------------------------
step "Installing Poetry $POETRY_VERSION for $DEV_USER"
if [[ "$(as_dev_user "$POETRY" --version 2>/dev/null)" == *"version $POETRY_VERSION)"* ]]; then
    echo "Poetry $POETRY_VERSION is already installed"
else
    pyenv_install "$POETRY_PYTHON_VERSION"
    # An older Poetry is kept next to the new one rather than deleted.
    if [[ -d "$POETRY_HOME/venv" ]]; then
        as_dev_user mv "$POETRY_HOME/venv" "$POETRY_HOME/venv.replaced-$(date +%Y%m%d%H%M%S)"
    fi
    as_dev_user mkdir -p "$POETRY_HOME" "$DEV_HOME/.local/bin"
    as_dev_user "$PYENV_ROOT/versions/$POETRY_PYTHON_VERSION/bin/python" -m venv "$POETRY_HOME/venv"
    as_dev_user "$POETRY_HOME/venv/bin/pip" install --quiet \
        "poetry==$POETRY_VERSION" "virtualenv==$POETRY_VIRTUALENV_VERSION"
    as_dev_user ln -sf "$POETRY_HOME/venv/bin/poetry" "$POETRY"
    as_dev_user "$POETRY" --version
fi

# ---------------------------------------------------------------------------
step "Setting up the project's Poetry environment"
as_dev_user "$POETRY" --directory "$SRC_DIR" env use \
    "$PYENV_ROOT/versions/$PROJECT_PYTHON_VERSION/bin/python"
as_dev_user "$POETRY" --directory "$SRC_DIR" install
as_dev_user "$POETRY" --directory "$SRC_DIR" run pytest -q

cat <<EOF

Done. Development tools are ready for $DEV_USER (open a new terminal first
if pyenv or poetry is not found):
  cd $SRC_DIR && poetry run pytest
EOF
