from __future__ import annotations

import csv
import hashlib
import json
import re
import shutil
import uuid
from datetime import date
from pathlib import Path
from typing import Any

from docx import Document
from openpyxl import load_workbook
from pptx import Presentation
from pypdf import PdfReader

IMAGE_EXTS = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
VIDEO_EXTS = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac"}
DOC_EXTS = {".pdf", ".docx", ".xlsx", ".xlsm", ".pptx", ".txt", ".md", ".csv", ".json"}
SUPPORTED_EXTS = IMAGE_EXTS | VIDEO_EXTS | DOC_EXTS
REQUIRED_PRODUCT_FIELDS = {
    "name": "产品名称",
    "supplier_name": "供应商",
    "unit_price": "采购单价",
    "carton_spec": "箱规尺寸",
    "gross_weight_kg_per_carton": "单箱毛重",
}


def missing_product_fields(values: dict[str, Any]) -> list[str]:
    return [label for key, label in REQUIRED_PRODUCT_FIELDS.items() if not str(values.get(key, "") or "").strip()]


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def safe_stem(value: str) -> str:
    value = re.sub(r"[<>:\"/\\|?*\x00-\x1f]+", "_", value.strip())
    value = re.sub(r"\s+", "_", value)
    return value[:90].strip("._ ") or "未命名产品"


def read_text(path: Path, max_chars: int = 30000) -> str:
    ext = path.suffix.lower()
    try:
        if ext in {".txt", ".md", ".json", ".csv"}:
            if ext == ".csv":
                with path.open("r", encoding="utf-8-sig", errors="replace", newline="") as stream:
                    rows = list(csv.reader(stream))[:300]
                return "\n".join(" | ".join(cell.strip() for cell in row[:30]) for row in rows)[:max_chars]
            return path.read_text(encoding="utf-8-sig", errors="replace")[:max_chars]
        if ext in {".xlsx", ".xlsm"}:
            workbook = load_workbook(path, read_only=True, data_only=True, keep_vba=ext == ".xlsm")
            lines = []
            for sheet in workbook.worksheets[:12]:
                lines.append(f"[工作表: {sheet.title}]")
                for index, row in enumerate(sheet.iter_rows(values_only=True)):
                    if index >= 400:
                        break
                    cells = [str(value).strip() if value is not None else "" for value in row[:40]]
                    if any(cells):
                        lines.append(" | ".join(cells))
            workbook.close()
            return "\n".join(lines)[:max_chars]
        if ext == ".pdf":
            reader = PdfReader(str(path))
            return "\n".join((page.extract_text() or "") for page in reader.pages[:80])[:max_chars]
        if ext == ".docx":
            document = Document(path)
            lines = [paragraph.text for paragraph in document.paragraphs if paragraph.text.strip()]
            for table in document.tables:
                for row in table.rows:
                    lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            return "\n".join(lines)[:max_chars]
        if ext == ".pptx":
            presentation = Presentation(path)
            lines = []
            for slide in presentation.slides[:100]:
                for shape in slide.shapes:
                    if getattr(shape, "has_text_frame", False) and shape.text.strip():
                        lines.append(shape.text.strip())
                    if getattr(shape, "has_table", False):
                        for row in shape.table.rows:
                            lines.append(" | ".join(cell.text.strip() for cell in row.cells))
            return "\n".join(lines)[:max_chars]
    except Exception as exc:
        return f"[解析失败: {path.name}: {exc}]"
    return ""


PRODUCT_COLUMN_ALIASES = {
    "name": {"产品名称", "产品名", "品名", "商品名称", "商品名", "商品标题", "产品标题", "标题", "款名", "product name", "product", "name", "title"},
    "supplier_name": {"供应商", "供应商名称", "供货商", "厂家", "供应厂家", "supplier", "supplier name", "vendor"},
    "brand": {"品牌", "brand"}, "category": {"产品分类", "类别", "分类", "category"},
    "model": {"产品型号", "型号", "model"}, "sku": {"sku", "自有sku", "产品sku", "货号"},
    "supplier_sku": {"供应商型号", "供应商sku", "供应商货号", "工厂货号", "supplier sku"},
    "description": {"产品描述", "描述", "卖点", "description"}, "material": {"材质", "材料", "material"},
    "colors": {"颜色", "颜色/变体", "颜色变体", "color"}, "sizes": {"尺码", "尺寸", "规格", "size"},
    "currency": {"币种", "货币", "价格币种", "currency"}, "unit_price": {"采购单价", "单价", "价格", "批发价", "price"},
    "moq": {"moq", "起订量", "最小起订量", "起订"}, "unit": {"单位", "销售单位", "unit"},
    "carton_spec": {"箱规", "外箱尺寸", "包装尺寸", "箱规（长×宽×高及单位）", "carton spec", "箱规/carton dimensions", "一箱箱规/cbm"},
    "units_per_carton": {"单箱装量", "每箱数量", "装箱数", "pcs/carton"},
    "carton_volume_cbm": {"单箱体积", "体积cbm", "cbm/ctn"},
    "gross_weight_kg_per_carton": {"单箱毛重", "单箱毛重 kg", "毛重/箱", "gross weight", "单箱毛重/gross weight kg"},
    "net_weight_kg_per_carton": {"单箱净重", "单箱净重 kg", "净重/箱", "net weight", "单箱净重/net weight kg"},
}


