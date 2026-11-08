"""Actual Unix/TCP service and ISO-TP integration on the virtual bench."""

import json
import os
import socket
import struct
import subprocess
import tempfile
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

from scripts.run_bench import interface_lock

ROOT = Path(__file__).resolve().parents[2]
BUILD = Path(
    os.environ.get(
        "AUTODIAG_BUILD_DIR", str(Path.home() / ".cache/autodiag/build/debug")
    )
)


def packet(method="health", params=None, identifier=None):
    value = {
        "version": 1,
        "id": identifier or uuid.uuid4().hex,
        "method": method,
        "params": params or {},
    }
    data = json.dumps(value).encode()
    return value, struct.pack("!I", len(data)) + data


def receive(connection):
    def exact(size):
        data = bytearray()
        while len(data) < size:
            part = connection.recv(size - len(data))
            assert part, "RPC peer closed an incomplete response"
            data.extend(part)
        return bytes(data)

    size = struct.unpack("!I", exact(4))[0]
    assert 1 <= size <= 65536
    return json.loads(exact(size))


def call(address, method="health", params=None):
    request, wire = packet(method, params)
    family = socket.AF_UNIX if isinstance(address, str) else socket.AF_INET
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(3)
        connection.connect(address)
        connection.sendall(wire)
        result = receive(connection)
    assert result["version"] == 1 and result["id"] == request["id"]
    return result


def wait_for(predicate, processes, timeout=5):
    deadline = time.monotonic() + timeout
    event = threading.Event()
    while time.monotonic() < deadline:
        for process in processes:
            assert process.poll() is None, process.communicate()
        try:
            value = predicate()
            if value:
                return value
        except (OSError, KeyError, AssertionError):
            pass
        event.wait(0.01)
    raise AssertionError("service condition did not become ready")


