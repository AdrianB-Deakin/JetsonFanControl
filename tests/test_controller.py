import signal

import pytest

from jetson_fan_control import controller
from jetson_fan_control.config import Config
from jetson_fan_control.controller import FanDecider, pick_temperature


class TestFanDecider:
    def test_starts_off_below_on_temp(self):
        decider = FanDecider(on_temp=55.0, off_temp=48.0)
        assert decider.decide(40.0) is False

    def test_starts_on_if_already_hot(self):
        decider = FanDecider(on_temp=55.0, off_temp=48.0)
        assert decider.decide(60.0) is True

    def test_turns_on_at_threshold(self):
        decider = FanDecider(on_temp=55.0, off_temp=48.0)
        decider.decide(40.0)
        assert decider.decide(55.0) is True

    def test_stays_on_in_hysteresis_band(self):
        decider = FanDecider(on_temp=55.0, off_temp=48.0)
        decider.decide(60.0)
        assert decider.decide(50.0) is True

    def test_turns_off_at_off_threshold(self):
        decider = FanDecider(on_temp=55.0, off_temp=48.0)
        decider.decide(60.0)
        assert decider.decide(48.0) is False

    def test_does_not_chatter_around_off_temp_alone(self):
        decider = FanDecider(on_temp=55.0, off_temp=48.0)
        decider.decide(60.0)
        decider.decide(50.0)  # still in band, stays on
        assert decider.fan_on is True
        decider.decide(47.9)
        assert decider.decide(50.0) is False  # off again, hasn't hit on_temp


class TestPickTemperature:
    def test_max_picks_hottest_online_sensor(self):
        readings = {
            "CPU": {"temp": 40.0, "online": True},
            "GPU": {"temp": 55.5, "online": True},
            "AUX": {"temp": 999.0, "online": False},
        }
        assert pick_temperature(readings, "max") == 55.5

    def test_named_sensor(self):
        readings = {
            "CPU": {"temp": 40.0, "online": True},
            "GPU": {"temp": 55.5, "online": True},
        }
        assert pick_temperature(readings, "CPU") == 40.0

    def test_unknown_sensor_raises(self):
        readings = {"CPU": {"temp": 40.0, "online": True}}
        with pytest.raises(RuntimeError):
            pick_temperature(readings, "GPU")

    def test_offline_sensor_raises_if_selected(self):
        readings = {"CPU": {"temp": 40.0, "online": False}}
        with pytest.raises(RuntimeError):
            pick_temperature(readings, "CPU")

    def test_no_online_sensors_raises(self):
        readings = {"CPU": {"temp": 40.0, "online": False}}
        with pytest.raises(RuntimeError):
            pick_temperature(readings, "max")


class _FakeFan:
    def __init__(self, *args):
        self.is_set_up = False

    def setup(self):
        self.is_set_up = True


class _MissingExpander(_FakeFan):
    def setup(self):
        raise controller.ExpanderFanError("no response")


@pytest.fixture
def fake_drivers(monkeypatch):
    monkeypatch.setattr(controller, "ExpanderFan", type("Expander", (_FakeFan,), {}))
    monkeypatch.setattr(controller, "GpioFan", type("Gpio", (_FakeFan,), {}))


@pytest.mark.parametrize(
    "driver, expected", [("expander", "Expander"), ("gpio", "Gpio"), ("auto", "Expander")]
)
def test_setup_fan_selects_configured_driver(fake_drivers, driver, expected):
    fan = controller.setup_fan(Config(fan_driver=driver))
    assert type(fan).__name__ == expected
    assert fan.is_set_up


def test_setup_fan_auto_falls_back_to_gpio_without_expander(fake_drivers, monkeypatch):
    monkeypatch.setattr(controller, "ExpanderFan", _MissingExpander)
    fan = controller.setup_fan(Config(fan_driver="auto"))
    assert type(fan).__name__ == "Gpio"
    assert fan.is_set_up


