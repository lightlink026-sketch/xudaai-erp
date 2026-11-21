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
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

load_dotenv()

import imagehash
from docx import Document
from fastapi import Depends, FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader
import httpx

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "./library")).resolve()
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
        """)
        file_columns = {row[1] for row in c.execute("PRAGMA table_info(files)")}
        if "image_embedding" not in file_columns:
            c.execute("ALTER TABLE files ADD COLUMN image_embedding TEXT")


def model_config() -> dict[str, str]:
    config = {"provider": os.getenv("MODEL_PROVIDER", "openai-compatible"), "base_url": os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"), "model": os.getenv("OPENAI_MODEL", "gpt-4o-mini"), "api_key": os.getenv("OPENAI_API_KEY", "")}
    if CONFIG_PATH.exists():
        try:
            saved = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            config.update({k: str(v) for k,v in saved.items() if k in config})
        except (OSError, ValueError):
            pass
    return config


def save_model_config(config: dict[str, str]) -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    try:
        CONFIG_PATH.chmod(0o600)
    except OSError:
        pass


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
    return {"library_dir": str(LIBRARY_DIR), "total_files": total, "errors": errs, "last_indexed": latest, "sync_interval_seconds": SYNC_INTERVAL, "dingtalk_enabled": os.getenv("DINGTALK_ENABLED", "false").lower() == "true", "answer_enabled": bool(cfg["api_key"]) or cfg["provider"] == "ollama", "model_provider": cfg["provider"], "model_name": cfg["model"]}


@app.get("/api/config/model")
def get_model_config():
    cfg = model_config()
    return {"provider": cfg["provider"], "base_url": cfg["base_url"], "model": cfg["model"], "has_api_key": bool(cfg["api_key"])}


@app.put("/api/config/model")
def put_model_config(payload: dict[str, Any]):
    current = model_config()
    provider = str(payload.get("provider", current["provider"]))
    defaults = {"deepseek": ("https://api.deepseek.com/v1", "deepseek-chat"), "openai": ("https://api.openai.com/v1", "gpt-4o-mini"), "ollama": ("http://host.docker.internal:11434/v1", "qwen2.5:7b"), "openai-compatible": (current["base_url"], current["model"])}
    if provider not in defaults:
        raise HTTPException(400, "Unsupported model provider")
    default_url, default_model = defaults[provider]
    base_url = str(payload.get("base_url") or default_url).strip().rstrip("/")
    model = str(payload.get("model") or default_model).strip()
    key = "" if payload.get("clear_api_key") else str(payload.get("api_key") or current["api_key"]).strip()
    if not base_url.startswith(("https://", "http://")) or not model:
        raise HTTPException(400, "A valid model URL and model name are required")
    new_config = {"provider":provider,"base_url":base_url,"model":model,"api_key":key}
    save_model_config(new_config)
    return {"saved":True,"provider":provider,"base_url":base_url,"model":model,"has_api_key":bool(key)}


@app.post("/api/config/model/test")
async def test_model_config():
    cfg = model_config()
    headers = {"Authorization": f"Bearer {cfg['api_key']}"} if cfg["api_key"] else {}
    try:
        async with httpx.AsyncClient(timeout=30) as client:
            response = await client.post(f"{cfg['base_url'].rstrip('/')}/chat/completions",headers=headers,json={"model":cfg["model"],"max_tokens":8,"messages":[{"role":"user","content":"Reply with OK"}]})
            response.raise_for_status()
            return {"ok":True,"model":cfg["model"],"message":"连接成功"}
    except httpx.HTTPStatusError as e:
        raise HTTPException(502, f"Model service returned HTTP {e.response.status_code}")
    except httpx.HTTPError:
        raise HTTPException(502, "无法连接到模型服务，请检查地址和网络")


@app.post("/api/index/sync")
def sync() -> dict[str, Any]:
    return sync_library()


@app.post("/api/files/upload")
async def upload_files(files: list[UploadFile] = File(...)) -> dict[str, Any]:
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
        dest = LIBRARY_DIR / safe_name
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


def image_matches(query_image: Image.Image, limit: int = 12) -> dict[str, Any]:
    query_hash = imagehash.phash(query_image)
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
        results.sort(key=lambda x:x["similarity"],reverse=True)
        method = "OpenCLIP visual similarity"
    else:
        with db() as c:
            rows = c.execute("SELECT id,name,path,image_hash FROM files WHERE image_hash IS NOT NULL AND status='ready'").fetchall()
        for row in rows:
            distance = query_hash - imagehash.hex_to_hash(row["image_hash"])
            results.append({"file_id":row["id"],"name":row["name"],"path":row["path"],"distance":distance,"similarity":round(1-distance/64,3),"preview_url":f"/api/files/{row['id']}/preview"})
        results.sort(key=lambda x:x["distance"])
        method = "pHash near-duplicate matching"
    return {"results":results[:max(1,min(limit,50))],"method":method}


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
) -> dict[str, Any]:
    """Chat over local product documents, optionally grounded by a submitted image."""
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
        matches = image_matches(query_image, 8)
        image_results, image_method = matches["results"], matches["method"]

    text_results = text_search(q=question, limit=8)["results"] if question else []
    related_results = related_text_for_images(image_results) if image_results else []
    sources: list[dict[str, Any]] = []
    seen_sources: set[tuple[int, int | None]] = set()
    for source in [*text_results, *related_results]:
        key = (source["file_id"], source.get("page"))
        if key not in seen_sources:
            seen_sources.add(key)
            sources.append(source)
    sources = sources[:12]

    cfg = model_config()
    configured = bool(cfg["api_key"]) or cfg["provider"] == "ollama"
    model = cfg["model"]
    answer_text = ""
    if configured:
        context_items = [f"[资料 {i+1}] 文件：{x['path']}；页码：{x.get('page') or '未标注'}\n{x['snippet']}" for i, x in enumerate(sources)]
        context_items.extend(f"[相似图片 {i+1}] 文件：{x['path']}；视觉相似度：{x.get('similarity', 0):.3f}" for i, x in enumerate(image_results[:8]))
        context = "\n\n".join(context_items) or "本地资料库没有找到可引用的文字资料。"
        prompt = f"当前问题：{question or '请识别图片并在产品资料库中查找相关信息。'}\n\n本地检索结果（只能把这些作为产品事实依据）：\n{context}\n\n请用中文直接回答。对检索不到的参数或价格明确说资料不足，不要猜测；在结尾列出引用文件名。可以描述你从图片看到的内容，但要区分视觉观察和资料库确认的信息。"
        try:
            history_items = json.loads(history)
        except (json.JSONDecodeError, TypeError):
            history_items = []
        messages: list[dict[str, Any]] = [{"role": "system", "content": "你是产品资料助手。回答优先依据检索到的用户资料；不要编造型号、参数、价格、库存、运费或合同条款。资料不足时明确说明。用户上传的图片可用于识别和检索，但不能凭外观确认未展示的具体参数。"}]
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
        except httpx.HTTPStatusError as e:
            raise HTTPException(502, f"模型服务返回 HTTP {e.response.status_code}；图片对话需要支持视觉输入的模型。")
        except (httpx.HTTPError, KeyError, IndexError, TypeError):
            raise HTTPException(502, "无法连接模型服务或模型未返回有效回答，请检查模型设置。")
    elif sources:
        answer_text = "我在本地资料库找到以下相关内容：\n\n" + "\n\n".join(f"**{x['name']}**\n{x['snippet'][:400]}" for x in sources[:4]) + "\n\n配置支持的 AI 模型后，我可以继续综合这些资料回答问题。"
    elif image_results:
        answer_text = f"已完成本地图片检索（{image_method}），找到 {len(image_results)} 张相似图片。要让我直接分析图片并结合资料回答，请在“模型设置”中配置支持图片输入的模型。"
    else:
        answer_text = "我暂时没有在已索引资料里找到相关内容。可以试试产品型号、文件名里的关键词，或上传产品图片查找相似图片。"

    return {"answer": answer_text, "sources": sources, "image_matches": image_results, "image_method": image_method, "model": model if configured else None, "used_ai": configured}


@app.post("/api/search/image")
async def image_search(image: UploadFile = File(...), limit: int = Form(12)) -> dict[str, Any]:
    raw = await image.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, f"Image exceeds {MAX_UPLOAD // 1024 // 1024} MB")
    try:
        query_image = Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        raise HTTPException(400, "Unsupported or invalid image")
    return image_matches(query_image, limit)


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
