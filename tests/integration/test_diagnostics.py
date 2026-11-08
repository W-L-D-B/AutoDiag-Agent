"""Exercise real ECU ISO-TP services without reading injected scenario truth."""

import os
import socket
import struct
import subprocess
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.run_bench import interface_lock

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(
    os.environ.get("AUTODIAG_BUILD_DIR", Path.home() / ".cache/autodiag/build/debug")
)
NODES = {"bms": 0, "hvac": 1, "body": 2}


def raw_listener(can_id):
    listener = socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW)
    listener.setsockopt(
        socket.SOL_CAN_RAW,
        socket.CAN_RAW_FILTER,
        struct.pack("=II", can_id, 0x7FF | 0x80000000 | 0x40000000),
    )
    listener.settimeout(3)
    listener.bind(("vcan0",))
    return listener


@contextmanager
def running_ecu(tmp_path, node="bms", fault="none"):
    executable = BUILD / "autodiag-ecu"
    assert executable.is_file(), "Build C++ ECU before diagnostics integration tests"
    index = NODES[node]
    with (
        interface_lock("vcan0"),
        raw_listener(0x700 + index) as heartbeat,
        socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, socket.CAN_ISOTP) as client,
        (tmp_path / f"{node}.stderr").open("w") as errors,
    ):
        client.settimeout(0.75)
        client.bind(("vcan0", 0x7E8 + index, 0x7E0 + index))
        command = [
            str(executable),
            "--node",
            node,
            "--interface",
            "vcan0",
            "--duration-ms",
            "8000",
            "--fault",
            fault,
            "--fault-after-ms",
            "0",
            "--fault-duration-ms",
            "0",
        ]
        process = subprocess.Popen(
            command, cwd=ROOT, stdout=subprocess.DEVNULL, stderr=errors
        )
        try:
            # A heartbeat is emitted only after both raw CAN and ISO-TP sockets are bound.
            heartbeat.recv(16)
            assert process.poll() is None
            yield client
        finally:
            if process.poll() is None:
                process.terminate()
                try:
                    process.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.wait(timeout=3)


def exchange(client, request):
    client.send(bytes(request))
    return client.recv(4096)


@pytest.mark.parametrize("node", NODES)
def test_identity_uses_actual_isotp_multiframe(tmp_path, node):
    with running_ecu(tmp_path, node) as client:
        with raw_listener(0x7E8 + NODES[node]) as wire:
            assert exchange(client, [0x22, 0xF1, 0x80]) == b"\x62\xf1\x80AutoDiag/1.0"
            wire.setblocking(False)
            frames = []
            while True:
                try:
                    frames.append(wire.recv(16))
                except BlockingIOError:
                    break
            payloads = [struct.unpack("=IB3x8s", frame)[2] for frame in frames]
            assert any(payload[0] >> 4 == 1 for payload in payloads), (
                "missing ISO-TP First Frame"
            )
            assert any(payload[0] >> 4 == 2 for payload in payloads), (
                "missing ISO-TP Consecutive Frame"
            )
        assert exchange(client, [0x22, 0xF1, 0x87]) == b"\x62\xf1\x87" + node.encode()


def test_session_signal_did_negative_responses_and_keepalive(tmp_path):
    with running_ecu(tmp_path) as client:
        assert exchange(client, [0x22, 0xD1, 0x00]) == b"\x7f\x22\x31"
        assert exchange(client, [0x10, 0x03]) == b"\x50\x03\x00\xfa\x00\xc8"
        response = exchange(client, [0x22, 0xD1, 0x00])
        assert response[:3] == b"\x62\xd1\x00"
        assert 4000 <= int.from_bytes(response[3:], "little") <= 4004
        assert exchange(client, [0x3E, 0x00]) == b"\x7e\x00"
        assert exchange(client, [0x3E, 0x80]) == b"\x7f\x3e\x12"
        assert exchange(client, [0x22, 0xAB, 0xCD]) == b"\x7f\x22\x31"
        assert exchange(client, [0x22, 0xD1]) == b"\x7f\x22\x13"
        assert exchange(client, [0x99]) == b"\x7f\x99\x11"
        assert exchange(client, [0x10, 0x01])[:2] == b"\x50\x01"
        assert exchange(client, [0x22, 0xD1, 0x00]) == b"\x7f\x22\x31"


def test_clean_did_remains_available_when_can_publication_is_dropped(tmp_path):
    with running_ecu(tmp_path, fault="drop_data") as client:
        assert exchange(client, [0x10, 0x03])[0] == 0x50
        response = exchange(client, [0x22, 0xD1, 0x02])
        assert response[:3] == b"\x62\xd1\x02"
        assert 320 <= int.from_bytes(response[3:], "little", signed=True) <= 324
        assert exchange(client, [0x19, 0x02, 0xFF]) == b"\x59\x02\x09"


def test_oversized_isotp_pdu_does_not_terminate_periodic_ecu(tmp_path):
    with running_ecu(tmp_path) as client, raw_listener(0x700) as heartbeat:
        client.send(b"\x22" + bytes(4095))
        # Wait for a subsequent heartbeat, then prove the diagnostic loop still responds.
        heartbeat.recv(16)
        assert exchange(client, [0x22, 0xF1, 0x87]) == b"\x62\xf1\x87bms"


def test_bad_can_crc_does_not_corrupt_the_diagnostic_model(tmp_path):
    with running_ecu(tmp_path, fault="bad_crc") as client:
        assert exchange(client, [0x10, 0x03])[0] == 0x50
        response = exchange(client, [0x22, 0xD1, 0x00])
        assert 4000 <= int.from_bytes(response[3:], "little") <= 4004
        assert exchange(client, [0x19, 0x02, 0xFF]) == b"\x59\x02\x09"


@pytest.mark.parametrize(
    "node,fault,dtc",
    [
        ("bms", "overtemperature", b"\xa1\x00\x01"),
        ("hvac", "lin_bad_checksum", b"\xa2\x00\x01"),
    ],
)
def test_dtcs_report_actual_signal_conditions(tmp_path, node, fault, dtc):
    with running_ecu(tmp_path, node, fault) as client:
        assert exchange(client, [0x19, 0x02, 0xFF]) == b"\x59\x02\x09" + dtc + b"\x09"
        assert exchange(client, [0x19, 0x02, 0x00]) == b"\x59\x02\x09"
