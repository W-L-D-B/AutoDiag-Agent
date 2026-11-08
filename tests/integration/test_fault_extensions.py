"""Cross-channel fault evidence for old counters and incorrect publication scaling."""

import json
import os
import socket
import struct
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from pathlib import Path

from scripts.replay_log import crc8
from scripts.run_bench import interface_lock

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(
    os.environ.get("AUTODIAG_BUILD_DIR", Path.home() / ".cache/autodiag/build/debug")
)


def run_scenario(tmp_path, scenario, counter_start=0):
    output = tmp_path / scenario
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/run_bench.py"),
            "--scenario",
            scenario,
            "--duration-ms",
            "3000",
            "--counter-start",
            str(counter_start),
            "--build-dir",
            str(BUILD),
            "--output",
            str(output),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=15,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = json.loads((output / "snapshot.json").read_text())
    frames = [
        row
        for line in (output / "frames.jsonl").read_text().splitlines()
        if (row := json.loads(line))["type"] == "frame"
    ]
    return snapshot, frames


def test_old_sequence_is_stale_and_old_even_when_counter_wraps(tmp_path):
    # Tick10 wraps 246+10 to0; the fault publishes254 after last accepted255.
    snapshot, frames = run_scenario(tmp_path, "old_bms_sequence", counter_start=246)
    bms = snapshot["nodes"]["bms"]
    assert bms["online"] and bms["data_status"] == "stale"
    assert bms["rejections"]["old"] > 0
    assert bms["rejections"]["duplicate"] == 0
    assert bms["data_counter"] == 255
    old_frames = [
        row for row in frames if row["id"] == 0x180 and row["reason"] == "old"
    ]
    assert old_frames
    assert {row["counter"] for row in old_frames} == {254}
    for row in old_frames:
        data = bytes.fromhex(row["data"])
        assert data[7] == crc8(0x180, data[:7])


def test_wrong_scale_remains_protocol_valid_and_online(tmp_path):
    snapshot, _frames = run_scenario(tmp_path, "wrong_bms_scale")
    bms = snapshot["nodes"]["bms"]
    assert bms["online"] and bms["data_status"] == "fresh"
    assert bms["signals"]["voltage_v"] == 40.0
    assert 12.5 <= bms["signals"]["current_a"] <= 12.9
    assert 32.0 <= bms["signals"]["temperature_c"] <= 32.4
    assert not any(bms["rejections"].values())


def stop_owned(process):
    if process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=3)


@contextmanager
def scaled_bms(tmp_path):
    with (
        interface_lock("vcan0"),
        socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as listener,
        socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, socket.CAN_ISOTP) as client,
        (tmp_path / "ecu.stderr").open("w") as errors,
    ):
        listener.setsockopt(
            socket.SOL_CAN_RAW,
            socket.CAN_RAW_FILTER,
            struct.pack("=II", 0x180, 0x7FF | 0x80000000 | 0x40000000),
        )
        listener.settimeout(3)
        listener.bind(("vcan0",))
        client.settimeout(1)
        client.bind(("vcan0", 0x7E8, 0x7E0))
        process = subprocess.Popen(
            [
                str(BUILD / "autodiag-ecu"),
                "--node",
                "bms",
                "--interface",
                "vcan0",
                "--duration-ms",
                "8000",
                "--fault",
                "wrong_scale",
                "--fault-after-ms",
                "0",
                "--fault-duration-ms",
                "0",
            ],
            cwd=ROOT,
            stdout=subprocess.DEVNULL,
            stderr=errors,
        )
        try:
            frame = struct.unpack("=IB3x8s", listener.recv(16))
            assert process.poll() is None
            yield frame[2], client
        finally:
            stop_owned(process)


def test_wrong_scale_does_not_change_internal_did_or_create_dtc(tmp_path):
    with scaled_bms(tmp_path) as (data, client):
        assert int.from_bytes(data[:2], "little") == 400
        assert data[7] == crc8(0x180, data[:7])
        client.send(b"\x10\x03")
        assert client.recv(4096) == b"\x50\x03\x00\xfa\x00\xc8"
        client.send(b"\x22\xd1\x00")
        reply = client.recv(4096)
        assert reply[:3] == b"\x62\xd1\x00"
        assert 4000 <= int.from_bytes(reply[3:], "little") <= 4004
        client.send(b"\x19\x02\xff")
        assert client.recv(4096) == b"\x59\x02\x09"


def test_old_sequence_recovers_after_fault_window_ends(tmp_path):
    with interface_lock("vcan0"):
        snapshot = tmp_path / "snapshot.json"
        log = tmp_path / "frames.jsonl"
        monitor = subprocess.Popen(
            [
                str(BUILD / "autodiag-monitor"),
                "--interface",
                "vcan0",
                "--duration-ms",
                "1600",
                "--snapshot",
                str(snapshot),
                "--log",
                str(log),
            ],
            cwd=ROOT,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        ecu = None
        try:
            deadline = time.monotonic() + 5
            event = threading.Event()
            while True:
                assert monitor.poll() is None, monitor.communicate()
                try:
                    ready = json.loads(snapshot.read_text()).get("ready") is True
                except (FileNotFoundError, json.JSONDecodeError):
                    ready = False
                if ready:
                    break
                assert time.monotonic() < deadline, "monitor readiness timed out"
                event.wait(0.01)
            ecu = subprocess.Popen(
                [
                    str(BUILD / "autodiag-ecu"),
                    "--node",
                    "bms",
                    "--interface",
                    "vcan0",
                    "--duration-ms",
                    "2000",
                    "--fault",
                    "old_sequence",
                    "--fault-after-ms",
                    "400",
                    "--fault-duration-ms",
                    "400",
                ],
                cwd=ROOT,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.PIPE,
            )
            stdout, stderr = monitor.communicate(timeout=6)
            assert monitor.returncode == 0, stdout + stderr
            bms = json.loads(snapshot.read_text())["nodes"]["bms"]
            assert bms["online"] and bms["data_status"] == "fresh"
            assert bms["rejections"]["old"] > 0
            assert bms["data_counter"] >= 8
        finally:
            if ecu is not None:
                stop_owned(ecu)
                if ecu.stderr is not None:
                    ecu.stderr.close()
            stop_owned(monitor)
