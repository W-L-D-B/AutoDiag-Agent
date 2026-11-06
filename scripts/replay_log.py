"""Independently reconstruct protocol-v1 observations from a CAN evidence log."""

from __future__ import annotations

import argparse
import json
import re
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import cantools

REASONS = (
    "bad_crc",
    "bad_length",
    "invalid_flags",
    "unknown_id",
    "invalid_payload",
    "duplicate",
    "old",
)
MESSAGE_IDS = {
    0x180: ("bms", "bms_status"),
    0x181: ("hvac", "hvac_status"),
    0x182: ("body", "body_status"),
    0x700: ("bms", "heartbeat"),
    0x701: ("hvac", "heartbeat"),
    0x702: ("body", "heartbeat"),
}

# Semantic v1 contract is independent of the caller-provided DBC. Comments and
# declaration order may change; wire layout, conversion and limits may not.
COMMON_SIGNALS = (
    ("counter", 48, 8, False, 1, 0, 255, ""),
    ("checksum", 56, 8, False, 1, 0, 255, ""),
)
DATA_SIGNALS = {
    0x180: (
        ("voltage_v", 0, 16, False, 0.1, 0, 800, "V"),
        ("current_a", 16, 16, True, 0.1, -200, 200, "A"),
        ("temperature_c", 32, 16, True, 0.1, -40, 150, "degC"),
    ),
    0x181: (
        ("cabin_temperature_c", 0, 16, True, 0.1, -40, 125, "degC"),
        ("target_temperature_c", 16, 16, True, 0.1, 16, 32, "degC"),
        ("fan_level", 32, 8, False, 1, 0, 7, ""),
        ("cabin_sensor_valid", 40, 1, False, 1, 0, 1, ""),
        # cantools represents a DBC [0|0] range as unknown bounds; the
        # application validator independently requires these bits to be zero.
        ("reserved", 41, 7, False, 1, None, None, ""),
    ),
    0x182: (
        ("speed_kph", 0, 16, False, 0.01, 0, 200, "km/h"),
        ("door_mask", 16, 8, False, 1, 0, 31, ""),
        ("light_mask", 24, 8, False, 1, 0, 7, ""),
        ("supply_voltage_v", 32, 16, False, 0.01, 0, 25, "V"),
    ),
}
MESSAGE_NAMES = {
    0x180: "BmsStatus",
    0x181: "HvacStatus",
    0x182: "BodyStatus",
    0x700: "BmsHeartbeat",
    0x701: "HvacHeartbeat",
    0x702: "BodyHeartbeat",
}


class ReplayError(ValueError):
    """A log is incomplete, malformed, or inconsistent with independent validation."""


def crc8(can_id: int, data: bytes) -> int:
    crc = 0xFF
    for value in bytes([(can_id >> 8) & 7, can_id & 0xFF]) + data:
        crc ^= value
        for _ in range(8):
            crc = ((crc << 1) ^ 0x1D) & 0xFF if crc & 0x80 else (crc << 1) & 0xFF
    return crc ^ 0xFF


def reject_counts() -> dict[str, int]:
    return dict.fromkeys(REASONS, 0)


@dataclass
class Node:
    data_time: int | None = None
    heartbeat_time: int | None = None
    data_counter: int | None = None
    heartbeat_counter: int | None = None
    uptime: int | None = None
    signals: dict[str, Any] | None = None
    rejections: dict[str, int] = field(default_factory=reject_counts)
    resynchronizations: int = 0

    def snapshot(self, now: int) -> dict[str, Any]:
        data_age = None if self.data_time is None else now - self.data_time
        heartbeat_age = (
            None if self.heartbeat_time is None else now - self.heartbeat_time
        )
        return {
            "online": heartbeat_age is not None and heartbeat_age < 1200,
            "data_status": "missing"
            if data_age is None
            else "fresh"
            if data_age < 500
            else "stale",
            "data_age_ms": data_age,
            "heartbeat_age_ms": heartbeat_age,
            "data_counter": self.data_counter,
            "heartbeat_counter": self.heartbeat_counter,
            "signals": self.signals,
            "rejections": self.rejections,
            "resynchronizations": self.resynchronizations,
        }


def integer(value: Any, name: str, low: int = 0, high: int = 0xFFFFFFFF) -> int:
    if type(value) is not int or not low <= value <= high:
        raise ReplayError(f"{name} must be an integer in [{low}, {high}]")
    return value


