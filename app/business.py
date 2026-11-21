"""Shared business data store and deterministic freight/commission calculations.

The document index remains in SQLite. Business records can use local SQLite for
single-computer use or PostgreSQL through DATABASE_URL for a NAS/team install.
"""
from __future__ import annotations

import csv
import io
import json
import os
import uuid
import time
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from sqlalchemy import create_engine, text

DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
DATABASE_URL = os.getenv("DATABASE_URL", f"sqlite:///{(DATA_DIR / 'business.sqlite3').as_posix()}")
if DATABASE_URL.startswith("postgres://"):
    DATABASE_URL = "postgresql+psycopg://" + DATABASE_URL[len("postgres://"):]
elif DATABASE_URL.startswith("postgresql://") and "+" not in DATABASE_URL.split(":", 1)[0]:
    DATABASE_URL = DATABASE_URL.replace("postgresql://", "postgresql+psycopg://", 1)
if DATABASE_URL.startswith("sqlite:"):
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    engine = create_engine(DATABASE_URL, connect_args={"check_same_thread": False}, pool_pre_ping=True)
else:
    engine = create_engine(DATABASE_URL, pool_pre_ping=True, pool_size=5, max_overflow=10)

RECORD_KINDS = {"customer", "freight_rate", "salesperson", "order", "commission_rule", "payroll", "product_note"}
WORKSPACES = {
    "product": "产品资料助手", "freight": "运费与贸易助手", "quote": "报价助手",
    "pi": "PI 助手", "customer": "客户助手", "sales": "销售助手",
    "payroll": "业绩与薪酬助手", "rules": "团队规则助手",
}


def init_business_store() -> None:
    for attempt in range(30):
        try:
            with engine.begin() as c:
                c.execute(text("""CREATE TABLE IF NOT EXISTS business_records (
                    id VARCHAR(40) PRIMARY KEY, kind VARCHAR(40) NOT NULL, name VARCHAR(240) NOT NULL,
                    payload TEXT NOT NULL, status VARCHAR(30) NOT NULL DEFAULT 'active',
                    created_at VARCHAR(40) NOT NULL, updated_at VARCHAR(40) NOT NULL,
                    updated_by VARCHAR(120) NOT NULL DEFAULT '')"""))
                c.execute(text("CREATE INDEX IF NOT EXISTS ix_business_records_kind_name ON business_records(kind, name)"))
                c.execute(text("""CREATE TABLE IF NOT EXISTS team_rules (
                    id VARCHAR(40) PRIMARY KEY, rule_key VARCHAR(120) NOT NULL, workspace VARCHAR(40) NOT NULL,
                    name VARCHAR(240) NOT NULL, body TEXT NOT NULL, version INTEGER NOT NULL,
                    active INTEGER NOT NULL DEFAULT 1, updated_by VARCHAR(120) NOT NULL DEFAULT '',
                    created_at VARCHAR(40) NOT NULL)"""))
                c.execute(text("CREATE INDEX IF NOT EXISTS ix_team_rules_workspace ON team_rules(workspace, active)"))
                c.execute(text("""CREATE TABLE IF NOT EXISTS team_conversations (
                    id VARCHAR(40) PRIMARY KEY, workspace VARCHAR(40) NOT NULL, title VARCHAR(240) NOT NULL,
                    messages TEXT NOT NULL, updated_by VARCHAR(120) NOT NULL DEFAULT '',
                    updated_at VARCHAR(40) NOT NULL)"""))
                c.execute(text("CREATE INDEX IF NOT EXISTS ix_team_conversations_workspace ON team_conversations(workspace, updated_at)"))
                c.execute(text("""CREATE TABLE IF NOT EXISTS business_audit (
                    id VARCHAR(40) PRIMARY KEY, actor VARCHAR(120) NOT NULL DEFAULT '', action VARCHAR(80) NOT NULL,
                    entity VARCHAR(80) NOT NULL, entity_id VARCHAR(40) NOT NULL DEFAULT '', detail TEXT NOT NULL,
                    created_at VARCHAR(40) NOT NULL)"""))
            return
        except Exception:
            if DATABASE_URL.startswith("sqlite:") or attempt == 29:
                raise
            time.sleep(1)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _decode(row: Any) -> dict[str, Any]:
    result = dict(row._mapping)
    result["data"] = json.loads(result.pop("payload"))
    return result


