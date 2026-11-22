from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path
from urllib.parse import urlparse

DB_PATH = Path.home() / ".xudaai-product-source-records.sqlite3"


def _connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH)
    connection.execute('PRAGMA busy_timeout=10000')
    connection.row_factory = sqlite3.Row
    connection.execute("""CREATE TABLE IF NOT EXISTS source_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        source_type TEXT NOT NULL,
        supplier_name TEXT NOT NULL DEFAULT '',
        product_name TEXT NOT NULL DEFAULT '',
        source_path TEXT NOT NULL DEFAULT '',
        source_url TEXT NOT NULL DEFAULT '',
        status TEXT NOT NULL DEFAULT '',
        missing_fields TEXT NOT NULL DEFAULT '',
        updated_at TEXT NOT NULL,
        UNIQUE(source_type, source_path, supplier_name, product_name)
    )""")
    connection.commit()
    return connection


def record_source(source_type: str, supplier_name: str, product_name: str, source_path: str, source_url: str = '', status: str = '', missing_fields: list[str] | None = None) -> None:
    now = datetime.now().astimezone().isoformat(timespec='seconds')
    with _connect() as db:
        db.execute("""INSERT INTO source_records(source_type,supplier_name,product_name,source_path,source_url,status,missing_fields,updated_at)
        VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(source_type,source_path,supplier_name,product_name) DO UPDATE SET
        source_url=CASE WHEN excluded.source_url<>'' THEN excluded.source_url ELSE source_records.source_url END,
        status=excluded.status, missing_fields=excluded.missing_fields, updated_at=excluded.updated_at""",
        (source_type, supplier_name or '', product_name or '', str(source_path or ''), source_url or '', status or '', '、'.join(missing_fields or []), now))


def query_sources(query: str = '', page: int = 1, page_size: int = 25) -> tuple[list[dict], int]:
    query = query.strip()
    where = "WHERE supplier_name LIKE ? OR product_name LIKE ? OR source_path LIKE ? OR source_url LIKE ?" if query else ''
    args = [f'%{query}%'] * 4 if query else []
    with _connect() as db:
        total = db.execute(f'SELECT COUNT(*) FROM source_records {where}', args).fetchone()[0]
        rows = db.execute(f'SELECT * FROM source_records {where} ORDER BY updated_at DESC,id DESC LIMIT ? OFFSET ?', [*args, page_size, (page - 1) * page_size]).fetchall()
    return [dict(row) for row in rows], total


def delete_source_records(ids: list[int], delete_yupoo_files: bool = False) -> tuple[int, list[str]]:
    removed_files: list[str] = []
    if not ids:
        return 0, removed_files
    with _connect() as db:
        placeholders = ','.join('?' for _ in ids)
        rows = db.execute(f'SELECT * FROM source_records WHERE id IN ({placeholders})', ids).fetchall()
        if delete_yupoo_files:
            for row in rows:
                if row['source_type'] != 'Yupoo':
                    continue
                original = Path(row['source_path'])
                if original.is_symlink():
                    continue
                folder = original.resolve()
                info_path = folder / 'info.json'
                if not folder.is_dir() or not info_path.is_file():
                    continue
                try:
                    info = json.loads(info_path.read_text(encoding='utf-8'))
                    parsed = urlparse(info.get('url', ''))
                    managed_album = bool(info.get('expected_image_count') is not None or info.get('images') or info.get('cover_path'))
                    if not parsed.hostname or not parsed.hostname.lower().endswith('.yupoo.com') or '/albums/' not in parsed.path or not managed_album:
                        continue
                except (OSError, ValueError, TypeError):
                    continue
                shutil.rmtree(folder)
                removed_files.append(str(folder))
        db.execute(f'DELETE FROM source_records WHERE id IN ({placeholders})', ids)
        return len(rows), removed_files
