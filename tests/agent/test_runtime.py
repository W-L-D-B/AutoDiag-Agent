import asyncio
import json

import pytest

from autodiag_agent.provider import ModelTurn, ProviderError
from autodiag_agent.rpc import strict_json
from autodiag_agent.runtime import TaskManager, validate_report
from autodiag_agent.tools import ToolRegistry


class Backend:
    async def call(self, method, _params):
        if method == "health":
            return {
                "evidence_id": "boot:health:1",
                "boot_id": "boot",
                "collector_running": True,
            }
        return {"evidence_id": "boot:snapshot:1", "nodes": {"bms": {"online": True}}}


@pytest.mark.parametrize("value", ['{"x":1e999}', '{"x":NaN}', '{"x":1,"x":2}'])
def test_agent_json_rejects_ambiguous_or_nonfinite_numbers(value):
    with pytest.raises(ValueError):
        strict_json(value)


class ScriptedProvider:
    mode = "test"

    def __init__(self):
        self.calls = 0

    async def complete(self, messages, _tools, force_tools=False):
        self.calls += 1
        if force_tools:
            message = {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "call1",
                        "type": "function",
                        "function": {"name": "get_service_health", "arguments": "{}"},
                    }
                ],
            }
        else:
            evidence = json.loads(messages[-1]["content"])["evidence_ids"][0]
            message = {
                "role": "assistant",
                "content": json.dumps(
                    {
                        "summary": "当前观测正常",
                        "hypotheses": [
                            {
                                "code": "healthy",
                                "title": "观测正常",
                                "evidence_ids": [evidence],
                            }
                        ],
                        "facts": [],
                        "limitations": ["软件台架"],
                    }
                ),
            }
        return ModelTurn(
            message, {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15}
        )

    async def close(self):
        return None


async def finish(manager, identifier):
    async with asyncio.timeout(3):
        while True:
            record = await manager.get(identifier)
            if record["status"] not in {"queued", "running"}:
                return record
            await asyncio.sleep(0)


def test_task_uses_real_tool_results_and_durable_report(tmp_path):
    async def run():
        provider = ScriptedProvider()
        manager = TaskManager(
            ToolRegistry(Backend()), provider, tmp_path / "tasks.sqlite3"
        )
        record = await manager.submit("检查电池状态")
        result = await finish(manager, record["task_id"])
        assert result["status"] == "completed"
        assert result["report"]["hypotheses"][0]["code"] == "healthy"
        assert result["usage"]["model_calls"] == 2
        assert result["usage"]["total_tokens"] == 30
        assert provider.calls == 2
        await manager.close()
        reopened = TaskManager(
            ToolRegistry(Backend()), ScriptedProvider(), tmp_path / "tasks.sqlite3"
        )
        assert (await reopened.get(record["task_id"]))["report"] == result["report"]
        await reopened.close()

    asyncio.run(run())


def test_report_cannot_invent_citations():
    report = {
        "summary": "unknown",
        "hypotheses": [{"code": "healthy", "title": "ok", "evidence_ids": ["made-up"]}],
        "facts": [],
        "limitations": [],
    }
    with pytest.raises(ValueError):
        validate_report(report, {"real": {}})


@pytest.mark.parametrize(
    "observation",
    [
        {"tool": "shell", "status": "error"},
        {"tool": "query_contract", "status": "success"},
    ],
)
def test_vehicle_diagnosis_requires_successful_live_evidence(observation):
    report = {
        "summary": "unknown",
        "hypotheses": [{"code": "healthy", "title": "ok", "evidence_ids": ["real"]}],
        "facts": [],
        "limitations": [],
    }
    with pytest.raises(ValueError, match="live"):
        validate_report(report, {"real": observation})


def test_provider_error_is_not_diagnosis_success(tmp_path):
    class Failed(ScriptedProvider):
        async def complete(self, *_args, **_kwargs):
            raise ProviderError("provider_timeout", "Model API timed out")

    async def run():
        manager = TaskManager(
            ToolRegistry(Backend()), Failed(), tmp_path / "tasks.sqlite3"
        )
        record = await manager.submit("检查")
        result = await finish(manager, record["task_id"])
        assert result["status"] == "failed"
        assert result["error"]["code"] == "provider_timeout"
        assert result["report"] is None
        await manager.close()

    asyncio.run(run())


def test_billed_truncated_response_retains_usage(tmp_path):
    class Truncated(ScriptedProvider):
        async def complete(self, *_args, **_kwargs):
            raise ProviderError(
                "output_limit",
                "Output token limit",
                {"prompt_tokens": 100, "completion_tokens": 2048, "total_tokens": 2148},
            )

    async def run():
        manager = TaskManager(
            ToolRegistry(Backend()), Truncated(), tmp_path / "tasks.sqlite3"
        )
        task = await manager.submit("检查")
        result = await finish(manager, task["task_id"])
        assert (
            result["status"] == "failed" and result["error"]["code"] == "output_limit"
        )
        assert result["usage"]["total_tokens"] == 2148
        assert result["usage"]["model_calls"] == 1
        await manager.close()

    asyncio.run(run())