def audit(actor: str, action: str, entity: str, entity_id: str, detail: dict[str, Any]) -> None:
    with engine.begin() as c:
        c.execute(text("INSERT INTO business_audit(id,actor,action,entity,entity_id,detail,created_at) VALUES(:id,:actor,:action,:entity,:entity_id,:detail,:created_at)"), {
            "id": uuid.uuid4().hex, "actor": actor[:120], "action": action[:80], "entity": entity[:80],
            "entity_id": entity_id[:40], "detail": json.dumps(detail, ensure_ascii=False, default=str)[:12000], "created_at": _now(),
        })


def list_records(kind: str, query: str = "", limit: int = 500) -> list[dict[str, Any]]:
    if kind not in RECORD_KINDS:
        return []
    query = query.strip()[:200]
    with engine.begin() as c:
        rows = c.execute(text("SELECT * FROM business_records WHERE kind=:kind AND status!='deleted' AND (:q='' OR name LIKE :like OR payload LIKE :like) ORDER BY updated_at DESC LIMIT :limit"), {
            "kind": kind, "q": query, "like": f"%{query}%", "limit": max(1, min(limit, 500)),
        }).fetchall()
    return [_decode(row) for row in rows]


def get_record(record_id: str) -> dict[str, Any] | None:
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM business_records WHERE id=:id AND status!='deleted'"), {"id": record_id}).fetchone()
    return _decode(row) if row else None


def save_record(kind: str, data: dict[str, Any], actor: str = "") -> dict[str, Any]:
    if kind not in RECORD_KINDS:
        raise ValueError("Unsupported business record type")
    record_id = str(data.get("id") or uuid.uuid4().hex)
    name = str(data.get("name") or data.get("order_no") or data.get("customer") or data.get("product") or data.get("seller_name") or "").strip()[:240]
    if not name:
        raise ValueError("请填写名称或型号")
    clean = {str(k)[:80]: v for k, v in data.items() if k not in {"created_at", "updated_at", "updated_by"}}
    now = _now()
    with engine.begin() as c:
        existing = c.execute(text("SELECT created_at FROM business_records WHERE id=:id"), {"id": record_id}).fetchone()
        created = existing[0] if existing else now
        c.execute(text("""INSERT INTO business_records(id,kind,name,payload,status,created_at,updated_at,updated_by)
            VALUES(:id,:kind,:name,:payload,'active',:created,:updated,:actor)
            ON CONFLICT(id) DO UPDATE SET kind=excluded.kind,name=excluded.name,payload=excluded.payload,
            status='active',updated_at=excluded.updated_at,updated_by=excluded.updated_by"""), {
            "id": record_id, "kind": kind, "name": name, "payload": json.dumps(clean, ensure_ascii=False, default=str),
            "created": created, "updated": now, "actor": actor[:120],
        })
    audit(actor, "upsert", kind, record_id, {"name": name})
    return get_record(record_id) or {}


def delete_record(record_id: str, actor: str = "") -> bool:
    record = get_record(record_id)
    if not record:
        return False
    with engine.begin() as c:
        c.execute(text("UPDATE business_records SET status='deleted',updated_at=:now,updated_by=:actor WHERE id=:id"), {"now": _now(), "actor": actor[:120], "id": record_id})
    audit(actor, "delete", record["kind"], record_id, {"name": record["name"]})
    return True


def list_rules(workspace: str = "") -> list[dict[str, Any]]:
    with engine.begin() as c:
        rows = c.execute(text("SELECT * FROM team_rules WHERE active=1 AND (:workspace='' OR workspace=:workspace) ORDER BY workspace,name"), {"workspace": workspace}).fetchall()
    return [dict(row._mapping) for row in rows]


def save_rule(workspace: str, name: str, body: str, rule_id: str = "", actor: str = "") -> dict[str, Any]:
    workspace = workspace if workspace in {*WORKSPACES, "all"} else "all"
    name, body = name.strip()[:240], body.strip()[:10000]
    if not name or not body:
        raise ValueError("规则名称和规则内容不能为空")
    with engine.begin() as c:
        prior = c.execute(text("SELECT rule_key,version FROM team_rules WHERE id=:id"), {"id": rule_id}).fetchone() if rule_id else None
        key = str(prior[0]) if prior else uuid.uuid4().hex
        version = int(prior[1]) + 1 if prior else 1
        c.execute(text("UPDATE team_rules SET active=0 WHERE rule_key=:key"), {"key": key})
        saved_id = uuid.uuid4().hex
        c.execute(text("INSERT INTO team_rules(id,rule_key,workspace,name,body,version,active,updated_by,created_at) VALUES(:id,:key,:workspace,:name,:body,:version,1,:actor,:now)"), {
            "id": saved_id, "key": key, "workspace": workspace, "name": name, "body": body,
            "version": version, "actor": actor[:120], "now": _now(),
        })
    audit(actor, "publish", "rule", saved_id, {"name": name, "workspace": workspace, "version": version})
    return {"id": saved_id, "rule_key": key, "workspace": workspace, "name": name, "body": body, "version": version, "active": 1, "updated_by": actor}


