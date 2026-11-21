# Xudaai Product Library

本项目是一个可迁移的浏览器版产品资料检索系统：先在个人电脑上运行，后续可用同一套容器配置迁移到支持 Docker Compose 的 NAS。浏览器、钉钉适配器和未来的 MCP 都调用统一的检索 API。

## 当前版本

- 本地网页：资料问答、以图搜图、索引管理。
- 增量索引：新建、修改、删除文件会在下一次同步时更新；未变更文件跳过。
- 文档解析：PDF、DOCX、XLSX、PPTX、TXT、Markdown、CSV。
- 图片索引：pHash 感知哈希，针对重复图、压缩、缩放和轻微修改；当前不提供不同角度/语义相似图的 CLIP 搜索。
- API：`/api/search/text`、`/api/search/image`、`/api/index/sync`、`/api/files/{id}/preview`。
- 钉钉：预留 `/api/dingtalk/webhook` 适配器开关和统一检索调用。它目前是内部适配契约示例，并非完整的钉钉企业应用回调实现；正式接入需按实际机器人类型完成事件回调、签名校验、消息发送和图片上传。

## 本地开发

要求 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force library, data | Out-Null
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>。把产品资料复制到 `library/`，在“资料同步”页面点击“立即同步”。设置 `LIBRARY_DIR` 可更换资料目录，`DATA_DIR` 设置索引和数据库目录。

## Docker Compose

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force library, data | Out-Null
docker compose up -d --build
```

默认网页端口为 `8000`。Compose 将 `./library` 只读挂载，`./data` 用于数据库和索引。迁移时保留配置、数据库和资料目录，修改挂载路径即可；也可以在新主机上重新扫描资料重建索引。

## 资料维护机制

每个文件记录相对路径、大小、修改时间、SHA-256、状态和索引时间。同步使用 SHA-256 识别实际内容变化：新增/变化的文件重新解析，删除文件从索引移除，未变化文件跳过。解析失败保留错误信息，可修复源文件后再次同步。

当前网页管理页提供手动增量同步。后续可增加定时扫描、失败重试队列、权限/用户管理和全量重建。索引目录应定期备份；原始文件仍是权威数据源。

## API 示例

```powershell
curl.exe -X POST -F "q=产品的安装方式" http://127.0.0.1:8000/api/search/text
curl.exe -X POST -F "image=@sample.jpg" http://127.0.0.1:8000/api/search/image
curl.exe -X POST http://127.0.0.1:8000/api/index/sync
```

## 钉钉与 MCP 规划

- 钉钉入口应作为独立 adapter 调用 `/api/search/*`，不得自行读取文件或访问数据库。
- 启用当前 webhook 示例：设置 `DINGTALK_ENABLED=true` 与随机 `DINGTALK_TOKEN`。此 token header 是开发期占位鉴权；上线前应替换为对应钉钉应用的官方回调签名/加密校验，并实现主动回复、重试和幂等处理。
- 钉钉通常需要可访问的 HTTPS 回调地址；部署到本地电脑/NAS 时需配置安全隧道或受控公网入口，避免裸露管理接口。
- MCP 可在检索 API 稳定后实现为薄适配层，复用相同搜索服务。

## 当前边界

- 当前文本检索是 SQLite FTS 关键词检索，尚未加入语义向量问答和大模型生成答案。
- 扫描版 PDF OCR、视频字幕/关键帧检索、CLIP/SigLIP 语义图搜、登录与权限控制尚未实现。
- 当前默认无身份认证，只应在可信本机或局域网环境试用，不要直接暴露到公网。