def payload_valid(can_id: int, data: bytes) -> bool:
    if can_id == 0x180:
        voltage, current, temperature = struct.unpack_from("<Hhh", data)
        return (
            voltage <= 8000 and -2000 <= current <= 2000 and -400 <= temperature <= 1500
        )
    if can_id == 0x181:
        cabin, target = struct.unpack_from("<hh", data)
        return (
            -400 <= cabin <= 1250
            and 160 <= target <= 320
            and data[4] <= 7
            and data[5] <= 1
        )
    if can_id == 0x182:
        speed = int.from_bytes(data[:2], "little")
        supply = int.from_bytes(data[4:6], "little")
        return speed <= 20000 and data[2] <= 31 and data[3] <= 7 and supply <= 2500
    expected_node = can_id - 0x700 + 1
    allowed_status = (1, 5) if can_id == 0x701 else (1,)
    return data[0] == expected_node and data[1] in allowed_status


def sequence_reason(previous: int | None, counter: int) -> str:
    if previous is None:
        return "accepted"
    delta = (counter - previous) & 0xFF
    return "duplicate" if delta == 0 else "accepted" if delta <= 127 else "old"


def validate_dbc(database) -> None:
    messages = database.messages
    if len(messages) != 6 or {message.frame_id for message in messages} != set(
        MESSAGE_IDS
    ):
        raise ReplayError(
            "DBC must contain exactly the six unique protocol-v1 message IDs"
        )
    for message in messages:
        can_id = message.frame_id
        if (
            message.name != MESSAGE_NAMES[can_id]
            or message.length != 8
            or message.is_extended_frame
            or message.is_fd
            or message.cycle_time != (100 if can_id < 0x700 else 250)
        ):
            raise ReplayError(f"DBC message {can_id:#x} is not protocol v1")
        if can_id in DATA_SIGNALS:
            specs = DATA_SIGNALS[can_id] + COMMON_SIGNALS
        else:
            node_id = can_id - 0x700 + 1
            specs = (
                ("node_id", 0, 8, False, 1, node_id, node_id, ""),
                ("status", 8, 8, False, 1, 1, 5 if can_id == 0x701 else 1, ""),
                ("uptime_ms", 16, 32, False, 1, 0, 0xFFFFFFFF, "ms"),
            ) + COMMON_SIGNALS
        if len(message.signals) != len(specs) or {
            signal.name for signal in message.signals
        } != {spec[0] for spec in specs}:
            raise ReplayError(f"DBC message {can_id:#x} has a different signal set")
        for name, start, length, signed, scale, minimum, maximum, unit in specs:
            signal_spec = message.get_signal_by_name(name)
            actual = (
                signal_spec.start,
                signal_spec.length,
                signal_spec.is_signed,
                signal_spec.scale,
                signal_spec.minimum,
                signal_spec.maximum,
                signal_spec.unit or "",
            )
            if actual != (start, length, signed, scale, minimum, maximum, unit) or (
                signal_spec.byte_order != "little_endian"
                or signal_spec.offset != 0
                or signal_spec.is_float
                or signal_spec.is_multiplexer
                or signal_spec.multiplexer_signal is not None
                or signal_spec.choices
            ):
                raise ReplayError(
                    f"DBC signal {message.name}.{name} is not protocol v1"
                )


