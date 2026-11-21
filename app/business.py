"""Shared business data store and deterministic freight/commission calculations.

The document index remains in SQLite. Business records can use local SQLite for
single-computer use or PostgreSQL through DATABASE_URL for a NAS/team install.
"""
from __future__ import annotations

import csv
import io
import json
import os
import re
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
            # Freight workbooks can contain hundreds of rates across multiple
            # carriers/sheets. Keep list/import reconciliation above that size
            # so later rows remain selectable and repeated imports stay stable.
            "kind": kind, "q": query, "like": f"%{query}%", "limit": max(1, min(limit, 5000)),
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
    safe_messages = []
    for message in messages[-100:]:
        if not isinstance(message, dict) or message.get("role") not in {"user", "assistant"}:
            continue
        safe = {"role": str(message["role"]), "content": str(message.get("content", ""))[:6000]}
        image_preview = str(message.get("image_preview", ""))
        if len(image_preview) <= 250_000 and re.fullmatch(r"data:image/(?:jpeg|png|webp);base64,[A-Za-z0-9+/]+={0,2}", image_preview):
            safe["image_preview"] = image_preview
        if safe["role"] == "assistant":
            safe["sources"] = []
            for source in message.get("sources", [])[:12] if isinstance(message.get("sources"), list) else []:
                if not isinstance(source, dict):
                    continue
                try:
                    file_id = max(0, int(source.get("file_id", 0)))
                except (TypeError, ValueError):
                    continue
                safe["sources"].append({"file_id": file_id, "name": str(source.get("name", ""))[:240], "path": str(source.get("path", ""))[:1000], "ext": str(source.get("ext", ""))[:20], "page": source.get("page"), "snippet": str(source.get("snippet", ""))[:700]})
            safe["image_matches"] = []
            for match in message.get("image_matches", [])[:50] if isinstance(message.get("image_matches"), list) else []:
                if not isinstance(match, dict):
                    continue
                try:
                    file_id = max(0, int(match.get("file_id", 0)))
                    similarity = max(0.0, min(1.0, float(match.get("similarity", 0))))
                except (TypeError, ValueError):
                    continue
                safe["image_matches"].append({"file_id": file_id, "name": str(match.get("name", ""))[:240], "path": str(match.get("path", ""))[:1000], "similarity": similarity, "preview_url": f"/api/files/{file_id}/preview"})
            safe["image_method"] = str(message.get("image_method", ""))[:80]
            safe["model"] = str(message.get("model", ""))[:200]
            safe["used_ai"] = bool(message.get("used_ai", False))
            safe["quote_records"] = []
            for record in message.get("quote_records", [])[:10] if isinstance(message.get("quote_records"), list) else []:
                if not isinstance(record, dict):
                    continue
                items = record.get("items", []) if isinstance(record.get("items"), list) else []
                safe["quote_records"].append({
                    "id": str(record.get("id", ""))[:40], "quote_no": str(record.get("quote_no", ""))[:80],
                    "customer": str(record.get("customer", ""))[:200], "currency": str(record.get("currency", ""))[:3],
                    "status": str(record.get("status", ""))[:30], "document_type": "pi" if record.get("document_type") == "pi" else "quote",
                    "total": str(record.get("total", ""))[:40], "quote_date": str(record.get("quote_date", ""))[:30],
                    "incoterm": str(record.get("incoterm", ""))[:100], "origin_port": str(record.get("origin_port", ""))[:150],
                    "destination_port": str(record.get("destination_port", ""))[:150], "shipping": str(record.get("shipping", "0"))[:40],
                    "shipping_term": str(record.get("shipping_term", ""))[:100],
                    "items": [{"name": str(item.get("name", ""))[:200], "description": str(item.get("description", ""))[:500], "configuration": str(item.get("configuration", ""))[:500], "colors": str(item.get("colors", ""))[:300], "size_breakdown": str(item.get("size_breakdown", ""))[:300], "carton_spec": str(item.get("carton_spec", ""))[:200], "units_per_carton": str(item.get("units_per_carton", ""))[:40], "carton_quantity": str(item.get("carton_quantity", ""))[:40], "carton_volume": str(item.get("carton_volume", ""))[:100], "gross_weight": str(item.get("gross_weight", ""))[:100], "net_weight": str(item.get("net_weight", ""))[:100], "moq": str(item.get("moq", ""))[:40], "quantity": str(item.get("quantity", ""))[:40], "unit": str(item.get("unit", ""))[:30], "unit_price": str(item.get("unit_price", ""))[:40], "amount": str(item.get("amount", ""))[:40], "source": str(item.get("source", ""))[:500]} for item in items[:50] if isinstance(item, dict)],
                })
            action = message.get("proposed_action")
            if isinstance(action, dict) and action.get("type") in {"customer.save", "customer.delete", "freight_rate.save", "freight.calculate", "freight.compare", "salesperson.save", "salesperson.delete", "order.save", "commission_rule.save", "payroll.calculate", "rule.save", "quote.draft", "pi.draft"} and isinstance(action.get("data"), dict):
                safe["proposed_action"] = {"type": action["type"], "data": action["data"]}
        safe_messages.append(safe)
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
    volume_factor = max(0.0, float(rate.get("volume_weight_kg_per_cbm") or (0 if rate.get("calculation_type") == "parcel_weight_tier" else 167)))
    divisor = float(rate.get("volumetric_divisor_cm3") or 0)
    volumetric_weight = cbm * 1_000_000 / divisor if divisor > 0 else cbm * volume_factor
    chargeable_weight = max(gross, volumetric_weight)
    basis = {"kg": chargeable_weight, "cbm": cbm, "carton": cartons, "shipment": 1.0,
             "parcel_weight": chargeable_weight / max(cartons, 1)}.get(unit)
    if basis is None:
        raise ValueError("该费率的计价方式还未确认，需先在货代规则中确认表格计算口径")
    rate_value = max(0.0, float(rate.get("rate", data.get("rate", 0)) or 0))
    min_charge = max(0.0, float(rate.get("minimum_charge", 0) or 0))
    fixed = max(0.0, float(rate.get("fixed_fee", 0) or 0))
    per_carton = max(0.0, float(rate.get("per_carton_fee", 0) or 0))
    calculation_type = str(rate.get("calculation_type", "unit_rate"))
    if calculation_type == "parcel_weight_tier":
        min_weight = float(rate.get("weight_min_kg") or 0)
        max_weight = float(rate.get("weight_max_kg") or 0)
        if basis < min_weight or (max_weight and basis > max_weight):
            raise ValueError(f"每件计费重 {basis:.3f} kg 不在此费率的 {min_weight:g}-{max_weight:g} kg 区间")
        if cartons <= 0:
            raise ValueError("此渠道按每件/每票计费，请提供包裹数量")
        base = (basis * rate_value + float(rate.get("per_carton_fee", 0) or 0)) * cartons
    elif calculation_type == "tiered_kg":
        tiers = rate.get("tiers", [])
        if not tiers:
            raise ValueError("重量分段费率没有可用区间")
        billable = __import__("math").ceil(chargeable_weight)
        if billable <= 5:
            base = float(tiers[0]["first_5kg"])
        elif billable < 11:
            base = float(tiers[0]["first_5kg"]) + (billable - 5) * float(tiers[0]["renewal_per_kg"])
        else:
            applicable = [tier for tier in tiers if billable >= float(tier.get("min_kg", 0)) and tier.get("rate_per_kg") is not None]
            if not applicable:
                raise ValueError("计费重量超出已导入费率范围")
            base = billable * float(max(applicable, key=lambda item: float(item["min_kg"]))["rate_per_kg"])
    elif calculation_type == "weight_band_kg":
        tiers = [tier for tier in rate.get("tiers", []) if float(tier.get("min_kg", 0)) <= chargeable_weight]
        if not tiers:
            raise ValueError("货物重量低于此渠道可计价的最低重量")
        selected = max(tiers, key=lambda item: float(item.get("min_kg", 0)))
        base = chargeable_weight * float(selected["rate_per_kg"])
    elif calculation_type == "first_renewal":
        first_weight = float(rate.get("first_weight") or 0)
        first_price = float(rate.get("first_price") or 0)
        additional_weight = float(rate.get("additional_weight") or 0)
        additional_price = float(rate.get("additional_price") or 0)
        if not all(value > 0 for value in (first_weight, first_price, additional_weight, additional_price)):
            raise ValueError("首重/续重规则数据不完整，不能自动计算")
        if unit != "kg":
            raise ValueError("首重续重规则必须按 kg 计费")
        billed_steps = max(0, int(__import__("math").ceil((basis - first_weight) / additional_weight)))
        base = first_price + billed_steps * additional_price if basis > first_weight else first_price
    else:
        base = basis * rate_value
    base = max(base, min_charge)
    total = round(base + fixed + per_carton * cartons, 2)
    incoterm = str(data.get("incoterm", "EXW")).upper()
    seller_pays = {"EXW": [], "FCA": ["origin"], "FAS": ["origin"], "FOB": ["origin"], "CFR": ["origin", "freight"], "CPT": ["origin", "freight"], "CIF": ["origin", "freight", "insurance"], "CIP": ["origin", "freight", "insurance"], "DAP": ["origin", "freight", "destination"], "DPU": ["origin", "freight", "destination"], "DDP": ["origin", "freight", "destination", "duties"]}.get(incoterm)
    if seller_pays is None:
        raise ValueError("暂不支持该贸易条款的自动费用归属，请选择 Incoterms 2020 条款或手动核对")
    return {"rate_unit": unit, "rate": rate_value, "cartons": cartons, "volume_cbm": cbm, "gross_weight_kg": gross,
            "volumetric_weight_kg": round(volumetric_weight, 3), "chargeable_weight_kg": round(chargeable_weight, 3),
            "basis_quantity": round(basis, 3), "base_freight": round(base, 2), "fixed_fee": fixed, "calculation_type": calculation_type,
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
    workbook_rows: list[tuple[str, list[tuple[Any, ...]]]] = []
    if suffix == ".csv":
        sheets = [(Path(filename).stem, list(csv.DictReader(io.StringIO(content.decode("utf-8-sig", errors="replace")))))]
    elif suffix in {".xlsx", ".xlsm"}:
        from openpyxl import load_workbook
        workbook = load_workbook(io.BytesIO(content), read_only=True, data_only=True)
        sheets = []
        for sheet in workbook.worksheets:
            raw_rows = list(sheet.iter_rows(values_only=True))
            workbook_rows.append((sheet.title, raw_rows))
            headers = [str(v or "").strip() for v in (raw_rows[0] if raw_rows else ())]
            rows = [dict(zip(headers, values)) for values in raw_rows[1:] if any(v is not None for v in values)]
            if rows:
                sheets.append((sheet.title, rows))
    else:
        raise ValueError("运费表请上传 CSV 或 XLSX 文件")
    aliases = {"起运地": "origin", "起运港": "origin", "origin": "origin", "出发地": "origin", "目的地": "destination", "目的港": "destination", "destination": "destination", "运输方式": "method", "运输": "method", "method": "method", "渠道": "channel", "产品名称": "channel", "服务名称": "channel", "service": "channel", "货代": "carrier", "货代公司": "carrier", "物流公司": "carrier", "承运商": "carrier", "carrier": "carrier", "计价单位": "rate_unit", "单位": "rate_unit", "rate unit": "rate_unit", "计价方式": "rate_unit", "计费方式": "rate_unit", "单价": "rate", "运价": "rate", "价格": "rate", "rate": "rate", "币种": "currency", "currency": "currency", "最低收费": "minimum_charge", "最低运费": "minimum_charge", "最低消费": "minimum_charge", "minimum charge": "minimum_charge", "固定费用": "fixed_fee", "操作费": "fixed_fee", "fixed fee": "fixed_fee", "每箱费用": "per_carton_fee", "每箱": "per_carton_fee", "per carton fee": "per_carton_fee", "体积重系数": "volume_weight_kg_per_cbm", "体积系数": "volume_weight_kg_per_cbm", "kg/cbm": "volume_weight_kg_per_cbm", "抛比": "volume_weight_kg_per_cbm", "首重": "first_weight", "首重价格": "first_price", "续重": "additional_weight", "续重价格": "additional_price", "生效日期": "effective_from", "有效期": "effective_from"}
    unit_map = {"kg": "kg", "公斤": "kg", "千克": "kg", "cbm": "cbm", "立方": "cbm", "立方米": "cbm", "m3": "cbm", "m³": "cbm", "carton": "carton", "箱": "carton", "箱/件": "carton", "shipment": "shipment", "票": "shipment", "每票": "shipment"}
    # Freight quotes often use merged, multi-row headers rather than a flat
    # database table. Recognize the two common source layouts explicitly.
    special_rows: list[tuple[str, int, dict[str, Any]]] = []
    def cell(row: tuple[Any, ...], col: int) -> str:
        return str(row[col] or "").strip() if col < len(row) else ""
    def number(value: Any) -> float | None:
        try:
            return float(str(value).replace(",", "").strip()) if value not in (None, "") else None
        except (TypeError, ValueError):
            return None
    def weight_range(value: Any) -> tuple[float, float] | None:
        match = re.fullmatch(r"\s*(\d+(?:\.\d+)?)\s*[-–~至]\s*(\d+(?:\.\d+)?)\s*", str(value or ""))
        return (float(match.group(1)), float(match.group(2))) if match else None
    for sheet_name, raw_rows in workbook_rows:
        # Country + weight band + RMB/kg + RMB/ticket layout (for example,
        # 全球普货专线JS in 欧美普货F牌小包.xlsx).
        for header_idx, header in enumerate(raw_rows[:12]):
            joined = " ".join(cell(header, c).casefold() for c in range(len(header)))
            if "国家" not in joined or "重量" not in joined or "rmb/kg" not in joined or "rmb/票" not in joined:
                continue
            country = ""
            for row_idx in range(header_idx + 1, len(raw_rows)):
                row = raw_rows[row_idx]
                if cell(row, 0) and not any(key in cell(row, 0) for key in ("备注", "说明", "时效")):
                    country = cell(row, 0)
                band = weight_range(row[1] if len(row) > 1 else None)
                rate, ticket_fee = number(row[2] if len(row) > 2 else None), number(row[3] if len(row) > 3 else None)
                if not country or not band or rate is None:
                    continue
                special_rows.append((sheet_name, 1_000_000 + row_idx, {
                    "name": f"{Path(filename).stem} / {sheet_name} / {country} / {band[0]:g}-{band[1]:g}kg",
                    "carrier": "欧美普货F牌小包", "channel": sheet_name, "origin": "中国", "destination": country,
                    "method": "小包按单件重量分段计价", "currency": "RMB", "rate_unit": "parcel_weight",
                    "rate": rate, "per_carton_fee": ticket_fee or 0, "weight_min_kg": band[0], "weight_max_kg": band[1],
                    "calculation_type": "parcel_weight_tier", "classification_status": "confirmed_from_table",
                    "formula_summary": f"每件按重量区间 {band[0]:g}-{band[1]:g} kg：RMB {rate:g}/kg + RMB {ticket_fee or 0:g}/票",
                    "delivery_time": cell(row, 4), "source_sheet": sheet_name,
                }))
        # First-5kg + renewal + 11/21/51/101kg tier format in the PEL
        # European UPS sheet. Both volume divisor and 300kg split notice are
        # retained as explicit rule metadata from the workbook.
        divisor = 0
        sheet_text = " ".join(cell(row, c) for row in raw_rows for c in range(min(len(row), 18)))
        if re.search(r"(?:除|/)\s*8000", sheet_text):
            divisor = 8000
        for header_idx, header in enumerate(raw_rows):
            header_text = " ".join(cell(header, c).casefold() for c in range(len(header)))
            if "首重5kg" not in header_text or "续重1kg" not in header_text or "101kg+" not in header_text:
                continue
            channel_row = raw_rows[header_idx - 1] if header_idx else ()
            for block, country_col in enumerate((1, 9)):
                channel = cell(channel_row, country_col + 1) or cell(channel_row, country_col + 2) or f"欧洲 UPS 费率区块 {block + 1}"
                for row_idx in range(header_idx + 1, len(raw_rows)):
                    row = raw_rows[row_idx]
                    country_group = cell(row, country_col)
                    if not country_group:
                        if row_idx > header_idx + 1:
                            break
                        continue
                    first_price, renewal = number(row[country_col + 1] if len(row) > country_col + 1 else None), number(row[country_col + 2] if len(row) > country_col + 2 else None)
                    tier_rates = [number(row[country_col + offset] if len(row) > country_col + offset else None) for offset in range(3, 7)]
                    if first_price is None or renewal is None or any(value is None for value in tier_rates):
                        continue
                    tiers = [{"first_5kg": first_price, "renewal_per_kg": renewal}, *[{"min_kg": minimum, "rate_per_kg": rate} for minimum, rate in zip((11, 21, 51, 101), tier_rates)]]
                    special_rows.append((sheet_name, 2_000_000 + row_idx * 10 + block, {
                        "name": f"{Path(filename).stem} / {sheet_name} / {channel} / {country_group}",
                        "carrier": cell(raw_rows[0], 0) or Path(filename).stem, "channel": channel, "origin": "中国", "destination": country_group,
                        "method": "空派 UPS 重量分段", "currency": "RMB", "rate_unit": "kg", "rate": tier_rates[-1],
                        "calculation_type": "tiered_kg", "tiers": tiers, "volumetric_divisor_cm3": divisor,
                        "split_above_kg": 300, "classification_status": "confirmed_from_table",
                        "formula_summary": f"首重5kg RMB {first_price:g}，续重 RMB {renewal:g}/kg；11/21/51/101kg+ 单价分别为 " + "/".join(f"{value:g}" for value in tier_rates) + f" RMB/kg；材积除数 {divisor or '未识别'}；超过300kg需分票",
                        "source_sheet": sheet_name,
                    }))
        # PEL DHL sections use two horizontal blocks and split each service
        # into single-parcel pricing and multi-piece weight bands.
        for header_idx, header in enumerate(raw_rows):
            header_text = " ".join(cell(header, c).casefold() for c in range(len(header)))
            previous_text = " ".join(cell(raw_rows[header_idx - 1], c).casefold() for c in range(len(raw_rows[header_idx - 1]))) if header_idx else ""
            if "国家/重量" not in previous_text or "运费/kg" not in header_text or not any(token in header_text for token in ("操作费/票", "操作费/件")):
                continue
            label_row = header
            channel_row = raw_rows[header_idx - 2] if header_idx > 1 else ()
            for block, country_col in enumerate((1, 10)):
                band_columns: list[tuple[float, int]] = []
                for col in range(country_col + 3, min(len(header), country_col + 9)):
                    label = cell(label_row, col) or cell(header, col)
                    match = re.match(r"\s*(\d+(?:\.\d+)?)\s*(?:-|kg|\+)", label.casefold())
                    if match:
                        band_columns.append((float(match.group(1)), col))
                if not band_columns:
                    continue
                channel = cell(channel_row, country_col + 1) or f"欧洲 DHL 费率区块 {block + 1}"
                for row_idx in range(header_idx + 1, len(raw_rows)):
                    row = raw_rows[row_idx]
                    country_group = cell(row, country_col)
                    if not country_group:
                        if row_idx > header_idx + 1:
                            break
                        continue
                    per_kg, per_ticket = number(row[country_col + 1] if len(row) > country_col + 1 else None), number(row[country_col + 2] if len(row) > country_col + 2 else None)
                    if per_kg is None:
                        continue
                    minimum_weight = min((minimum for minimum, _ in band_columns), default=0)
                    special_rows.append((sheet_name, 3_000_000 + row_idx * 20 + block * 2, {
                        "name": f"{Path(filename).stem} / {sheet_name} / {channel} / {country_group} / single parcel",
                        "carrier": cell(raw_rows[0], 0) or Path(filename).stem, "channel": f"{channel} 单件", "origin": "中国", "destination": country_group,
                        "method": "DHL 按单件重量+票费", "currency": "RMB", "rate_unit": "parcel_weight", "rate": per_kg,
                        "per_carton_fee": per_ticket or 0, "weight_min_kg": 0.02, "weight_max_kg": 30,
                        "calculation_type": "parcel_weight_tier", "classification_status": "confirmed_from_table",
                        "formula_summary": f"单件 0.02-30 kg：RMB {per_kg:g}/kg + RMB {per_ticket or 0:g}/票",
                        "volumetric_divisor_cm3": divisor, "source_sheet": sheet_name,
                    }))
                    tiers = [{"min_kg": minimum, "rate_per_kg": number(row[col])} for minimum, col in band_columns if number(row[col]) is not None]
                    if tiers:
                        special_rows.append((sheet_name, 3_000_000 + row_idx * 20 + block * 2 + 1, {
                            "name": f"{Path(filename).stem} / {sheet_name} / {channel} / {country_group} / multi-piece",
                            "carrier": cell(raw_rows[0], 0) or Path(filename).stem, "channel": f"{channel} 多件", "origin": "中国", "destination": country_group,
                            "method": "DHL 多件按总重量分段", "currency": "RMB", "rate_unit": "kg", "rate": tiers[-1]["rate_per_kg"],
                            "calculation_type": "weight_band_kg", "tiers": tiers, "volumetric_divisor_cm3": divisor,
                            "minimum_weight_kg": minimum_weight, "classification_status": "confirmed_from_table",
                            "formula_summary": "多件合票按总计费重量分段计价：" + "; ".join(f"{tier['min_kg']:g}kg+ RMB {tier['rate_per_kg']:g}/kg" for tier in tiers),
                            "source_sheet": sheet_name,
                        }))
    saved = []
    existing = {(str(record["data"].get("source_sheet", "")), int(record["data"].get("source_row", -1))): record for record in list_records("freight_rate", limit=2000) if record["data"].get("source_file") == filename}
    seen_rows: set[tuple[str, int]] = set()
    source_row = 0
    for special_sheet, special_row, special_data in special_rows:
        sheets.append((special_sheet, [{"__special_data": special_data, "__source_row": special_row}]))
    for sheet_name, rows in sheets:
        for row in rows:
            source_row += 1
            special_data = row.get("__special_data") if isinstance(row, dict) else None
            record_row = int(row.get("__source_row", source_row)) if special_data else source_row
            if special_data:
                data = dict(special_data)
                data["source_sheet"] = sheet_name[:120]
                data["source_row"] = record_row
                data["source_file"] = filename[:240]
                data["classification_status"] = data.get("classification_status", "confirmed_from_table")
                data["id"] = existing[(sheet_name, record_row)]["id"] if (sheet_name, record_row) in existing else data.get("id")
                seen_rows.add((sheet_name, record_row))
                saved.append(save_record("freight_rate", data, actor))
                continue
            mapped = {aliases.get(str(k or "").strip().casefold(), str(k or "").strip()): v for k, v in row.items()}
            if not mapped.get("rate") and not mapped.get("first_price"):
                continue
            data = {k: v for k, v in mapped.items() if k in {"origin", "destination", "method", "channel", "carrier", "rate_unit", "rate", "currency", "minimum_charge", "fixed_fee", "per_carton_fee", "volume_weight_kg_per_cbm", "effective_from", "first_weight", "first_price", "additional_weight", "additional_price"}}
            try:
                for key in ("rate", "minimum_charge", "fixed_fee", "per_carton_fee", "volume_weight_kg_per_cbm", "first_weight", "first_price", "additional_weight", "additional_price"):
                    if key in data and data[key] not in (None, ""):
                        data[key] = float(str(data[key]).replace(",", ""))
            except (TypeError, ValueError):
                continue
            raw_unit = str(data.get("rate_unit") or "").strip().casefold()
            data["rate_unit"] = unit_map.get(raw_unit, raw_unit or "")
            data["source_file"] = filename[:240]
            data["source_sheet"] = sheet_name[:120]
            data["source_row"] = source_row
            data["carrier"] = str(data.get("carrier") or Path(filename).stem).strip()[:160]
            data["calculation_type"] = "first_renewal" if data.get("first_weight") and data.get("first_price") and data.get("additional_weight") and data.get("additional_price") else "unit_rate"
            if data["calculation_type"] == "first_renewal" and not data["rate_unit"]:
                data["rate_unit"] = "kg"
            data["classification_status"] = "confirmed_from_table" if data["rate_unit"] in {"kg", "cbm", "carton", "shipment"} else "needs_review"
            data["formula_summary"] = (f"首重 {data.get('first_weight')} kg/{data.get('first_price')}，续重每 {data.get('additional_weight')} kg/{data.get('additional_price')}" if data["calculation_type"] == "first_renewal" else f"按 {data['rate_unit']} 计价；单价 {data.get('rate')}；最低收费 {data.get('minimum_charge', 0)}；固定费 {data.get('fixed_fee', 0)}；每箱费 {data.get('per_carton_fee', 0)}")
            data["name"] = " / ".join(str(data.get(k, "")) for k in ("carrier", "channel", "origin", "destination", "method", "rate_unit", "rate") if str(data.get(k, ""))) or f"{filename} 第 {source_row} 行"
            key = (sheet_name, source_row)
            if key in existing:
                data["id"] = existing[key]["id"]
            seen_rows.add(key)
            saved.append(save_record("freight_rate", data, actor))
    if saved:
        for row_key, record in existing.items():
            if (str(record["data"].get("source_sheet", "")), int(record["data"].get("source_row", -1))) not in seen_rows:
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
