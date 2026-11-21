from __future__ import annotations

import hashlib
import hmac
import io
import base64
import asyncio
import json
import os
import re
import sqlite3
import uuid
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import imagehash
from docx import Document
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader
import httpx
from sqlalchemy import text
from .quotes import list_quotes, logistics_totals, normalize_quote, pdf_response, search_quotes
from .business import (
    WORKSPACES, audit as business_audit, calculate_commission, compute_freight,
    delete_record as delete_business_record, engine as business_engine,
    get_record as get_business_record, import_customer_rows, import_freight_rows, init_business_store,
    list_conversations as list_business_conversations, list_records as list_business_records,
    list_rules as list_team_rules, load_conversation as load_business_conversation,
    save_conversation as save_business_conversation, save_record as save_business_record,
    save_rule as save_team_rule,
)

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "./library")).resolve()
TEMPLATES_DIR = Path(os.getenv("TEMPLATES_DIR", str(DATA_DIR / "templates"))).resolve()
DB_PATH = DATA_DIR / "library.sqlite3"
CONFIG_PATH = DATA_DIR / "settings.json"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024
SYNC_INTERVAL = max(0, int(os.getenv("SYNC_INTERVAL_SECONDS", "300")))
SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
basic_auth = HTTPBasic(auto_error=False)


def require_auth(credentials: HTTPBasicCredentials | None = Depends(basic_auth)):
    expected_password = os.getenv("APP_PASSWORD", "")
    if not expected_password:
        return
    expected_user = os.getenv("APP_USER", "admin")
    if not credentials or not hmac.compare_digest(credentials.username, expected_user) or not hmac.compare_digest(credentials.password, expected_password):
        raise HTTPException(status_code=401, detail="Authentication required", headers={"WWW-Authenticate": "Basic"})


app = FastAPI(title="Xudaai Product Library", version="0.2.0", dependencies=[Depends(require_auth)])
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")
_sync_task: asyncio.Task | None = None
_clip_model: Any = None
_clip_preprocess: Any = None
_clip_torch: Any = None
_clip_device: Any = None
_dingtalk_task: asyncio.Task | None = None
_whisper_model: Any = None
VIDEO_TYPES = {".mp4", ".mov", ".mkv", ".avi", ".webm", ".m4v", ".mp3", ".wav", ".m4a", ".aac"}
SUPPORTED.update(VIDEO_TYPES)


def db() -> sqlite3.Connection:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    return conn


