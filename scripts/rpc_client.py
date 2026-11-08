"""Small independent CLI client for the local framed RPC contract."""

from __future__ import annotations

import argparse
import json
import math
import socket
import struct
import time
import uuid
from pathlib import Path

MAX_FRAME = 65536


class RpcFailure(RuntimeError):
    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code


def unique_object(pairs):
    output = {}
    for name, value in pairs:
        if name in output:
            raise ValueError("duplicate JSON field")
        output[name] = value
    return output


def reject_nonfinite(_token):
    raise ValueError("nonfinite JSON number")


def finite_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("JSON number overflow")
    return value


def receive_exact(connection: socket.socket, count: int, deadline: float) -> bytes:
    data = bytearray()
    while len(data) < count:
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("RPC response deadline exceeded")
        connection.settimeout(remaining)
        chunk = connection.recv(count - len(data))
        if not chunk:
            raise ConnectionError("RPC peer closed before response completed")
        data.extend(chunk)
    return bytes(data)


def validate_response(value: object, request_id: str) -> dict:
    if not isinstance(value, dict) or set(value) != {
        "version",
        "id",
        "ok",
        "result",
        "error",
    }:
        raise ValueError("invalid RPC response envelope")
    if (
        type(value["version"]) is not int
        or value["version"] != 1
        or value["id"] != request_id
    ):
        raise ValueError("RPC version or request ID mismatch")
    if type(value["ok"]) is not bool:
        raise ValueError("RPC ok flag is not boolean")
    if value["ok"]:
        if not isinstance(value["result"], dict) or value["error"] is not None:
            raise ValueError("invalid RPC success branch")
    else:
        if value["result"] is not None or not isinstance(value["error"], dict):
            raise ValueError("invalid RPC error branch")
        error = value["error"]
        if set(error) != {"code", "message", "retryable", "details"}:
            raise ValueError("invalid RPC error fields")
        if not isinstance(error["code"], str) or not isinstance(error["message"], str):
            raise ValueError("invalid RPC error text")
        if type(error["retryable"]) is not bool or not isinstance(
            error["details"], dict
        ):
            raise ValueError("invalid RPC error metadata")
    return value


def request(
    method: str,
    params: dict | None = None,
    *,
    socket_path: Path | None = None,
    host: str = "127.0.0.1",
    port: int = 18770,
    timeout: float = 2.5,
) -> dict:
    if params is not None and not isinstance(params, dict):
        raise TypeError("params must be an object")
    identifier = uuid.uuid4().hex
    payload = json.dumps(
        {"version": 1, "id": identifier, "method": method, "params": params or {}},
        ensure_ascii=False,
        allow_nan=False,
    ).encode("utf-8")
    if not 1 <= len(payload) <= MAX_FRAME:
        raise ValueError("RPC request exceeds frame limit")
    deadline = time.monotonic() + timeout
    family = socket.AF_UNIX if socket_path is not None else socket.AF_INET
    address = str(socket_path) if socket_path is not None else (host, port)
    with socket.socket(family, socket.SOCK_STREAM) as connection:
        connection.settimeout(timeout)
        connection.connect(address)
        connection.settimeout(max(0.001, deadline - time.monotonic()))
        connection.sendall(struct.pack("!I", len(payload)) + payload)
        size = struct.unpack("!I", receive_exact(connection, 4, deadline))[0]
        if not 1 <= size <= MAX_FRAME:
            raise ValueError("invalid RPC response length")
        raw = receive_exact(connection, size, deadline)
    return validate_response(
        json.loads(
            raw.decode("utf-8"),
            object_pairs_hook=unique_object,
            parse_constant=reject_nonfinite,
            parse_float=finite_float,
        ),
        identifier,
    )


def call(method: str, params: dict | None = None, **connection) -> dict:
    response = request(method, params, **connection)
    if not response["ok"]:
        raise RpcFailure(response["error"]["code"], response["error"]["message"])
    return response["result"]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--method", default="health")
    parser.add_argument("--params", default="{}")
    parser.add_argument("--socket", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=18770)
    args = parser.parse_args()
    try:
        parameters = json.loads(args.params)
        if not isinstance(parameters, dict):
            raise TypeError("params must be an object")
        response = request(
            args.method,
            parameters,
            socket_path=args.socket,
            host=args.host,
            port=args.port,
        )
        print(json.dumps(response, ensure_ascii=False, indent=2))
    except (OSError, ValueError, TypeError, ConnectionError, RpcFailure) as error:
        parser.exit(2, f"RPC failed: {error}\n")


if __name__ == "__main__":
    main()