class Decoder:
    def __init__(self, dbc: Path):
        try:
            self.database = cantools.database.load_file(dbc, strict=True)
        except (ValueError, cantools.database.errors.Error) as error:
            raise ReplayError(f"cannot load protocol-v1 DBC: {error}") from error
        validate_dbc(self.database)
        self.nodes = {name: Node() for name in ("bms", "hvac", "body")}
        self.global_rejections = reject_counts()

    def ingest(self, row: dict[str, Any]) -> None:
        required = {
            "raw_id",
            "id",
            "dlc",
            "data",
            "accepted",
            "reason",
            "node",
            "message",
            "counter",
        }
        if not required.issubset(row):
            raise ReplayError(
                f"frame is missing fields: {sorted(required - row.keys())}"
            )
        raw_id = integer(row.get("raw_id"), "raw_id")
        can_id = raw_id & 0x7FF
        if row.get("id") != can_id or type(row.get("id")) is not int:
            raise ReplayError("arbitration ID disagrees with raw_id")
        dlc = integer(row.get("dlc"), "dlc", high=255)
        encoded = row.get("data")
        if (
            not isinstance(encoded, str)
            or not re.fullmatch(r"[0-9A-F]*", encoded)
            or len(encoded) != 2 * min(dlc, 8)
        ):
            raise ReplayError(
                "data must contain exactly the recorded DLC in uppercase hex"
            )
        data = bytes.fromhex(encoded)
        node_name, message = MESSAGE_IDS.get(can_id, (None, None))
        counter = data[6] if node_name is not None and dlc >= 7 else None
        now = integer(row["elapsed_ms"], "elapsed_ms", high=2**63 - 1)
        node = self.nodes.get(node_name)
        if raw_id & ~0x7FF:
            reason = "invalid_flags"
        elif node is None:
            reason = "unknown_id"
        elif dlc != 8:
            reason = "bad_length"
        elif crc8(can_id, data[:7]) != data[7]:
            reason = "bad_crc"
        elif not payload_valid(can_id, data):
            reason = "invalid_payload"
        elif message == "heartbeat":
            uptime = int.from_bytes(data[2:6], "little")
            epoch_changed = node.heartbeat_time is not None and (
                now - node.heartbeat_time >= 1200 or uptime < node.uptime
            )
            reason = (
                "resynchronized"
                if epoch_changed
                else sequence_reason(node.heartbeat_counter, counter)
            )
            if reason in ("accepted", "resynchronized"):
                if epoch_changed:
                    node.data_time = node.data_counter = node.signals = None
                    node.resynchronizations += 1
                node.heartbeat_time, node.heartbeat_counter, node.uptime = (
                    now,
                    counter,
                    uptime,
                )
        else:
            reason = sequence_reason(node.data_counter, counter)
            if reason == "accepted":
                values = self.database.get_message_by_frame_id(can_id).decode(
                    data,
                    allow_truncated=False,
                    allow_excess=False,
                    decode_choices=False,
                )
                values = {
                    key: value
                    for key, value in values.items()
                    if key not in ("counter", "checksum", "reserved")
                }
                for key, value in values.items():
                    if isinstance(value, float):
                        values[key] = round(value, 2)
                if "cabin_sensor_valid" in values:
                    values["cabin_sensor_valid"] = bool(values["cabin_sensor_valid"])
                node.data_time, node.data_counter, node.signals = now, counter, values
        accepted = reason in ("accepted", "resynchronized")
        if not accepted:
            self.global_rejections[reason] += 1
            if node is not None and reason != "unknown_id":
                node.rejections[reason] += 1
        expected = {
            "accepted": accepted,
            "reason": reason,
            "node": node_name,
            "message": message,
            "counter": counter,
        }
        if type(row.get("accepted")) is not bool:
            raise ReplayError("accepted must be a boolean")
        if counter is not None and type(row.get("counter")) is not int:
            raise ReplayError("counter must be an integer")
        for name, value in expected.items():
            if row.get(name) != value:
                raise ReplayError(
                    f"recorded {name}={row.get(name)!r} disagrees with computed {value!r}"
                )


def replay_records(records, dbc: Path) -> dict[str, Any]:
    decoder = Decoder(Path(dbc))
    started = ended = False
    previous_time = 0
    for number, row in enumerate(records, 1):
        if (
            not isinstance(row, dict)
            or type(row.get("schema_version")) is not int
            or row["schema_version"] != 1
        ):
            raise ReplayError(f"row {number}: unsupported schema")
        now = integer(row.get("elapsed_ms"), "elapsed_ms", high=2**63 - 1)
        if now < previous_time or ended:
            raise ReplayError(
                f"row {number}: backwards time or record after capture_end"
            )
        previous_time = now
        kind = row.get("type")
        if kind == "capture_start":
            if started or number != 1 or now != 0:
                raise ReplayError("capture_start must be the first row at elapsed_ms=0")
            started = True
        elif not started:
            raise ReplayError("log must start with capture_start")
        elif kind == "frame":
            try:
                decoder.ingest(row)
            except ReplayError as error:
                raise ReplayError(f"row {number}: {error}") from error
        elif kind == "capture_end":
            ended = True
        else:
            raise ReplayError(f"row {number}: unknown record type")
    if not started or not ended:
        raise ReplayError("log requires both capture_start and capture_end")
    return {
        "schema_version": 1,
        "capture_state": "completed",
        "ready": False,
        "elapsed_ms": previous_time,
        "global_rejections": decoder.global_rejections,
        "nodes": {
            name: node.snapshot(previous_time) for name, node in decoder.nodes.items()
        },
    }


def reject_nonfinite(token: str):
    raise ReplayError(f"nonfinite JSON value {token}")


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ReplayError(f"duplicate JSON field {key}")
        result[key] = value
    return result


def read_records(path: Path):
    with path.open(encoding="utf-8") as stream:
        for number, line in enumerate(stream, 1):
            try:
                yield json.loads(
                    line,
                    parse_constant=reject_nonfinite,
                    object_pairs_hook=unique_object,
                )
            except json.JSONDecodeError as error:
                raise ReplayError(f"line {number}: malformed JSON") from error


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dbc", type=Path, required=True)
    parser.add_argument("--log", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    try:
        if args.output.exists():
            raise ReplayError("output already exists; choose a new evidence path")
        result = replay_records(read_records(args.log), args.dbc)
        args.output.parent.mkdir(parents=True, exist_ok=True)
        with args.output.open("x", encoding="utf-8") as stream:
            json.dump(result, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        print(f"Replay verified: {args.output}")
    except (OSError, ValueError, cantools.database.errors.Error) as error:
        parser.exit(2, f"replay error: {error}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
