from __future__ import annotations

import asyncio
import hashlib
import re
import statistics
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, ValidationError

from .config import ROOT, redact
from .rpc import RpcError

Node = Literal["bms", "hvac", "body"]


class Params(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)


class ReadDid(Params):
    node: Node
    did: int = Field(ge=0, le=65535)


class ReadDtcs(Params):
    node: Node
    status_mask: int = Field(default=255, ge=0, le=255)


class Window(Params):
    node: Node
    signal: str = Field(min_length=1, max_length=64, pattern=r"^[a-z_]+$")
    window_ms: int = Field(default=3000, ge=1, le=60000)
    max_samples: int = Field(default=16, ge=1, le=32)


class Health(Params):
    node: Node | None = None


class Query(Params):
    query: str = Field(min_length=1, max_length=1000)
    limit: int = Field(default=2, ge=1, le=3)


class Probe(Params):
    node: Node
    kind: Literal["identity", "diagnostic", "freshness"] = "diagnostic"


class Regression(Params):
    node: Node
    suite: Literal["diagnostic", "freshness"] = "diagnostic"


DEFINITIONS = {
    "list_ecus": (
        Params,
        "List ECU online/freshness state and available diagnostic capabilities.",
    ),
    "read_did": (
        ReadDid,
        "Read an actual UDS DID. BMS temperature D102=53506, voltage D100=53504; HVAC input D200=53760.",
    ),
    "read_dtcs": (ReadDtcs, "Read actual ECU diagnostic trouble codes and statuses."),
    "get_signal_window": (
        Window,
        "Read accepted historical signal samples and computed statistics. Never infer missing samples.",
    ),
    "get_service_health": (
        Health,
        "Read service health AND current ECU signals, CRC/sequence rejection counters, online/freshness state.",
    ),
    "query_contract": (
        Query,
        "Search only public project interface/UDS/CAN documentation for units, IDs and rules.",
    ),
    "run_probe": (
        Probe,
        "Run a fixed read-only identity, diagnostic or freshness probe.",
    ),
    "run_regression": (
        Regression,
        "Repeat fixed identity and diagnostic/freshness probes. Does not inject faults or modify ECU state.",
    ),
}


def evidence_ids(value) -> list[str]:
    found = set()
    if isinstance(value, dict):
        for key, item in value.items():
            if key == "evidence_id" and isinstance(item, str) and 0 < len(item) <= 256:
                found.add(item)
            else:
                found.update(evidence_ids(item))
    elif isinstance(value, list):
        for item in value:
            found.update(evidence_ids(item))
    return sorted(found)


def response(status: str, summary: str, data: dict, next_actions=None) -> dict:
    return {
        "status": status,
        "summary": redact(summary),
        "data": data,
        "evidence_ids": evidence_ids(data),
        "artifacts": [],
        "next_actions": next_actions or [],
    }


class ToolRegistry:
    def __init__(self, client, docs_root: Path = ROOT / "docs"):
        self.client = client
        self.docs_root = docs_root

    def definitions(self) -> list[dict]:
        return [
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": description,
                    "parameters": model.model_json_schema(),
                },
            }
            for name, (model, description) in DEFINITIONS.items()
        ]

    async def call(self, name: str, arguments: dict) -> dict:
        if name not in DEFINITIONS:
            return response(
                "error",
                "Unknown tool",
                {"code": "unknown_tool"},
                ["Choose a listed tool"],
            )
        try:
            params = DEFINITIONS[name][0].model_validate(arguments).model_dump()
        except ValidationError:
            return response(
                "error",
                "Tool arguments violate the input schema",
                {"code": "invalid_params"},
                ["Check required fields, types and ranges"],
            )
        try:
            async with asyncio.timeout(5):
                data = await self._execute(name, params)
            if len(str(data)) > 20000:
                return response(
                    "error",
                    "Tool result exceeds context limit",
                    {"code": "result_too_large"},
                    ["Request a smaller sample window"],
                )
            return response("success", f"{name} completed with observed evidence", data)
        except RpcError as error:
            return response(
                "error",
                str(error),
                {"code": error.code, "retryable": error.retryable, **error.details},
                [
                    "Retry a bounded probe"
                    if error.retryable
                    else "Check the protocol contract"
                ],
            )
        except (TimeoutError, OSError, ValueError, KeyError, TypeError) as error:
            return response(
                "error",
                f"Tool execution failed ({type(error).__name__})",
                {"code": "tool_failed"},
                ["Inspect service availability and input contract"],
            )

    async def _execute(self, name: str, params: dict) -> dict:
        if name == "query_contract":
            return self._search(params["query"], params["limit"])
        if name == "get_service_health":
            health = await self.client.call("health", {})
            snapshot = await self.client.call("snapshot", {})
            if params["node"] is not None and isinstance(snapshot.get("nodes"), dict):
                snapshot["nodes"] = {
                    params["node"]: snapshot["nodes"].get(params["node"])
                }
            return {"service": health, "snapshot": snapshot}
        if name == "run_regression":
            identity = await self.client.call(
                "probe", {"node": params["node"], "kind": "identity"}
            )
            check = await self.client.call(
                "probe", {"node": params["node"], "kind": params["suite"]}
            )
            return {
                "identity": identity,
                "check": check,
                "passed": identity.get("passed") is True
                and check.get("passed") is True,
            }
        method = {"get_signal_window": "signal_window", "run_probe": "probe"}.get(
            name, name
        )
        data = await self.client.call(method, params)
        if name == "get_signal_window":
            values = [
                item["value"]
                for item in data.get("samples", [])
                if type(item.get("value")) in (int, float)
            ]
            data["statistics"] = {
                "count": len(values),
                "minimum": min(values) if values else None,
                "maximum": max(values) if values else None,
                "mean": statistics.fmean(values) if values else None,
                "latest": values[-1] if values else None,
            }
        return data

    def _search(self, query: str, limit: int) -> dict:
        terms = set(re.findall(r"[a-z0-9_]+|[\u4e00-\u9fff]{1,2}", query.lower()))
        candidates = []
        for filename in ("protocol-v1.md", "diagnostics-v1.md", "service-api-v1.md"):
            path = self.docs_root / filename
            if path.is_symlink() or not path.is_file() or path.stat().st_size > 100000:
                continue
            text = path.read_text(encoding="utf-8")
            digest = hashlib.sha256(text.encode()).hexdigest()[:12]
            lines = text.splitlines()
            for start in range(0, len(lines), 16):
                snippet = "\n".join(lines[start : start + 16])[:1600]
                score = sum(snippet.lower().count(term) for term in terms)
                if score:
                    candidates.append((score, filename, start + 1, digest, snippet))
        candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
        return {
            "query": query,
            "snippets": [
                {
                    "evidence_id": f"contract:{digest}:{start}",
                    "document": filename,
                    "line": start,
                    "text": snippet,
                }
                for _, filename, start, digest, snippet in candidates[:limit]
            ],
        }
