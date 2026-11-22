from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
from pathlib import Path
from typing import Any

from core import safe_stem


def _opencli() -> str:
    candidate = shutil.which("opencli") or shutil.which("opencli.cmd")
    if candidate:
        return candidate
    raise RuntimeError("没有找到 OpenCLI。请先安装 OpenCLIApp，并确保 opencli 命令可在 Windows 终端运行。")


def _run(args: list[str], timeout: int = 900) -> str:
    exe = _opencli()
    command = [exe, *args]
    if os.name == "nt" and Path(exe).suffix.lower() in {".cmd", ".bat"}:
        command = [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/s", "/c", subprocess.list2cmdline(command)]
    completed = subprocess.run(command, capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=timeout, check=False)
    output = (completed.stdout or "").strip()
    if completed.returncode:
        raise RuntimeError((completed.stderr or output or f"OpenCLI 退出码 {completed.returncode}").strip()[-5000:])
    return output


def _json_payload(text: str) -> Any:
    # OpenCLI can print runtime notices before its JSON result.
    decoder = json.JSONDecoder()
    for i, char in enumerate(text):
        if char not in "[{": continue
        try:
            value, _ = decoder.raw_decode(text[i:])
            return value
        except ValueError:
            continue
    return {}


def download_1688(url: str, output_root: Path) -> dict[str, Any]:
    if not re.search(r"(^|\.)1688\.com|detail\.1688\.com", url, re.I):
        raise ValueError("请输入有效的 1688 商品或店铺链接。")
    output_root = output_root.expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    details_text = _run(["1688", "item", url, "--format", "json"], timeout=240)
    details = _json_payload(details_text)
    if isinstance(details, list): details = details[0] if details else {}
    if not isinstance(details, dict): details = {}
    details["_source_url"] = url
    title = str(details.get("title") or details.get("name") or "1688 商品").strip()
    supplier = str(details.get("seller_name") or details.get("supplier_name") or "1688供应商").strip()
    item_id = str(details.get("offer_id") or re.search(r"(?:offer/|offerId=)(\d+)", url, re.I).group(1) if re.search(r"(?:offer/|offerId=)(\d+)", url, re.I) else "")
    product_name = safe_stem(title)
    supplier_dir = output_root / safe_stem(supplier)
    product_dir = supplier_dir / product_name
    if product_dir.exists():
        # Never replace an earlier manually completed folder on a repeated download.
        suffix = item_id or __import__("hashlib").sha256(url.encode()).hexdigest()[:8]
        product_dir = supplier_dir / f"{product_name}_{suffix}"
    product_dir.mkdir(parents=True, exist_ok=True)
    assets_dir = product_dir / "原始资料" / "1688下载素材"
    assets_dir.mkdir(parents=True, exist_ok=True)
    _run(["1688", "download", url, "--output", str(assets_dir), "--format", "json"], timeout=900)
    (product_dir / "1688商品信息.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    (product_dir / "原始资料" / "1688商品信息.json").write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
    return {"supplier": supplier, "title": title, "product_dir": str(product_dir), "source_dir": str(product_dir), "details": details, "source_product_id": item_id}