def init_db() -> None:
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS files(
          id INTEGER PRIMARY KEY, path TEXT UNIQUE NOT NULL, name TEXT NOT NULL,
          ext TEXT NOT NULL, size INTEGER NOT NULL, mtime REAL NOT NULL,
          sha256 TEXT NOT NULL, status TEXT NOT NULL, error TEXT, indexed_at TEXT,
          image_hash TEXT
        );
        CREATE TABLE IF NOT EXISTS chunks(
          id INTEGER PRIMARY KEY, file_id INTEGER NOT NULL REFERENCES files(id) ON DELETE CASCADE,
          page INTEGER, content TEXT NOT NULL
        );
        CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(content, file_id UNINDEXED, page UNINDEXED, tokenize='unicode61');
        CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
        CREATE TABLE IF NOT EXISTS quotes(
          id TEXT PRIMARY KEY, quote_no TEXT UNIQUE NOT NULL, customer TEXT NOT NULL,
          currency TEXT NOT NULL, status TEXT NOT NULL, data TEXT NOT NULL,
          created_at TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS quote_match_cache(
          cache_key TEXT PRIMARY KEY, data TEXT NOT NULL, updated_at TEXT NOT NULL
        );
        """)
        file_columns = {row[1] for row in c.execute("PRAGMA table_info(files)")}
        if "image_embedding" not in file_columns:
            c.execute("ALTER TABLE files ADD COLUMN image_embedding TEXT")


def read_model_store() -> dict[str, Any]:
    if not CONFIG_PATH.exists():
        return {}
    try:
        stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        return stored if isinstance(stored, dict) else {}
    except (OSError, ValueError):
        return {}


def model_config(profile_id: str | None = None) -> dict[str, str]:
    config = {"provider": os.getenv("MODEL_PROVIDER", "openai-compatible"), "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "api_key": os.getenv("OPENAI_API_KEY", "")}
    saved = read_model_store()
    config.update({k: str(v) for k, v in saved.items() if k in config})
    selected_id = profile_id or str(saved.get("active_profile_id") or "")
    if selected_id:
        profile = next((item for item in saved.get("profiles", []) if isinstance(item, dict) and item.get("id") == selected_id), None)
        if profile:
            config.update({k: str(profile.get(k, "")) for k in config})
    return config


def save_model_config(config: dict[str, str], profile_id: str, profile_name: str) -> str:
    saved = read_model_store()
    profiles = [item for item in saved.get("profiles", []) if isinstance(item, dict)]
    existing = next((item for item in profiles if item.get("id") == profile_id), None)
    if profile_id in {"", "new"}:
        if not profiles:
            old_config = {key: str(saved.get(key, os.getenv(env_key, default))) for key, env_key, default in [
                ("provider", "MODEL_PROVIDER", "openai-compatible"),
                ("base_url", "OPENAI_BASE_URL", "https://api.openai.com/v1"),
                ("model", "OPENAI_MODEL", "gpt-4o-mini"),
                ("api_key", "OPENAI_API_KEY", ""),
            ]}
            old_name = f"{old_config['provider']} · {old_config['model']}"
            profiles.append({"id": "current", "name": old_name, **old_config})
        profile_id = str(uuid.uuid4())
    if not profile_name.strip():
        raise HTTPException(400, "请填写模型配置名称")
    profile = {"id": profile_id, "name": profile_name.strip()[:60], **config}
    if existing:
        profiles = [profile if item.get("id") == profile_id else item for item in profiles]
    else:
        profiles.append(profile)
    saved.update(config)
    saved["profiles"] = profiles
    saved["active_profile_id"] = profile_id
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return profile_id


def safe_model_profile(profile: dict[str, Any]) -> dict[str, Any]:
    return {"id": str(profile.get("id", "")), "name": str(profile.get("name", "")), "provider": str(profile.get("provider", "")), "base_url": str(profile.get("base_url", "")), "model": str(profile.get("model", "")), "has_api_key": bool(profile.get("api_key"))}


def visual_embedding(image: Image.Image) -> list[float]:
    global _clip_model, _clip_preprocess, _clip_torch, _clip_device
    if _clip_model is None:
        try:
            import open_clip
            import torch
        except ImportError as e:
            raise RuntimeError("CLIP mode needs the optional open_clip_torch package") from e
        _clip_model, _, _clip_preprocess = open_clip.create_model_and_transforms("ViT-B-32", pretrained="laion2b_s34b_b79k")
        _clip_torch = torch
        _clip_device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
        _clip_model = _clip_model.to(_clip_device)
        _clip_model.eval()
    with _clip_torch.no_grad():
        tensor = _clip_preprocess(image.convert("RGB")).unsqueeze(0).to(_clip_device)
        vector = _clip_model.encode_image(tensor)
        vector = vector / vector.norm(dim=-1, keepdim=True)
    return vector.cpu().float().tolist()[0]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def extract(path: Path) -> list[tuple[int | None, str]]:
    ext = path.suffix.lower()
    if ext in VIDEO_TYPES:
        if os.getenv("ENABLE_MEDIA_AI", "false").lower() != "true":
            return [(None, f"视频/音频文件：{path.name}。请启用 ENABLE_MEDIA_AI 并安装媒体依赖，以索引音视频内容。")]
        global _whisper_model
        try:
            from faster_whisper import WhisperModel
        except ImportError as e:
            raise RuntimeError("启用音视频转写需要安装 requirements-media.txt") from e
        if _whisper_model is None:
            _whisper_model = WhisperModel(os.getenv("WHISPER_MODEL", "small"), device="cpu", compute_type="int8")
        segments, _ = _whisper_model.transcribe(str(path), vad_filter=True)
        return [(int(seg.start), seg.text.strip()) for seg in segments if seg.text.strip()]
    if ext in IMAGE_TYPES:
        try:
            import pytesseract
            with Image.open(path) as im:
                ocr = pytesseract.image_to_string(im, lang=os.getenv("OCR_LANG", "chi_sim+eng"))
            return [(None, ocr)] if ocr.strip() else []
        except (ImportError, OSError):
            return []
    if ext == ".pdf":
        reader = PdfReader(str(path))
        pages = [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
        if any(text.strip() for _, text in pages):
            return pages
        if os.getenv("ENABLE_MEDIA_AI", "false").lower() == "true":
            try:
                import fitz
                import pytesseract
                from PIL import Image
                ocr_pages=[]
                pdf=fitz.open(path)
                for i,page in enumerate(pdf):
                    pix=page.get_pixmap(matrix=fitz.Matrix(2,2),alpha=False)
                    image=Image.frombytes("RGB",[pix.width,pix.height],pix.samples)
                    ocr_pages.append((i+1,pytesseract.image_to_string(image,lang=os.getenv("OCR_LANG","chi_sim+eng"))))
                return ocr_pages
            except (ImportError, OSError):
                return pages
        return pages
    if ext == ".docx":
        d = Document(path)
        blocks = [p.text for p in d.paragraphs if p.text.strip()]
        blocks += [" | ".join(c.text for r in t.rows for c in r.cells) for t in d.tables]
        return [(None, "\n".join(blocks))]
    if ext == ".xlsx":
        wb = load_workbook(path, read_only=True, data_only=True)
        return [(None, "\n".join(f"[{ws.title}] " + " | ".join(str(v) for row in ws.iter_rows(values_only=True) for v in row if v is not None) for ws in wb.worksheets))]
    if ext == ".pptx":
        prs = Presentation(path)
        return [(i + 1, "\n".join(s.text for s in slide.shapes if hasattr(s, "text_frame"))) for i, slide in enumerate(prs.slides)]
    return [(None, path.read_text(encoding="utf-8", errors="replace"))]


def split_text(text: str, max_chars: int = 1100) -> list[str]:
    paras = [re.sub(r"\s+", " ", p).strip() for p in text.splitlines() if p.strip()]
    chunks: list[str] = []
    cur = ""
    for para in paras:
        if len(cur) + len(para) > max_chars and cur:
            chunks.append(cur)
            cur = ""
        cur = f"{cur} {para}".strip()
    if cur:
        chunks.append(cur)
    return chunks


def index_one(path: Path, rel: str) -> dict[str, Any]:
    st = path.stat()
    digest = sha256(path)
    with db() as c:
        old = c.execute("SELECT id,sha256,image_embedding FROM files WHERE path=?", (rel,)).fetchone()
        needs_embedding = path.suffix.lower() in IMAGE_TYPES and os.getenv("IMAGE_SEARCH_MODE", "phash").lower() == "clip"
        if old and old["sha256"] == digest and not (needs_embedding and not old["image_embedding"]):
            return {"path": rel, "status": "unchanged"}
        try:
            extracted = extract(path)
            ih = None
            embedding = None
            if path.suffix.lower() in IMAGE_TYPES:
                with Image.open(path) as im:
                    ih = str(imagehash.phash(im.convert("RGB")))
                    if os.getenv("IMAGE_SEARCH_MODE", "phash").lower() == "clip":
                        embedding = json.dumps(visual_embedding(im), separators=(",", ":"))
            if old:
                fid = old["id"]
                c.execute("DELETE FROM chunks_fts WHERE file_id=?", (fid,))
                c.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                c.execute("UPDATE files SET name=?,ext=?,size=?,mtime=?,sha256=?,status='ready',error=NULL,indexed_at=?,image_hash=?,image_embedding=? WHERE id=?", (path.name,path.suffix.lower(),st.st_size,st.st_mtime,digest,datetime.now(timezone.utc).isoformat(),ih,embedding,fid))
            else:
                cur = c.execute("INSERT INTO files(path,name,ext,size,mtime,sha256,status,indexed_at,image_hash,image_embedding) VALUES(?,?,?,?,?,?,'ready',?,?,?)", (rel,path.name,path.suffix.lower(),st.st_size,st.st_mtime,digest,datetime.now(timezone.utc).isoformat(),ih,embedding))
                fid = cur.lastrowid
            for page, body in extracted:
                for part in split_text(body):
                    c.execute("INSERT INTO chunks(file_id,page,content) VALUES(?,?,?)", (fid,page,part))
                    c.execute("INSERT INTO chunks_fts(content,file_id,page) VALUES(?,?,?)", (part,fid,page))
            return {"path": rel, "status": "indexed"}
        except Exception as e:
            if old:
                c.execute("UPDATE files SET status='error',error=? WHERE id=?", (str(e)[:500],old["id"]))
            else:
                c.execute("INSERT INTO files(path,name,ext,size,mtime,sha256,status,error) VALUES(?,?,?,?,?,?,'error',?)", (rel,path.name,path.suffix.lower(),st.st_size,st.st_mtime,digest,str(e)[:500]))
            return {"path": rel, "status": "error", "error": str(e)[:200]}


def sync_library() -> dict[str, Any]:
    if not LIBRARY_DIR.exists():
        LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    seen: set[str] = set()
    results = []
    for p in LIBRARY_DIR.rglob("*"):
        if p.is_file() and not p.is_symlink() and p.suffix.lower() in SUPPORTED:
            rel = p.relative_to(LIBRARY_DIR).as_posix()
            seen.add(rel)
            results.append(index_one(p, rel))
    with db() as c:
        current = {r[0] for r in c.execute("SELECT path FROM files")}
        removed = current - seen
        for rel in removed:
            fid = c.execute("SELECT id FROM files WHERE path=?", (rel,)).fetchone()[0]
            c.execute("DELETE FROM chunks_fts WHERE file_id=?", (fid,))
            c.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
            c.execute("DELETE FROM files WHERE id=?", (fid,))
    return {"scanned": len(seen), "indexed": sum(x["status"] == "indexed" for x in results), "unchanged": sum(x["status"] == "unchanged" for x in results), "errors": [x for x in results if x["status"] == "error"], "removed": len(removed)}


def file_record(file_id: int) -> sqlite3.Row:
    with db() as c:
        row = c.execute("SELECT * FROM files WHERE id=?", (file_id,)).fetchone()
    if not row:
        raise HTTPException(404, "File not found")
    return row


@app.on_event("startup")
def startup() -> None:
    init_db()
    init_business_store()


async def periodic_sync() -> None:
    while True:
        await asyncio.sleep(SYNC_INTERVAL)
        try:
            await asyncio.to_thread(sync_library)
        except Exception:
            # A later cycle retries; manual sync exposes per-file errors to the UI.
            continue


@app.on_event("startup")
async def start_periodic_sync() -> None:
    global _sync_task, _dingtalk_task
    if SYNC_INTERVAL:
        _sync_task = asyncio.create_task(periodic_sync())
    if os.getenv("DINGTALK_ENABLED", "false").lower() == "true":
        client_id = os.getenv("DINGTALK_CLIENT_ID", "")
        client_secret = os.getenv("DINGTALK_CLIENT_SECRET", "")
        if not client_id or not client_secret:
            raise RuntimeError("DINGTALK_ENABLED requires DINGTALK_CLIENT_ID and DINGTALK_CLIENT_SECRET")
        from .dingtalk import run_stream
        _dingtalk_task = asyncio.create_task(run_stream(client_id, client_secret, answer, lambda q: text_search(q=q)))


@app.on_event("shutdown")
async def stop_periodic_sync() -> None:
    if _sync_task:
        _sync_task.cancel()
    if _dingtalk_task:
        _dingtalk_task.cancel()


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status() -> dict[str, Any]:
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        errs = c.execute("SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0]
        latest = c.execute("SELECT MAX(indexed_at) FROM files").fetchone()[0]
    cfg = model_config()
    profiles = [item for item in read_model_store().get("profiles", []) if isinstance(item, dict)]
    if not profiles:
        profiles = [cfg]
    usable_profiles = [item for item in profiles if item.get("api_key") or item.get("provider") == "ollama"]
    return {"library_dir": str(LIBRARY_DIR), "total_files": total, "errors": errs, "last_indexed": latest, "sync_interval_seconds": SYNC_INTERVAL, "dingtalk_enabled": os.getenv("DINGTALK_ENABLED", "false").lower() == "true", "answer_enabled": bool(usable_profiles), "configured_model_count": len(usable_profiles), "model_count": len(profiles), "model_provider": cfg["provider"], "model_name": cfg["model"]}


@app.get("/api/quotes")
def quotes_list() -> dict[str, Any]:
    with db() as c:
        return {"quotes": list_quotes(c)}


@app.get("/api/quotes/templates")
def quote_templates() -> dict[str, Any]:
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    return {"templates": [{"name": item.name, "size": item.stat().st_size} for item in sorted(TEMPLATES_DIR.glob("*.docx")) if item.is_file()]}


@app.get("/api/quotes/defaults")
def quote_defaults_get() -> dict[str, Any]:
    path = TEMPLATES_DIR / "quote_defaults.json"
    if not path.is_file():
        return {"defaults": {}}
    try:
        values = json.loads(path.read_text(encoding="utf-8"))
        return {"defaults": values if isinstance(values, dict) else {}}
    except (OSError, ValueError):
        return {"defaults": {}}


def discover_company_profiles() -> list[dict[str, str]]:
    """Read company contact profiles from LIBRARY_DIR/公司资料/<company folder>."""
    root = LIBRARY_DIR / "公司资料"
    if not root.is_dir():
        return []
    company_dirs = sorted((item for item in root.iterdir() if item.is_dir()), key=lambda item: item.name.casefold())
    if any(item.is_file() and item.suffix.lower() in {".docx", ".pdf", ".txt", ".md", ".csv"} for item in root.iterdir()):
        company_dirs.append(root)
    profiles: list[dict[str, str]] = []
    for folder in company_dirs:
        text_parts: list[str] = []
        sources: list[str] = []
        candidates = [path for path in folder.rglob("*") if path.is_file() and path.suffix.lower() in {".docx", ".pdf", ".txt", ".md", ".csv"}]
        candidates.sort(key=lambda path: (0 if path.suffix.lower() in {".docx", ".pdf"} else 1, path.name.casefold()))
        for path in candidates[:12]:
            try:
                text_parts.extend(text[:12000] for _, text in extract(path) if text.strip())
                sources.append(path.relative_to(LIBRARY_DIR).as_posix())
            except Exception:
                continue
        content = "\n".join(text_parts)[:60000]
        name = folder.name if folder != root else "公司资料"
        name_match = re.search(r"(?:公司名称|企业名称|公司抬头|Company\s+Name|Legal\s+Name)\s*[:：|]?\s*([^\n|]{2,100})", content, re.I)
        english_name_match = re.search(r"(?im)^\s*([A-Z][A-Za-z0-9&.,' -]{2,70}?(?:Co\.,?\s*Ltd\.?|Limited|Ltd\.?|Inc\.?))", content)
        if name_match:
            candidate_name = name_match.group(1).strip(" ：:|")
            if candidate_name and len(candidate_name) < 100:
                name = candidate_name
        phone_match = re.search(r"(?:电话|联系电话|手机|Tel(?:ephone)?|Phone|Mobile|WhatsApp)\s*[:：]?\s*(\+?\d[\d\s().-]{7,}\d)", content, re.I)
        email_match = re.search(r"[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}", content, re.I)
        address_match = re.search(r"(?:公司地址|地址|住所|Registered\s+Address|Address)\s*[:：]?\s*([^\n]{5,250})", content, re.I)
        address = address_match.group(1).strip(" ：:") if address_match else ""
        if not address:
            address_match = re.search(r"(?:[\u4e00-\u9fff]{2,}(?:省|自治区)[^\n]{4,160}|(?:Room|Suite|Unit)\s+[^\n]{10,220})", content, re.I)
            address = address_match.group(0).strip() if address_match else ""
        english_address_match = re.search(r"(?im)^\s*((?:Room|Suite|Unit)\s+\d{1,8}[^\n]{10,250})", content)
        profiles.append({
            "id": (folder.relative_to(LIBRARY_DIR).as_posix() if folder != root else "公司资料"),
            "name": name,
            "name_en": english_name_match.group(1).strip() if english_name_match else "",
            "address": address[:500],
            "address_en": english_address_match.group(1).strip() if english_address_match else "",
            "phone": phone_match.group(1).strip() if phone_match else "",
            "email": email_match.group(0) if email_match else "",
            "sources": "、".join(sources[:5]),
        })
    return profiles


@app.get("/api/quotes/companies")
def quote_companies() -> dict[str, Any]:
    defaults = quote_defaults_get().get("defaults", {})
    return {"companies": discover_company_profiles(), "selected_profile_id": defaults.get("company_profile_id", "")}


@app.put("/api/quotes/defaults")
async def quote_defaults_put(payload: dict[str, Any]) -> dict[str, Any]:
    keys = {"company_name", "company_contact", "company_address", "company_phone", "company_email", "company_profile_id", "language", "currency", "incoterm", "payment_terms", "payment_note", "shipping_term", "tax_term", "tax_note", "delivery_time", "bank_details", "template_name"}
    values = {key: str(payload.get(key, "")).strip()[:1000] for key in keys}
    if values.get("currency") and not re.fullmatch(r"[A-Za-z]{3}", values["currency"]):
        raise HTTPException(422, "默认币种请使用三位代码，例如 USD")
    if values.get("template_name") and Path(values["template_name"]).name != values["template_name"]:
        raise HTTPException(422, "无效模板名称")
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMPLATES_DIR / "quote_defaults.json"
    temp = path.with_suffix(".json.tmp")
    temp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    temp.replace(path)
    return {"defaults": values}


@app.get("/api/quotes/customers")
def quote_customers_get() -> dict[str, Any]:
    path = TEMPLATES_DIR / "customers.json"
    try:
        values = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
        return {"customers": values if isinstance(values, list) else []}
    except (OSError, ValueError):
        return {"customers": []}


@app.post("/api/quotes/customers")
async def quote_customers_put(payload: dict[str, Any]) -> dict[str, Any]:
    name = str(payload.get("name", "")).strip()[:200]
    address = str(payload.get("address", "")).strip()[:500]
    if not name:
        raise HTTPException(422, "请填写客户名称")
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    path = TEMPLATES_DIR / "customers.json"
    try:
        customers = json.loads(path.read_text(encoding="utf-8")) if path.is_file() else []
    except (OSError, ValueError):
        customers = []
    customers = [item for item in customers if isinstance(item, dict) and str(item.get("name", "")).casefold() != name.casefold()]
    customer = {"id": uuid.uuid4().hex[:12], "name": name, "address": address}
    customers.append(customer)
    path.write_text(json.dumps(customers[-500:], ensure_ascii=False, indent=2), encoding="utf-8")
    return {"customer": customer, "customers": customers[-500:]}


@app.get("/api/workspaces")
def workspaces_get() -> dict[str, Any]:
    return {"workspaces": [{"id": key, "name": name} for key, name in WORKSPACES.items()]}


@app.get("/api/conversations")
def conversations_get(workspace: str = "", limit: int = 100) -> dict[str, Any]:
    return {"conversations": list_business_conversations(workspace, limit)}


@app.get("/api/conversations/{conversation_id}")
def conversation_get(conversation_id: str) -> dict[str, Any]:
    conversation = load_business_conversation(conversation_id)
    if not conversation:
        raise HTTPException(404, "找不到这段对话")
    return {"conversation": conversation}


@app.post("/api/conversations")
def conversation_save(payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    try:
        conversation = save_business_conversation(str(payload.get("id", "")), str(payload.get("workspace", "product")), payload.get("messages", []), x_actor)
        return {"conversation": conversation}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.get("/api/team/rules")
def team_rules_get(workspace: str = "") -> dict[str, Any]:
    return {"rules": list_team_rules(workspace)}


@app.post("/api/team/rules")
def team_rule_create(payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    try:
        rule = save_team_rule(str(payload.get("workspace", "all")), str(payload.get("name", "")), str(payload.get("body", "")), actor=x_actor)
        return {"rule": rule}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/team/rules/{rule_id}")
def team_rule_update(rule_id: str, payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    try:
        rule = save_team_rule(str(payload.get("workspace", "all")), str(payload.get("name", "")), str(payload.get("body", "")), rule_id, x_actor)
        return {"rule": rule}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/team/rules/{rule_id}")
def team_rule_delete(rule_id: str, x_actor: str = Header(default="")) -> dict[str, Any]:
    with business_engine.begin() as c:
        result = c.execute(text("UPDATE team_rules SET active=0 WHERE id=:id AND active=1"), {"id": rule_id})
    if result.rowcount:
        business_audit(x_actor, "deactivate", "rule", rule_id, {})
    return {"deleted": bool(result.rowcount)}


@app.get("/api/business/records/{kind}")
def business_records_get(kind: str, q: str = "", limit: int = 100) -> dict[str, Any]:
    if kind not in {"customer", "freight_rate", "salesperson", "order", "commission_rule", "payroll", "product_note"}:
        raise HTTPException(404, "未知业务资料类型")
    return {"records": list_business_records(kind, q, limit)}


@app.post("/api/business/records/{kind}")
def business_record_create(kind: str, payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    try:
        record = save_business_record(kind, payload, x_actor)
        return {"record": record}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.put("/api/business/records/{kind}/{record_id}")
def business_record_update(kind: str, record_id: str, payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    if kind not in {"customer", "freight_rate", "salesperson", "order", "commission_rule", "payroll", "product_note"}:
        raise HTTPException(404, "未知业务资料类型")
    existing = get_business_record(record_id)
    if not existing or existing["kind"] != kind:
        raise HTTPException(404, "找不到对应业务记录")
    try:
        return {"record": save_business_record(kind, {**payload, "id": record_id}, x_actor)}
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete("/api/business/records/{record_id}")
def business_record_delete(record_id: str, x_actor: str = Header(default="")) -> dict[str, Any]:
    return {"deleted": delete_business_record(record_id, x_actor)}


@app.post("/api/business/freight/import")
async def freight_import(file: UploadFile = File(...), x_actor: str = Header(default="")) -> dict[str, Any]:
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD // 1024 // 1024} MB")
    folder = LIBRARY_DIR / "运费资料"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / (Path(file.filename or "运费表.csv").name[:180])
    destination.write_bytes(content)
    relative_path = destination.relative_to(LIBRARY_DIR).as_posix()
    index_result = index_one(destination, relative_path)
    try:
        rates = import_freight_rows(relative_path, content, x_actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not rates:
        raise HTTPException(422, "没有识别到运费行。表格需包含运价、计价单位等列；原文件仍可上传到资料库后让 AI 检索。")
    return {"imported": len(rates), "rates": rates, "source_file": relative_path, "file_status": index_result["status"]}


@app.post("/api/business/customers/import")
async def customer_import(file: UploadFile = File(...), x_actor: str = Header(default="")) -> dict[str, Any]:
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, f"文件超过 {MAX_UPLOAD // 1024 // 1024} MB")
    folder = LIBRARY_DIR / "客户资料"
    folder.mkdir(parents=True, exist_ok=True)
    destination = folder / Path(file.filename or "客户表.csv").name[:180]
    destination.write_bytes(content)
    relative_path = destination.relative_to(LIBRARY_DIR).as_posix()
    index_one(destination, relative_path)
    try:
        customers = import_customer_rows(relative_path, content, x_actor)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if not customers:
        raise HTTPException(422, "没有识别到客户行。表格需要客户名称/公司名称列；原文件已保存并建立索引。")
    return {"imported": len(customers), "customers": customers, "source_file": relative_path}


@app.post("/api/business/freight/calculate")
def freight_calculate(payload: dict[str, Any]) -> dict[str, Any]:
    rate_id = str(payload.get("rate_id", ""))
    rate_record = get_business_record(rate_id) if rate_id else None
    if not rate_id:
        raise HTTPException(422, "请先选择资料库中已导入的运费率，避免使用 AI 猜测运价")
    if not rate_record or rate_record["kind"] != "freight_rate":
        raise HTTPException(404, "找不到所选运费率")
    try:
        calculation = compute_freight(payload, rate_record["data"] if rate_record else None)
        return {"calculation": calculation}
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/business/payroll/calculate")
def payroll_calculate(payload: dict[str, Any]) -> dict[str, Any]:
    try:
        return {"calculation": calculate_commission(str(payload.get("seller_id", "")), str(payload.get("start_date", "")), str(payload.get("end_date", "")))}
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/business/payroll/drafts")
def payroll_draft_create(payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    try:
        calculation = calculate_commission(str(payload.get("seller_id", "")), str(payload.get("start_date", "")), str(payload.get("end_date", "")))
        calculation["name"] = f"{calculation['seller_name']} {calculation['period_start']} 至 {calculation['period_end']} 工资草稿"
        record = save_business_record("payroll", calculation, x_actor)
        return {"record": record}
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc


@app.post("/api/business/payroll/{record_id}/approve")
def payroll_approve(record_id: str, x_actor: str = Header(default="")) -> dict[str, Any]:
    record = get_business_record(record_id)
    if not record or record["kind"] != "payroll":
        raise HTTPException(404, "找不到工资草稿")
    data = {**record["data"], "status": "approved", "approved_at": datetime.now(timezone.utc).isoformat(), "approved_by": x_actor}
    return {"record": save_business_record("payroll", {**data, "id": record_id}, x_actor)}


@app.get("/api/business/payroll/{record_id}/csv")
def payroll_csv(record_id: str) -> Response:
    record = get_business_record(record_id)
    if not record or record["kind"] != "payroll":
        raise HTTPException(404, "找不到工资明细")
    data = record["data"]
    import csv
    output = io.StringIO(newline="")
    writer = csv.writer(output)
    writer.writerow(["销售人员", data.get("seller_name", ""), "期间", f"{data.get('period_start', '')} ~ {data.get('period_end', '')}"])
    writer.writerow(["提成口径", data.get("basis", ""), "提成比例(%)", data.get("rate_percent", "")])
    writer.writerow(["订单号", "日期", "币种", "计提基数", "提成金额"])
    for order in data.get("orders", []):
        writer.writerow([order.get("order_no", ""), order.get("date", ""), order.get("currency", ""), order.get("basis_amount", ""), order.get("commission", "")])
    writer.writerow(["币种汇总", "计提基数", "提成总额"])
    for currency, totals in data.get("currency_totals", {}).items():
        writer.writerow([currency, totals.get("basis_amount", ""), totals.get("commission", "")])
    output.seek(0)
    filename = f"payroll-{record_id[:12]}.csv"
    return Response(content="\ufeff" + output.getvalue(), media_type="text/csv; charset=utf-8", headers={"Content-Disposition": f'attachment; filename="{filename}"'})


@app.post("/api/quotes/from-chat")
async def quote_from_chat(payload: dict[str, Any]) -> dict[str, Any]:
    model_id = str(payload.get("model_id", "local"))
    if model_id == "local":
        raise HTTPException(400, "请在对话底部选择一个已配置模型，再从对话整理单据")
    profiles = [item for item in read_model_store().get("profiles", []) if isinstance(item, dict)]
    if profiles and model_id not in {str(item.get("id")) for item in profiles}:
        raise HTTPException(404, "所选模型配置不存在")
    cfg = model_config(model_id)
    selected_ids = payload.get("selected_products", [])
    if not isinstance(selected_ids, list):
        selected_ids = []
    if len(selected_ids) > 20:
        raise HTTPException(422, "一次最多选择 20 个产品；请分批生成报价单")
    doc_type = "PI" if str(payload.get("document_type", "quote")).lower() == "pi" else "报价单"
    requested_currency = str(payload.get("currency", "USD")).upper()
    if requested_currency not in {"USD", "EUR", "CNY", "GBP", "JPY"}:
        requested_currency = "USD"
    document_language = "en" if str(payload.get("language", "zh")).lower() == "en" else "zh"
    force_refresh = bool(payload.get("force_refresh", False))
    selected_context = []
    selected_groups: dict[str, dict[str, Any]] = {}
    cached_items: dict[str, dict[str, Any]] = {}
    cache_keys: dict[str, str] = {}
    with db() as c:
        for raw_id in selected_ids[:20]:
            try:
                file_id = int(raw_id)
            except (TypeError, ValueError):
                continue
            file_row = c.execute("SELECT id,name,path,ext,sha256,mtime FROM files WHERE id=? AND status='ready'", (file_id,)).fetchone()
            if not file_row:
                continue
            parent = Path(file_row["path"]).parent.as_posix()
            selection_id = str(file_row["id"])
            group = {"selection_id": selection_id, "folder": parent, "selected_path": file_row["path"], "images": [], "docs": [], "source_versions": [(file_row["path"], file_row["sha256"], file_row["mtime"]) ]}
            if file_row["ext"] in IMAGE_TYPES:
                group["images"].append({"id": file_row["id"], "name": file_row["name"], "path": file_row["path"]})
            else:
                rows = c.execute("SELECT content,page FROM chunks WHERE file_id=? ORDER BY id LIMIT 12", (file_id,)).fetchall()
                group["docs"].extend(f"{file_row['path']}（第 {row['page'] or '未标注'} 页）：{row['content'][:1200]}" for row in rows)
            if parent not in {"", "."}:
                prefix = parent.rstrip("/") + "/"
                image_exts = tuple(IMAGE_TYPES)
                image_placeholders = ",".join("?" for _ in image_exts)
                if not group["images"]:
                    image_row = c.execute(
                        f"SELECT id,name,path,sha256,mtime FROM files WHERE status='ready' AND ext IN ({image_placeholders}) AND substr(path,1,?)=? ORDER BY name LIMIT 1",
                        (*image_exts, len(prefix), prefix),
                    ).fetchone()
                    if image_row:
                        group["images"].append({"id": image_row["id"], "name": image_row["name"], "path": image_row["path"]})
                        group["source_versions"].append((image_row["path"], image_row["sha256"], image_row["mtime"]))
                siblings = c.execute(
                    f"SELECT f.id,f.name,f.path,f.ext,f.sha256,f.mtime FROM files f WHERE f.status='ready' AND f.ext NOT IN ({image_placeholders}) AND substr(f.path,1,?)=? ORDER BY CASE WHEN f.ext IN ('.txt','.md','.docx') THEN 0 WHEN f.ext='.xlsx' THEN 1 ELSE 2 END,f.name LIMIT 30",
                    (*image_exts, len(prefix), prefix),
                ).fetchall()
                for sibling in siblings:
                    group["source_versions"].append((sibling["path"], sibling["sha256"], sibling["mtime"]))
                    rows = c.execute("SELECT content,page FROM chunks WHERE file_id=? ORDER BY id LIMIT 20", (sibling["id"],)).fetchall()
                    group["docs"].extend(f"{sibling['path']}（第 {row['page'] or '未标注'} 页）：{row['content'][:1400]}" for row in rows)
                # Supplier price books are often stored at the supplier root rather
                # than beside each product. Match their indexed rows by the model code.
                model_codes = list(dict.fromkeys(re.findall(r"\d{3,}", parent + "/" + file_row["name"])))
                supplier_root = parent.split("/")[0]
                root_prefix = supplier_root + "/"
                pricebooks = c.execute("SELECT id,path,sha256,mtime FROM files WHERE status='ready' AND ext='.xlsx' AND substr(path,1,?)=? ORDER BY path LIMIT 20", (len(root_prefix), root_prefix)).fetchall()
                for pricebook in pricebooks:
                    for code in model_codes:
                        rows = c.execute("SELECT content,page FROM chunks WHERE file_id=? AND content LIKE ? ORDER BY id LIMIT 8", (pricebook["id"], f"%{code}%")).fetchall()
                        group["docs"].extend(f"{pricebook['path']}（报价表，第 {row['page'] or '未标注'} 页）：{row['content'][:1600]}" for row in rows)
                        if rows:
                            group["source_versions"].append((pricebook["path"], pricebook["sha256"], pricebook["mtime"]))
            selected_groups[selection_id] = group
        for selection_id, group in selected_groups.items():
            images = "、".join(dict.fromkeys(item["name"] for item in group["images"])) or "无"
            excerpts = list(dict.fromkeys(group["docs"]))
            price_lines = [line for line in excerpts if ".xlsx" in line.lower() or "报价表" in line]
            detail_lines = [line for line in excerpts if line not in price_lines]
            prioritized = detail_lines[:1] + price_lines[:1] + detail_lines[1:2]
            block = f"勾选项 ID：{selection_id}\n选择文件：{group['selected_path']}\n产品目录：{group['folder']}\n产品图片：{images}\n关联资料：\n" + ("\n".join(line[:850] for line in prioritized) or "未找到关联文字资料")
            cache_identity = {
                "selected_path": group["selected_path"], "sources": sorted(set(group["source_versions"])),
                "provider": cfg.get("provider", ""), "base_url": cfg.get("base_url", ""), "model": cfg.get("model", ""),
                "currency": requested_currency, "language": document_language,
            }
            cache_key = hashlib.sha256(json.dumps(cache_identity, ensure_ascii=False, sort_keys=True).encode("utf-8")).hexdigest()
            cache_keys[selection_id] = cache_key
            cache_row = c.execute("SELECT data FROM quote_match_cache WHERE cache_key=?", (cache_key,)).fetchone() if not force_refresh else None
            if cache_row:
                try:
                    cached_item = json.loads(cache_row["data"])
                    if isinstance(cached_item, dict):
                        cached_items[selection_id] = cached_item
                except (TypeError, ValueError):
                    pass
            if selection_id not in cached_items:
                selected_context.append(block[:1900])
    if not selected_groups:
        raise HTTPException(400, "请先勾选检索结果中的产品图片或产品资料")
    schema = {"items":[{"selection_id":"", "name":"", "description":"", "colors":"", "size_breakdown":"", "configuration":"", "carton_spec":"", "units_per_carton":"", "carton_quantity":"", "carton_volume":"", "gross_weight":"", "net_weight":"", "quantity":"1", "moq":"0", "unit":"pairs", "unit_price":"0", "discount_percent":"0", "source":"需人工核对"}]}
    if selected_context and not cfg["api_key"] and cfg["provider"] != "ollama":
        raise HTTPException(400, "所选模型尚未配置 API Key")
    instruction = (
        f"根据每一个勾选项分别关联对应图片、目录里的商品详情和报价表，整理{doc_type}产品明细。必须针对每个不同的勾选项 ID 恰好输出一行；不能按目录合并，不能漏掉任何 ID。每行 selection_id 必须原样填写对应的‘勾选项 ID’。单据币种为 {requested_currency}；从明确对应的报价表/币种列提取型号、名称、描述、颜色、尺码范围、配置、MOQ、单价。禁止把相近款式的价格或尺码套用到当前产品。找不到确切匹配的单价填 0 并标需核对，不要编造。"
        "颜色字段只写颜色名称，不要在颜色字段里重复分色数量；每行对应一款，quantity 是该款唯一总数量。尺码、配置、装箱规格（长×宽×高及原始单位）、单箱装量（双/箱或件/箱）、明确箱数、单箱体积（m³）、毛重和净重只有资料里明确写出才填；体积只取资料明确值，不要自行推测。数量采用 MOQ（若未提供则为 1），单位优先按资料取值，否则鞋类用 pairs、其他用 pcs。客户、公司、付款和贸易条款不在这里提取。"
        + ("所有产品文字字段必须输出英文；型号、SKU、数字和品牌原样保留。" if document_language == "en" else "产品文字字段按资料原文输出。")
        + f"只返回合法 JSON，不要 Markdown。字段结构：{json.dumps(schema, ensure_ascii=False)}\n\n已勾选的图片和关联产品资料：\n" + "\n\n".join(selected_context)[:40000]
    )
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        if selected_context:
            async with httpx.AsyncClient(timeout=90) as client:
                response = await client.post(f"{cfg['base_url'].rstrip('/')}/chat/completions", headers=headers, json={"model": cfg["model"], "temperature": 0, "messages":[{"role":"system","content":"你是结构化单据草稿整理器。严格遵守用户要求的 JSON 字段和不猜测原则。"},{"role":"user","content":instruction}]})
                response.raise_for_status()
                content = response.json()["choices"][0]["message"]["content"]
        else:
            content = '{"items":[]}'
        if isinstance(content, list):
            content = "".join(str(part.get("text", "")) for part in content if isinstance(part, dict))
        cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(content).strip(), flags=re.I)
        draft = json.loads(cleaned)
        if not isinstance(draft, dict) or not isinstance(draft.get("items"), list):
            raise ValueError("missing required JSON fields")
        # Map each result back to exactly one selected result, preserving one row per choice.
        selection_ids = [selection_id for selection_id in selected_groups if selection_id not in cached_items]
        matched_items: dict[str, dict[str, Any]] = {}
        for index, item in enumerate(draft["items"]):
            if isinstance(item, dict):
                requested_id = str(item.get("selection_id", "")).strip()
                selection_id = requested_id if requested_id in selection_ids else next((candidate for candidate in selection_ids if candidate not in matched_items), "")
                if not selection_id or selection_id in matched_items:
                    continue
                matched_items[selection_id] = item
        unique_items = []
        for selection_id in selected_groups:
            group = selected_groups[selection_id]
            item = dict(cached_items.get(selection_id) or matched_items.get(selection_id) or {
                "name": Path(group["folder"]).name or Path(group["selected_path"]).stem or ("Unidentified product" if document_language == "en" else "待识别产品"),
                "description": "", "colors": "", "size_breakdown": "", "configuration": "", "carton_spec": "", "units_per_carton": "", "carton_quantity": "", "carton_volume": "", "gross_weight": "", "net_weight": "", "quantity": "1", "moq": "0", "unit": "pairs", "unit_price": "0", "discount_percent": "0",
                "source": "Product details need review" if document_language == "en" else "未自动识别，请核对",
            })
            folder = group["folder"]
            item.pop("selection_id", None)
            if not str(item.get("name", "")).strip():
                item["name"] = Path(folder).name or Path(group["selected_path"]).stem or (f"Product {selection_id}" if document_language == "en" else f"产品 {selection_id}")
            if selection_id in cached_items:
                pass
            elif selection_id not in matched_items:
                item["source"] = item["source"] if document_language == "en" else f"{folder} · {item['source']}"
            else:
                moq_value = str(item.get("moq") or "0").strip()
                moq_number = float(moq_value) if re.fullmatch(r"\d+(?:\.\d+)?", moq_value) else 0
                if not str(item.get("quantity", "")).strip() or (str(item.get("quantity", "1")).strip() == "1" and moq_number > 1):
                    item["quantity"] = item.get("moq") or "1"
                model_source = str(item.get("source", "")).strip()
                if document_language == "en":
                    item["source"] = model_source or "Product data"
                else:
                    item["source"] = folder if not model_source or model_source == "需人工核对" else f"{folder} · {model_source}"
            if selection_id not in cached_items:
                with db() as c:
                    c.execute(
                        "INSERT INTO quote_match_cache(cache_key,data,updated_at) VALUES(?,?,?) ON CONFLICT(cache_key) DO UPDATE SET data=excluded.data,updated_at=excluded.updated_at",
                        (cache_keys[selection_id], json.dumps(item, ensure_ascii=False), datetime.now(timezone.utc).isoformat()),
                    )
            representative_image = group["images"]
            if representative_image:
                item["image_path"] = representative_image[0]["path"]
                item["image_file_id"] = representative_image[0]["id"]
            unique_items.append(item)
        draft["items"] = unique_items
        return {"draft": draft, "cache": {"hits": len(cached_items), "misses": len(selected_context), "refreshed": force_refresh}}
    except httpx.HTTPStatusError as exc:
        raise HTTPException(502, f"模型整理单据失败，HTTP {exc.response.status_code}")
    except (httpx.HTTPError, KeyError, IndexError, TypeError, ValueError, json.JSONDecodeError):
        raise HTTPException(502, "模型没有返回有效的单据数据，请补充信息后重试")


@app.post("/api/quotes/templates")
async def quote_template_upload(file: UploadFile = File(...)) -> dict[str, Any]:
    if Path(file.filename or "").suffix.lower() != ".docx":
        raise HTTPException(415, "报价/PI 模板请上传 DOCX 格式的 Word 文件")
    content = await file.read(MAX_UPLOAD + 1)
    if len(content) > MAX_UPLOAD:
        raise HTTPException(413, f"模板文件超过 {MAX_UPLOAD // 1024 // 1024} MB")
    if not content.startswith(b"PK"):
        raise HTTPException(400, "文件不是有效的 DOCX 模板")
    TEMPLATES_DIR.mkdir(parents=True, exist_ok=True)
    stem = re.sub(r"[^\w\-.\u4e00-\u9fff]+", "_", Path(file.filename or "template.docx").stem)[:80] or "template"
    destination = TEMPLATES_DIR / f"{stem}_{uuid.uuid4().hex[:8]}.docx"
    destination.write_bytes(content)
    return {"name": destination.name, "size": len(content)}


@app.post("/api/quotes")
async def quote_create(payload: dict[str, Any]) -> dict[str, Any]:
    data = normalize_quote(payload)
    now = datetime.now(timezone.utc).isoformat()
    quote_id = str(uuid.uuid4())
    prefix = "PI" if data["document_type"] == "pi" else "Q"
    quote_no = f"{prefix}{date.today().strftime('%Y%m%d')}-{quote_id[:8].upper()}"
    with db() as c:
        c.execute("INSERT INTO quotes(id,quote_no,customer,currency,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (quote_id, quote_no, data["customer"], data["currency"], "draft", json.dumps(data, ensure_ascii=False), now, now))
    return {"id": quote_id, "quote_no": quote_no, "status": "draft", "data": data}


@app.get("/api/quotes/{quote_id}")
def quote_get(quote_id: str) -> dict[str, Any]:
    with db() as c:
        row = c.execute("SELECT id,quote_no,status,data FROM quotes WHERE id=?", (quote_id,)).fetchone()
    if not row:
        raise HTTPException(404, "找不到这张报价单")
    return {"id": row["id"], "quote_no": row["quote_no"], "status": row["status"], "data": json.loads(row["data"])}


@app.put("/api/quotes/{quote_id}")
async def quote_update(quote_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    data = normalize_quote(payload)
    now = datetime.now(timezone.utc).isoformat()
    with db() as c:
        row = c.execute("SELECT status FROM quotes WHERE id=?", (quote_id,)).fetchone()
        if not row:
            raise HTTPException(404, "找不到这张报价单")
        if row["status"] != "draft":
            raise HTTPException(409, "已生成的报价单不可直接修改，请复制为新报价")
        c.execute("UPDATE quotes SET customer=?,currency=?,data=?,updated_at=? WHERE id=?", (data["customer"], data["currency"], json.dumps(data, ensure_ascii=False), now, quote_id))
        row = c.execute("SELECT quote_no FROM quotes WHERE id=?", (quote_id,)).fetchone()
    return {"id": quote_id, "quote_no": row["quote_no"], "status": "draft", "data": data}


@app.post("/api/quotes/{quote_id}/pdf")
def quote_generate_pdf(quote_id: str):
    response = pdf_response(DB_PATH, quote_id, DATA_DIR / "quotes", TEMPLATES_DIR, LIBRARY_DIR)
    with db() as c:
        c.execute("UPDATE quotes SET status='issued',updated_at=? WHERE id=?", (datetime.now(timezone.utc).isoformat(), quote_id))
    return response


@app.get("/api/quotes/{quote_id}/pdf")
def quote_download_pdf(quote_id: str):
    return pdf_response(DB_PATH, quote_id, DATA_DIR / "quotes", TEMPLATES_DIR, LIBRARY_DIR)


@app.get("/api/config/models")
def get_model_profiles():
    saved = read_model_store()
    profiles = [item for item in saved.get("profiles", []) if isinstance(item, dict)]
    if not profiles:
        current = model_config()
        profiles = [{"id": "current", "name": f"{current['provider']} · {current['model']}", **current}]
    active_id = str(saved.get("active_profile_id") or profiles[0]["id"])
    return {"active_profile_id": active_id, "profiles": [safe_model_profile(item) for item in profiles]}


@app.get("/api/config/model")
def get_model_config(profile_id: str | None = None):
    cfg = model_config(profile_id)
    saved = read_model_store()
    selected_id = profile_id or str(saved.get("active_profile_id") or "current")
    profile = next((item for item in saved.get("profiles", []) if isinstance(item, dict) and item.get("id") == selected_id), None)
    return {"profile_id": selected_id, "name": str(profile.get("name", "")) if profile else f"{cfg['provider']} · {cfg['model']}", "provider": cfg["provider"], "base_url": cfg["base_url"], "model": cfg["model"], "has_api_key": bool(cfg["api_key"])}


@app.put("/api/config/model")
def put_model_config(payload: dict[str, Any]):
    profile_id = str(payload.get("profile_id") or "current")
    current = model_config(None if profile_id == "new" else profile_id)
    provider = str(payload.get("provider", current["provider"]))
    defaults = {"deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"), "siliconflow": ("https://api.siliconflow.cn/v1", "deepseek-ai/DeepSeek-V4.1-Flash"), "openai": ("https://api.openai.com/v1", "gpt-4o-mini"), "ollama": ("http://host.docker.internal:11434/v1", "qwen2.5:7b"), "openai-compatible": (current["base_url"], current["model"])}
    if provider not in defaults:
        raise HTTPException(400, "Unsupported model provider")
    default_url, default_model = defaults[provider]
    base_url = str(payload.get("base_url") or default_url).strip().rstrip("/")
    model = str(payload.get("model") or default_model).strip()
    fallback_key = "" if profile_id == "new" or current["provider"] != provider else current["api_key"]
    key = "" if payload.get("clear_api_key") else str(payload.get("api_key") or fallback_key).strip()
    if not base_url.startswith(("https://", "http://")) or not model:
        raise HTTPException(400, "A valid model URL and model name are required")
    new_config = {"provider":provider,"base_url":base_url,"model":model,"api_key":key}
    saved_id = save_model_config(new_config, profile_id, str(payload.get("profile_name", "")))
    return {"saved":True,"profile_id":saved_id,"name":str(payload.get("profile_name", "")).strip(),"provider":provider,"base_url":base_url,"model":model,"has_api_key":bool(key)}


@app.delete("/api/config/model/{profile_id}")
def delete_model_profile(profile_id: str):
    saved = read_model_store()
    profiles = [item for item in saved.get("profiles", []) if isinstance(item, dict)]
    if not profiles and profile_id == "current":
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text("{}", encoding="utf-8")
        try:
            CONFIG_PATH.chmod(0o600)
        except OSError:
            pass
        return {"deleted": True, "active_profile_id": None, "profiles": []}
    remaining = [item for item in profiles if item.get("id") != profile_id]
    if len(remaining) == len(profiles):
        raise HTTPException(404, "模型配置不存在")
    active_id = saved.get("active_profile_id")
    if active_id == profile_id:
        active_id = remaining[0]["id"] if remaining else None
    saved["profiles"] = remaining
    saved["active_profile_id"] = active_id
    if active_id:
        active = next(item for item in remaining if item.get("id") == active_id)
        saved.update({key: str(active.get(key, "")) for key in ("provider", "base_url", "model", "api_key")})
    else:
        saved.update({"provider": "openai-compatible", "base_url": "https://api.openai.com/v1", "model": "gpt-4o-mini", "api_key": ""})
    CONFIG_PATH.write_text(json.dumps(saved, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass
    return {"deleted": True, "active_profile_id": active_id, "profiles": [safe_model_profile(item) for item in remaining]}


@app.post("/api/config/model/test")
async def test_model_config(payload: dict[str, Any] | None = None):
    profile_id = str((payload or {}).get("profile_id") or "")
    cfg = model_config(profile_id or None)
    if not cfg["api_key"] and cfg["provider"] != "ollama":
        raise HTTPException(400, "尚未保存 API Key。请先填写 API Key 并保存设置，再测试连接。")
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{cfg['base_url'].rstrip('/')}/chat/completions",headers=headers,json={"model":cfg["model"],"max_tokens":8,"messages":[{"role":"user","content":"Reply with OK"}]})
            response.raise_for_status()
            return {"ok":True,"model":cfg["model"],"message":"连接成功"}
    except httpx.HTTPStatusError as e:
        if e.response.status_code == 401:
            raise HTTPException(401, "API Key 未通过认证。请确认复制的是硅基流动 API Key，保存后再测试。")
        raise HTTPException(502, f"Model service returned HTTP {e.response.status_code}")
    except httpx.HTTPError:
        raise HTTPException(502, "无法连接到模型服务，请检查地址和网络")


@app.post("/api/index/sync")
def sync() -> dict[str, Any]:
    return sync_library()


@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...), folder: str = Form(default="")) -> dict[str, Any]:
    """Store user-uploaded materials under the configured library, then index them."""
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    saved = []
    total_bytes = 0
    for item in files[:100]:
        safe_name = Path(item.filename or "upload.bin").name
        ext = Path(safe_name).suffix.lower()
        if ext not in SUPPORTED:
            saved.append({"name": safe_name, "status": "skipped", "error": "Unsupported file type"})
            continue
        content = await item.read(MAX_UPLOAD + 1)
        total_bytes += len(content)
        if len(content) > MAX_UPLOAD or total_bytes > MAX_UPLOAD * 4:
            raise HTTPException(413, "Upload limit exceeded")
        parts = [part.strip() for part in folder.replace("\\", "/").split("/") if part.strip() not in {"", "."}]
        if any(part == ".." for part in parts):
            raise HTTPException(400, "Invalid folder")
        dest = LIBRARY_DIR.joinpath(*parts, safe_name)
        if not dest.resolve().is_relative_to(LIBRARY_DIR):
            raise HTTPException(400, "Invalid filename")
        if dest.exists():
            dest = dest.with_name(f"{dest.stem}_{hashlib.sha256(content).hexdigest()[:8]}{dest.suffix}")
        dest.write_bytes(content)
        saved.append(index_one(dest, dest.relative_to(LIBRARY_DIR).as_posix()))
    return {"files": saved, "uploaded": sum(x["status"] == "indexed" for x in saved)}


@app.post("/api/search/text")
def text_search(q: str = Form(...), limit: int = Form(8)) -> dict[str, Any]:
    q = q.strip()[:300]
    terms = [t for t in re.findall(r"[\w-]+", q) if len(t) > 1]
    if not terms:
        terms = [q] if q else []
    if not terms:
        return {"query": q, "results": []}
    match = " OR ".join('"' + t.replace('"','') + '"' for t in terms[:20])
    with db() as c:
        rows = c.execute("SELECT f.id file_id,f.name,f.path,f.ext,x.page,x.content,bm25(chunks_fts) rank FROM chunks_fts x JOIN files f ON f.id=CAST(x.file_id AS INTEGER) WHERE chunks_fts MATCH ? AND f.status='ready' ORDER BY rank LIMIT ?", (match,max(1,min(limit,30)))).fetchall()
        if not rows and re.search(r"[\u3400-\u9fff]", q):
            # SQLite's unicode61 tokenizer does not segment Chinese words. Fall back
            # to literal phrase and adjacent-character matching for Chinese queries.
            like = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            rows = c.execute("SELECT f.id file_id,f.name,f.path,f.ext,x.page,x.content,0 rank FROM chunks x JOIN files f ON f.id=x.file_id WHERE x.content LIKE ? ESCAPE '\\' AND f.status='ready' ORDER BY f.name LIMIT ?", (f"%{like}%",max(1,min(limit,30)))).fetchall()
            if not rows:
                bigrams = list(dict.fromkeys(q[i:i+2] for i in range(max(1,len(q)-1)) if re.search(r"[\u3400-\u9fff]", q[i:i+2])))[:12]
                if bigrams:
                    clauses = " OR ".join("x.content LIKE ?" for _ in bigrams)
                    rows = c.execute(f"SELECT f.id file_id,f.name,f.path,f.ext,x.page,x.content,0 rank FROM chunks x JOIN files f ON f.id=x.file_id WHERE ({clauses}) AND f.status='ready' LIMIT ?", (*[f"%{x}%" for x in bigrams],max(1,min(limit,30)))).fetchall()
    return {"query": q, "results": [{"file_id":r["file_id"],"name":r["name"],"path":r["path"],"ext":r["ext"],"page":r["page"],"snippet":r["content"][:700],"preview_url":f"/api/files/{r['file_id']}/preview" if r["ext"] in IMAGE_TYPES else None} for r in rows]}


@app.post("/api/answer")
async def answer(payload: dict[str, Any]):
    """Answer from indexed citations using an OpenAI-compatible Chat Completions API."""
    question = str(payload.get("question", "")).strip()
    if not question:
        raise HTTPException(400, "Question is required")
    cfg = model_config()
    api_key = cfg["api_key"]
    if not api_key and cfg["provider"] != "ollama":
        raise HTTPException(503, "AI answer is not configured. Set OPENAI_API_KEY or use /api/search/text.")
    found = text_search(q=question, limit=8)["results"]
    if not found:
        return {"answer": "在当前已索引资料中没有找到可靠依据。", "sources": []}
    context = "\n\n".join(f"[来源 {i+1}] 文件：{x['path']}；页码：{x['page'] or '未标注'}\n{x['snippet']}" for i,x in enumerate(found))
    base = cfg["base_url"].rstrip("/")
    model = cfg["model"]
    try:
        async with httpx.AsyncClient(timeout=90) as client:
            headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
            response = await client.post(f"{base}/chat/completions", headers=headers, json={"model": model, "temperature": 0.1, "messages": [{"role": "system", "content": "你是产品资料助手。只能依据用户提供的资料片段回答，不要补充片段中没有的产品事实。若资料不足，明确说明。回答末尾列出引用来源编号。"}, {"role": "user", "content": f"资料片段：\n{context}\n\n问题：{question}"}]})
            response.raise_for_status()
            answer_text = response.json()["choices"][0]["message"]["content"]
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Model service returned HTTP {e.response.status_code}")
    except (httpx.HTTPError, KeyError, IndexError, TypeError):
        raise HTTPException(502, "Could not get a valid answer from the configured model service")
    return {"answer": answer_text, "sources": found, "model": model}


def image_matches(query_image: Image.Image, limit: int = 50, min_similarity: float = 0.0) -> dict[str, Any]:
    query_hash = imagehash.phash(query_image)
    min_similarity = max(0.0, min(0.95, min_similarity))
    results = []
    mode = os.getenv("IMAGE_SEARCH_MODE", "phash").lower()
    if mode == "clip":
        try:
            query_vector = visual_embedding(query_image)
        except RuntimeError as e:
            raise HTTPException(503, str(e))
        with db() as c:
            rows = c.execute("SELECT id,name,path,image_embedding FROM files WHERE image_embedding IS NOT NULL AND status='ready'").fetchall()
        for row in rows:
            stored = json.loads(row["image_embedding"])
            score = sum(a*b for a,b in zip(query_vector,stored))
            results.append({"file_id":row["id"],"name":row["name"],"path":row["path"],"similarity":round(score,4),"preview_url":f"/api/files/{row['id']}/preview"})
        results = [x for x in results if x["similarity"] >= min_similarity]
        results.sort(key=lambda x:x["similarity"],reverse=True)
        method = "OpenCLIP visual similarity"
    else:
        with db() as c:
            rows = c.execute("SELECT id,name,path,image_hash FROM files WHERE image_hash IS NOT NULL AND status='ready'").fetchall()
        for row in rows:
            distance = query_hash - imagehash.hex_to_hash(row["image_hash"])
            results.append({"file_id":row["id"],"name":row["name"],"path":row["path"],"distance":distance,"similarity":round(1-distance/64,3),"preview_url":f"/api/files/{row['id']}/preview"})
        results = [x for x in results if x["similarity"] >= min_similarity]
        results.sort(key=lambda x:x["distance"])
        method = "pHash near-duplicate matching"
    return {"results":results[:max(1,min(limit,50))],"method":method,"min_similarity":min_similarity}


def related_text_for_images(matches: list[dict[str, Any]], limit: int = 8) -> list[dict[str, Any]]:
    """Find documents stored beside visually matching product images."""
    found: list[dict[str, Any]] = []
    seen: set[tuple[int, int | None]] = set()
    image_exts = tuple(IMAGE_TYPES)
    placeholders = ",".join("?" for _ in image_exts)
    with db() as c:
        for match in matches[:6]:
            parent = Path(match["path"]).parent.as_posix()
            prefix = "" if parent == "." else parent + "/"
            rows = c.execute(
                f"SELECT f.id file_id,f.name,f.path,f.ext,x.page,x.content FROM files f JOIN chunks x ON x.file_id=f.id WHERE f.status='ready' AND f.ext NOT IN ({placeholders}) AND (?='' OR substr(f.path,1,?)=?) ORDER BY f.name LIMIT 12",
                (*image_exts, prefix, len(prefix), prefix),
            ).fetchall()
            for row in rows:
                key = (row["file_id"], row["page"])
                if key in seen:
                    continue
                seen.add(key)
                found.append({"file_id": row["file_id"], "name": row["name"], "path": row["path"], "ext": row["ext"], "page": row["page"], "snippet": row["content"][:700]})
                if len(found) >= limit:
                    return found
    return found


@app.post("/api/assistant/chat")
async def assistant_chat(
    message: str = Form(default=""),
    image: UploadFile | None = File(default=None),
    history: str = Form(default="[]"),
    model_id: str = Form(default="local"),
    workspace: str = Form(default="product"),
    image_threshold: float = Form(default=0.20),
) -> dict[str, Any]:
    """Chat over a module-specific team assistant grounded in indexed records."""
    if workspace not in WORKSPACES:
        workspace = "product"
    question = message.strip()[:3000]
    raw_image: bytes | None = None
    query_image: Image.Image | None = None
    image_results: list[dict[str, Any]] = []
    image_method = ""
    if image is not None:
        raw_image = await image.read(MAX_UPLOAD + 1)
        if len(raw_image) > MAX_UPLOAD:
            raise HTTPException(413, f"Image exceeds {MAX_UPLOAD // 1024 // 1024} MB")
        try:
            query_image = Image.open(io.BytesIO(raw_image)).convert("RGB")
        except Exception:
            raise HTTPException(400, "Unsupported or invalid image")
        matches = image_matches(query_image, 50, image_threshold)
        image_results, image_method = matches["results"], matches["method"]

    text_results = text_search(q=question, limit=8)["results"] if question else []
    related_results = related_text_for_images(image_results) if image_results else []
    module_record_kinds = {
        "product": ["product_note"], "freight": ["freight_rate"], "quote": ["customer", "freight_rate"],
        "pi": ["customer", "order"], "customer": ["customer", "order"],
        "sales": ["salesperson", "order"], "payroll": ["salesperson", "order", "commission_rule", "payroll"],
        "rules": [],
    }
    business_context = []
    for kind in module_record_kinds.get(workspace, []):
        for record in list_business_records(kind, limit=40):
            business_context.append(f"[{kind}] id={record['id']} {record['name']}: {json.dumps(record['data'], ensure_ascii=False, default=str)[:900]}")
    quote_records: list[dict[str, Any]] = []
    if workspace in {"quote", "pi", "customer", "sales", "freight"}:
        searching_quote_number = bool(re.search(r"\bQ\d{8}-[A-F0-9]{8}\b", question, re.I))
        pi_only = {"pi"} if workspace == "pi" and not searching_quote_number else None
        with db() as c:
            quote_records = search_quotes(c, question, pi_only, limit=10)
        for record in quote_records:
            products = "; ".join(
                f"{item['name']} × {item['quantity']} {item['unit']} @ {item['unit_price']} = {item['amount']}"
                f"；颜色={item['colors'] or '未记录'}；尺码={item['size_breakdown'] or '未记录'}；MOQ={item['moq'] or '未记录'}；"
                f"箱规={item['carton_spec'] or '未记录'}；单箱装量={item['units_per_carton'] or '未记录'}；箱数={item['carton_quantity'] or '按数量计算'}；"
                f"单箱体积={item['carton_volume'] or '未记录'}；毛重/净重={item['gross_weight'] or '未记录'} / {item['net_weight'] or '未记录'}；来源={item['source'] or '未记录'}"
                for item in record["items"][:12]
            )
            logistics = logistics_totals(record["items"])
            total_cartons = f"{logistics['cartons']:.0f} 箱" if logistics["cartons"] is not None else f"缺箱数（{logistics['unknown_cartons']} 款）"
            total_cbm = f"{logistics['volume_cbm']:.3f} m³" if logistics["volume_cbm"] is not None else f"缺体积/箱数（{logistics['unknown_volume']} 款）"
            total_gross = f"{logistics['gross_weight_kg']:.3f} kg" if logistics["gross_weight_kg"] is not None else f"缺毛重/箱数（{logistics['unknown_gross']} 款）"
            total_net = f"{logistics['net_weight_kg']:.3f} kg" if logistics["net_weight_kg"] is not None else f"缺净重/箱数（{logistics['unknown_net']} 款）"
            business_context.append(f"[已保存{('PI' if record['document_type'] == 'pi' else '报价单')}数据库记录] 单号={record['quote_no']}；记录ID={record['id']}；客户={record['customer']}；状态={record['status']}；日期={record['quote_date']}；币种与总额={record['currency']} {record['total']}；贸易条款={record['incoterm']}；起运港/目的港={record['origin_port']} / {record['destination_port']}；运费条款={record['shipping_term']}，运费={record['currency']} {record['shipping']}；起运地费用={record['origin_charges']}；保险={record['insurance_term']} {record['insurance_amount']}；目的地费用={record['destination_charges']}；关税={record['duties']}；税费={record['tax_term']} {record['tax_amount']}；物流汇总（按单据现有字段）=箱数 {total_cartons}，总CBM {total_cbm}，毛重 {total_gross}，净重 {total_net}；产品明细={products}")
    shared_rules = list_team_rules(workspace) + list_team_rules("all")
    rule_context = "\n".join(f"- [{rule['name']} v{rule['version']}] {rule['body']}" for rule in shared_rules)
    sources: list[dict[str, Any]] = []
    seen_sources: set[tuple[int, int | None]] = set()
    for source in [*text_results, *related_results]:
        key = (source["file_id"], source.get("page"))
        if key not in seen_sources:
            seen_sources.add(key)
            sources.append(source)
    sources = sources[:12]

    local_only = model_id == "local"
    stored_profiles = [item for item in read_model_store().get("profiles", []) if isinstance(item, dict)]
    valid_profile_ids = {str(item.get("id")) for item in stored_profiles} if stored_profiles else {"current"}
    if not local_only and model_id not in valid_profile_ids:
        raise HTTPException(404, "所选模型配置不存在，请刷新模型列表。")
    cfg = model_config(None if local_only else model_id)
    configured = not local_only and (bool(cfg["api_key"]) or cfg["provider"] == "ollama")
    if not local_only and not configured:
        raise HTTPException(400, "所选模型尚未保存 API Key，请到模型管理中配置后再使用。")
    model = cfg["model"]
    answer_text = ""
    proposed_action = None
    if configured:
        context_items = [f"[资料 {i+1}] 文件：{x['path']}；页码：{x.get('page') or '未标注'}\n{x['snippet']}" for i, x in enumerate(sources)]
        context_items.extend(f"[相似图片 {i+1}] 文件：{x['path']}；视觉相似度：{x.get('similarity', 0):.3f}" for i, x in enumerate(image_results[:8]))
        context_items.extend(business_context[:40])
        if rule_context:
            context_items.append("[团队共享规则]\n" + rule_context[:10000])
        context = "\n\n".join(context_items) or "本地资料库没有找到可引用的文字资料。"
        module_instruction = {
            "product": "你负责产品图片和产品资料查询。优先返回型号、规格、价格和可追溯来源。",
            "freight": "你负责运费及贸易条款。若上下文匹配到报价/PI记录，必须承认已找到单据，并逐条引用记录里的产品数量、价格和箱规/重量字段；按上下文中系统计算的物流汇总回答，不能把有记录的重量或体积说成缺失。若箱规尺寸单位缺失或尺寸乘积与单箱体积字段不一致，引用两种记录并提示核实，不要静默覆盖。EXW且运费为0/Excluded时，明确说明当前单据不含运费，不能称为DDP。只根据适用的已上传货代费率和明确输入计算；缺少计费规则时不提供运费金额、不猜参数。",
            "quote": "你负责报价。可以指导并整理产品、客户、运费和贸易条款；总金额必须按系统字段和规则计算，资料缺失时标注待确认。",
            "pi": "你负责 PI 形式发票。优先复用已确认报价，不自行修改产品价格、付款和交期条款。",
            "customer": "你负责客户档案查询与维护建议。只把已提供的客户记录当作事实。",
            "sales": "你负责销售人员与订单归属查询。",
            "payroll": "你负责业绩和提成核算说明。必须区分回款、销售额和毛利口径；未知数据不能补造，计算结果先作为待复核草稿。",
            "rules": "你负责团队规则整理。把用户提出的规则改动整理成明确、可审核的文字；不得声称规则已经发布，除非用户已在规则维护窗口发布。",
        }.get(workspace, "")
        prompt = f"当前助手窗口：{WORKSPACES[workspace]}。{module_instruction}\n当前问题：{question or '请识别图片并在产品资料库中查找相关信息。'}\n\n本地资料与业务记录（只能把这些作为事实依据）：\n{context}\n\n系统已保存的报价/PI数据库记录是业务记录，不是本地文件；若上方出现匹配记录，必须优先据此回答客户、单号、状态、金额和产品明细，并引用单号。没有匹配记录时才说明数据库未找到。对其他检索不到的参数或价格明确说资料不足，不要猜测；在结尾列出引用文件名或记录类型。可以描述你从图片看到的内容，但要区分视觉观察和资料库确认的信息。"
        try:
            history_items = json.loads(history)
        except (json.JSONDecodeError, TypeError):
            history_items = []
        system_instruction = "你是团队业务 AI 助手。回答优先依据检索到的团队资料和已发布规则；不要编造型号、参数、价格、库存、运费、回款或合同条款。资料不足时明确说缺少什么。图片只能支持外观观察和相似图检索。"
        system_instruction += '\n当用户明确要求新增、修改、删除客户/销售/运费率/订单/提成规则、计算运费、生成工资草稿或生成报价/PI 草稿，且必填数据充分时，在回答末尾另附一个动作块：[[ACTION_JSON]]{"type":"customer.save","data":{...}}[[/ACTION_JSON]]。动作类型只允许 customer.save/customer.delete/freight_rate.save/freight.calculate/salesperson.save/salesperson.delete/order.save/commission_rule.save/payroll.calculate/rule.save/quote.draft/pi.draft。报价/PI 的产品名称、数量、币种与价格必须来自当前资料库引用或用户明确输入；缺少价格、客户或数量时先简短追问，不输出动作块。运费动作必须使用系统已导入的 rate_id，不能猜测价格。删除动作必须有已存在的 id。动作只在用户点击确认后执行，并且生成的是可编辑草稿。'
        messages: list[dict[str, Any]] = [{"role": "system", "content": system_instruction}]
        if isinstance(history_items, list):
            for item in history_items[-8:]:
                if isinstance(item, dict) and item.get("role") in {"user", "assistant"}:
                    content = str(item.get("content", ""))[:3000]
                    if content:
                        messages.append({"role": item["role"], "content": content})
        user_content: Any = prompt
        if query_image is not None and raw_image is not None:
            # Downscale before sending to a model endpoint to keep requests manageable.
            resized = query_image.copy()
            resized.thumbnail((1200, 1200))
            buffer = io.BytesIO()
            resized.save(buffer, format="JPEG", quality=82, optimize=True)
            image_url = "data:image/jpeg;base64," + base64.b64encode(buffer.getvalue()).decode("ascii")
            user_content = [{"type": "text", "text": prompt}, {"type": "image_url", "image_url": {"url": image_url}}]
        messages.append({"role": "user", "content": user_content})
        headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
        try:
            async with httpx.AsyncClient(timeout=120) as client:
                response = await client.post(f"{cfg['base_url'].rstrip('/')}/chat/completions", headers=headers, json={"model": model, "temperature": 0.1, "messages": messages})
                response.raise_for_status()
                answer_text = response.json()["choices"][0]["message"]["content"]
                action_match = re.search(r"\[\[ACTION_JSON\]\]\s*(\{.*?\})\s*\[\[/ACTION_JSON\]\]", answer_text, re.S)
                if action_match:
                    try:
                        candidate = json.loads(action_match.group(1))
                        allowed_actions = {"customer.save", "customer.delete", "freight_rate.save", "freight.calculate", "salesperson.save", "salesperson.delete", "order.save", "commission_rule.save", "payroll.calculate", "rule.save", "quote.draft", "pi.draft"}
                        if isinstance(candidate, dict) and candidate.get("type") in allowed_actions and isinstance(candidate.get("data"), dict):
                            proposed_action = {"type": candidate["type"], "data": candidate["data"]}
                            answer_text = (answer_text[:action_match.start()] + answer_text[action_match.end():]).strip()
                    except (ValueError, TypeError):
                        pass
        except httpx.HTTPStatusError as e:
            if e.response.status_code == 401:
                raise HTTPException(401, "模型服务认证失败。请检查这条模型配置中的 API Key。")
            if query_image is not None and e.response.status_code == 400:
                raise HTTPException(502, "模型拒绝了图片请求。请确认模型名称正确，并且硅基流动模型目录标注了“视觉”能力。")
            raise HTTPException(502, f"模型服务返回 HTTP {e.response.status_code}，请检查该模型配置。")
        except (httpx.HTTPError, KeyError, IndexError, TypeError):
            raise HTTPException(502, "无法连接模型服务或模型未返回有效回答，请检查模型设置。")
    elif quote_records:
        answer_text = "已在系统报价/PI数据库中找到以下记录：\n\n" + "\n\n".join(
            f"- {item['quote_no']}（{'PI' if item['document_type'] == 'pi' else '报价单'}，{item['status']}）：客户 {item['customer']}；{item['currency']} {item['total']}；日期 {item['quote_date']}；贸易条款 {item['incoterm']}；运费 {item['currency']} {item['shipping']}（{item['shipping_term']}）。产品：" + "; ".join(f"{product['name']} × {product['quantity']} {product['unit']}" for product in item['items'][:8])
            for item in quote_records
        )
    elif sources:
        answer_text = "我在本地资料库找到以下相关内容：\n\n" + "\n\n".join(f"**{x['name']}**\n{x['snippet'][:400]}" for x in sources[:4]) + "\n\n配置支持的 AI 模型后，我可以继续综合这些资料回答问题。"
    elif image_results:
        answer_text = f"已完成本地图片检索（{image_method}），找到 {len(image_results)} 张相似图片。要让我直接分析图片并结合资料回答，请在“模型设置”中配置支持图片输入的模型。"
    else:
        answer_text = "我暂时没有在已索引资料里找到相关内容。可以试试产品型号、文件名里的关键词，或上传产品图片查找相似图片。"

    # Keep a deterministic database check visible even when a model overlooks structured records.
    if workspace == "freight" and quote_records:
        verified_lines = []
        for record in quote_records[:5]:
            logistics = logistics_totals(record["items"])
            lines = "; ".join(
                f"{item['name']} × {item['quantity']} {item['unit']} @ {record['currency']} {item['unit_price']} = {record['currency']} {item['amount']}；"
                f"箱规 {item['carton_spec'] or '未记录'}；单箱装量 {item['units_per_carton'] or '未记录'}；单箱体积 {item['carton_volume'] or '未记录'}；"
                f"毛重/净重 {item['gross_weight'] or '未记录'} / {item['net_weight'] or '未记录'}"
                for item in record["items"][:20]
            ) or "该单据没有商品行"
            cartons = f"{logistics['cartons']:.0f} 箱" if logistics["cartons"] is not None else "缺少箱数"
            cbm = f"{logistics['volume_cbm']:.3f} m³" if logistics["volume_cbm"] is not None else "资料不足"
            gross = f"{logistics['gross_weight_kg']:.3f} kg" if logistics["gross_weight_kg"] is not None else "资料不足"
            net = f"{logistics['net_weight_kg']:.3f} kg" if logistics["net_weight_kg"] is not None else "资料不足"
            verified_lines.append(
                f"{record['quote_no']}：{record['document_type'].upper()}，客户 {record['customer']}，总额 {record['currency']} {record['total']}；"
                f"条款 {record['incoterm'] or '未记录'}；运费 {record['currency']} {record['shipping']}（{record['shipping_term'] or '未记录'}）。"
                f"按单据当前字段汇总：{cartons}，总CBM {cbm}，毛重 {gross}，净重 {net}。商品行：{lines}。"
            )
        answer_text += "\n\n数据库核对（以系统报价/PI记录为准）：\n" + "\n".join(verified_lines)

    return {"answer": answer_text, "sources": sources, "image_matches": image_results, "quote_records": quote_records, "image_method": image_method, "proposed_action": proposed_action, "model": model if configured else None, "used_ai": configured}


@app.post("/api/assistant/execute")
def assistant_execute(payload: dict[str, Any], x_actor: str = Header(default="")) -> dict[str, Any]:
    workspace = str(payload.get("workspace", "product"))
    action = str(payload.get("type", ""))
    data = payload.get("data")
    if not isinstance(data, dict):
        raise HTTPException(422, "AI 动作数据格式无效")
    allowed_by_workspace = {
        "customer": {"customer.save", "customer.delete"},
        "freight": {"freight_rate.save", "freight.calculate"},
        "sales": {"salesperson.save", "salesperson.delete", "order.save"},
        "payroll": {"commission_rule.save", "payroll.calculate"},
        "rules": {"rule.save"},
        "quote": {"quote.draft"}, "pi": {"pi.draft"},
    }
    if action not in allowed_by_workspace.get(workspace, set()):
        raise HTTPException(403, "该 AI 窗口没有执行此类操作的权限")
    try:
        if action.endswith(".delete"):
            record_id = str(data.get("id", ""))
            record = get_business_record(record_id) if record_id else None
            expected_kind = action.split(".", 1)[0]
            if not record or record["kind"] != expected_kind or not delete_business_record(record_id, x_actor):
                raise HTTPException(404, "找不到要删除的记录")
            return {"result": {"message": "记录已删除。"}}
        if action.endswith(".save") and action != "rule.save":
            kind = action.split(".", 1)[0]
            if kind == "freight_rate":
                unit = str(data.get("rate_unit", "cbm")).lower()
                if unit not in {"kg", "cbm", "carton", "shipment"}:
                    raise HTTPException(422, "计价单位请使用 kg、cbm、carton 或 shipment")
                data["rate"] = float(data.get("rate", 0))
                if data["rate"] <= 0:
                    raise HTTPException(422, "运费单价必须大于 0")
                if not data.get("origin") or not data.get("destination"):
                    raise HTTPException(422, "运费规则需要起运地和目的地")
            if kind == "salesperson" and not str(data.get("name", "")).strip():
                raise HTTPException(422, "销售人员姓名不能为空")
            if kind == "order" and not str(data.get("order_no", data.get("name", ""))).strip():
                raise HTTPException(422, "订单号不能为空")
            if kind == "order":
                if not get_business_record(str(data.get("seller_id", ""))):
                    raise HTTPException(422, "请关联一位已登记的销售人员")
                try:
                    date.fromisoformat(str(data.get("date", "")))
                except ValueError as exc:
                    raise HTTPException(422, "订单日期请使用 YYYY-MM-DD") from exc
            if kind == "commission_rule":
                if data.get("basis", "collected") not in {"collected", "net_sales", "gross_profit"}:
                    raise HTTPException(422, "提成口径无效")
                rate_percent = float(data.get("rate_percent", 0) or 0)
                if not 0 <= rate_percent <= 100:
                    raise HTTPException(422, "提成比例必须在 0 到 100 之间")
            return {"result": {"message": "记录已保存。", "record": save_business_record(kind, data, x_actor)}}
        if action == "freight.calculate":
            rate_id = str(data.get("rate_id", ""))
            rate_record = get_business_record(rate_id) if rate_id else None
            if not rate_id:
                raise HTTPException(422, "请先选择已导入的运费率；系统不会让 AI 猜测运价")
            if not rate_record or rate_record["kind"] != "freight_rate":
                raise HTTPException(404, "运费率记录不存在")
            return {"result": {"calculation": compute_freight(data, rate_record["data"] if rate_record else None)}}
        if action == "payroll.calculate":
            result = calculate_commission(str(data.get("seller_id", "")), str(data.get("start_date", "")), str(data.get("end_date", "")))
            result["name"] = f"{result['seller_name']} {result['period_start']} 至 {result['period_end']} 工资草稿"
            return {"result": {"calculation": result, "draft": save_business_record("payroll", result, x_actor)}}
        if action in {"quote.draft", "pi.draft"}:
            data["document_type"] = "pi" if action == "pi.draft" else "quote"
            normalized = normalize_quote(data)
            quote_id = str(uuid.uuid4())
            quote_no = f"{'PI' if action == 'pi.draft' else 'QT'}{date.today().strftime('%Y%m%d')}-{quote_id[:8].upper()}"
            now = datetime.now(timezone.utc).isoformat()
            with db() as c:
                c.execute("INSERT INTO quotes(id,quote_no,customer,currency,status,data,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?)", (quote_id, quote_no, normalized["customer"], normalized["currency"], "draft", json.dumps(normalized, ensure_ascii=False), now, now))
            return {"result": {"message": "单据草稿已建立，可打开后编辑确认，再生成 PDF。", "quote_draft": {"id": quote_id, "quote_no": quote_no, "status": "draft", "data": normalized}}}
        if action == "rule.save":
            rule = save_team_rule(str(data.get("workspace", "all")), str(data.get("name", "")), str(data.get("body", "")), actor=x_actor)
            return {"result": {"message": f"共享规则已发布 v{rule['version']}。", "rule": rule}}
    except (ValueError, TypeError) as exc:
        raise HTTPException(422, str(exc)) from exc
    raise HTTPException(422, "暂不支持此 AI 操作")


@app.post("/api/search/image")
async def image_search(image: UploadFile = File(...), limit: int = Form(50), min_similarity: float = Form(default=0.0)) -> dict[str, Any]:
    raw = await image.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, f"Image exceeds {MAX_UPLOAD // 1024 // 1024} MB")
    try:
        query_image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Unsupported or invalid image")
    return image_matches(query_image, limit, min_similarity)


@app.get("/api/files/{file_id}/preview")
def preview(file_id: int):
    row = file_record(file_id)
    path = (LIBRARY_DIR / row["path"]).resolve()
    if not path.is_relative_to(LIBRARY_DIR) or not path.is_file():
        raise HTTPException(404, "File unavailable")
    return FileResponse(path)


@app.get("/api/files/{file_id}/open")
def open_file(file_id: int):
    row = file_record(file_id)
    path = (LIBRARY_DIR / row["path"]).resolve()
    if not path.is_relative_to(LIBRARY_DIR) or not path.is_file():
        raise HTTPException(404, "File unavailable")
    return FileResponse(path, filename=path.name)


@app.post("/api/dingtalk/webhook")
async def dingtalk_webhook(payload: dict[str, Any], x_dingtalk_token: str | None = Header(default=None)):
    if os.getenv("DINGTALK_ENABLED", "false").lower() != "true":
        raise HTTPException(404, "DingTalk adapter is disabled")
    token = os.getenv("DINGTALK_TOKEN", "")
    if not token or not x_dingtalk_token or not hmac.compare_digest(token, x_dingtalk_token):
        raise HTTPException(401, "Invalid DingTalk adapter token")
    # Stable adapter contract for the future enterprise bot callback.
    query = str(payload.get("text", "")).strip()
    if not query:
        return JSONResponse({"text": "请发送产品问题。"})
    result = text_search(q=query)
    return JSONResponse({"text": "\n\n".join(f"{x['name']}：{x['snippet']}" for x in result["results"][:5]) or "未检索到相关资料。", "results": result["results"]})
