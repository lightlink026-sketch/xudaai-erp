# Xudaai Product Library

本项目是一个可迁移的浏览器版产品资料检索系统：同一套 Docker Compose 可运行在开发电脑或支持 Docker Compose 的 NAS。网页、钉钉适配器和未来的 MCP 都调用统一的检索 API；迁移时修改 `.env` 中的数据目录和资料目录挂载路径。

## 当前版本

- 本地网页：资料搜索、可选模型问答、以图搜图、资料上传和索引管理。
- 增量索引：新建、修改、删除文件会在下次同步时更新；默认每 5 分钟扫描一次，也可在网页手动同步；未变更文件跳过。
- 文档解析：PDF、DOCX、XLSX、PPTX、TXT、Markdown、CSV。
- 图片索引：支持 pHash 近重复检索；设置 `IMAGE_SEARCH_MODE=clip` 并以 `ENABLE_CLIP=1` 构建后，使用 OpenCLIP 做语义/视觉相似检索。CLIP 首次使用会下载模型并消耗较多内存和存储，需确认 NAS CPU 架构有可用的 PyTorch wheel。
- API：`/api/search/text`、`/api/search/image`、`/api/index/sync`、`/api/files/{id}/preview`。
- 钉钉：提供 `/api/dingtalk/webhook` 的开发期适配契约，接入统一文本检索。它目前不是完整的钉钉企业应用回调实现；正式接入需按机器人类型完成回调验签/解密、事件解析、主动回复和图片上传。
- 可选 AI 问答：配置 OpenAI 兼容接口的 API Key、服务地址和模型，回答严格基于检索片段并附来源。ChatGPT Plus/Pro 订阅不能替代 API Key；也可后续增加 Ollama 适配器。

## 本地开发

要求 Python 3.11+。

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
pip install -r requirements.txt
# Optional, for CLIP semantic image search:
pip install -r requirements-clip.txt
Copy-Item .env.example .env
New-Item -ItemType Directory -Force library, data | Out-Null
uvicorn app.main:app --reload
```

打开 <http://127.0.0.1:8000>。把产品资料复制到 `library/`，在“资料同步”页面点击“立即同步”，或使用网页上传。直接运行 Python 时，资料目录通过 `LIBRARY_DIR` 设置，索引和数据库目录通过 `DATA_DIR` 设置。

## Docker Compose

```powershell
Copy-Item .env.example .env
New-Item -ItemType Directory -Force library, data | Out-Null
docker compose up -d --build
```

默认网页端口为 `8000`。`.env` 中 `LIBRARY_HOST_PATH` 指向原始资料目录，`APP_DATA_HOST_PATH` 指向数据库/索引目录，`APP_PORT` 设置网页端口。NAS 上把这两个值改为 NAS 上实际目录（例如 `/volume1/docker/xudaai/library`、`/volume1/docker/xudaai/data`），再将项目 Compose 文件部署到 NAS。应用内路径保持 `/srv/app/library` 与 `/srv/app/data` 不变。资料目录需允许应用容器读写（网页上传功能需要写权限）。迁移时保留配置、数据库和资料目录；也可以复制原始资料后执行一次同步重建索引。

## 资料维护机制

每个文件记录相对路径、大小、修改时间、SHA-256、状态和索引时间。同步使用 SHA-256 识别实际内容变化：新增/变化的文件重新解析，删除文件从索引移除，未变化文件跳过。解析失败保留错误信息，可修复源文件后再次同步。

网页管理页支持手动增量同步、上传资料和状态查看；默认每 300 秒自动扫描一次（`SYNC_INTERVAL_SECONDS=0` 可关闭）。索引目录应定期备份；原始文件仍是权威数据源。

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

- 当前文本检索是 SQLite FTS 关键词检索；配置 API Key 后可基于检索片段生成大模型答案。ChatGPT 订阅不能作为 API 计费凭证。
- 扫描版 PDF OCR、视频字幕/关键帧检索、网页用户权限控制和钉钉官方回调协议尚未实现。
- 当前默认无网页用户认证，只应在可信本机或局域网环境试用，不要直接暴露到公网。钉钉 webhook token 是开发期占位，不足以用于公网生产。

## 迁移到 NAS

1. 在台式机用 Docker Compose 运行并确认资料、搜索、索引更新流程。
2. 把整个项目配置、原始资料目录和 `data/` 复制到 NAS；不要只复制数据库而遗漏原件。
3. 在 NAS 修改 `.env`：`LIBRARY_HOST_PATH`、`APP_DATA_HOST_PATH`、`MODEL_CACHE_HOST_PATH` 指向 NAS 实际目录；`APP_PORT` 选择空闲端口。
4. NAS 如支持对应 PyTorch 架构，可保留 `ENABLE_CLIP=1` 和 `IMAGE_SEARCH_MODE=clip`；否则设置 `ENABLE_CLIP=0`、`IMAGE_SEARCH_MODE=phash`。
5. 在 NAS 项目目录启动 `docker compose up -d --build`，之后局域网设备通过 `http://NAS地址:端口` 使用。
6. 避免同时在台式机和 NAS 对同一 SQLite 数据目录写入。迁移完成并验证后再停掉旧实例。

H99 Pro 目前不适合作为容器运行主机；若它仅提供 SMB 共享，可存原始文件，但服务需要部署在台式机或支持 Docker Compose 的 NAS 上。
