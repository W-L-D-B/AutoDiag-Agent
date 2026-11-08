from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from .config import redact


def safe_data(data):
    if isinstance(data, str):
        return redact(data)
    if isinstance(data, dict):
        return {key: safe_data(value) for key, value in data.items()}
    if isinstance(data, list):
        return [safe_data(value) for value in data]
    return data


class TaskStore:
    def __init__(self, path: Path):
        path = path.expanduser().absolute()
        if path.is_symlink() or path.parent.is_symlink():
            raise ValueError("Checkpoint path cannot be a symlink")
        path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        if path.parent.stat().st_uid != os.geteuid():
            raise PermissionError("Checkpoint directory belongs to another user")
        path.parent.chmod(0o700)
        self.connection = sqlite3.connect(path)
        path.chmod(0o600)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=FULL")
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS tasks (id TEXT PRIMARY KEY, status TEXT NOT NULL, payload TEXT NOT NULL)"
        )
        self.connection.execute(
            "CREATE TABLE IF NOT EXISTS metadata (name TEXT PRIMARY KEY, payload TEXT NOT NULL)"
        )
        self.connection.commit()
        for record in self.all():
            if record["status"] in {"queued", "running"}:
                record["status"] = "interrupted"
                record["error"] = {
                    "code": "process_interrupted",
                    "message": "Agent process stopped before task completion",
                }
                self.save(record)

    def save(self, record: dict):
        payload = json.dumps(safe_data(record), ensure_ascii=False, allow_nan=False)
        if len(payload.encode()) > 1000000:
            raise ValueError("Checkpoint exceeds size limit")
        with self.connection:
            self.connection.execute(
                "INSERT INTO tasks(id,status,payload) VALUES(?,?,?) ON CONFLICT(id) DO UPDATE SET status=excluded.status,payload=excluded.payload",
                (record["task_id"], record["status"], payload),
            )
            self.connection.execute(
                "DELETE FROM tasks WHERE id IN (SELECT id FROM tasks WHERE status NOT IN ('queued','running','interrupted') ORDER BY rowid DESC LIMIT -1 OFFSET 1000)"
            )

    def get(self, task_id: str) -> dict | None:
        row = self.connection.execute(
            "SELECT payload FROM tasks WHERE id=?", (task_id,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def all(self) -> list[dict]:
        return [
            json.loads(row[0])
            for row in self.connection.execute("SELECT payload FROM tasks")
        ]

    def get_metadata(self, name: str):
        row = self.connection.execute(
            "SELECT payload FROM metadata WHERE name=?", (name,)
        ).fetchone()
        return json.loads(row[0]) if row else None

    def set_metadata(self, name: str, value):
        with self.connection:
            self.connection.execute(
                "INSERT INTO metadata(name,payload) VALUES(?,?) ON CONFLICT(name) DO UPDATE SET payload=excluded.payload",
                (name, json.dumps(value, allow_nan=False)),
            )

    def close(self):
        self.connection.close()
