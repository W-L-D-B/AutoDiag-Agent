import copy
import json
import socket
import struct
import threading

import pytest

from scripts.rpc_client import request, validate_response


def envelope():
    return {
        "version": 1,
        "id": "test",
        "ok": True,
        "result": {"value": 1},
        "error": None,
    }


def test_valid_response_retains_typed_result():
    assert validate_response(envelope(), "test")["result"] == {"value": 1}


def test_invalid_parameters_are_rejected_before_connecting():
    with pytest.raises(TypeError):
        request("snapshot", [])


@pytest.mark.parametrize(
    "field,value",
    [("version", True), ("version", 2), ("id", "other"), ("ok", 1), ("error", {})],
)
def test_rejects_malformed_response(field, value):
    response = copy.deepcopy(envelope())
    response[field] = value
    with pytest.raises(ValueError):
        validate_response(response, "test")


def test_typed_error_branch_is_validated():
    response = {
        "version": 1,
        "id": "test",
        "ok": False,
        "result": None,
        "error": {
            "code": "uds_timeout",
            "message": "timeout",
            "retryable": True,
            "details": {},
        },
    }
    assert not validate_response(response, "test")["ok"]
    response["error"]["retryable"] = 1
    with pytest.raises(ValueError):
        validate_response(response, "test")


@pytest.mark.parametrize("encoding,number", [("utf-16", "1"), ("utf-8", "1e999")])
def test_wire_rejects_non_utf8_and_overflow_numbers(encoding, number):
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        listener.settimeout(3)

        def respond():
            with listener.accept()[0] as peer:
                size = struct.unpack("!I", peer.recv(4))[0]
                data = bytearray()
                while len(data) < size:
                    data.extend(peer.recv(size - len(data)))
                response = envelope()
                response["id"] = json.loads(data)["id"]
                raw = json.dumps(response).replace('"value": 1', '"value": ' + number)
                body = raw.encode(encoding)
                peer.sendall(struct.pack("!I", len(body)) + body)

        worker = threading.Thread(target=respond)
        worker.start()
        try:
            with pytest.raises((UnicodeError, ValueError)):
                request("health", {}, host="127.0.0.1", port=listener.getsockname()[1])
        finally:
            worker.join(timeout=3)
            assert not worker.is_alive()
