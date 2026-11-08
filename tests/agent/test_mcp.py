import asyncio
import json
import struct
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client


def test_actual_stdio_mcp_handshake_and_registry_tool(tmp_path):
    async def run():
        async def backend(reader, writer):
            try:
                size = struct.unpack("!I", await reader.readexactly(4))[0]
                request = json.loads(await reader.readexactly(size))
                response = {
                    "version": 1,
                    "id": request["id"],
                    "ok": True,
                    "error": None,
                    "result": {
                        "evidence_id": "boot:ecus:1",
                        "observed_at_ms": 0,
                        "ecus": [],
                    },
                }
                body = json.dumps(response).encode()
                writer.write(struct.pack("!I", len(body)) + body)
                await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()

        path = tmp_path / "backend.sock"
        server = await asyncio.start_unix_server(backend, path=path)
        try:
            parameters = StdioServerParameters(
                command=sys.executable,
                args=["-m", "autodiag_agent.mcp_server", "--socket", str(path)],
                cwd=Path(__file__).resolve().parents[2],
            )
            async with (
                asyncio.timeout(15),
                stdio_client(parameters) as (reader, writer),
                ClientSession(reader, writer, read_timeout_seconds=5) as session,
            ):
                await session.initialize()
                tools = await session.list_tools()
                assert {tool.name for tool in tools.tools} >= {
                    "read_did",
                    "list_ecus",
                    "query_contract",
                }
                result = await session.call_tool("list_ecus", {})
                assert not result.is_error
                data = result.structured_content
                assert data["evidence_ids"] == ["boot:ecus:1"]
                invalid = await session.call_tool(
                    "read_did", {"node": "bms", "did": True}
                )
                assert invalid.is_error
        finally:
            server.close()
            await server.wait_closed()

    asyncio.run(run())