def read_product_rows(path: Path) -> list[dict[str, Any]]:
    """Read clearly tabular multi-product sheets using recognizable headers."""
    path = path.resolve()
    if path.suffix.lower() not in {".xlsx", ".xlsm"}:
        return []
    workbook = load_workbook(path, read_only=True, data_only=True, keep_vba=path.suffix.lower() == ".xlsm")
    found: list[dict[str, Any]] = []
    alias_to_key = {alias.casefold(): key for key, aliases in PRODUCT_COLUMN_ALIASES.items() for alias in aliases}
    try:
        for sheet in workbook.worksheets:
            header_row = None
            mapped: dict[int, str] = {}
            last_supplier = ""
            for row_idx, row in enumerate(sheet.iter_rows(min_row=1, max_row=15, values_only=True), start=1):
                normalized = [re.sub(r"\s+", " ", str(value or "").strip()).casefold() for value in row]
                current = {col: alias_to_key[value] for col, value in enumerate(normalized) if value in alias_to_key}
                if "name" in current:
                    header_row, mapped = row_idx, current
                    break
            if header_row is None:
                continue
            for row_idx, row in enumerate(sheet.iter_rows(min_row=header_row + 1, values_only=True), start=header_row + 1):
                values = {key: str(row[col]).strip() for col, key in mapped.items() if col < len(row) and row[col] is not None and str(row[col]).strip()}
                if not values.get("name"):
                    continue
                values["source_row"] = f"{sheet.title}!{row_idx}"
                values["source_file"] = path.name
                last_supplier = values.get("supplier_name", last_supplier)
                values["supplier_name"] = last_supplier or "未指定供应商"
                found.append(values)
                if len(found) >= 1000:
                    break
            if len(found) >= 1000:
                break
    finally:
        workbook.close()
    return found


def scan_product_folder(folder: Path, extra_excel: Path | None = None, max_files: int = 500) -> dict[str, Any]:
    folder = folder.resolve()
    if not folder.is_dir():
        raise ValueError("请选择一个产品资料文件夹。")
    if not isinstance(max_files, int) or not 1 <= max_files <= 30000:
        raise ValueError("目录扫描上限必须在 1–30000 个文件之间。")
    files = sorted(path for path in folder.rglob("*") if path.is_file() and not path.is_symlink() and path.suffix.lower() in SUPPORTED_EXTS)
    if len(files) > max_files:
        raise ValueError(f"该扫描目录包含 {len(files)} 个受支持文件，超过当前上限 {max_files}。请调整上限或移出无关文件。")
    has_external_excel = bool(extra_excel and extra_excel.is_file() and extra_excel.suffix.lower() in {".xlsx", ".xlsm"})
    if not files and not has_external_excel:
        raise ValueError("该目录中没有支持的图片、视频或文档。")
    texts = []
    sources = []
    budget = 70000
    parse_paths = [path for path in files if path.suffix.lower() in DOC_EXTS]
    if extra_excel and extra_excel.is_file() and extra_excel.resolve() not in {path.resolve() for path in files}:
        parse_paths.append(extra_excel.resolve())
    for path in parse_paths:
        if budget <= 0:
            break
        text = read_text(path, min(18000, budget))
        if text.strip():
            relative = path.relative_to(folder).as_posix() if path.is_relative_to(folder) else path.name
            texts.append(f"\n===== 来源文件: {relative} =====\n{text}")
            sources.append(relative)
            budget -= len(text)
    images = [path for path in files if path.suffix.lower() in IMAGE_EXTS]
    videos = [path for path in files if path.suffix.lower() in VIDEO_EXTS]
    return {
        "folder": str(folder),
        "folder_name": folder.name,
        "files": files,
        "images": images,
        "videos": videos,
        "source_documents": sources,
        "source_text": "\n".join(texts),
    }


