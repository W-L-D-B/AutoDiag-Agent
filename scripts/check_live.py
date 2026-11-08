"""Read-only acceptance of actual MCP -> Unix RPC -> C++ -> ISO-TP paths."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client

ROOT = Path(__file__).resolve().parents[1]


async def verify(socket_path: Path, output: Path) -> None:
    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "autodiag_agent.mcp_server", "--socket", str(socket_path)],
        cwd=ROOT,
    )
    observations = {}
    cases = [
        ("list_ecus", {}),
        ("get_service_health", {}),
        ("read_did", {"node": "bms", "did": 53506}),
        ("read_dtcs", {"node": "bms"}),
        ("get_signal_window", {"node": "bms", "signal": "temperature_c"}),
        ("query_contract", {"query": "D102 temperature"}),
        ("run_probe", {"node": "hvac", "kind": "diagnostic"}),
        ("run_regression", {"node": "body", "suite": "diagnostic"}),
    ]
    async with (
        asyncio.timeout(45),
        stdio_client(parameters) as (reader, writer),
        ClientSession(reader, writer, read_timeout_seconds=5) as session,
    ):
        initialized = await session.initialize()
        definitions = await session.list_tools()
        assert len(definitions.tools) == 8
        for name, arguments in cases:
            result = await session.call_tool(name, arguments)
            assert not result.is_error, (name, result)
            observations[name] = result.structured_content
            assert observations[name]["status"] == "success"
        assert len(observations["list_ecus"]["data"]["ecus"]) == 3
        assert observations["read_did"]["data"]["valid"]
        assert "temperature_c" in observations["read_did"]["data"]["values"]
        assert observations["get_signal_window"]["data"]["statistics"]["count"] > 0
        invalid = await session.call_tool("read_did", {"node": "bms", "did": True})
        assert invalid.is_error
    report = {
        "mode": "actual_mcp_native_backend",
        "mcp_protocol": str(initialized.protocol_version),
        "listed_tools": len(definitions.tools),
        "observations": observations,
        "invalid_parameters_rejected": True,
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("x", encoding="utf-8") as stream:
        json.dump(report, stream, ensure_ascii=False, indent=2)
    print(f"Actual MCP/native acceptance passed: {len(cases)} tools; {output}")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--socket",
        type=Path,
        default=Path.home() / ".cache/autodiag/runtime/service.sock",
    )
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(verify(args.socket, args.output))


if __name__ == "__main__":
    main()
