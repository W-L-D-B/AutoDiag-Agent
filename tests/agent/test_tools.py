import asyncio
from pathlib import Path

from autodiag_agent.tools import ToolRegistry


class Backend:
    def __init__(self):
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        return {"evidence_id": f"boot:{method}:1", "observed_at_ms": 10, "value": 7}


def test_invalid_arguments_cannot_reach_backend():
    backend = Backend()
    registry = ToolRegistry(backend)
    result = asyncio.run(registry.call("read_did", {"node": "bms", "did": True}))
    assert result["status"] == "error"
    assert not backend.calls


def test_unknown_tool_and_extra_arguments_are_rejected():
    backend = Backend()
    registry = ToolRegistry(backend)
    assert (
        asyncio.run(registry.call("shell", {"command": "whoami"}))["status"] == "error"
    )
    assert (
        asyncio.run(registry.call("list_ecus", {"file": ".env"}))["status"] == "error"
    )
    assert not backend.calls


def test_real_evidence_ids_are_preserved():
    registry = ToolRegistry(Backend())
    result = asyncio.run(registry.call("read_did", {"node": "bms", "did": 53506}))
    assert result["status"] == "success"
    assert result["evidence_ids"] == ["boot:read_did:1"]


def test_doc_search_does_not_read_arbitrary_files(tmp_path: Path):
    (tmp_path / "protocol-v1.md").write_text("# CRC\nCRC uses polynomial 0x1D.\n")
    (tmp_path / ".env").write_text("private-value")
    registry = ToolRegistry(Backend(), docs_root=tmp_path)
    result = asyncio.run(registry.call("query_contract", {"query": "CRC", "limit": 1}))
    assert result["evidence_ids"]
    assert "private-value" not in str(result)
