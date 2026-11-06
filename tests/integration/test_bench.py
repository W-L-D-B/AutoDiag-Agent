import json
import os
import signal
import socket
import struct
import subprocess
import sys
import threading
import time
from pathlib import Path

import cantools
import pytest

from scripts.run_bench import interface_lock

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(
    os.environ.get(
        "AUTODIAG_BUILD_DIR", str(Path.home() / ".cache/autodiag/build/debug")
    )
)
SCENARIOS = (
    "baseline",
    "drop_bms_data",
    "corrupt_hvac_crc",
    "body_sequence_freeze",
    "bms_overtemperature",
    "lin_bad_checksum",
    "node_exit",
)


@pytest.fixture(scope="module", autouse=True)
def require_built_programs():
    assert (BUILD / "autodiag-ecu").is_file(), (
        "C++ ECU must be built before integration tests"
    )
    assert (BUILD / "autodiag-monitor").is_file(), (
        "C++ monitor must be built before integration tests"
    )


def run_cli(output, scenario="baseline", duration=3000, *extra, success=True):
    command = [
        sys.executable,
        str(ROOT / "scripts/run_bench.py"),
        "--scenario",
        scenario,
        "--duration-ms",
        str(duration),
        "--output",
        str(output),
        "--build-dir",
        str(BUILD),
        *extra,
    ]
    result = subprocess.run(
        command, cwd=ROOT, capture_output=True, text=True, timeout=20, check=False
    )
    if success:
        assert result.returncode == 0, result.stdout + result.stderr
    return result


def assert_snapshot_equal(left, right):
    if isinstance(left, dict):
        assert left.keys() == right.keys()
        for key in left:
            assert_snapshot_equal(left[key], right[key])
    elif isinstance(left, float):
        assert left == pytest.approx(right, abs=1e-9)
    else:
        assert left == right


def verify_replay(output):
    result = subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/replay_log.py"),
            "--dbc",
            str(ROOT / "config/autodiag.dbc"),
            "--log",
            str(output / "frames.jsonl"),
            "--output",
            str(output / "replay.json"),
        ],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    snapshot = json.loads((output / "snapshot.json").read_text())
    replay = json.loads((output / "replay.json").read_text())
    assert_snapshot_equal(snapshot, replay)


@pytest.mark.parametrize("scenario", SCENARIOS)
def test_real_bus_scenarios_and_independent_replay(tmp_path, scenario):
    output = tmp_path / scenario
    run_cli(output, scenario)
    manifest = json.loads((output / "manifest.json").read_text())
    snapshot = json.loads((output / "snapshot.json").read_text())
    assert manifest["status"] == "completed"
    assert len({process["pid"] for process in manifest["processes"].values()}) == 4
    assert all(
        process["returncode"]
        == (42 if name == "body" and scenario == "node_exit" else 0)
        for name, process in manifest["processes"].items()
    )
    nodes = snapshot["nodes"]
    if scenario != "node_exit":
        assert all(node["online"] for node in nodes.values())
    if scenario == "baseline":
        assert all(node["data_status"] == "fresh" for node in nodes.values())
        assert not any(snapshot["global_rejections"].values())
    elif scenario == "drop_bms_data":
        assert nodes["bms"]["online"] and nodes["bms"]["data_status"] == "stale"
        assert not any(snapshot["global_rejections"].values())
    elif scenario == "corrupt_hvac_crc":
        assert nodes["hvac"]["data_status"] == "stale"
        assert nodes["hvac"]["rejections"]["bad_crc"] > 0
    elif scenario == "body_sequence_freeze":
        assert nodes["body"]["data_status"] == "stale"
        assert nodes["body"]["rejections"]["duplicate"] > 0
        assert nodes["body"]["resynchronizations"] == 0
    elif scenario == "bms_overtemperature":
        assert nodes["bms"]["signals"]["temperature_c"] == 125.0
        assert nodes["bms"]["data_status"] == "fresh"
        assert not any(snapshot["global_rejections"].values())
    elif scenario == "lin_bad_checksum":
        assert nodes["hvac"]["signals"]["cabin_sensor_valid"] is False
        assert nodes["hvac"]["data_status"] == "fresh"
        assert not any(snapshot["global_rejections"].values())
    else:
        assert not nodes["body"]["online"]
        assert nodes["body"]["data_status"] == "stale"
    verify_replay(output)


