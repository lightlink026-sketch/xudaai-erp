from __future__ import annotations

import hashlib
import hmac
import io
import json
import os
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import imagehash
from docx import Document
from fastapi import FastAPI, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from openpyxl import load_workbook
from PIL import Image
from pptx import Presentation
from pypdf import PdfReader

ROOT = Path(__file__).resolve().parent
DATA_DIR = Path(os.getenv("DATA_DIR", "./data")).resolve()
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", "./library")).resolve()
DB_PATH = DATA_DIR / "library.sqlite3"
MAX_UPLOAD = int(os.getenv("MAX_UPLOAD_MB", "25")) * 1024 * 1024
SUPPORTED = {".pdf", ".docx", ".xlsx", ".pptx", ".txt", ".md", ".csv", ".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
IMAGE_TYPES = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tif", ".tiff", ".gif"}
app = FastAPI(title="Xudaai Product Library", version="0.1.0")
app.mount("/static", StaticFiles(directory=ROOT / "static"), name="static")


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


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for block in iter(lambda: f.read(1024 * 1024), b""):
            h.update(block)
    return h.hexdigest()


def extract(path: Path) -> list[tuple[int | None, str]]:
    ext = path.suffix.lower()
    if ext in IMAGE_TYPES:
        return []
    if ext == ".pdf":
        reader = PdfReader(str(path))
        return [(i + 1, p.extract_text() or "") for i, p in enumerate(reader.pages)]
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
        old = c.execute("SELECT id,sha256 FROM files WHERE path=?", (rel,)).fetchone()
        if old and old["sha256"] == digest:
            return {"path": rel, "status": "unchanged"}
        try:
            extracted = extract(path)
            ih = None
            if path.suffix.lower() in IMAGE_TYPES:
                with Image.open(path) as im:
                    ih = str(imagehash.phash(im.convert("RGB")))
            if old:
                fid = old["id"]
                c.execute("DELETE FROM chunks_fts WHERE file_id=?", (fid,))
                c.execute("DELETE FROM chunks WHERE file_id=?", (fid,))
                c.execute("UPDATE files SET name=?,ext=?,size=?,mtime=?,sha256=?,status='ready',error=NULL,indexed_at=?,image_hash=? WHERE id=?", (path.name,path.suffix.lower(),st.st_size,st.st_mtime,digest,datetime.now(timezone.utc).isoformat(),ih,fid))
            else:
                cur = c.execute("INSERT INTO files(path,name,ext,size,mtime,sha256,status,indexed_at,image_hash) VALUES(?,?,?,?,?,?,'ready',?,?)", (rel,path.name,path.suffix.lower(),st.st_size,st.st_mtime,digest,datetime.now(timezone.utc).isoformat(),ih))
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
        if p.is_file() and p.suffix.lower() in SUPPORTED:
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


@app.get("/", response_class=HTMLResponse)
def home() -> str:
    return (ROOT / "static" / "index.html").read_text(encoding="utf-8")


@app.get("/api/status")
def status() -> dict[str, Any]:
    with db() as c:
        total = c.execute("SELECT COUNT(*) FROM files").fetchone()[0]
        errs = c.execute("SELECT COUNT(*) FROM files WHERE status='error'").fetchone()[0]
        latest = c.execute("SELECT MAX(indexed_at) FROM files").fetchone()[0]
    return {"library_dir": str(LIBRARY_DIR), "total_files": total, "errors": errs, "last_indexed": latest, "dingtalk_enabled": os.getenv("DINGTALK_ENABLED", "false").lower() == "true"}


@app.post("/api/index/sync")
def sync() -> dict[str, Any]:
    return sync_library()


@app.post("/api/search/text")
def text_search(q: str = Form(...), limit: int = Form(8)) -> dict[str, Any]:
    terms = [t for t in re.findall(r"[\w-]+", q) if len(t) > 1]
    if not terms:
        return {"query": q, "results": []}
    match = " OR ".join('"' + t.replace('"','') + '"' for t in terms[:20])
    with db() as c:
        rows = c.execute("SELECT f.id file_id,f.name,f.path,f.ext,x.page,x.content,bm25(chunks_fts) rank FROM chunks_fts x JOIN files f ON f.id=CAST(x.file_id AS INTEGER) WHERE chunks_fts MATCH ? AND f.status='ready' ORDER BY rank LIMIT ?", (match,max(1,min(limit,30)))).fetchall()
    return {"query": q, "results": [{"file_id":r["file_id"],"name":r["name"],"path":r["path"],"ext":r["ext"],"page":r["page"],"snippet":r["content"][:700],"preview_url":f"/api/files/{r['file_id']}/preview" if r["ext"] in IMAGE_TYPES else None} for r in rows]}


@app.post("/api/search/image")
async def image_search(image: UploadFile = File(...), limit: int = Form(12)) -> dict[str, Any]:
    raw = await image.read(MAX_UPLOAD + 1)
    if len(raw) > MAX_UPLOAD:
        raise HTTPException(413, f"Image exceeds {MAX_UPLOAD // 1024 // 1024} MB")
    try:
        query_hash = imagehash.phash(Image.open(io.BytesIO(raw)).convert("RGB"))
    except Exception:
        raise HTTPException(400, "Unsupported or invalid image")
    with db() as c:
        rows = c.execute("SELECT id,name,path,image_hash FROM files WHERE image_hash IS NOT NULL AND status='ready'").fetchall()
    results = []
    for row in rows:
        distance = query_hash - imagehash.hex_to_hash(row["image_hash"])
        results.append({"file_id":row["id"],"name":row["name"],"path":row["path"],"distance":distance,"similarity":round(1-distance/64,3),"preview_url":f"/api/files/{row['id']}/preview"})
    results.sort(key=lambda x:x["distance"])
    return {"results":results[:max(1,min(limit,50))],"method":"pHash (exact and near-duplicate matching)"}


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
