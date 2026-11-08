"""Expose the exact bounded registry through the MCP SDK v2 stdio transport."""

from __future__ import annotations

import argparse
import asyncio
import json
from pathlib import Path

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import CallToolResult, ListToolsResult, TextContent, Tool

from .rpc import RpcClient
from .tools import ToolRegistry


def create_server(registry: ToolRegistry) -> Server:
    async def list_tools(_context, _params):
        return ListToolsResult(
            tools=[
                Tool(
                    name=item["function"]["name"],
                    description=item["function"]["description"],
                    inputSchema=item["function"]["parameters"],
                )
                for item in registry.definitions()
            ]
        )

    async def call_tool(_context, params):
        result = await registry.call(params.name, params.arguments or {})
        return CallToolResult(
            content=[TextContent(text=json.dumps(result, ensure_ascii=False))],
            structuredContent=result,
            isError=result["status"] == "error",
        )

    return Server(
        "autodiag-tools",
        version="1.0.0",
        on_list_tools=list_tools,
        on_call_tool=call_tool,
    )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path.home() / ".cache/autodiag/runtime/service.sock",
    )
    args = parser.parse_args()

    async def run():
        server = create_server(ToolRegistry(RpcClient(args.socket)))
        async with stdio_server() as (reader, writer):
            await server.run(reader, writer, server.create_initialization_options())

    asyncio.run(run())


if __name__ == "__main__":
    main()
