"""Copy shared business data from SOURCE_DATABASE_URL into DATABASE_URL."""
from __future__ import annotations

import os
import sys

from dotenv import load_dotenv
from sqlalchemy import create_engine, text

load_dotenv()

source_url = os.getenv("SOURCE_DATABASE_URL", "").strip()
if not source_url:
    raise SystemExit("请设置 SOURCE_DATABASE_URL 指向旧的 business.sqlite3")

from app.business import engine as destination_engine, init_business_store

init_business_store()
source_engine = create_engine(source_url, connect_args={"check_same_thread": False} if source_url.startswith("sqlite:") else {})
tables = ("business_records", "team_rules", "team_conversations", "business_audit")
copied = {}
with source_engine.connect() as source, destination_engine.begin() as destination:
    for table in tables:
        try:
            rows = source.execute(text(f"SELECT * FROM {table}")).mappings().all()
        except Exception as exc:
            raise SystemExit(f"读取源表 {table} 失败：{exc}") from exc
        if not rows:
            copied[table] = 0
            continue
        columns = list(rows[0].keys())
        names = ",".join(columns)
        values = ",".join(f":{column}" for column in columns)
        statement = text(f"INSERT INTO {table} ({names}) VALUES ({values}) ON CONFLICT DO NOTHING")
        result = destination.execute(statement, [dict(row) for row in rows])
        copied[table] = result.rowcount

for table, count in copied.items():
    print(f"{table}: {count} rows copied")
print("业务数据库迁移完成。旧数据库未修改；请抽查记录后再切换正式访问。")