def save_conversation(conversation_id: str, workspace: str, messages: list[dict[str, Any]], actor: str = "") -> dict[str, Any]:
    if workspace not in WORKSPACES:
        raise ValueError("未知的 AI 助手窗口")
    safe_messages = [{"role": str(m.get("role", ""))[:20], "content": str(m.get("content", ""))[:6000]} for m in messages[-100:] if isinstance(m, dict) and m.get("role") in {"user", "assistant"}]
    if not safe_messages:
        raise ValueError("对话内容不能为空")
    conversation_id = conversation_id or uuid.uuid4().hex
    title = next((m["content"].strip()[:120] for m in safe_messages if m["role"] == "user"), WORKSPACES[workspace])
    now = _now()
    with engine.begin() as c:
        c.execute(text("""INSERT INTO team_conversations(id,workspace,title,messages,updated_by,updated_at)
            VALUES(:id,:workspace,:title,:messages,:actor,:now) ON CONFLICT(id) DO UPDATE SET
            workspace=excluded.workspace,title=excluded.title,messages=excluded.messages,
            updated_by=excluded.updated_by,updated_at=excluded.updated_at"""), {
            "id": conversation_id, "workspace": workspace, "title": title,
            "messages": json.dumps(safe_messages, ensure_ascii=False), "actor": actor[:120], "now": now,
        })
    return {"id": conversation_id, "workspace": workspace, "title": title, "messages": safe_messages, "updated_by": actor, "updated_at": now}


def list_conversations(workspace: str = "", limit: int = 100) -> list[dict[str, Any]]:
    with engine.begin() as c:
        rows = c.execute(text("SELECT id,workspace,title,updated_by,updated_at FROM team_conversations WHERE (:workspace='' OR workspace=:workspace) ORDER BY updated_at DESC LIMIT :limit"), {"workspace": workspace, "limit": max(1, min(limit, 200))}).fetchall()
    return [dict(row._mapping) for row in rows]


def load_conversation(conversation_id: str) -> dict[str, Any] | None:
    with engine.begin() as c:
        row = c.execute(text("SELECT * FROM team_conversations WHERE id=:id"), {"id": conversation_id}).fetchone()
    if not row:
        return None
    result = dict(row._mapping)
    result["messages"] = json.loads(result["messages"])
    return result


def compute_freight(data: dict[str, Any], rate: dict[str, Any] | None = None) -> dict[str, Any]:
    rate = rate or {}
    cartons = max(0.0, float(data.get("cartons", 0) or 0))
    cbm = max(0.0, float(data.get("volume_cbm", 0) or 0))
    gross = max(0.0, float(data.get("gross_weight_kg", 0) or 0))
    unit = str(rate.get("rate_unit", data.get("rate_unit", "cbm"))).lower()
    volume_factor = max(0.0, float(rate.get("volume_weight_kg_per_cbm", 167) or 167))
    volumetric_weight = cbm * volume_factor
    chargeable_weight = max(gross, volumetric_weight)
    basis = {"kg": chargeable_weight, "cbm": cbm, "carton": cartons, "shipment": 1.0}.get(unit)
    if basis is None:
        raise ValueError("计价单位只支持 kg、cbm、carton 或 shipment")
    rate_value = max(0.0, float(rate.get("rate", data.get("rate", 0)) or 0))
    min_charge = max(0.0, float(rate.get("minimum_charge", 0) or 0))
    fixed = max(0.0, float(rate.get("fixed_fee", 0) or 0))
    per_carton = max(0.0, float(rate.get("per_carton_fee", 0) or 0))
    base = max(basis * rate_value, min_charge)
    total = round(base + fixed + per_carton * cartons, 2)
    incoterm = str(data.get("incoterm", "EXW")).upper()
    seller_pays = {"EXW": [], "FCA": ["origin"], "FAS": ["origin"], "FOB": ["origin"], "CFR": ["origin", "freight"], "CPT": ["origin", "freight"], "CIF": ["origin", "freight", "insurance"], "CIP": ["origin", "freight", "insurance"], "DAP": ["origin", "freight", "destination"], "DPU": ["origin", "freight", "destination"], "DDP": ["origin", "freight", "destination", "duties"]}.get(incoterm)
    if seller_pays is None:
        raise ValueError("暂不支持该贸易条款的自动费用归属，请选择 Incoterms 2020 条款或手动核对")
    return {"rate_unit": unit, "rate": rate_value, "cartons": cartons, "volume_cbm": cbm, "gross_weight_kg": gross,
            "volumetric_weight_kg": round(volumetric_weight, 3), "chargeable_weight_kg": round(chargeable_weight, 3),
            "basis_quantity": round(basis, 3), "base_freight": round(base, 2), "fixed_fee": fixed,
            "per_carton_fee": per_carton, "freight_total": total, "currency": str(rate.get("currency", data.get("currency", "USD"))).upper(),
            "incoterm": incoterm, "seller_cost_components": seller_pays,
            "assumptions": ["体积重系数和最低收费按所选运费率记录；未提供保险、目的港费用或关税金额时不计入总额。"]}


