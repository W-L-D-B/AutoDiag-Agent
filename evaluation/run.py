"""Run frozen evaluator-only tasks against the real virtual bench and model."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import time
from decimal import Decimal
from pathlib import Path

from autodiag_agent.config import Config
from autodiag_agent.provider import DeepSeekProvider
from autodiag_agent.rpc import RpcClient
from autodiag_agent.runtime import TERMINAL, TaskManager
from autodiag_agent.tools import ToolRegistry
from evaluation.rules import diagnose
from scripts.live_stack import LiveStack, StackOptions

ROOT = Path(__file__).resolve().parents[1]


def cny_balance(data: dict) -> Decimal:
    if data.get("is_available") is not True:
        raise RuntimeError("Model account is unavailable or has insufficient balance")
    for item in data.get("balance_infos", []):
        if item.get("currency") == "CNY":
            value = Decimal(item["total_balance"])
            if value.is_finite() and value >= 0:
                return value
    raise RuntimeError("Cannot enforce evaluation budget without a valid CNY balance")


async def wait_for_case(client: RpcClient, scenario: str) -> None:
    deadline = time.monotonic() + 8
    while time.monotonic() < deadline:
        data = await client.call("snapshot", {})
        nodes = data["nodes"]
        ready = {
            "baseline": all(
                node["online"] and node["data_status"] == "fresh"
                for node in nodes.values()
            ),
            "drop_bms_data": nodes["bms"]["data_status"] == "stale",
            "corrupt_hvac_crc": nodes["hvac"]["data_status"] == "stale"
            and nodes["hvac"]["rejections"]["bad_crc"] > 0,
            "body_sequence_freeze": nodes["body"]["data_status"] == "stale"
            and nodes["body"]["rejections"]["duplicate"] > 0,
            "bms_overtemperature": (nodes["bms"]["signals"] or {}).get(
                "temperature_c", 0
            )
            > 90,
            "lin_bad_checksum": (nodes["hvac"]["signals"] or {}).get(
                "cabin_sensor_valid"
            )
            is False,
            "node_exit": not nodes["body"]["online"],
            "old_bms_sequence": nodes["bms"]["data_status"] == "stale"
            and nodes["bms"]["rejections"]["old"] > 0,
            "wrong_bms_scale": (nodes["bms"]["signals"] or {}).get("voltage_v", 400)
            < 100,
        }[scenario]
        if ready:
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("The controlled test condition did not become observable")


def summarize(rows: list[dict], mode: str, task_hash: str) -> dict:
    def group(items):
        completed = sum(item["status"] == "completed" for item in items)
        correct = sum(item["correct"] for item in items)
        return {
            "tasks": len(items),
            "completed": completed,
            "correct": correct,
            "completion_rate": completed / len(items) if items else 0,
            "accuracy": correct / len(items) if items else 0,
            "rules_correct": sum(item["rules_correct"] for item in items),
            "rules_accuracy": sum(item["rules_correct"] for item in items) / len(items)
            if items
            else 0,
            "valid_citation_reports": completed if mode == "live" else None,
            "rejected_invalid_reports": sum(
                (item.get("error") or {}).get("code") == "invalid_report"
                for item in items
            ),
            "total_prompt_tokens": sum(
                item.get("usage", {}).get("prompt_tokens", 0) for item in items
            ),
            "total_completion_tokens": sum(
                item.get("usage", {}).get("completion_tokens", 0) for item in items
            ),
            "total_tool_calls": sum(
                item.get("usage", {}).get("tool_calls", 0) for item in items
            ),
            "mean_model_calls": sum(
                item.get("usage", {}).get("model_calls", 0) for item in items
            )
            / len(items)
            if items
            else 0,
            "elapsed_ms": sum(item["elapsed_ms"] for item in items),
        }

    return {
        "schema_version": 1,
        "mode": mode,
        "task_set_sha256": task_hash,
        "overall": group(rows),
        "by_split": {
            name: group([row for row in rows if row["split"] == name])
            for name in ("development", "holdout")
        },
        "by_family": {
            name: group([row for row in rows if row["family"] == name])
            for name in sorted({row["family"] for row in rows})
        },
        "grounding_note": "Reference validation checks observed evidence IDs, not general semantic entailment. Diagnosis accuracy uses evaluator-only expected codes.",
    }


async def evaluate(args) -> None:
    task_path = ROOT / "evaluation/tasks.json"
    raw = task_path.read_bytes()
    task_hash = hashlib.sha256(raw).hexdigest()
    tasks = json.loads(raw)["tasks"]
    if args.split != "all":
        tasks = [task for task in tasks if task["split"] == args.split]
    if args.ids:
        tasks = [task for task in tasks if task["id"] in args.ids.split(",")]
    if args.limit:
        tasks = tasks[: args.limit]
    if not tasks:
        raise ValueError("No tasks selected")
    args.output.mkdir(parents=True, exist_ok=False, mode=0o700)
    configuration = Config.from_env()
    initial = None
    if args.mode == "live":
        probe = DeepSeekProvider(configuration)
        try:
            initial = cny_balance(await probe.balance())
        finally:
            await probe.close()
    rows = []
    for index, task in enumerate(tasks, 1):
        if args.mode == "live":
            probe = DeepSeekProvider(configuration)
            try:
                current = cny_balance(await probe.balance())
            finally:
                await probe.close()
            if initial - current >= Decimal(str(args.budget_cny)) - Decimal("0.25"):
                raise RuntimeError(
                    "Evaluation spending limit reached before the next task"
                )
        options = StackOptions(
            scenario=task["scenario"],
            seed=task["seed"],
            duration_ms=180000,
            build_dir=args.build_dir,
            counter_start=task["counter_start"],
        )
        started = time.monotonic()
        with LiveStack(options) as stack:
            client = RpcClient(options.socket_path)
            await wait_for_case(client, task["scenario"])
            if task["preparation"] == "stop_service":
                service = stack.processes["service"]
                service.terminate()
                await asyncio.to_thread(service.wait, 3)
            rules = await diagnose(client, task["question"])
            record = {
                "status": "completed",
                "report": {"hypotheses": [{"code": rules}]},
                "usage": {},
            }
            if args.mode == "live":
                provider = DeepSeekProvider(configuration)
                manager = TaskManager(
                    ToolRegistry(client),
                    provider,
                    args.output / f"{task['id']}.sqlite3",
                    max_calls=configuration.max_calls,
                    budget_floor_cny=initial - Decimal(str(args.budget_cny)),
                )
                try:
                    submitted = await manager.submit(task["question"])
                    deadline = time.monotonic() + 150
                    while time.monotonic() < deadline:
                        record = await manager.get(submitted["task_id"])
                        if record["status"] in TERMINAL:
                            break
                        await asyncio.sleep(0.1)
                    else:
                        await manager.cancel(submitted["task_id"])
                        raise TimeoutError(
                            "Agent task did not finish within its evaluation deadline"
                        )
                    (args.output / f"{task['id']}-trace.json").write_text(
                        json.dumps(record, ensure_ascii=False, indent=2),
                        encoding="utf-8",
                    )
                finally:
                    await manager.close()
            hypotheses = (record.get("report") or {}).get("hypotheses") or []
            predicted = hypotheses[0]["code"] if hypotheses else None
            row = {
                "id": task["id"],
                "family": task["family"],
                "split": task["split"],
                "status": record["status"],
                "predicted": predicted,
                "correct": predicted in task["expected_codes"],
                "rules_predicted": rules,
                "rules_correct": rules in task["expected_codes"],
                "usage": record.get("usage", {}),
                "error": record.get("error"),
                "elapsed_ms": int((time.monotonic() - started) * 1000),
            }
            rows.append(row)
            with (args.output / "results.jsonl").open("a", encoding="utf-8") as stream:
                stream.write(json.dumps(row, ensure_ascii=False) + "\n")
            (args.output / "summary.json").write_text(
                json.dumps(
                    summarize(rows, args.mode, task_hash), ensure_ascii=False, indent=2
                ),
                encoding="utf-8",
            )
            print(
                f"[{index}/{len(tasks)}] {task['id']} {record['status']} correct={row['correct']} calls={row['usage'].get('model_calls', 0)}",
                flush=True,
            )
    if initial is not None:
        probe = DeepSeekProvider(configuration)
        try:
            final = cny_balance(await probe.balance())
        finally:
            await probe.close()
        summary = summarize(rows, args.mode, task_hash)
        summary["account_balance_decrease_cny"] = str(max(Decimal(0), initial - final))
        summary["cost_note"] = (
            "Observed account balance decrease; concurrent account activity and billing precision can affect attribution."
        )
        (args.output / "summary.json").write_text(
            json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
        )


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--mode", choices=["rules", "live"], default="rules")
    parser.add_argument(
        "--split", choices=["all", "development", "holdout"], default="all"
    )
    parser.add_argument("--ids")
    parser.add_argument("--limit", type=int)
    parser.add_argument("--budget-cny", type=float, default=5)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--build-dir", type=Path, default=StackOptions.build_dir)
    args = parser.parse_args()
    if (
        not 0.25 < args.budget_cny <= 5
        or args.limit is not None
        and not 1 <= args.limit <= 60
    ):
        parser.error("budget must be >0.25..5 CNY, limit 1..60")
    asyncio.run(evaluate(args))


if __name__ == "__main__":
    main()
