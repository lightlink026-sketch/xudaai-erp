from __future__ import annotations

import json
import sqlite3
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any

DB_PATH = Path.home() / ".xudaai-product-source-records.sqlite3"


def _connect() -> sqlite3.Connection:
    db = sqlite3.connect(DB_PATH, timeout=10)
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=10000")
    db.execute("""CREATE TABLE IF NOT EXISTS tasks (
        task_id TEXT PRIMARY KEY, task_type TEXT NOT NULL, source_type TEXT NOT NULL,
        summary TEXT NOT NULL DEFAULT '', supplier TEXT NOT NULL DEFAULT '',
        product_count INTEGER NOT NULL DEFAULT 0, status TEXT NOT NULL DEFAULT 'queued',
        output_path TEXT NOT NULL DEFAULT '', detail TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL
    )""")
    db.commit()
    return db


def create_task(task_type: str, source_type: str, summary: str, output_path: str = "") -> str:
    task_id = uuid.uuid4().hex
    now = datetime.now().isoformat(timespec="seconds")
    with _connect() as db:
        db.execute("INSERT INTO tasks(task_id,task_type,source_type,summary,output_path,created_at,updated_at) VALUES(?,?,?,?,?,?,?)",
                   (task_id, task_type, source_type, summary, output_path, now, now))
    return task_id


def update_task(task_id: str, *, status: str, supplier: str = "", product_count: int = 0,
                output_path: str = "", detail: str = "") -> None:
    with _connect() as db:
        db.execute("UPDATE tasks SET status=?,supplier=?,product_count=?,output_path=?,detail=?,updated_at=? WHERE task_id=?",
                   (status, supplier, int(product_count), output_path, detail[:12000], datetime.now().isoformat(timespec="seconds"), task_id))


def list_tasks(task_type: str = "all", query: str = "", page: int = 1, page_size: int = 25) -> tuple[list[dict[str, Any]], int]:
    clauses, args = [], []
    if task_type in {"download", "upload"}:
        clauses.append("task_type=?"); args.append(task_type)
    if query.strip():
        term = f"%{query.strip()}%"
        clauses.append("(summary LIKE ? OR source_type LIKE ? OR supplier LIKE ? OR status LIKE ? OR output_path LIKE ?)")
        args.extend([term] * 5)
    where = " WHERE " + " AND ".join(clauses) if clauses else ""
    with _connect() as db:
        total = db.execute(f"SELECT COUNT(*) FROM tasks{where}", args).fetchone()[0]
        rows = db.execute(f"SELECT * FROM tasks{where} ORDER BY created_at DESC LIMIT ? OFFSET ?", [*args, page_size, (max(1, page)-1)*page_size]).fetchall()
    return [dict(row) for row in rows], total


def delete_tasks(task_ids: list[str]) -> int:
    if not task_ids: return 0
    with _connect() as db:
        cur = db.execute(f"DELETE FROM tasks WHERE task_id IN ({','.join('?' for _ in task_ids)})", task_ids)
        return cur.rowcount
