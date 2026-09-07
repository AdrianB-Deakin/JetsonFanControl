import pytest

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
