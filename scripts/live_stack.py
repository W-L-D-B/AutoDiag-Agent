"""Supervise the live AutoDiag processes; stop only the children this run owns."""

from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import ExitStack
from dataclasses import dataclass
from pathlib import Path
from urllib.error import URLError
from urllib.request import ProxyHandler, build_opener

from scripts.rpc_client import RpcFailure, call
from scripts.run_bench import (
    BenchError,
    atomic_json,
    finalize_run,
    interface_lock,
    validate_virtual_interface,
)

ROOT = Path(__file__).resolve().parents[1]


@dataclass
class StackOptions:
    scenario: str = "baseline"
    interface: str = "vcan0"
    socket_path: Path = Path.home() / ".cache/autodiag/runtime/service.sock"
    port: int = 18770
    agent_port: int = 18771
    duration_ms: int = 3600000
    seed: int = 1
    counter_start: int = 0
    build_dir: Path = Path.home() / ".cache/autodiag/build/debug"
    with_agent: bool = False
    agent_db: Path = Path.home() / ".cache/autodiag/agent/tasks.sqlite3"


class LiveStack:
    def __init__(self, options: StackOptions):
        self.options = options
        self.processes: dict[str, subprocess.Popen] = {}
        self.handles = []
        self.resources = ExitStack()
        self.output: Path | None = None
        self.closed = False
        self.manifest = {
            "schema_version": 1,
            "status": "starting",
            "error": None,
            "processes": {},
        }

    def _start(self, name: str, arguments: list[str]) -> None:
        if self.output is None:
            raise RuntimeError("stack output directory is not ready")
        stdout = (self.output / f"{name}.stdout.log").open("x", encoding="utf-8")
        self.handles.append(stdout)
        stderr = (self.output / f"{name}.stderr.log").open("x", encoding="utf-8")
        self.handles.append(stderr)
        process = subprocess.Popen(
            arguments,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            start_new_session=True,
        )
        self.processes[name] = process
        self.manifest["processes"][name] = {
            "pid": process.pid,
            "command": arguments,
            "returncode": None,
        }

    def _wait(self, predicate, description: str, timeout: float = 8) -> None:
        deadline = time.monotonic() + timeout
        event = threading.Event()
        while True:
            self.check()
            try:
                if predicate():
                    return
            except (OSError, ValueError, RpcFailure, URLError, TimeoutError):
                pass
            if time.monotonic() >= deadline:
                raise BenchError(
                    f"readiness timed out: {description}; inspect {self.output}"
                )
            event.wait(0.02)

    def check(self) -> None:
        for name, process in self.processes.items():
            code = process.poll()
            if code is not None and not (
                name == "body" and self.options.scenario == "node_exit" and code == 42
            ):
                raise BenchError(f"{name} exited {code}; inspect {self.output}")

    def __enter__(self):
        options = self.options
        validate_virtual_interface(options.interface)
        configuration = json.loads((ROOT / "config/scenarios.json").read_text())
        if options.scenario not in configuration["scenarios"]:
            raise ValueError("unknown scenario")
        for executable in ("autodiag-ecu", "autodiag-service", "autodiag-gateway"):
            if not os.access(options.build_dir / executable, os.X_OK):
                raise BenchError(f"build {executable} before starting the live stack")
        self.resources.enter_context(interface_lock(options.interface))
        complete = False
        try:
            control = Path.home() / ".cache/autodiag/control"
            control.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.output = Path(tempfile.mkdtemp(prefix="run-", dir=control))
            options.socket_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            self.manifest.update(
                {
                    "scenario": options.scenario,
                    "seed": options.seed,
                    "interface": options.interface,
                    "socket": str(options.socket_path),
                }
            )
            self._start(
                "service",
                [
                    str(options.build_dir / "autodiag-service"),
                    "--interface",
                    options.interface,
                    "--socket",
                    str(options.socket_path),
                ],
            )
            self._wait(
                lambda: call("health", socket_path=options.socket_path)[
                    "collector_running"
                ],
                "Unix service",
            )
            self._start(
                "gateway",
                [
                    str(options.build_dir / "autodiag-gateway"),
                    "--socket",
                    str(options.socket_path),
                    "--host",
                    "127.0.0.1",
                    "--port",
                    str(options.port),
                ],
            )
            self._wait(
                lambda: call("health", port=options.port)["collector_running"],
                "TCP gateway",
            )
            fault = configuration["scenarios"][options.scenario]
            for node in ("bms", "hvac", "body"):
                command = [
                    str(options.build_dir / "autodiag-ecu"),
                    "--node",
                    node,
                    "--interface",
                    options.interface,
                    "--duration-ms",
                    str(options.duration_ms + 5000),
                    "--seed",
                    str(options.seed),
                    "--counter-start",
                    str(options.counter_start),
                ]
                if fault["node"] == node:
                    command += [
                        "--fault",
                        fault["fault"],
                        "--fault-after-ms",
                        "1000",
                        "--fault-duration-ms",
                        "0",
                    ]
                self._start(node, command)
            self._wait(
                lambda: all(
                    node["online"]
                    for node in call("snapshot", socket_path=options.socket_path)[
                        "nodes"
                    ].values()
                ),
                "ECU heartbeats",
            )
            if options.with_agent:
                self._start(
                    "agent",
                    [
                        sys.executable,
                        "-m",
                        "autodiag_agent.server",
                        "--socket",
                        str(options.socket_path),
                        "--port",
                        str(options.agent_port),
                        "--db",
                        str(options.agent_db),
                    ],
                )
                opener = build_opener(ProxyHandler({}))

                def agent_ready():
                    with opener.open(
                        f"http://127.0.0.1:{options.agent_port}/health", timeout=1
                    ) as response:
                        return response.status == 200

                self._wait(agent_ready, "Agent HTTP service")
            self.manifest["status"] = "running"
            atomic_json(self.output / "manifest.json", self.manifest)
            complete = True
            return self
        finally:
            if not complete:
                self.close("failed")

    def close(self, status: str = "completed") -> None:
        if self.closed:
            return
        self.closed = True
        self.manifest["status"] = status
        try:
            if self.output is not None:
                finalize_run(self.processes, self.handles, self.manifest, self.output)
        finally:
            self.resources.close()

    def __exit__(self, kind, _value, _traceback):
        self.close(
            "interrupted"
            if kind is KeyboardInterrupt
            else "failed"
            if kind
            else "completed"
        )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--scenario", default="baseline")
    parser.add_argument("--interface", default="vcan0")
    parser.add_argument("--duration-ms", type=int, default=3600000)
    parser.add_argument("--seed", type=int, default=1)
    parser.add_argument("--socket", type=Path, default=StackOptions.socket_path)
    parser.add_argument("--port", type=int, default=18770)
    parser.add_argument("--agent-port", type=int, default=18771)
    parser.add_argument("--with-agent", action="store_true")
    parser.add_argument(
        "--build-dir",
        type=Path,
        default=Path(os.environ.get("AUTODIAG_BUILD_DIR", str(StackOptions.build_dir))),
    )
    args = parser.parse_args()
    if not 500 <= args.duration_ms <= 86395000 or not 0 <= args.seed <= 0xFFFFFFFF:
        parser.error("duration must be 500..86395000 ms and seed must be uint32")
    if not all(1 <= port <= 65535 for port in (args.port, args.agent_port)):
        parser.error("ports must be 1..65535")
    interrupted = False

    def stop(_signal, _frame):
        nonlocal interrupted
        if not interrupted:
            interrupted = True
            raise KeyboardInterrupt

    previous = {
        kind: signal.signal(kind, stop) for kind in (signal.SIGINT, signal.SIGTERM)
    }
    try:
        options = StackOptions(
            scenario=args.scenario,
            interface=args.interface,
            socket_path=args.socket.expanduser(),
            port=args.port,
            agent_port=args.agent_port,
            duration_ms=args.duration_ms,
            seed=args.seed,
            with_agent=args.with_agent,
            build_dir=args.build_dir.expanduser(),
        )
        with LiveStack(options) as stack:
            print(
                f"Live stack ready. TCP=127.0.0.1:{options.port} socket={options.socket_path}",
                flush=True,
            )
            print(f"Private control evidence: {stack.output}", flush=True)
            deadline = time.monotonic() + options.duration_ms / 1000
            event = threading.Event()
            while time.monotonic() < deadline:
                stack.check()
                event.wait(0.1)
    except KeyboardInterrupt:
        return 130
    except (BenchError, OSError, ValueError, RpcFailure) as error:
        parser.exit(2, f"stack error: {error}\n")
    finally:
        for kind, handler in previous.items():
            signal.signal(kind, handler)
    return 0


if __name__ == "__main__":
    sys.exit(main())