def make_product_package(info: dict[str, Any], extra_excel: Path | None, output_root: Path, values: dict[str, Any], main_image: str = "") -> tuple[Path, dict[str, Any]]:
    if len(info["files"]) + 1 > 250:
        raise ValueError("一个产品档案最多上传 250 个文件，请先把无关资料移出该产品文件夹。")
    source_root = Path(info["folder"]).resolve()
    product_name = str(values.get("name") or source_root.name).strip()
    if not product_name:
        raise ValueError("请填写产品名称。")
    product_code = str(values.get("product_code") or f"P-{date.today():%Y%m%d}-{uuid.uuid4().hex[:8].upper()}")
    product_id = str(values.get("product_id") or uuid.uuid4())
    output_root = output_root.resolve()
    if output_root == source_root or output_root.is_relative_to(source_root):
        raise ValueError("标准档案输出目录不能放在供应商源文件夹里面，以免将生成档案再次当作原始资料导入。")
    supplier_name = str(values.get("supplier_name", "")).strip()
    supplier_root = output_root / safe_stem(supplier_name) if supplier_name else output_root
    destination = supplier_root / f"{product_code}-{safe_stem(product_name)}"
    if destination.exists():
        manifest_path = destination / "产品档案.json"
        try:
            previous = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            previous = {}
        if previous.get("product_id") != product_id:
            raise FileExistsError(f"输出目录已存在且不属于当前产品，未覆盖：{destination}")
        # Replace only an archive created for this same stable product ID.
        shutil.rmtree(destination)
    images_dir = destination / "图片"
    gallery_dir = images_dir / "副图"
    videos_dir = destination / "视频"
    originals_dir = destination / "原始资料"
    specs_dir = destination / "规格"
    for path in (gallery_dir, videos_dir, originals_dir, specs_dir):
        path.mkdir(parents=True, exist_ok=True)

    image_manifest = []
    selected = next((path for path in info["images"] if path.relative_to(source_root).as_posix() == main_image), info["images"][0] if info["images"] else None)
    for path in info["images"]:
        is_main = selected is not None and path.resolve() == selected.resolve()
        name = f"主图{path.suffix.lower()}" if is_main else f"{len(image_manifest):02d}_{safe_stem(path.stem)}{path.suffix.lower()}"
        target = images_dir / name if is_main else gallery_dir / name
        if target.exists():
            target = target.with_name(f"{target.stem}_{file_sha256(path)[:8]}{target.suffix}")
        shutil.copy2(path, target)
        image_manifest.append({"path": target.relative_to(destination).as_posix(), "role": "main" if is_main else "gallery", "source": path.relative_to(source_root).as_posix(), "sha256": file_sha256(path)})

    video_manifest = []
    for path in info["videos"]:
        target = videos_dir / f"{safe_stem(path.stem)}{path.suffix.lower()}"
        if target.exists():
            target = videos_dir / f"{safe_stem(path.stem)}_{file_sha256(path)[:8]}{path.suffix.lower()}"
        shutil.copy2(path, target)
        video_manifest.append({"path": target.relative_to(destination).as_posix(), "source": path.relative_to(source_root).as_posix(), "sha256": file_sha256(path)})

    source_manifest = []
    for path in info["files"]:
        if path.suffix.lower() in IMAGE_EXTS | VIDEO_EXTS:
            continue
        relative = path.relative_to(source_root)
        target = originals_dir / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)
        source_manifest.append({"path": target.relative_to(destination).as_posix(), "source": relative.as_posix(), "sha256": file_sha256(path)})
    if extra_excel and extra_excel.is_file() and not extra_excel.resolve().is_relative_to(source_root):
        target = originals_dir / "补充Excel" / extra_excel.name
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(extra_excel, target)
        source_manifest.append({"path": target.relative_to(destination).as_posix(), "source": str(extra_excel), "sha256": file_sha256(extra_excel)})

    missing_fields = missing_product_fields(values)
    product = {
        "schema_version": 1,
        "product_id": product_id,
        "product_code": product_code,
        "name": product_name,
        "category": str(values.get("category", "")).strip(),
        "brand": str(values.get("brand", "")).strip(),
        "model": str(values.get("model", "")).strip(),
        "sku": str(values.get("sku", "")).strip(),
        "supplier_name": str(values.get("supplier_name", "")).strip(),
        "supplier_sku": str(values.get("supplier_sku", "")).strip(),
        "description": str(values.get("description", "")).strip(),
        "colors": str(values.get("colors", "")).strip(),
        "sizes": str(values.get("sizes", "")).strip(),
        "material": str(values.get("material", "")).strip(),
        "unit": str(values.get("unit", "pcs")).strip(),
        "currency": str(values.get("currency", "")).strip(),
        "unit_price": str(values.get("unit_price", "")).strip(),
        "moq": str(values.get("moq", "")).strip(),
        "carton_spec": str(values.get("carton_spec", "")).strip(),
        "units_per_carton": str(values.get("units_per_carton", "")).strip(),
        "carton_quantity": str(values.get("carton_quantity", "")).strip(),
        "carton_volume_cbm": str(values.get("carton_volume_cbm", "")).strip(),
        "gross_weight_kg_per_carton": str(values.get("gross_weight_kg_per_carton", "")).strip(),
        "net_weight_kg_per_carton": str(values.get("net_weight_kg_per_carton", "")).strip(),
        "lead_time": str(values.get("lead_time", "")).strip(),
        "notes": str(values.get("notes", "")).strip(),
        "field_sources": values.get("field_sources", {}),
        "source_type": str(values.get("source_type", "")).strip(),
        "source_url": str(values.get("source_url", "")).strip(),
        "source_product_id": str(values.get("source_product_id", "")).strip(),
        "updated_at": __import__("datetime").datetime.now().isoformat(timespec="seconds"),
        "review_status": "needs_info" if missing_fields else "ready_for_review",
        "missing_fields": missing_fields,
        "source_folder_name": source_root.name,
        "images": image_manifest,
        "videos": video_manifest,
        "source_files": source_manifest,
    }
    with (destination / "产品档案.json").open("w", encoding="utf-8") as stream:
        json.dump(product, stream, ensure_ascii=False, indent=2)
    return destination, product
