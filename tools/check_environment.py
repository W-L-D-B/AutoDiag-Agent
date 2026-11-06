"""Exercise the local development environment; only send traffic on virtual CAN."""

from __future__ import annotations

import importlib
import importlib.metadata
import json
import platform
import socket
import struct
import subprocess
import sys
import tempfile
from pathlib import Path


def receive_exact(connection: socket.socket, size: int) -> bytes:
    data = bytearray()
    while len(data) < size:
        chunk = connection.recv(size - len(data))
        if not chunk:
            raise RuntimeError("stream closed before the full payload arrived")
        data.extend(chunk)
    return bytes(data)


def stream_roundtrip(family: socket.AddressFamily) -> None:
    with tempfile.TemporaryDirectory(prefix="autodiag-env-") as directory:
        address = (
            ("127.0.0.1", 0) if family == socket.AF_INET else f"{directory}/ipc.sock"
        )
        with socket.socket(family, socket.SOCK_STREAM) as server:
            server.settimeout(3)
            server.bind(address)
            server.listen(1)
            with socket.socket(family, socket.SOCK_STREAM) as client:
                client.settimeout(3)
                client.connect(server.getsockname())
                with server.accept()[0] as connection:
                    connection.settimeout(3)
                    client.sendall(b"autodiag")
                    if receive_exact(connection, 8) != b"autodiag":
                        raise RuntimeError("stream payload mismatch")
                    connection.sendall(b"OK")
                    if receive_exact(client, 2) != b"OK":
                        raise RuntimeError("stream response mismatch")


def virtual_can_roundtrip() -> None:
    device = Path("/sys/class/net/vcan0").resolve(strict=True)
    if not device.is_relative_to("/sys/devices/virtual/net"):
        raise RuntimeError(
            "Refusing to transmit: vcan0 is not a virtual network device"
        )
    with (
        socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as receiver,
        socket.socket(socket.AF_CAN, socket.SOCK_RAW, socket.CAN_RAW) as sender,
    ):
        receiver.settimeout(3)
        receiver.setsockopt(
            socket.SOL_CAN_RAW, socket.CAN_RAW_FILTER, struct.pack("=II", 0x7A0, 0x7FF)
        )
        receiver.bind(("vcan0",))
        sender.bind(("vcan0",))
        frame = struct.pack("=IB3x8s", 0x7A0, 8, b"AUTODIAG")
        sender.send(frame)
        if receiver.recv(16) != frame:
            raise RuntimeError("CAN frame mismatch")


def isotp_roundtrip() -> None:
    with (
        socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, socket.CAN_ISOTP) as receiver,
        socket.socket(socket.AF_CAN, socket.SOCK_DGRAM, socket.CAN_ISOTP) as sender,
    ):
        receiver.settimeout(3)
        sender.settimeout(3)
        receiver.bind(("vcan0", 0x711, 0x710))
        sender.bind(("vcan0", 0x710, 0x711))
        payload = bytes(range(48))
        sender.send(payload)
        if receiver.recv(4096) != payload:
            raise RuntimeError("ISO-TP multi-frame payload mismatch")


def main() -> int:
    results: dict[str, object] = {
        "platform": platform.platform(),
        "python": platform.python_version(),
    }
    failures = []
    checks = {
        "tcp_roundtrip": lambda: stream_roundtrip(socket.AF_INET),
        "unix_socket_roundtrip": lambda: stream_roundtrip(socket.AF_UNIX),
        "virtual_can_roundtrip": virtual_can_roundtrip,
        "isotp_48_byte_roundtrip": isotp_roundtrip,
    }
    for name, check in checks.items():
        try:
            check()
            results[name] = "PASS"
        except (OSError, RuntimeError, ValueError, struct.error) as error:
            results[name] = f"FAIL: {error}"
            failures.append(name)
    packages = {
        "mcp": "mcp",
        "pydantic": "pydantic",
        "httpx": "httpx",
        "python-can": "can",
        "cantools": "cantools",
        "udsoncan": "udsoncan",
        "PyYAML": "yaml",
        "pytest": "pytest",
        "ruff": "ruff",
    }
    for package, module in packages.items():
        try:
            importlib.import_module(module)
            results[package] = importlib.metadata.version(package)
        except (ImportError, OSError, RuntimeError, ValueError, TypeError) as error:
            results[package] = f"FAIL: {error}"
            failures.append(package)
    try:
        remote = subprocess.run(
            ["git", "ls-remote", "origin"],
            capture_output=True,
            text=True,
            timeout=25,
            check=False,
        )
        results["github_remote"] = "PASS" if remote.returncode == 0 else "FAIL"
    except (OSError, subprocess.TimeoutExpired) as error:
        results["github_remote"] = f"FAIL: {error}"
    if results["github_remote"] != "PASS":
        failures.append("github_remote")
    results["overall"] = "PASS" if not failures else "FAIL"
    report = json.dumps(results, ensure_ascii=False, indent=2)
    print(report)
    output = Path("artifacts/environment-check.json")
    output.parent.mkdir(exist_ok=True)
    output.write_text(report + "\n", encoding="utf-8")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
