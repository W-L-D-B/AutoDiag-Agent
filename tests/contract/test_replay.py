import copy
from pathlib import Path

import pytest

from scripts.replay_log import ReplayError, crc8, read_records, replay_records

ROOT = Path(__file__).resolve().parents[2]
DBC = ROOT / "config/autodiag.dbc"


def frame(can_id, wire, time_ms, *, accepted=True, reason="accepted"):
    node = {
        0x180: "bms",
        0x181: "hvac",
        0x182: "body",
        0x700: "bms",
        0x701: "hvac",
        0x702: "body",
    }.get(can_id & 0x7FF)
    message = (
        "heartbeat"
        if 0x700 <= (can_id & 0x7FF) <= 0x702
        else f"{node}_status"
        if node
        else None
    )
    return {
        "schema_version": 1,
        "type": "frame",
        "elapsed_ms": time_ms,
        "raw_id": can_id,
        "id": can_id & 0x7FF,
        "dlc": len(wire),
        "data": wire.hex().upper(),
        "accepted": accepted,
        "reason": reason,
        "node": node,
        "message": message,
        "counter": wire[6] if node and len(wire) >= 7 else None,
    }


def wire(can_id, first_seven):
    data = bytes(first_seven)
    return data + bytes([crc8(can_id, data)])


def records(*rows, end=1000):
    return [
        {"schema_version": 1, "type": "capture_start", "elapsed_ms": 0},
        *rows,
        {"schema_version": 1, "type": "capture_end", "elapsed_ms": end},
    ]


def test_golden_crc_and_independent_decode():
    assert crc8(0x180, bytes.fromhex("A00F7D00400100")) == 0x9A
    snapshot = replay_records(
        records(frame(0x180, bytes.fromhex("A00F7D004001009A"), 100), end=200), DBC
    )
    bms = snapshot["nodes"]["bms"]
    assert bms["signals"] == {
        "voltage_v": 400.0,
        "current_a": 12.5,
        "temperature_c": 32.0,
    }
    assert bms["data_status"] == "fresh"
    assert not bms["online"]
    assert bms["data_age_ms"] == 100


def test_frozen_sequence_remains_stale_even_after_expiry():
    payload = bytes.fromhex("A00F7D004001009A")
    snapshot = replay_records(
        records(
            frame(0x180, payload, 0),
            frame(0x180, payload, 700, accepted=False, reason="duplicate"),
        ),
        DBC,
    )
    assert snapshot["nodes"]["bms"]["data_status"] == "stale"
    assert snapshot["nodes"]["bms"]["data_age_ms"] == 1000
    assert snapshot["global_rejections"]["duplicate"] == 1


def test_counter_wrap_is_forward():
    a = wire(0x180, bytes.fromhex("A00F7D004001FF"))
    b = wire(0x180, bytes.fromhex("A00F7D00400100"))
    snapshot = replay_records(
        records(frame(0x180, a, 0), frame(0x180, b, 100), end=200), DBC
    )
    assert snapshot["nodes"]["bms"]["data_counter"] == 0
    assert not any(snapshot["global_rejections"].values())


def test_restart_clears_old_data():
    hb1 = wire(0x700, bytes([1, 1, 232, 3, 0, 0, 10]))
    hb2 = wire(0x700, bytes([1, 1, 0, 0, 0, 0, 0]))
    snapshot = replay_records(
        records(
            frame(0x700, hb1, 0),
            frame(0x180, bytes.fromhex("A00F7D004001009A"), 100),
            frame(0x700, hb2, 200, reason="resynchronized"),
            end=250,
        ),
        DBC,
    )
    bms = snapshot["nodes"]["bms"]
    assert bms["online"] and bms["data_status"] == "missing"
    assert bms["signals"] is None and bms["data_counter"] is None
    assert bms["resynchronizations"] == 1


@pytest.mark.parametrize(
    "field,value",
    [
        ("accepted", False),
        ("reason", "duplicate"),
        ("node", "hvac"),
        ("id", 1),
        ("counter", 5),
    ],
)
def test_replay_rejects_tampered_claims(field, value):
    row = frame(0x180, bytes.fromhex("A00F7D004001009A"), 0)
    row[field] = value
    with pytest.raises(ReplayError):
        replay_records(records(row), DBC)


def test_bad_crc_does_not_replace_good_value():
    good = bytes.fromhex("A00F7D004001009A")
    bad = good[:-1] + bytes([good[-1] ^ 1])
    result = replay_records(
        records(
            frame(0x180, good, 0),
            frame(0x180, bad, 900, accepted=False, reason="bad_crc"),
        ),
        DBC,
    )
    assert result["nodes"]["bms"]["data_age_ms"] == 1000
    assert result["nodes"]["bms"]["rejections"]["bad_crc"] == 1


@pytest.mark.parametrize(
    "mutation", ["missing_start", "missing_end", "backwards", "after_end", "bad_hex"]
)
def test_replay_rejects_incomplete_or_malformed_evidence(mutation):
    rows = records(frame(0x180, bytes.fromhex("A00F7D004001009A"), 100))
    if mutation == "missing_start":
        rows.pop(0)
    elif mutation == "missing_end":
        rows.pop()
    elif mutation == "backwards":
        rows[-1]["elapsed_ms"] = 99
    elif mutation == "after_end":
        rows.append(copy.deepcopy(rows[1]))
    else:
        rows[1]["data"] = "nothex"
    with pytest.raises(ReplayError):
        replay_records(rows, DBC)