def test_unknown_fan_driver_is_rejected():
    with pytest.raises(ValueError):
        Config(fan_driver="pwm")


class _FakeJtop:
    """Stands in for jtop: start() fails until `fail_starts` attempts have run."""

    attempts = 0
    fail_starts = 0

    def __init__(self, interval):
        self.interval = interval

    def start(self):
        type(self).attempts += 1
        if type(self).attempts <= type(self).fail_starts:
            raise controller.JtopException("The jtop.service is not active.\nPlease run...")


@pytest.fixture
def fake_jtop(monkeypatch):
    fake = type("FakeJtop", (_FakeJtop,), {"attempts": 0, "fail_starts": 0})
    monkeypatch.setattr(controller, "jtop", fake)
    monkeypatch.setattr(controller.time, "sleep", lambda _s: None)
    return fake


def test_connect_jtop_retries_until_service_is_ready(fake_jtop):
    fake_jtop.fail_starts = 3
    jetson = controller.connect_jtop(interval=2.0, timeout=60.0)
    assert fake_jtop.attempts == 4
    assert jetson.interval == 2.0


def test_connect_jtop_gives_up_after_timeout(fake_jtop):
    fake_jtop.fail_starts = 10**6
    with pytest.raises(controller.JtopException):
        controller.connect_jtop(interval=2.0, timeout=0.0)
    assert fake_jtop.attempts == 1


class _FakeLiveFan:
    def __init__(self, on):
        self.on = on

    def get(self):
        return self.on

    def set(self, on):
        self.on = on


class _FakeLiveJtop:
    """Reports each temperature in `temps` once, then stops."""

    def __init__(self, temps):
        self._temps = list(temps)
        self.temperature = None

    def ok(self):
        if not self._temps:
            return False
        self.temperature = {"CPU": {"temp": self._temps.pop(0), "online": True}}
        return True


def run_with(monkeypatch, caplog, fan_on, temps):
    monkeypatch.setattr(controller, "setup_fan", lambda _config: _FakeLiveFan(fan_on))
    monkeypatch.setattr(controller, "connect_jtop", lambda _interval: _FakeLiveJtop(temps))
    caplog.set_level("INFO", logger=controller.__name__)
    controller.run(Config(on_temp=40.0, off_temp=30.0, history_db="", fail_safe_on_exit=False))
    return [r.getMessage() for r in caplog.records if r.name == controller.__name__]


def test_run_logs_connection_and_first_reading_when_fan_already_right(monkeypatch, caplog):
    # The fan powers up on; above on_temp nothing changes, but the log should
    # still show the loop started.
    messages = run_with(monkeypatch, caplog, fan_on=True, temps=[46.0, 46.5])
    assert "Connected to jtop.service" in messages
    assert "temp=46.0C -> fan stays ON" in messages
    assert not any("46.5" in m for m in messages)


def test_run_logs_first_reading_once_when_it_switches_the_fan(monkeypatch, caplog):
    messages = run_with(monkeypatch, caplog, fan_on=True, temps=[35.0, 25.0])
    assert "temp=35.0C -> fan OFF" in messages
    assert not any("stays" in m for m in messages)


def test_connect_jtop_leaves_no_alarm_behind(fake_jtop):
    controller.connect_jtop(interval=2.0)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL


def test_connect_jtop_cuts_short_a_start_that_hangs(fake_jtop, monkeypatch):
    # jtop.start() blocked forever when it connected while the service was
    # still initialising; signal.pause() blocks the same way.
    monkeypatch.setattr(fake_jtop, "start", lambda self: signal.pause())
    with pytest.raises(controller.JtopStartTimeout):
        controller.connect_jtop(interval=2.0, attempt_timeout=0.05)
    assert signal.getitimer(signal.ITIMER_REAL) == (0.0, 0.0)
    assert signal.getsignal(signal.SIGALRM) == signal.SIG_DFL
