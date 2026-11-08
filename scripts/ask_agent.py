"""Submit a question to the local Agent and wait for its evidence report."""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

import httpx


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    source = parser.add_mutually_exclusive_group(required=True)
    source.add_argument("--question")
    source.add_argument("--task-id")
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()
    with httpx.Client(
        base_url="http://127.0.0.1:18771", trust_env=False, timeout=5
    ) as client:
        if args.question:
            response = client.post("/tasks", json={"question": args.question})
            response.raise_for_status()
            identifier = response.json()["task_id"]
        else:
            identifier = args.task_id
            if len(identifier) != 32 or any(
                letter not in "0123456789abcdef" for letter in identifier
            ):
                parser.error("task ID must be 32 lowercase hexadecimal characters")
        print("Task:", identifier, flush=True)
        deadline = time.monotonic() + 150
        previous = None
        while time.monotonic() < deadline:
            response = client.get(f"/tasks/{identifier}")
            response.raise_for_status()
            record = response.json()
            if record["status"] != previous:
                previous = record["status"]
                print("Status:", previous, flush=True)
            if previous not in {"queued", "running"}:
                if args.output:
                    args.output.parent.mkdir(parents=True, exist_ok=True)
                    with args.output.open("x", encoding="utf-8") as stream:
                        json.dump(record, stream, ensure_ascii=False, indent=2)
                print(
                    json.dumps(
                        {
                            "report": record["report"],
                            "error": record["error"],
                            "usage": record["usage"],
                        },
                        ensure_ascii=False,
                        indent=2,
                    )
                )
                if previous != "completed":
                    raise SystemExit(1)
                return
            time.sleep(0.3)
    raise TimeoutError(f"Task polling timed out; saved task ID: {identifier}")


if __name__ == "__main__":
    main()