@contextmanager
def live_stack(fault_node=None, fault=None):
    for executable in ("autodiag-service", "autodiag-gateway", "autodiag-ecu"):
        assert (BUILD / executable).is_file(), f"build {executable} first"
    processes = []
    with (
        interface_lock("vcan0"),
        tempfile.TemporaryDirectory(prefix="adsvc-") as directory,
    ):
        unix = str(Path(directory) / "service.sock")
        with socket.socket() as available:
            available.bind(("127.0.0.1", 0))
            port = available.getsockname()[1]

        def start(arguments):
            process = subprocess.Popen(
                arguments,
                cwd=ROOT,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
            processes.append(process)
            return process

        try:
            service = start(
                [
                    str(BUILD / "autodiag-service"),
                    "--interface",
                    "vcan0",
                    "--socket",
                    unix,
                ]
            )
            wait_for(lambda: call(unix)["result"]["collector_running"], processes)
            gateway = start(
                [str(BUILD / "autodiag-gateway"), "--socket", unix, "--port", str(port)]
            )
            address = ("127.0.0.1", port)
            wait_for(lambda: call(address)["ok"], processes)
            for node in ("bms", "hvac", "body"):
                command = [
                    str(BUILD / "autodiag-ecu"),
                    "--node",
                    node,
                    "--duration-ms",
                    "20000",
                ]
                if node == fault_node:
                    command += [
                        "--fault",
                        fault,
                        "--fault-after-ms",
                        "100",
                        "--fault-duration-ms",
                        "0",
                    ]
                start(command)
            wait_for(
                lambda: all(
                    n["online"]
                    for n in call(unix, "snapshot")["result"]["nodes"].values()
                ),
                processes,
            )
            yield {
                "unix": unix,
                "tcp": address,
                "processes": processes,
                "service": service,
                "gateway": gateway,
                "start": start,
            }
        finally:
            for process in reversed(processes):
                if process.poll() is None:
                    process.terminate()
            deadline = time.monotonic() + 4
            for process in processes:
                try:
                    process.communicate(timeout=max(0.05, deadline - time.monotonic()))
                except subprocess.TimeoutExpired:
                    process.kill()
                    process.communicate(timeout=3)


def test_live_methods_use_actual_bus_and_diagnostic_responses():
    with live_stack() as stack:
        address = stack["tcp"]
        health = call(address)["result"]
        assert health["service"] == "autodiag-service" and health["collector_running"]
        assert health["evidence_id"].startswith(health["boot_id"])
        ecus = call(address, "list_ecus")["result"]["ecus"]
        assert {item["node"] for item in ecus} == {"bms", "hvac", "body"}
        for node, did, field in [
            ("bms", 0xD102, "temperature_c"),
            ("hvac", 0xD200, "cabin_temperature_c"),
            ("body", 0xD300, "speed_kph"),
        ]:
            response = call(address, "read_did", {"node": node, "did": did})
            assert response["ok"], response
            assert field in response["result"]["values"]
            assert response["result"]["valid"]
            identity = call(address, "read_did", {"node": node, "did": 0xF180})
            assert identity["result"]["values"] == {"software_version": "AutoDiag/1.0"}
            assert len(identity["result"]["response_hex"]) == 30
        assert call(address, "read_dtcs", {"node": "bms"})["result"]["dtcs"] == []
        for kind in ("identity", "diagnostic", "freshness"):
            assert call(address, "probe", {"node": "bms", "kind": kind})["result"][
                "passed"
            ]
        assert call(address, "contract")["result"]["limits"]["max_clients"] == 8
        window = call(
            address,
            "signal_window",
            {"node": "bms", "signal": "temperature_c", "window_ms": 5000},
        )["result"]
        assert window["sample_count"] >= 1 and window["returned_count"] == len(
            window["samples"]
        )


def test_tcp_accepts_fragmentation_and_concatenated_transactions():
    with (
        live_stack() as stack,
        socket.create_connection(stack["tcp"], timeout=3) as connection,
    ):
        first, wire = packet(identifier="first")
        second, next_wire = packet("snapshot", identifier="second")
        connection.sendall(wire[:2])
        connection.sendall(wire[2:] + next_wire)
        assert receive(connection)["id"] == first["id"]
        assert receive(connection)["id"] == second["id"]
        for data in [b'{"version":1,"version":1}', b"[]", b'{"x":NaN}']:
            connection.sendall(struct.pack("!I", len(data)) + data)
            assert receive(connection)["error"]["code"] == "invalid_request"


@pytest.mark.parametrize("header", [b"\0\0\0\0", b"\0\1\0\1"])
def test_invalid_lengths_close_connection(header):
    with (
        live_stack() as stack,
        socket.create_connection(stack["tcp"], timeout=3) as connection,
    ):
        connection.sendall(header)
        try:
            assert connection.recv(1) == b""
        except ConnectionResetError:
            pass


def test_incomplete_request_has_absolute_deadline():
    with (
        live_stack() as stack,
        socket.create_connection(stack["tcp"], timeout=3) as connection,
    ):
        connection.sendall(struct.pack("!I", 20) + b"x")
        start = time.monotonic()
        assert connection.recv(1) == b""
        assert time.monotonic() - start < 3


def test_requests_are_strict_and_real_negative_responses_are_preserved():
    with live_stack() as stack:
        address = stack["tcp"]
        assert call(address, "unknown")["error"]["code"] == "method_not_found"
        assert (
            call(address, "snapshot", {"extra": 1})["error"]["code"] == "invalid_params"
        )
        assert (
            call(address, "read_did", {"node": "bms", "did": True})["error"]["code"]
            == "invalid_params"
        )
        negative = call(address, "read_did", {"node": "bms", "did": 0xDFFF})
        assert negative["error"]["code"] == "uds_negative_response"
        assert negative["error"]["details"]["nrc"] == 0x31


def test_concurrent_tester_calls_do_not_swap_responses():
    with live_stack() as stack, ThreadPoolExecutor(max_workers=4) as workers:

        def read(did):
            result = call(stack["tcp"], "read_did", {"node": "bms", "did": did})
            assert result["ok"], result
            assert result["result"]["did"] == did
            return result["result"]

        values = list(workers.map(read, [0xD100, 0xD101, 0xD102] * 3))
        assert len({item["evidence_id"] for item in values}) == 9


@pytest.mark.parametrize("fault", ["drop_data", "bad_crc"])
def test_periodic_wire_fault_does_not_fabricate_diagnostic_data(fault):
    with live_stack("bms", fault) as stack:

        def stale():
            node = call(stack["tcp"], "snapshot")["result"]["nodes"]["bms"]
            return node if node["online"] and node["data_status"] == "stale" else None

        node = wait_for(stale, stack["processes"])
        if fault == "bad_crc":
            assert node["rejections"]["bad_crc"] > 0
        diagnostic = call(stack["tcp"], "read_did", {"node": "bms", "did": 0xD100})
        assert diagnostic["ok"]
        assert 300 <= diagnostic["result"]["values"]["voltage_v"] <= 450


@pytest.mark.parametrize(
    "node,fault,did,code",
    [
        ("bms", "overtemperature", 0xD102, "A10001"),
        ("hvac", "lin_bad_checksum", 0xD200, "A20001"),
    ],
)
def test_service_decodes_fault_dids_and_dtcs(node, fault, did, code):
    with live_stack(node, fault) as stack:

        def dtc_present():
            result = call(stack["tcp"], "read_dtcs", {"node": node})
            return (
                result["result"]["dtcs"]
                if result["ok"] and result["result"]["dtcs"]
                else None
            )

        dtcs = wait_for(dtc_present, stack["processes"])
        assert dtcs[0]["code"] == code and dtcs[0]["status"] == 9
        result = call(stack["tcp"], "read_did", {"node": node, "did": did})["result"]
        if node == "bms":
            assert result["values"]["temperature_c"] == 125
        else:
            assert not result["valid"] and not result["values"]["cabin_sensor_valid"]
            assert call(stack["tcp"], "read_did", {"node": "hvac", "did": 0xD201})[
                "result"
            ]["valid"]


def test_gateway_recovers_after_backend_restart_and_socket_is_not_stolen():
    with live_stack() as stack:
        first = call(stack["tcp"])["result"]["boot_id"]
        duplicate = subprocess.run(
            [str(BUILD / "autodiag-service"), "--socket", stack["unix"]],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
        assert duplicate.returncode != 0
        assert call(stack["tcp"])["result"]["boot_id"] == first
        stack["service"].terminate()
        stack["service"].communicate(timeout=3)
        assert not Path(stack["unix"]).exists()
        assert call(stack["tcp"])["error"]["code"] == "unavailable"
        stack["processes"].remove(stack["service"])
        stack["start"]([str(BUILD / "autodiag-service"), "--socket", stack["unix"]])
        result = wait_for(lambda: call(stack["tcp"])["result"], stack["processes"])
        assert result["boot_id"] != first


def test_missing_ecu_is_a_typed_timeout_not_a_zero_measurement():
    with live_stack() as stack:
        bms = stack["processes"][2]
        bms.terminate()
        bms.communicate(timeout=3)
        result = call(stack["tcp"], "read_did", {"node": "bms", "did": 0xD102})
        assert not result["ok"] and result["result"] is None
        assert result["error"]["code"] == "uds_timeout"


def test_timeout_quarantines_tester_instead_of_accepting_late_reply():
    with live_stack() as stack, ThreadPoolExecutor(max_workers=2) as workers:
        bms = stack["processes"][2]
        bms.terminate()
        bms.communicate(timeout=3)
        with socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, socket.CAN_ISOTP) as ecu:
            ecu.settimeout(1)
            ecu.bind(("vcan0", 0x7E0, 0x7E8))
            params = {"node": "bms", "did": 0xF187}
            first = workers.submit(call, stack["tcp"], "read_did", params)
            assert ecu.recv(4096) == b"\x22\xf1\x87"
            assert first.result(timeout=2)["error"]["code"] == "uds_timeout"
            second = workers.submit(call, stack["tcp"], "read_did", params)
            # Same SID/DID: a stale positive reply would otherwise pass correlation.
            ecu.settimeout(0.15)
            try:
                ecu.recv(4096)
            except TimeoutError:
                pass
            ecu.send(b"\x62\xf1\x87bms")
            result = second.result(timeout=2)
            assert not result["ok"] and result["result"] is None
            assert result["error"]["code"] == "uds_quarantined"
            assert not result["error"]["retryable"]
            assert (
                call(stack["tcp"], "read_dtcs", {"node": "bms"})["error"]["code"]
                == "uds_quarantined"
            )
            assert call(stack["tcp"], "read_did", {"node": "hvac", "did": 0xF187})["ok"]


def test_gateway_backpressure_is_bounded_and_backend_remains_responsive():
    with live_stack() as stack:
        clients = []
        try:
            for _ in range(9):
                connection = socket.create_connection(stack["tcp"], timeout=2)
                connection.settimeout(1)
                connection.sendall(struct.pack("!I", 100) + b"x")
                clients.append(connection)
            try:
                assert clients[-1].recv(1) == b""
            except ConnectionResetError:
                pass
            status = Path(f"/proc/{stack['gateway'].pid}/status").read_text()
            threads = int(
                next(
                    line.split(":", 1)[1]
                    for line in status.splitlines()
                    if line.startswith("Threads:")
                )
            )
            assert threads <= 9
            assert call(stack["unix"])["result"]["collector_running"]
        finally:
            for connection in clients:
                connection.close()
