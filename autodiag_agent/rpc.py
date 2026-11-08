from __future__ import annotations

import asyncio
import json
import math
import struct
import uuid
from pathlib import Path

MAX_FRAME = 65536


class RpcError(RuntimeError):
    def __init__(self, code: str, message: str, retryable: bool = False, details=None):
        super().__init__(message)
        self.code = code
        self.retryable = retryable
        self.details = details or {}


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("Duplicate JSON key")
        result[key] = value
    return result


def invalid_constant(_value):
    raise ValueError("Nonfinite JSON number")


def strict_json(text: str):
    return json.loads(
        text,
        object_pairs_hook=unique_object,
        parse_constant=invalid_constant,
        parse_float=finite_float,
    )


def finite_float(token):
    value = float(token)
    if not math.isfinite(value):
        raise ValueError("JSON number overflow")
    return value


class RpcClient:
    def __init__(self, socket_path: Path | str, timeout: float = 2):
        self.socket_path = str(socket_path)
        self.timeout = timeout

    async def call(self, method: str, params: dict) -> dict:
        request_id = uuid.uuid4().hex
        payload = json.dumps(
            {"version": 1, "id": request_id, "method": method, "params": params},
            allow_nan=False,
            separators=(",", ":"),
        ).encode()
        if not 1 <= len(payload) <= MAX_FRAME:
            raise RpcError("invalid_request", "RPC request exceeds frame limit")
        writer = None
        try:
            async with asyncio.timeout(self.timeout):
                reader, writer = await asyncio.open_unix_connection(self.socket_path)
                writer.write(struct.pack("!I", len(payload)) + payload)
                await writer.drain()
                size = struct.unpack("!I", await reader.readexactly(4))[0]
                if not 1 <= size <= MAX_FRAME:
                    raise RpcError("internal_error", "Invalid upstream frame size")
                response = strict_json((await reader.readexactly(size)).decode("utf-8"))
                return self.validate(response, request_id)
        except TimeoutError as error:
            raise RpcError("unavailable", "RPC transaction timed out", True) from error
        except (OSError, asyncio.IncompleteReadError) as error:
            raise RpcError(
                "unavailable", "Diagnostic service is unavailable", True
            ) from error
        except (ValueError, UnicodeDecodeError) as error:
            raise RpcError(
                "internal_error", "Invalid upstream JSON response"
            ) from error
        finally:
            if writer is not None:
                writer.close()
                try:
                    await writer.wait_closed()
                except OSError:
                    pass

    @staticmethod
    def validate(response, request_id: str) -> dict:
        if not isinstance(response, dict) or set(response) != {
            "version",
            "id",
            "ok",
            "result",
            "error",
        }:
            raise RpcError("internal_error", "Invalid upstream response envelope")
        if (
            type(response["version"]) is not int
            or response["version"] != 1
            or response["id"] != request_id
        ):
            raise RpcError("internal_error", "Upstream version or request ID mismatch")
        if type(response["ok"]) is not bool:
            raise RpcError("internal_error", "Invalid upstream success flag")
        if response["ok"]:
            if (
                not isinstance(response["result"], dict)
                or response["error"] is not None
            ):
                raise RpcError("internal_error", "Invalid upstream success branch")
            return response["result"]
        error = response["error"]
        if response["result"] is not None or not isinstance(error, dict):
            raise RpcError("internal_error", "Invalid upstream error branch")
        if (
            set(error) != {"code", "message", "retryable", "details"}
            or not isinstance(error["code"], str)
            or not isinstance(error["message"], str)
            or type(error["retryable"]) is not bool
            or not isinstance(error["details"], dict)
        ):
            raise RpcError("internal_error", "Invalid upstream error fields")
        raise RpcError(
            error["code"], error["message"], error["retryable"], error["details"]
        )
