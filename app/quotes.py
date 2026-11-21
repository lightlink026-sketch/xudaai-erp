from __future__ import annotations

import io
import json
import os
import re
import sqlite3
import uuid
from datetime import date
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any
from xml.sax.saxutils import escape

from fastapi import HTTPException
from fastapi.responses import FileResponse


def _money(value: Any, field: str, places: int) -> Decimal:
    try:
        number = Decimal(str(value))
    except (InvalidOperation, ValueError):
        raise HTTPException(422, f"{field} 必须是有效数字")
    if not number.is_finite() or abs(number) > Decimal("999999999999"):
        raise HTTPException(422, f"{field} 超出允许范围")
    return number.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)


def normalize_quote(payload: dict[str, Any]) -> dict[str, Any]:
    currency = str(payload.get("currency", "USD")).strip().upper()
    if not re.fullmatch(r"[A-Z]{3}", currency):
        raise HTTPException(422, "币种请使用三位代码，例如 USD、EUR 或 CNY")
    places = 0 if currency == "JPY" else 2
    items = payload.get("items")
    if not isinstance(items, list) or not 1 <= len(items) <= 100:
        raise HTTPException(422, "报价单需要 1 到 100 个产品明细")
    normalized_items = []
    subtotal = Decimal("0")
    for index, raw in enumerate(items, 1):
        if not isinstance(raw, dict):
            raise HTTPException(422, f"第 {index} 行格式无效")
        name = str(raw.get("name", "")).strip()[:200]
        if not name:
            raise HTTPException(422, f"请填写第 {index} 行的产品名称或型号")
        quantity = _money(raw.get("quantity", 1), f"第 {index} 行数量", 3)
        unit_price = _money(raw.get("unit_price", 0), f"第 {index} 行单价", places)
        discount = _money(raw.get("discount_percent", 0), f"第 {index} 行折扣", 2)
        moq = _money(raw.get("moq", 0), f"第 {index} 行 MOQ", 0)
        if quantity <= 0 or unit_price < 0 or moq < 0 or not 0 <= discount <= 100:
            raise HTTPException(422, f"第 {index} 行数量、单价或折扣超出范围")
        amount = (quantity * unit_price * (Decimal("1") - discount / Decimal("100"))).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
        subtotal += amount
        image_path = str(raw.get("image_path", "")).strip().replace("\\", "/")[:1000]
        try:
            image_file_id = max(0, min(2_147_483_647, int(raw.get("image_file_id", 0) or 0)))
        except (TypeError, ValueError):
            image_file_id = 0
        if image_path:
            parsed_image_path = PurePosixPath(image_path)
            if parsed_image_path.is_absolute() or ".." in parsed_image_path.parts:
                raise HTTPException(422, f"第 {index} 行产品图片路径无效")
        normalized_items.append({
            "name": name,
            "image_path": image_path,
            "image_file_id": image_file_id,
            "description": str(raw.get("description", "")).strip()[:1500],
            "configuration": str(raw.get("configuration", "")).strip()[:1000],
            "colors": str(raw.get("colors", "")).strip()[:1000],
            "size_breakdown": str(raw.get("size_breakdown", "")).strip()[:1000],
            "carton_spec": str(raw.get("carton_spec", "")).strip()[:200],
            "carton_volume": str(raw.get("carton_volume", "")).strip()[:100],
            "gross_weight": str(raw.get("gross_weight", "")).strip()[:100],
            "net_weight": str(raw.get("net_weight", "")).strip()[:100],
            "moq": str(moq),
            "quantity": str(quantity),
            "unit": str(raw.get("unit", "pcs")).strip()[:30] or "pcs",
            "unit_price": str(unit_price),
            "discount_percent": str(discount),
            "amount": str(amount),
            "source": str(raw.get("source", "人工确认")).strip()[:500],
        })
    shipping = _money(payload.get("shipping", 0), "运费", places)
    other_fees = _money(payload.get("other_fees", 0), "其他费用", places)
    tax_amount = _money(payload.get("tax_amount", 0), "税额", places)
    if shipping < 0 or other_fees < 0 or tax_amount < 0:
        raise HTTPException(422, "运费和其他费用不能为负数")
    quote_date = str(payload.get("quote_date", "")).strip() or date.today().isoformat()
    try:
        date.fromisoformat(quote_date)
    except ValueError:
        raise HTTPException(422, "报价日期格式应为 YYYY-MM-DD")
    data = {
        "customer": str(payload.get("customer", "")).strip()[:200],
        "customer_address": str(payload.get("customer_address", "")).strip()[:500],
        "quote_date": quote_date,
        "valid_until": str(payload.get("valid_until", "")).strip()[:30],
        "currency": currency,
        "exchange_rate": str(payload.get("exchange_rate", "")).strip()[:40],
        "exchange_date": str(payload.get("exchange_date", "")).strip()[:30],
        "origin_port": str(payload.get("origin_port", "")).strip()[:150],
        "destination_port": str(payload.get("destination_port", "")).strip()[:150],
        "insurance_term": str(payload.get("insurance_term", "Buyer")).strip()[:100],
        "document_type": "pi" if str(payload.get("document_type", "quote")).lower() == "pi" else "quote",
        "language": "en" if str(payload.get("language", "zh")).lower() == "en" else "zh",
        "template_name": str(payload.get("template_name", "")).strip()[:150],
        "incoterm": str(payload.get("incoterm", "")).strip()[:100],
        "payment_terms": str(payload.get("payment_terms", "")).strip()[:500],
        "payment_note": str(payload.get("payment_note", "")).strip()[:500],
        "shipping_term": str(payload.get("shipping_term", "Excluded")).strip()[:100],
        "tax_term": str(payload.get("tax_term", "Excluded")).strip()[:100],
        "tax_amount": str(tax_amount),
        "tax_note": str(payload.get("tax_note", "")).strip()[:300],
        "delivery_time": str(payload.get("delivery_time", "")).strip()[:200],
        "bank_details": str(payload.get("bank_details", "")).strip()[:1000],
        "notes": str(payload.get("notes", "")).strip()[:2000],
        "company_name": str(payload.get("company_name", "")).strip()[:200],
        "company_address": str(payload.get("company_address", "")).strip()[:500],
        "company_phone": str(payload.get("company_phone", "")).strip()[:100],
        "company_email": str(payload.get("company_email", "")).strip()[:200],
        "company_contact": str(payload.get("company_contact", "")).strip()[:1000] or "\n".join(value for value in [str(payload.get("company_address", "")).strip(), str(payload.get("company_phone", "")).strip(), str(payload.get("company_email", "")).strip()] if value),
        "shipping": str(shipping),
        "other_fees": str(other_fees),
        "subtotal": str(subtotal.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)),
        "total": str((subtotal + shipping + other_fees + tax_amount).quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)),
        "items": normalized_items,
    }
    return data