def test_counter_wrap_and_cpp_payloads_match_dbc(tmp_path):
    output = tmp_path / "wrap"
    run_cli(output, "baseline", 1500, "--counter-start", "250")
    rows = [
        json.loads(line) for line in (output / "frames.jsonl").read_text().splitlines()
    ]
    database = cantools.database.load_file(ROOT / "config/autodiag.dbc", strict=True)
    counters = {can_id: set() for can_id in (0x180, 0x181, 0x182)}
    for row in rows:
        if row["type"] != "frame" or row["id"] not in counters:
            continue
        assert row["accepted"]
        decoded = database.get_message_by_frame_id(row["id"]).decode(
            bytes.fromhex(row["data"]), allow_excess=False, allow_truncated=False
        )
        assert decoded["counter"] == row["counter"]
        counters[row["id"]].add(decoded["counter"])
    assert all(255 in values and 0 in values for values in counters.values())
    verify_replay(output)


def test_fixed_seed_repeats_data_for_shared_counters(tmp_path):
    outputs = [tmp_path / "first", tmp_path / "second"]
    maps = []
    for output in outputs:
        run_cli(output, "baseline", 1500, "--seed", "7")
        maps.append(
            {
                (row["id"], row["counter"]): row["data"]
                for line in (output / "frames.jsonl").read_text().splitlines()
                if (row := json.loads(line))["type"] == "frame"
                and row["id"] in (0x180, 0x181, 0x182)
            }
        )
    common = maps[0].keys() & maps[1].keys()
    assert len(common) >= 30
    assert all(maps[0][key] == maps[1][key] for key in common)


def test_nonempty_output_is_preserved(tmp_path):
    output = tmp_path / "existing"
    output.mkdir()
    sentinel = output / "keep.txt"
    sentinel.write_text("keep this")
    result = run_cli(output, duration=500, success=False)
    assert result.returncode == 2
    assert sentinel.read_text() == "keep this"
    assert list(output.iterdir()) == [sentinel]


def test_refuses_physical_or_noncan_interface_before_launch(tmp_path):
    output = tmp_path / "unsafe"
    result = run_cli(output, "baseline", 500, "--interface", "lo", success=False)
    assert result.returncode == 2
    assert not output.exists()


def test_competing_runner_rejected_without_creating_output(tmp_path):
    output = tmp_path / "competing"
    with interface_lock("vcan0"):
        result = run_cli(output, duration=500, success=False)
    assert result.returncode == 2
    assert "already owns" in result.stderr
    assert not output.exists()


def test_failed_child_is_reaped_and_monitor_stopped(tmp_path):
    fake_build = tmp_path / "bad-build"
    fake_build.mkdir()
    (fake_build / "autodiag-monitor").symlink_to((BUILD / "autodiag-monitor").resolve())
    fake_ecu = fake_build / "autodiag-ecu"
    fake_ecu.write_text("#!/bin/sh\nexit 7\n")
    fake_ecu.chmod(0o755)
    output = tmp_path / "failure"
    result = run_cli(
        output, "baseline", 3000, "--build-dir", str(fake_build), success=False
    )
    assert result.returncode == 2
    manifest = json.loads((output / "manifest.json").read_text())
    assert manifest["status"] == "failed"
    assert all(
        process["returncode"] is not None for process in manifest["processes"].values()
    )
    for process in manifest["processes"].values():
        with pytest.raises(ProcessLookupError):
            os.kill(process["pid"], 0)


