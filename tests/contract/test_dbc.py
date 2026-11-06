from pathlib import Path

import cantools
import pytest

ROOT = Path(__file__).resolve().parents[2]
DATA_IDS = {0x180, 0x181, 0x182}
ALL_IDS = DATA_IDS | {0x700, 0x701, 0x702}


@pytest.fixture(scope="module")
def database():
    return cantools.database.load_file(ROOT / "config/autodiag.dbc", strict=True)


def test_contract_has_six_classic_eight_byte_messages(database):
    assert {message.frame_id for message in database.messages} == ALL_IDS
    for message in database.messages:
        assert message.length == 8
        assert not message.is_extended_frame
        assert message.cycle_time == (100 if message.frame_id in DATA_IDS else 250)
        assert message.get_signal_by_name("counter").start == 48
        assert message.get_signal_by_name("checksum").start == 56


def test_independent_bms_known_wire_vector(database):
    wire = bytes.fromhex("A00F7D004001009A")
    values = database.get_message_by_frame_id(0x180).decode(
        wire, allow_truncated=False, allow_excess=False
    )
    assert values == {
        "voltage_v": 400.0,
        "current_a": 12.5,
        "temperature_c": 32.0,
        "counter": 0,
        "checksum": 0x9A,
    }
    assert database.encode_message(0x180, values, strict=True) == wire


@pytest.mark.parametrize("can_id", sorted(ALL_IDS))
@pytest.mark.parametrize("size", [7, 9])
def test_decode_requires_exact_length(database, can_id, size):
    with pytest.raises(cantools.database.errors.DecodeError):
        database.get_message_by_frame_id(can_id).decode(
            bytes(size), allow_truncated=False, allow_excess=False
        )


def test_signed_little_endian_and_validity_bit(database):
    values = {
        "cabin_temperature_c": -5.0,
        "target_temperature_c": 24.0,
        "fan_level": 3,
        "cabin_sensor_valid": 1,
        "reserved": 0,
        "counter": 255,
        "checksum": 0,
    }
    wire = database.encode_message(0x181, values, strict=True)
    assert wire[:6] == bytes.fromhex("CEFFF0000301")
    decoded = database.get_message_by_frame_id(0x181).decode(wire, allow_excess=False)
    assert decoded["cabin_temperature_c"] == -5.0
    assert decoded["counter"] == 255


def test_body_door_mask_includes_trunk(database):
    values = {
        "speed_kph": 12.34,
        "door_mask": 31,
        "light_mask": 7,
        "supply_voltage_v": 12.5,
        "counter": 0,
        "checksum": 0,
    }
    wire = database.encode_message(0x182, values, strict=True)
    assert wire[:6] == bytes.fromhex("D2041F07E204")


@pytest.mark.parametrize(
    "signal,value",
    [("voltage_v", 800.1), ("current_a", -200.1), ("temperature_c", 150.1)],
)
def test_bms_encoding_rejects_out_of_contract_values(database, signal, value):
    values = {
        "voltage_v": 400.0,
        "current_a": 12.5,
        "temperature_c": 32.0,
        "counter": 0,
        "checksum": 0,
    }
    values[signal] = value
    with pytest.raises(cantools.database.errors.EncodeError):
        database.encode_message(0x180, values, strict=True)
