import pytest

from jetson_fan_control import gpio_fan
from jetson_fan_control.gpio_fan import GpioFan, GpioFanError


@pytest.fixture
def fake_gpio_root(tmp_path, monkeypatch):
    """Point GpioFan at a throwaway directory standing in for /sys/class/gpio,
    with the pin already "exported" (as it would be after a real export)."""
    monkeypatch.setattr(gpio_fan, "_GPIO_ROOT", tmp_path)
    pin_dir = tmp_path / "gpio487"
    pin_dir.mkdir()
    (pin_dir / "direction").write_text("in")
    (pin_dir / "value").write_text("0")
    return tmp_path


def test_set_on_then_off_round_trips_through_value_file(fake_gpio_root):
    fan = GpioFan(gpio_number=487, gpio_line_name="PY.03")
    fan.setup()

    fan.set(True)
    assert fan.get() is True
    assert (fake_gpio_root / "gpio487" / "value").read_text().strip() == "1"

    fan.set(False)
    assert fan.get() is False
    assert (fake_gpio_root / "gpio487" / "value").read_text().strip() == "0"


def test_set_before_setup_raises(fake_gpio_root):
    fan = GpioFan(gpio_number=487, gpio_line_name="PY.03")
    with pytest.raises(GpioFanError):
        fan.set(True)