def list_quotes(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    rows = conn.execute("SELECT id, quote_no, customer, currency, status, data, updated_at FROM quotes ORDER BY updated_at DESC LIMIT 200").fetchall()
    result = []
    for row in rows:
        data = json.loads(row["data"])
        result.append({"id": row["id"], "quote_no": row["quote_no"], "customer": row["customer"], "currency": row["currency"], "status": row["status"], "total": data["total"], "updated_at": row["updated_at"]})
    return result


def render_pdf(data: dict[str, Any], quote_no: str, output_path: Path, template_path: Path | None = None, library_dir: Path | None = None) -> None:
    try:
        from reportlab.lib import colors
        from reportlab.lib.enums import TA_CENTER, TA_RIGHT
        from reportlab.lib.pagesizes import A4
        from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
        from reportlab.lib.units import mm
        from reportlab.pdfbase import pdfmetrics
        from reportlab.pdfbase.ttfonts import TTFont
        from reportlab.platypus import Image as PDFImage, Paragraph, SimpleDocTemplate, Spacer, Table, TableStyle
        from PIL import Image as PILImage
    except ImportError as exc:
        raise HTTPException(503, "PDF 组件尚未安装，请安装 requirements.txt 后重启应用") from exc

    font_candidates = [
        os.getenv("QUOTE_FONT_PATH", ""),
        r"C:\Windows\Fonts\msyh.ttc",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/noto/NotoSansCJK-Regular.ttc",
    ]
    font_path = next((Path(item) for item in font_candidates if item and Path(item).is_file()), None)
    if not font_path:
        raise HTTPException(503, "找不到中文 PDF 字体，请配置 QUOTE_FONT_PATH 或安装 Noto CJK 字体")
    try:
        if "QuoteCJK" not in pdfmetrics.getRegisteredFontNames():
            pdfmetrics.registerFont(TTFont("QuoteCJK", str(font_path), subfontIndex=0))
    except Exception as exc:
        raise HTTPException(503, f"无法载入中文字体：{font_path.name}") from exc

    output_path.parent.mkdir(parents=True, exist_ok=True)
    doc = SimpleDocTemplate(str(output_path), pagesize=A4, rightMargin=16*mm, leftMargin=16*mm, topMargin=15*mm, bottomMargin=16*mm, title=quote_no)
    styles = getSampleStyleSheet()
    title = ParagraphStyle("QuoteTitle", parent=styles["Title"], fontName="QuoteCJK", fontSize=18, leading=24, alignment=TA_CENTER, textColor=colors.HexColor("#244d3b"), spaceAfter=9)
    body = ParagraphStyle("QuoteBody", parent=styles["BodyText"], fontName="QuoteCJK", fontSize=8.5, leading=12, wordWrap="CJK")
    small = ParagraphStyle("QuoteSmall", parent=body, fontSize=7, leading=9, textColor=colors.HexColor("#59645d"))
    right = ParagraphStyle("QuoteRight", parent=body, alignment=TA_RIGHT)
    story = []
    safe = lambda value: escape(str(value or "")).replace("\n", "<br/>")
    english = data.get("language") == "en"
    word = lambda zh, en: en if english else zh
    if english:
        title_text = "PROFORMA INVOICE" if data.get("document_type") == "pi" else "QUOTATION"
    else:
        title_text = "形式发票 / PROFORMA INVOICE" if data.get("document_type") == "pi" else "产品报价单 / QUOTATION"
    def product_photo(item: dict[str, Any], max_width: float = 23*mm, max_height: float = 23*mm):
        relative = str(item.get("image_path", "")).strip()
        if not relative or library_dir is None:
            return Paragraph("—", small)
        try:
            image_path = (library_dir.resolve() / Path(*PurePosixPath(relative).parts)).resolve()
            if not image_path.is_relative_to(library_dir.resolve()) or not image_path.is_file() or image_path.suffix.lower() not in {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}:
                return Paragraph("图片不可用", small)
            with PILImage.open(image_path) as source:
                image = source.convert("RGB")
                image.thumbnail((600, 600))
                buffer = io.BytesIO()
                image.save(buffer, format="JPEG", quality=84, optimize=True)
                width, height = image.size
            scale = min(max_width / width, max_height / height)
            buffer.seek(0)
            return PDFImage(buffer, width=width * scale, height=height * scale)
        except Exception:
            return Paragraph("图片不可用", small)

    def product_gallery() -> list[Any]:
        photo_items = [item for item in data["items"] if item.get("image_path")]
        if not photo_items:
            return []
        gallery_rows = [[Paragraph(word("产品图片", "Product Image"), body), Paragraph(word("产品 / 型号", "Product / Model"), body)]]
        for item in photo_items:
            gallery_rows.append([product_photo(item, 30*mm, 25*mm), Paragraph(safe(item["name"]), body)])
        gallery = Table(gallery_rows, colWidths=[36*mm, 142*mm], repeatRows=1, hAlign="LEFT")
        gallery.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#eaf1eb")),("FONTNAME",(0,0),(-1,-1),"QuoteCJK"),
            ("VALIGN",(0,0),(-1,-1),"MIDDLE"),("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#dce4dd")),
            ("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),
            ("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5),
        ]))
        return [Paragraph(word("产品图片", "Product Images"), body), Spacer(1, 2*mm), gallery, Spacer(1, 5*mm)]
    def footer(canvas, _doc):
        canvas.saveState()
        canvas.setFont("QuoteCJK", 7)
        canvas.setFillColor(colors.HexColor("#89958d"))
        canvas.drawString(16*mm, 9*mm, quote_no)
        canvas.drawRightString(A4[0]-16*mm, 9*mm, f"{_doc.page}")
        canvas.restoreState()
    if template_path and template_path.is_file():
        from docx import Document
        template = Document(str(template_path))
        items_text = "\n".join(f"{i}. {item['name']} | {item.get('description') or ''} | {item.get('colors') or ''} | {item.get('size_breakdown') or ''} | {item.get('configuration') or ''} | {word('箱规', 'Carton')}: {item.get('carton_spec') or '—'} | {word('单箱体积', 'Volume/carton')}: {item.get('carton_volume') or '—'} | {word('毛重', 'Gross weight')}: {item.get('gross_weight') or '—'} | {word('净重', 'Net weight')}: {item.get('net_weight') or '—'} | {item['quantity']} {item['unit']} × {item['unit_price']} = {item['amount']} {data['currency']}" for i, item in enumerate(data["items"], 1))
        values = {
            "quote_no": quote_no, "document_no": quote_no, "customer": data.get("customer", ""),
            "customer_address": data.get("customer_address", ""), "quote_date": data.get("quote_date", ""),
            "valid_until": data.get("valid_until", ""), "currency": data["currency"], "incoterm": data.get("incoterm", ""),
            "payment_terms": data.get("payment_terms", ""), "payment_note": data.get("payment_note", ""),
            "shipping_term": data.get("shipping_term", ""), "tax_term": data.get("tax_term", ""),
            "tax_note": data.get("tax_note", ""), "company_name": data.get("company_name", ""),
            "company_address": data.get("company_address", ""), "company_phone": data.get("company_phone", ""),
            "company_email": data.get("company_email", ""),
            "exchange_rate": data.get("exchange_rate", ""), "exchange_date": data.get("exchange_date", ""),
            "origin_port": data.get("origin_port", ""), "destination_port": data.get("destination_port", ""),
            "insurance_term": data.get("insurance_term", ""),
            "delivery_time": data.get("delivery_time", ""), "bank_details": data.get("bank_details", ""),
            "company_contact": data.get("company_contact", ""), "subtotal": data["subtotal"],
            "shipping": data["shipping"], "other_fees": data["other_fees"], "total": data["total"],
            "tax_amount": data.get("tax_amount", "0"),
            "items": items_text, "notes": data.get("notes", ""), "document_title": title_text,
        }
        token = re.compile(r"\{\{\s*([a-z_]+)\s*\}\}")
        def fill_template(text: str) -> str:
            return token.sub(lambda match: str(values.get(match.group(1), "")), text)
        if not any("{{document_title}}" in paragraph.text for paragraph in template.paragraphs):
            story.append(Paragraph(safe(title_text), title))
        for paragraph in template.paragraphs:
            text = fill_template(paragraph.text).strip()
            if text:
                story.append(Paragraph(safe(text), body))
        for doc_table in template.tables:
            table_rows = []
            for doc_row in doc_table.rows:
                table_rows.append([Paragraph(safe(fill_template(cell.text)), small) for cell in doc_row.cells])
            if table_rows:
                widths = [178*mm/max(1, len(table_rows[0]))] * len(table_rows[0])
                story.extend([Table(table_rows, colWidths=widths, repeatRows=1, style=TableStyle([("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#dce4dd")),("VALIGN",(0,0),(-1,-1),"TOP"),("BACKGROUND",(0,0),(-1,0),colors.HexColor("#eaf1eb")),("LEFTPADDING",(0,0),(-1,-1),5),("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),5),("BOTTOMPADDING",(0,0),(-1,-1),5)])), Spacer(1, 3*mm)])
        story.extend(product_gallery())
        doc.build(story, onFirstPage=footer, onLaterPages=footer)
        return
    story.append(Paragraph(safe(title_text), title))
    if data.get("company_name"):
        story.extend([Paragraph(safe(data["company_name"]), ParagraphStyle("Company", parent=body, fontSize=12, leading=16)), Paragraph(safe(data.get("company_contact", "")), small), Spacer(1, 5*mm)])
    meta = [
        [Paragraph(f"{word('报价单号', 'Quote No.')}: {safe(quote_no)}", body), Paragraph(f"{word('日期', 'Date')}: {safe(data['quote_date'])}", right)],
        [Paragraph(f"{word('客户', 'Customer')}: {safe(data.get('customer') or '—')}", body), Paragraph(f"{word('有效期至', 'Valid Until')}: {safe(data.get('valid_until') or '—')}", right)],
        [Paragraph(f"{word('客户地址', 'Customer Address')}: {safe(data.get('customer_address') or '—')}", body), Paragraph(f"{word('币种', 'Currency')}: {safe(data['currency'])}", right)],
    ]
    story.append(Table(meta, colWidths=[105*mm, 73*mm], style=TableStyle([("VALIGN",(0,0),(-1,-1),"TOP"),("BOTTOMPADDING",(0,0),(-1,-1),5),("LINEBELOW",(0,-1),(-1,-1),0.6,colors.HexColor("#dce4dd"))])))
    story.append(Spacer(1, 6*mm))
    rows = [[Paragraph(word("图片", "Image"), body), Paragraph(word("产品 / 型号", "Product / Model"), body), Paragraph(word("描述", "Description"), body), Paragraph(word("数量", "Quantity"), body), Paragraph(word("单价", "Unit Price"), body), Paragraph(word("折扣", "Discount"), body), Paragraph(word("金额", "Amount"), body)]]
    for item in data["items"]:
        details = item.get("description") or "—"
        if item.get("source"):
            details += f"\n{word('价格/资料来源', 'Source')}: {item['source']}"
        if item.get("configuration"):
            details += f"\n{word('颜色/尺码/配比', 'Configuration')}: {item['configuration']}"
        if item.get("colors"):
            details += f"\n{word('颜色/颜色数量', 'Colors / Color Qty.')}: {item['colors']}"
        if item.get("size_breakdown"):
            details += f"\n{word('尺码配比', 'Size Breakdown')}: {item['size_breakdown']}"
        for key, zh_label, en_label in (
            ("carton_spec", "箱规", "Carton dimensions"),
            ("carton_volume", "单箱体积", "Volume per carton"),
            ("gross_weight", "毛重", "Gross weight"),
            ("net_weight", "净重", "Net weight"),
        ):
            if item.get(key):
                details += f"\n{word(zh_label, en_label)}: {item[key]}"
        if Decimal(item.get("moq", "0")) > 0:
            details += f"\nMOQ: {item['moq']} {item['unit']}"
        rows.append([
            product_photo(item), Paragraph(safe(item["name"]), body), Paragraph(safe(details), small),
            Paragraph(f"{safe(item['quantity'])} {safe(item['unit'])}", body), Paragraph(safe(item["unit_price"]), body),
            Paragraph(f"{safe(item['discount_percent'])}%", body), Paragraph(safe(item["amount"]), body),
        ])
    table = Table(rows, colWidths=[26*mm, 30*mm, 40*mm, 20*mm, 20*mm, 16*mm, 20*mm], repeatRows=1, hAlign="LEFT")
    table.setStyle(TableStyle([
        ("BACKGROUND",(0,0),(-1,0),colors.HexColor("#eaf1eb")),("TEXTCOLOR",(0,0),(-1,0),colors.HexColor("#244d3b")),
        ("FONTNAME",(0,0),(-1,-1),"QuoteCJK"),("VALIGN",(0,0),(-1,-1),"TOP"),
        ("GRID",(0,0),(-1,-1),0.35,colors.HexColor("#dce4dd")),("LEFTPADDING",(0,0),(-1,-1),5),
        ("RIGHTPADDING",(0,0),(-1,-1),5),("TOPPADDING",(0,0),(-1,-1),6),("BOTTOMPADDING",(0,0),(-1,-1),6),
    ]))
    story.extend([table, Spacer(1, 5*mm)])
    summary = [
        [Paragraph(word("商品小计", "Subtotal"), body), Paragraph(f"{safe(data['currency'])} {safe(data['subtotal'])}", right)],
        [Paragraph(word("运费", "Shipping"), body), Paragraph(f"{safe(data['currency'])} {safe(data['shipping'])}", right)],
        [Paragraph(word("其他费用", "Other Charges"), body), Paragraph(f"{safe(data['currency'])} {safe(data['other_fees'])}", right)],
        [Paragraph(word("税额", "Tax"), body), Paragraph(f"{safe(data['currency'])} {safe(data.get('tax_amount', '0'))}", right)],
        [Paragraph(word("总计", "Total"), ParagraphStyle("TotalLabel", parent=body, fontSize=11)), Paragraph(f"{safe(data['currency'])} {safe(data['total'])}", ParagraphStyle("Total", parent=right, fontSize=12, textColor=colors.HexColor("#17765d")))],
    ]
    story.append(Table(summary, colWidths=[140*mm, 38*mm], hAlign="RIGHT", style=TableStyle([("ALIGN",(1,0),(1,-1),"RIGHT"),("LINEABOVE",(0,4),(-1,4),0.8,colors.HexColor("#9db7a4")),("TOPPADDING",(0,0),(-1,-1),4),("BOTTOMPADDING",(0,0),(-1,-1),4)])))
    terms = [(word("贸易条款", "Trade Terms"), " / ".join(x for x in [data.get("incoterm", ""), data.get("origin_port", ""), data.get("destination_port", "")] if x)), (word("付款条款", "Payment Terms"), " / ".join(x for x in [data.get("payment_terms", ""), data.get("payment_note", "")] if x)), (word("运费", "Shipping"), f"{data.get('shipping_term','')}; {data.get('currency')} {data.get('shipping','0')}"), (word("保险", "Insurance"), data.get("insurance_term")), (word("税费", "Tax Terms"), " / ".join(x for x in [data.get("tax_term", ""), data.get("tax_note", ""), f"{data.get('currency')} {data.get('tax_amount','0')}"] if x)), (word("汇率基准", "Exchange Rate Basis"), f"{data.get('exchange_rate','')} / {data.get('exchange_date','')}" if data.get("exchange_rate") else ""), (word("交付时间", "Delivery Time"), data.get("delivery_time")), (word("收款信息", "Bank Details"), data.get("bank_details")), (word("备注", "Notes"), data.get("notes"))]
    for label, value in terms:
        if value:
            story.extend([Spacer(1, 4*mm), Paragraph(f"<b>{label}</b>：{safe(value)}", body)])
    story.extend([Spacer(1, 7*mm), Paragraph(word("本报价内容以双方最终确认为准。", "This quotation is subject to final confirmation by both parties."), small)])

    doc.build(story, onFirstPage=footer, onLaterPages=footer)


def pdf_response(db_path: Path, quote_id: str, quotes_dir: Path, templates_dir: Path | None = None, library_dir: Path | None = None) -> FileResponse:
    with sqlite3.connect(db_path) as conn:
        conn.row_factory = sqlite3.Row
        row = conn.execute("SELECT quote_no, data FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到这张报价单")
    try:
        safe_id = str(uuid.UUID(quote_id))
    except (ValueError, AttributeError):
        raise HTTPException(400, "无效的报价单编号")
    output_path = (quotes_dir / f"{safe_id}.pdf").resolve()
    if not output_path.is_relative_to(quotes_dir.resolve()):
        raise HTTPException(400, "无效的报价单编号")
    data = json.loads(row["data"])
    template_name = str(data.get("template_name", ""))
    templates_dir = (templates_dir or quotes_dir.parent / "templates").resolve()
    template_path = (templates_dir / template_name).resolve() if template_name else None
    if template_path and (not template_path.is_relative_to(templates_dir) or not template_path.is_file()):
        raise HTTPException(404, "这张单据引用的模板不存在，请重新选择模板")
    render_pdf(data, row["quote_no"], output_path, template_path, library_dir)
    filename = f"{row['quote_no']}.pdf"
    return FileResponse(output_path, media_type="application/pdf", filename=filename)
