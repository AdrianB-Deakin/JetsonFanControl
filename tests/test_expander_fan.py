import pytest

from jetson_fan_control import expander_fan
from jetson_fan_control.expander_fan import ExpanderFan, ExpanderFanError


class FakeSMBus:
    """Stands in for smbus2.SMBus talking to a PCF8574A: one byte of pin
    latches, with the two input pins externally held low (so they read 0
    even though their latches are 1)."""

    def __init__(self, bus):
        self.latches = 0xFF
        self.writes = []

    def read_byte(self, address):
        return self.latches & 0b1111_1100

    def write_byte(self, address, value):
        self.writes.append(value)
        self.latches = value


@pytest.fixture
def fake_bus(monkeypatch):
    buses = []

    def factory(bus):
        buses.append(FakeSMBus(bus))
        return buses[-1]

    monkeypatch.setattr(expander_fan, "SMBus", factory)
    return buses


def test_set_off_then_on_changes_only_the_fan_pin(fake_bus):
    fan = ExpanderFan(i2c_bus=8, i2c_address=0x38, pin=5)
    fan.setup()

    fan.set(False)
    assert fan.get() is False
    assert fake_bus[0].latches == 0b1101_1111

    fan.set(True)
    assert fan.get() is True
    assert fake_bus[0].latches == 0b1111_1111


def test_input_pins_are_never_latched_low(fake_bus):
    fan = ExpanderFan(i2c_bus=8, i2c_address=0x38, pin=5)
    fan.setup()

    fan.set(False)
    fan.set(True)

    assert all(value & 0b0000_0011 == 0b0000_0011 for value in fake_bus[0].writes)


def test_set_before_setup_raises(fake_bus):
    fan = ExpanderFan(i2c_bus=8, i2c_address=0x38, pin=5)
    with pytest.raises(ExpanderFanError):
        fan.set(True)


def test_setup_reports_missing_expander(monkeypatch):
    class DeadBus(FakeSMBus):
        def read_byte(self, address):
            raise OSError(121, "Remote I/O error")

    monkeypatch.setattr(expander_fan, "SMBus", DeadBus)
    fan = ExpanderFan(i2c_bus=8, i2c_address=0x38, pin=5)
    with pytest.raises(ExpanderFanError):
        fan.setup()
