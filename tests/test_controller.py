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
