"""Run three real C++ ECU processes and their observer on a virtual CAN interface."""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import json
import os
import re
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
SCENARIOS = (
    "baseline",
    "drop_bms_data",
    "corrupt_hvac_crc",
    "body_sequence_freeze",
    "bms_overtemperature",
    "lin_bad_checksum",
    "node_exit",
    "old_bms_sequence",
    "wrong_bms_scale",
)
NODES = ("bms", "hvac", "body")


class BenchError(RuntimeError):
    """A bench cannot safely start or did not complete successfully."""


def validate_virtual_interface(interface: str) -> None:
    if not re.fullmatch(r"vcan[A-Za-z0-9_.-]{0,11}", interface):
        raise BenchError(
            "only a vcan interface name of at most 15 characters is allowed"
        )
    device = Path("/sys/class/net") / interface
    try:
        resolved = device.resolve(strict=True)
        if (
            not resolved.is_relative_to("/sys/devices/virtual/net")
            or (device / "type").read_text().strip() != "280"
        ):
            raise BenchError("interface is not virtual CAN")
        result = subprocess.run(
            ["ip", "-details", "-json", "link", "show", "dev", interface],
            capture_output=True,
            text=True,
            timeout=5,
            check=True,
        )
        links = json.loads(result.stdout)
        if len(links) != 1 or links[0].get("linkinfo", {}).get("info_kind") != "vcan":
            raise BenchError("interface kind is not vcan")
        if "UP" not in links[0].get("flags", []):
            raise BenchError(
                "vcan interface is down; run sudo /usr/local/sbin/autodiag-vcan up"
            )
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        raise BenchError(f"cannot validate {interface}: {error}") from error


@contextlib.contextmanager
def interface_lock(interface: str):
    """Prevent two supervisors from mixing the same protocol IDs on one bus."""
    if not re.fullmatch(r"vcan[A-Za-z0-9_.-]{0,11}", interface):
        raise BenchError("invalid interface lock name")
    directory = Path(tempfile.gettempdir()) / f"autodiag-bench-{os.getuid()}"
    directory.mkdir(mode=0o700, exist_ok=True)
    info = directory.lstat()
    if (
        not stat.S_ISDIR(info.st_mode)
        or info.st_uid != os.getuid()
        or stat.S_IMODE(info.st_mode) != 0o700
    ):
        raise BenchError("unsafe runtime lock directory")
    descriptor = os.open(
        directory / f"{interface}.lock",
        os.O_RDWR | os.O_CREAT | os.O_CLOEXEC | os.O_NOFOLLOW,
        0o600,
    )
    try:
        info = os.fstat(descriptor)
        if (
            not stat.S_ISREG(info.st_mode)
            or info.st_uid != os.getuid()
            or info.st_nlink != 1
            or stat.S_IMODE(info.st_mode) & 0o077
        ):
            raise BenchError("unsafe runtime lock file")
        try:
            fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise BenchError(f"another bench already owns {interface}") from error
        yield
    finally:
        os.close(descriptor)


def atomic_json(path: Path, value: Any) -> None:
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as stream:
            temporary = Path(stream.name)
            json.dump(value, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
        temporary.replace(path)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)


def claim_output(output: Path) -> None:
    if output.is_symlink():
        raise BenchError("output must not be a symbolic link")
    output.mkdir(parents=True, exist_ok=True)
    if not output.is_dir() or any(output.iterdir()):
        raise BenchError("output directory is not empty; choose a new directory")
    with (output / ".run-claim").open("x", encoding="utf-8") as stream:
        stream.write(f"{os.getpid()}\n")


def stop_children(processes: dict[str, subprocess.Popen]) -> None:
    for process in processes.values():
        if process.poll() is None:
            process.terminate()
    deadline = time.monotonic() + 3
    for process in processes.values():
        try:
            process.wait(timeout=max(0, deadline - time.monotonic()))
        except subprocess.TimeoutExpired:
            pass
    for process in processes.values():
        if process.poll() is None:
            process.kill()
    for process in processes.values():
        process.wait(timeout=3)


def finalize_run(processes, handles, manifest, output: Path) -> None:
    """Finish owned-process cleanup and evidence before delivering termination."""
    termination_signals = {signal.SIGINT, signal.SIGTERM}
    previous_mask = signal.pthread_sigmask(signal.SIG_BLOCK, termination_signals)
    try:
        try:
            stop_children(processes)
        finally:
            for handle in handles:
                handle.close()
            for name, process in processes.items():
                manifest["processes"][name]["returncode"] = process.returncode
            pending = bool(signal.sigpending() & termination_signals)
            if pending:
                manifest["status"] = "interrupted"
                manifest["error"] = "received termination signal during finalization"
            atomic_json(output / "manifest.json", manifest)
            if not pending and signal.sigpending() & termination_signals:
                manifest["status"] = "interrupted"
                manifest["error"] = "received termination signal during finalization"
                atomic_json(output / "manifest.json", manifest)
    finally:
        signal.pthread_sigmask(signal.SIG_SETMASK, previous_mask)