def calculate_commission(seller_id: str, start_date: str, end_date: str) -> dict[str, Any]:
    seller = get_record(seller_id)
    if not seller or seller["kind"] != "salesperson":
        raise ValueError("找不到销售人员")
    orders = [r["data"] for r in list_records("order", limit=500) if str(r["data"].get("seller_id")) == seller_id and start_date <= str(r["data"].get("date", "")) <= end_date]
    if not orders:
        raise ValueError("所选期间没有该销售人员的订单记录，无法生成业绩/提成明细")
    rules = [r["data"] for r in list_records("commission_rule", limit=500) if r["data"].get("seller_id") in {"", seller_id} and str(r["data"].get("effective_from", "0000-00-00")) <= end_date and str(r["data"].get("effective_to", "9999-12-31")) >= start_date]
    rules.sort(key=lambda r: str(r.get("effective_from", "")), reverse=True)
    if not rules:
        raise ValueError("该期间没有适用的提成规则，请先发布提成口径和比例")
    rule = rules[0]
    basis = str(rule.get("basis", "collected"))
    if basis not in {"collected", "net_sales", "gross_profit"}:
        raise ValueError("提成基数只支持 collected、net_sales 或 gross_profit")
    totals_by_currency: dict[str, dict[str, float]] = {}
    rows = []
    for order in orders:
        base = max(0.0, float(order.get(basis, 0) or 0) - float(order.get("refund_amount", 0) or 0))
        commission = round(base * float(rule.get("rate_percent", 0) or 0) / 100 + float(rule.get("fixed_per_order", 0) or 0), 2)
        currency = str(order.get("currency", "USD")).upper()
        totals_by_currency.setdefault(currency, {"basis_amount": 0.0, "commission": 0.0})
        totals_by_currency[currency]["basis_amount"] += base
        totals_by_currency[currency]["commission"] += commission
        rows.append({"order_no": order.get("order_no", order.get("name", "")), "date": order.get("date", ""), "currency": currency, "basis_amount": round(base, 2), "commission": commission})
    currency_totals = {currency: {key: round(value, 2) for key, value in totals.items()} for currency, totals in totals_by_currency.items()}
    single_currency = next(iter(currency_totals.values())) if len(currency_totals) == 1 else None
    return {"seller_id": seller_id, "seller_name": seller["name"], "period_start": start_date, "period_end": end_date,
            "basis": basis, "basis_amount": single_currency["basis_amount"] if single_currency else None, "rate_percent": float(rule.get("rate_percent", 0) or 0),
            "commission_total": single_currency["commission"] if single_currency else None, "currency_totals": currency_totals, "orders": rows,
            "status": "draft", "note": "仅使用系统中已录入的订单与回款；请复核后确认工资结算。"}


