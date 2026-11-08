"""Loopback-only asynchronous task API; credentials stay in the Python process."""

from __future__ import annotations

import argparse
import asyncio
import re
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from starlette.applications import Starlette
from starlette.convertors import Convertor, register_url_convertor
from starlette.middleware import Middleware
from starlette.middleware.trustedhost import TrustedHostMiddleware
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from .config import Config
from .provider import DeepSeekProvider
from .rpc import RpcClient, strict_json
from .runtime import TaskError, TaskManager
from .tools import ToolRegistry


class InputError(ValueError):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


async def body_object(request: Request) -> dict:
    if (
        request.headers.get("content-type", "").split(";", 1)[0].strip()
        != "application/json"
    ):
        raise InputError(415, "Use application/json")
    data = bytearray()
    try:
        async with asyncio.timeout(2):
            async for chunk in request.stream():
                data.extend(chunk)
                if len(data) > 8192:
                    raise InputError(413, "Request body exceeds limit")
    except TimeoutError as error:
        raise InputError(408, "Request body timed out") from error
    try:
        value = strict_json(data.decode("utf-8"))
    except (ValueError, UnicodeDecodeError) as error:
        raise InputError(400, "Invalid JSON request") from error
    if not isinstance(value, dict):
        raise InputError(400, "Request must be an object")
    return value


def create_app(manager: TaskManager) -> Starlette:
    @asynccontextmanager
    async def lifespan(_app):
        try:
            yield
        finally:
            await manager.close()

    async def health(_request):
        configuration = getattr(manager.provider, "config", None)
        model = (
            configuration.public()
            if configuration is not None
            else {"provider": "test", "configured": True}
        )
        return JSONResponse(
            {
                "status": "ok",
                "model": model,
                "max_running": manager.max_running,
                "max_pending": manager.max_pending,
            }
        )

    async def submit(request):
        try:
            value = await body_object(request)
            if set(value) != {"question"}:
                raise InputError(400, "Expected only question")
            record = await manager.submit(value["question"])
            return JSONResponse(record, status_code=202)
        except InputError as error:
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": str(error)}},
                status_code=error.status,
            )
        except (ValueError, TypeError):
            return JSONResponse(
                {
                    "error": {
                        "code": "invalid_request",
                        "message": "Question must contain 1..2000 characters",
                    }
                },
                status_code=400,
            )
        except TaskError as error:
            return JSONResponse(
                {"error": {"code": error.code, "message": str(error)}}, status_code=503
            )

    async def task(request):
        identifier = request.path_params["task_id"]
        if not re.fullmatch(r"[a-f0-9]{32}", identifier):
            return JSONResponse(
                {"error": {"code": "not_found", "message": "Task not found"}},
                status_code=404,
            )
        try:
            if request.method == "GET":
                record = await manager.get(identifier)
            else:
                value = await body_object(request)
                if value:
                    raise InputError(400, "Task action expects an empty object")
                action = request.path_params["action"]
                record = await (
                    manager.cancel(identifier)
                    if action == "cancel"
                    else manager.resume(identifier)
                )
            if record is None:
                return JSONResponse(
                    {"error": {"code": "not_found", "message": "Task not found"}},
                    status_code=404,
                )
            return JSONResponse(record, status_code=200)
        except InputError as error:
            return JSONResponse(
                {"error": {"code": "invalid_request", "message": str(error)}},
                status_code=error.status,
            )
        except TaskError as error:
            return JSONResponse(
                {"error": {"code": error.code, "message": str(error)}}, status_code=409
            )

    return Starlette(
        routes=[
            Route("/health", health),
            Route("/tasks", submit, methods=["POST"]),
            Route("/tasks/{task_id}", task),
            Route("/tasks/{task_id}/{action:task_action}", task, methods=["POST"]),
        ],
        lifespan=lifespan,
        middleware=[
            Middleware(
                TrustedHostMiddleware,
                allowed_hosts=["127.0.0.1", "localhost", "10.0.2.2", "testserver"],
            )
        ],
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path.home() / ".cache/autodiag/runtime/service.sock",
    )
    parser.add_argument(
        "--db", type=Path, default=Path.home() / ".cache/autodiag/agent/tasks.sqlite3"
    )
    parser.add_argument("--port", type=int, default=18771)
    parser.add_argument("--reset-budget", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.port <= 65535:
        parser.error("port must be 1..65535")

    async def serve():
        config = Config.from_env()
        provider = DeepSeekProvider(config)
        manager = TaskManager(
            ToolRegistry(RpcClient(args.socket)),
            provider,
            args.db,
            max_calls=config.max_calls,
        )
        if args.reset_budget:
            manager.store.set_metadata("provider_budget", None)
        server = uvicorn.Server(
            uvicorn.Config(
                create_app(manager),
                host="127.0.0.1",
                port=args.port,
                log_level="warning",
                limit_concurrency=32,
                timeout_keep_alive=5,
                timeout_graceful_shutdown=5,
            )
        )
        try:
            await server.serve()
        finally:
            await manager.close()

    asyncio.run(serve())


# Restrict action route before building any application.
class TaskAction(Convertor):
    regex = "cancel|resume"

    def convert(self, value: str) -> str:
        return value

    def to_string(self, value: str) -> str:
        if value not in {"cancel", "resume"}:
            raise ValueError("Invalid task action")
        return value


register_url_convertor("task_action", TaskAction())

if __name__ == "__main__":
    main()
