import asyncio
import json

import httpx

from autodiag_agent.runtime import TaskManager
from autodiag_agent.server import create_app
from autodiag_agent.tools import ToolRegistry
from tests.agent.test_runtime import Backend, ScriptedProvider


def test_http_jobs_and_input_boundaries(tmp_path):
    async def run():
        manager = TaskManager(
            ToolRegistry(Backend()), ScriptedProvider(), tmp_path / "tasks.sqlite3"
        )
        try:
            async with httpx.AsyncClient(
                transport=httpx.ASGITransport(app=create_app(manager)),
                base_url="http://localhost",
            ) as client:
                health = await client.get("/health")
                assert health.status_code == 200
                assert "api_key" not in health.text
                assert (
                    await client.post("/tasks", json={"question": False})
                ).status_code == 400
                assert (
                    await client.post("/tasks", json={"question": "x", "path": ".env"})
                ).status_code == 400
                assert (
                    await client.post(
                        "/tasks",
                        content='{"question":"x","question":"y"}',
                        headers={"content-type": "application/json"},
                    )
                ).status_code == 400
                assert (
                    await client.post(
                        "/tasks", content="{}", headers={"content-type": "text/plain"}
                    )
                ).status_code == 415
                assert (
                    await client.post(
                        "/tasks",
                        content="x" * 9000,
                        headers={"content-type": "application/json"},
                    )
                ).status_code == 413
                assert (await client.get("/tasks/not-a-task")).status_code == 404
                response = await client.post("/tasks", json={"question": "检查"})
                assert response.status_code == 202
                identifier = response.json()["task_id"]
                async with asyncio.timeout(3):
                    while True:
                        task = (await client.get("/tasks/" + identifier)).json()
                        if task["status"] == "completed":
                            break
                        await asyncio.sleep(0)
                assert task["report"]["hypotheses"][0]["code"] == "healthy"
                assert (
                    await client.post(f"/tasks/{identifier}/resume", json={})
                ).status_code == 409
                assert (
                    await client.post(f"/tasks/{identifier}/arbitrary", json={})
                ).status_code == 404
                assert "messages" not in json.dumps(task)
        finally:
            await manager.close()

    asyncio.run(run())