@pytest.mark.parametrize(
    "can_id,payload,reason",
    [
        (0x80000180, bytes.fromhex("A00F7D004001009A"), "invalid_flags"),
        (0x180, bytes(6), "bad_length"),
        (0x333, bytes(8), "unknown_id"),
        (0x180, wire(0x180, bytes.fromhex("A00F7D00DD0500")), "invalid_payload"),
        (0x700, wire(0x700, bytes([2, 1, 0, 0, 0, 0, 0])), "invalid_payload"),
        (0x700, wire(0x700, bytes([1, 5, 0, 0, 0, 0, 0])), "invalid_payload"),
        (0x701, wire(0x701, bytes([2, 3, 0, 0, 0, 0, 0])), "invalid_payload"),
    ],
)
def test_independent_validation_rejects_malformed_frames(can_id, payload, reason):
    result = replay_records(
        records(frame(can_id, payload, 0, accepted=False, reason=reason)), DBC
    )
    assert result["global_rejections"][reason] == 1
    assert all(node["signals"] is None for node in result["nodes"].values())


def test_unknown_id_only_increments_global_counter():
    result = replay_records(
        records(frame(0x333, bytes(8), 0, accepted=False, reason="unknown_id")), DBC
    )
    assert result["global_rejections"]["unknown_id"] == 1
    assert all(
        node["rejections"]["unknown_id"] == 0 for node in result["nodes"].values()
    )


def test_freshness_and_online_strict_boundary():
    hb = wire(0x700, bytes([1, 1, 0, 0, 0, 0, 0]))
    data = bytes.fromhex("A00F7D004001009A")
    rows = [frame(0x700, hb, 0), frame(0x180, data, 700)]
    before = replay_records(records(*rows, end=1199), DBC)["nodes"]["bms"]
    boundary = replay_records(records(*rows, end=1200), DBC)["nodes"]["bms"]
    assert before["online"] and before["data_status"] == "fresh"
    assert not boundary["online"] and boundary["data_status"] == "stale"


def test_heartbeat_reappearance_after_timeout_resynchronizes_equal_counter():
    hb = wire(0x700, bytes([1, 1, 0, 0, 0, 0, 0]))
    hb_later = wire(0x700, bytes([1, 1, 1, 0, 0, 0, 0]))
    result = replay_records(
        records(
            frame(0x700, hb, 0),
            frame(0x180, bytes.fromhex("A00F7D004001009A"), 100),
            frame(0x700, hb_later, 1200, reason="resynchronized"),
            end=1250,
        ),
        DBC,
    )
    assert result["nodes"]["bms"]["resynchronizations"] == 1
    assert result["nodes"]["bms"]["data_status"] == "missing"


def test_old_sequence_and_bad_heartbeat_do_not_mutate_last_good():
    data = bytes.fromhex("A00F7D004001009A")
    old = wire(0x180, bytes.fromhex("A00F7D004001C8"))
    result = replay_records(
        records(
            frame(0x180, data, 0), frame(0x180, old, 900, accepted=False, reason="old")
        ),
        DBC,
    )
    assert result["nodes"]["bms"]["data_counter"] == 0
    assert result["nodes"]["bms"]["data_age_ms"] == 1000


@pytest.mark.parametrize("value", [True, 0.0])
def test_counter_metadata_requires_integer(value):
    row = frame(0x180, bytes.fromhex("A00F7D004001009A"), 0)
    row["counter"] = value
    with pytest.raises(ReplayError):
        replay_records(records(row), DBC)


@pytest.mark.parametrize(
    "line",
    ['{"bad":', '{"schema_version": NaN}', '{"schema_version":1,"schema_version":1}'],
)
def test_json_reader_rejects_malformed_and_nonfinite(tmp_path, line):
    path = tmp_path / "bad.jsonl"
    path.write_text(line)
    with pytest.raises(ReplayError):
        list(read_records(path))


def test_unknown_frame_still_requires_nullable_metadata_fields():
    row = frame(0x333, bytes(8), 0, accepted=False, reason="unknown_id")
    del row["node"]
    with pytest.raises(ReplayError):
        replay_records(records(row), DBC)


@pytest.mark.parametrize(
    "old,new",
    [
        ("(0.1,0) [0|800]", "(0.1,100) [0|800]"),
        ("(0.1,0) [0|800]", "(0.2,0) [0|800]"),
        ("voltage_v : 0|16@1+", "voltage_v : 1|15@1+"),
        ("voltage_v : 0|16@1+", "voltage_v : 0|16@1-"),
        ("voltage_v :", "wrong_voltage :"),
        ("BmsStatus: 8", "WrongStatus: 8"),
        ('[0|800] "V"', '[0|800] "kV"'),
        ('[0|800] "V"', '[0|900] "V"'),
        ("BO_ 384 100;", "BO_ 384 101;"),
    ],
)
def test_rejects_semantically_changed_dbc(tmp_path, old, new):
    source = DBC.read_text()
    assert old in source
    modified = tmp_path / "modified.dbc"
    modified.write_text(source.replace(old, new, 1))
    with pytest.raises(ReplayError):
        replay_records(records(), modified)


def test_rejects_duplicate_message_id_in_dbc(tmp_path):
    source = DBC.read_text()
    first = source[
        source.index("BO_ 384 BmsStatus:") : source.index("BO_ 385 HvacStatus:")
    ]
    modified = tmp_path / "duplicate.dbc"
    modified.write_text(source + "\n" + first.replace("BmsStatus:", "DuplicateBms:"))
    with pytest.raises(ReplayError):
        replay_records(records(), modified)


def test_accepts_dbc_nonsemantic_comment_and_declaration_order_changes(tmp_path):
    import cantools

    database = cantools.database.load_file(DBC, strict=True)
    database.messages.reverse()
    modified = tmp_path / "reordered.dbc"
    modified.write_text(
        database.as_dbc_string() + '\nCM_ "nonsemantic extra comment";\n'
    )
    result = replay_records(records(), modified)
    assert result["nodes"]["bms"]["data_status"] == "missing"