def import_freight_rows(filename: str, content: bytes, actor: str = "") -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace"))))
    elif suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheet = workbook.active
        iterator = sheet.iter_rows(values_only=True)
        headers = [str(v or "").strip() for v in next(iterator, ())]
        rows = [dict(zip(headers, values)) for values in iterator if any(v is not None for v in values)]
    else:
        raise ValueError("运费表请上传 CSV 或 XLSX 文件")
    aliases = {"起运地": "origin", "起运港": "origin", "origin": "origin", "出发地": "origin", "目的地": "destination", "目的港": "destination", "destination": "destination", "运输方式": "method", "运输": "method", "method": "method", "计价单位": "rate_unit", "单位": "rate_unit", "rate unit": "rate_unit", "计价方式": "rate_unit", "单价": "rate", "运价": "rate", "rate": "rate", "币种": "currency", "currency": "currency", "最低收费": "minimum_charge", "最低运费": "minimum_charge", "minimum charge": "minimum_charge", "固定费用": "fixed_fee", "fixed fee": "fixed_fee", "每箱费用": "per_carton_fee", "per carton fee": "per_carton_fee", "体积重系数": "volume_weight_kg_per_cbm", "kg/cbm": "volume_weight_kg_per_cbm", "生效日期": "effective_from", "有效期": "effective_from"}
    saved = []
    existing = {int(record["data"].get("source_row", -1)): record for record in list_records("freight_rate", limit=500) if record["data"].get("source_file") == filename}
    seen_rows: set[int] = set()
    for idx, row in enumerate(rows, 1):
        mapped = {aliases.get(str(k or "").strip().casefold() if str(k or "").strip().casefold() in aliases else str(k or "").strip(), str(k or "").strip()): v for k, v in row.items()}
        if not mapped.get("rate"):
            continue
        data = {k: v for k, v in mapped.items() if k in {"origin", "destination", "method", "rate_unit", "rate", "currency", "minimum_charge", "fixed_fee", "per_carton_fee", "volume_weight_kg_per_cbm", "effective_from"}}
        try:
            data["rate"] = float(str(data["rate"]).replace(",", ""))
            data["minimum_charge"] = float(str(data.get("minimum_charge", 0) or 0).replace(",", ""))
        except (TypeError, ValueError):
            continue
        data["rate_unit"] = str(data.get("rate_unit") or "cbm").lower()
        data["source_file"] = filename[:240]
        data["source_row"] = idx
        data["name"] = " / ".join(str(data.get(k, "")) for k in ("origin", "destination", "method", "rate_unit", "rate") if str(data.get(k, ""))) or f"{filename} 第 {idx} 行"
        if idx in existing:
            data["id"] = existing[idx]["id"]
        seen_rows.add(idx)
        saved.append(save_record("freight_rate", data, actor))
    if saved:
        for row_number, record in existing.items():
            if row_number not in seen_rows:
                delete_record(record["id"], actor)
    return saved


def import_customer_rows(filename: str, content: bytes, actor: str = "") -> list[dict[str, Any]]:
    suffix = Path(filename).suffix.lower()
    if suffix == ".csv":
        rows = list(csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace"))))
    elif suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        iterator = workbook.active.iter_rows(values_only=True)
        headers = [str(v or "").strip() for v in next(iterator, ())]
        rows = [dict(zip(headers, values)) for values in iterator if any(v is not None for v in values)]
    else:
        raise ValueError("客户表请上传 CSV 或 XLSX 文件")
    aliases = {
        "客户名称": "name", "公司名称": "name", "公司": "name", "name": "name", "customer": "name", "customer name": "name", "company": "name",
        "联系人": "contact", "contact": "contact", "contact person": "contact",
        "地址": "address", "客户地址": "address", "address": "address",
        "电话": "phone", "联系电话": "phone", "phone": "phone", "tel": "phone", "mobile": "phone",
        "邮箱": "email", "电子邮箱": "email", "email": "email",
        "国家": "country", "国家/地区": "country", "country": "country",
        "备注": "notes", "notes": "notes",
    }
    existing = {r["name"].casefold(): r for r in list_records("customer", limit=500)}
    saved = []
    for row in rows:
        mapped = {aliases.get(str(k or "").strip().casefold(), aliases.get(str(k or "").strip(), "")): v for k, v in row.items()}
        name = str(mapped.get("name") or "").strip()
        if not name:
            continue
        data = {key: (str(value).strip() if value is not None else "") for key, value in mapped.items() if key in {"contact", "address", "phone", "email", "country", "notes"} and value not in (None, "")}
        prior = existing.get(name.casefold())
        if prior:
            data = {**prior["data"], **data, "id": prior["id"], "name": name}
        else:
            data["name"] = name
        data["source_file"] = filename[:240]
        saved.append(save_record("customer", data, actor))
        existing[name.casefold()] = saved[-1]
    return saved