def test_sigint_stops_only_owned_processes_and_records_evidence(tmp_path):
    output = tmp_path / "interrupted"
    process = subprocess.Popen(
        [
            sys.executable,
            str(ROOT / "scripts/run_bench.py"),
            "--duration-ms",
            "30000",
            "--output",
            str(output),
            "--build-dir",
            str(BUILD),
        ],
        cwd=ROOT,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    try:
        deadline = time.monotonic() + 8
        event = threading.Event()
        while True:
            try:
                manifest = json.loads((output / "manifest.json").read_text())
            except (FileNotFoundError, json.JSONDecodeError):
                manifest = {}
            if manifest.get("status") == "running":
                break
            assert time.monotonic() < deadline, "supervisor did not start"
            assert process.poll() is None, process.communicate()
            event.wait(0.01)
        process.send_signal(signal.SIGINT)
        stdout, stderr = process.communicate(timeout=8)
        assert process.returncode == 130, stdout + stderr
        manifest = json.loads((output / "manifest.json").read_text())
        assert manifest["status"] == "interrupted"
        assert all(
            child["returncode"] is not None for child in manifest["processes"].values()
        )
        for child in manifest["processes"].values():
            with pytest.raises(ProcessLookupError):
                os.kill(child["pid"], 0)
    finally:
        if process.poll() is None:
            process.terminate()
            process.communicate(timeout=8)


def monitor_cli(snapshot: Path, log: Path):
    return subprocess.run(
        [
            str(BUILD / "autodiag-monitor"),
            "--interface",
            "vcan0",
            "--duration-ms",
            "100",
            "--snapshot",
            str(snapshot),
            "--log",
            str(log),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )


def test_monitor_same_output_path_preserves_existing_file(tmp_path):
    target = tmp_path / "existing.txt"
    target.write_text("keep this evidence")
    result = monitor_cli(target, target)
    assert result.returncode != 0
    assert target.read_text() == "keep this evidence"


@pytest.mark.parametrize("aliased", ["snapshot", "log"])
def test_monitor_rejects_symlink_output_without_touching_victim(tmp_path, aliased):
    victim = tmp_path / "victim.txt"
    victim.write_text("private existing data")
    snapshot = tmp_path / "snapshot.json"
    log = tmp_path / "frames.jsonl"
    (snapshot if aliased == "snapshot" else log).symlink_to(victim)
    result = monitor_cli(snapshot, log)
    assert result.returncode != 0
    assert victim.read_text() == "private existing data"


def test_monitor_rejects_hardlink_aliases_before_opening_outputs(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    snapshot.write_text("keep this evidence")
    log = tmp_path / "frames.jsonl"
    log.hardlink_to(snapshot)
    result = monitor_cli(snapshot, log)
    assert result.returncode != 0
    assert snapshot.read_text() == log.read_text() == "keep this evidence"


def test_monitor_ignores_legacy_predictable_temp_symlink(tmp_path):
    victim = tmp_path / "victim.txt"
    victim.write_text("do not overwrite")
    snapshot = tmp_path / "snapshot.json"
    log = tmp_path / "frames.jsonl"
    launcher = tmp_path / "launch-monitor.sh"
    launcher.write_text(
        "#!/bin/sh\n"
        'ln -s "$1" "$2.tmp.$$"\n'
        'exec "$4" --interface vcan0 --duration-ms 100 --snapshot "$2" --log "$3"\n'
    )
    launcher.chmod(0o755)
    result = subprocess.run(
        [
            str(launcher),
            str(victim),
            str(snapshot),
            str(log),
            str(BUILD / "autodiag-monitor"),
        ],
        capture_output=True,
        text=True,
        timeout=5,
        check=False,
    )
    assert result.returncode == 0, result.stdout + result.stderr
    assert victim.read_text() == "do not overwrite"
    assert json.loads(snapshot.read_text())["capture_state"] == "completed"
    assert log.read_text().splitlines()[-1]


def test_monitor_can_stop_while_frames_keep_arriving(tmp_path):
    snapshot = tmp_path / "snapshot.json"
    log = tmp_path / "frames.jsonl"
    process = subprocess.Popen(
        [
            str(BUILD / "autodiag-monitor"),
            "--interface",
            "vcan0",
            "--duration-ms",
            "30000",
            "--snapshot",
            str(snapshot),
            "--log",
            str(log),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    stop = threading.Event()

    def transmit():
        with socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as sender:
            sender.setblocking(False)
            sender.bind(("vcan0",))
            frame = struct.pack("=IB3x8s", 0x321, 8, b"BUSYTEST")
            deadline = time.monotonic() + 3
            while not stop.is_set() and time.monotonic() < deadline:
                try:
                    sender.send(frame)
                except OSError:
                    stop.wait(0.0001)

    producer = threading.Thread(target=transmit)
    try:
        deadline = time.monotonic() + 5
        while not snapshot.exists():
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "monitor did not become ready"
            stop.wait(0.005)
        producer.start()
        while log.stat().st_size < 1024:
            assert process.poll() is None, process.communicate()
            assert time.monotonic() < deadline, "monitor did not record traffic"
            stop.wait(0.005)
        process.send_signal(signal.SIGTERM)
        stdout, stderr = process.communicate(timeout=1.5)
        assert process.returncode == 0, stdout + stderr
        assert json.loads(snapshot.read_text())["capture_state"] == "completed"
    finally:
        stop.set()
        if producer.ident is not None:
            producer.join(timeout=3)
        if process.poll() is None:
            process.kill()
            process.communicate(timeout=5)