def run_bench(args: argparse.Namespace) -> dict[str, Any]:
    validate_virtual_interface(args.interface)
    build = args.build_dir.expanduser().resolve()
    for executable in ("autodiag-ecu", "autodiag-monitor"):
        path = build / executable
        if not path.is_file() or not os.access(path, os.X_OK):
            raise BenchError(f"missing executable {path}; build the C++ targets first")
    configuration = json.loads(
        (ROOT / "config/scenarios.json").read_text(encoding="utf-8")
    )
    if configuration.get("schema_version") != 1:
        raise BenchError("unsupported scenario schema")
    scenario = configuration["scenarios"][args.scenario]
    output = args.output.expanduser().absolute()
    processes: dict[str, subprocess.Popen] = {}
    handles = []
    manifest = {
        "schema_version": 1,
        "scenario": args.scenario,
        "seed": args.seed,
        "interface": args.interface,
        "duration_ms": args.duration_ms,
        "counter_start": args.counter_start,
        "status": "starting",
        "error": None,
        "scenario_configuration": scenario,
        "processes": {},
        "outputs": {"snapshot": "snapshot.json", "frames": "frames.jsonl"},
        "note": "Harness truth artifact; diagnostic agents must not receive this manifest.",
    }

    def start(name: str, command: list[str]) -> None:
        stdout = (output / f"{name}.stdout.log").open("x", encoding="utf-8")
        handles.append(stdout)
        stderr = (output / f"{name}.stderr.log").open("x", encoding="utf-8")
        handles.append(stderr)
        process = subprocess.Popen(
            command,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        processes[name] = process
        manifest["processes"][name] = {
            "command": command,
            "pid": process.pid,
            "returncode": None,
        }

    def check_nodes() -> None:
        for name, process in processes.items():
            result = process.poll()
            expected = 42 if name == "body" and args.scenario == "node_exit" else 0
            if name != "monitor" and result is not None and result != expected:
                raise BenchError(
                    f"{name} exited {result}, expected {expected}; inspect its stderr log"
                )

    with interface_lock(args.interface):
        claim_output(output)
        try:
            monitor = [
                str(build / "autodiag-monitor"),
                "--interface",
                args.interface,
                "--duration-ms",
                str(args.duration_ms),
                "--snapshot",
                str(output / "snapshot.json"),
                "--log",
                str(output / "frames.jsonl"),
            ]
            start("monitor", monitor)
            ready_deadline = time.monotonic() + 5
            poll_event = threading.Event()
            while True:
                if processes["monitor"].poll() is not None:
                    raise BenchError(
                        "monitor exited before readiness; inspect monitor.stderr.log"
                    )
                try:
                    snapshot = json.loads(
                        (output / "snapshot.json").read_text(encoding="utf-8")
                    )
                except (FileNotFoundError, json.JSONDecodeError):
                    snapshot = {}
                if (
                    snapshot.get("ready") is True
                    and snapshot.get("capture_state") == "listening"
                ):
                    break
                if time.monotonic() >= ready_deadline:
                    raise BenchError("monitor readiness timed out")
                poll_event.wait(0.01)
            for node in NODES:
                command = [
                    str(build / "autodiag-ecu"),
                    "--node",
                    node,
                    "--interface",
                    args.interface,
                    "--duration-ms",
                    str(args.duration_ms),
                    "--seed",
                    str(args.seed),
                    "--counter-start",
                    str(args.counter_start),
                ]
                if scenario["node"] == node:
                    command += [
                        "--fault",
                        scenario["fault"],
                        "--fault-after-ms",
                        "1000",
                        "--fault-duration-ms",
                        "0",
                    ]
                start(node, command)
            manifest["status"] = "running"
            atomic_json(output / "manifest.json", manifest)
            deadline = time.monotonic() + args.duration_ms / 1000 + 10
            while any(process.poll() is None for process in processes.values()):
                check_nodes()
                monitor_result = processes["monitor"].poll()
                if monitor_result is not None and monitor_result != 0:
                    raise BenchError(
                        f"monitor exited {monitor_result}; inspect monitor.stderr.log"
                    )
                if time.monotonic() >= deadline:
                    raise BenchError("bench process completion timed out")
                poll_event.wait(0.01)
            check_nodes()
            snapshot = json.loads(
                (output / "snapshot.json").read_text(encoding="utf-8")
            )
            if (
                snapshot.get("capture_state") != "completed"
                or snapshot.get("ready") is not False
            ):
                raise BenchError("monitor did not publish a completed snapshot")
            manifest["status"] = "completed"
        except (BenchError, OSError, ValueError, KeyError) as error:
            manifest["status"], manifest["error"] = "failed", str(error)
            raise
        except KeyboardInterrupt:
            manifest["status"], manifest["error"] = (
                "interrupted",
                "received interruption signal",
            )
            raise
        finally:
            finalize_run(processes, handles, manifest, output)
    return manifest


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", choices=SCENARIOS, default="baseline")
    parser.add_argument("--interface", default="vcan0")
    parser.add_argument("--duration-ms", type=int, default=3000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--counter-start", type=int, default=0)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=Path(
            os.environ.get(
                "AUTODIAG_BUILD_DIR", str(Path.home() / ".cache/autodiag/build/debug")
            )
        ),
    )
    args = parser.parse_args()
    if not 500 <= args.duration_ms <= 600000 or (
        args.scenario != "baseline" and args.duration_ms < 2500
    ):
        parser.error(
            "duration must be 500..600000 ms; fault scenarios require at least 2500 ms"
        )
    if not 0 <= args.seed <= 0xFFFFFFFF or not 0 <= args.counter_start <= 255:
        parser.error("seed must be uint32 and counter-start must be uint8")

    interrupted = False

    def interrupt(_signum, _frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    old_term = signal.signal(signal.SIGTERM, interrupt)
    old_int = signal.signal(signal.SIGINT, interrupt)
    try:
        manifest = run_bench(args)
        print(f"Bench {manifest['status']}: {args.output}")
        return 0
    except KeyboardInterrupt:
        print("Bench interrupted; owned processes stopped", file=sys.stderr)
        return 130
    except (
        BenchError,
        OSError,
        ValueError,
        KeyError,
        subprocess.SubprocessError,
    ) as error:
        parser.exit(2, f"bench error: {error}\n")
    finally:
        signal.signal(signal.SIGTERM, old_term)
        signal.signal(signal.SIGINT, old_int)


if __name__ == "__main__":
    sys.exit(main())
